"""serial_bridge v2：MFSP 帧协议编解码 + 映射 + 插件接线（不依赖真实硬件）。

覆盖：
  - CRC8 标准检查值、帧结构（SYNC/LEN/CMD/PAYLOAD/CRC8）
  - 共享测试向量（device/mfsp_vectors.json，pytest 与设备端同源逐字节断言）
  - 任意切包喂入一致性、垃圾前缀/坏 CRC 重同步恢复、帧内嵌 SYNC
  - 心跳/uptime/debug 侧通道、通道改写（默认 ch0）
  - map_in/map_out 虚拟端口映射语义（边缘触发、缩放）
  - 插件接线：MFSP 入向链路（虚拟端口→映射→engine.inject）、出向映射改道、
    在线转变自动 ping
"""

import importlib.util
import json
import queue
import time
from pathlib import Path

import pytest

import api

_HERE = Path(__file__).parent.parent / "plugins" / "serial_bridge"
_VECTORS = _HERE / "device" / "mfsp_vectors.json"


def _load(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def codec_mod():
    return _load("_codec.py", "serial_bridge_codec_mfsp_test")


def _hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


# ---- CRC8 / 帧结构 ----

def test_crc8_check_value(codec_mod):
    """CRC-8（多项式 0x07，初值 0）标准检查值：'123456789' → 0xF4。"""
    assert codec_mod.crc8(b"123456789") == 0xF4
    assert codec_mod.crc8(b"") == 0x00
    assert codec_mod.crc8(bytes([0x10, 0x3C, 0x64])) == 0x9C   # 与向量一致


def test_frame_structure(codec_mod):
    f = codec_mod.mfsp_frame(0x10, [60, 100])
    assert f == bytes([0x7E, 0x03, 0x10, 0x3C, 0x64, 0x9C])
    assert f[0] == 0x7E and f[1] == len(f) - 3                 # LEN 覆盖 CMD+PAYLOAD
    assert codec_mod.crc8(f[2:-1]) == f[-1]                    # CRC 覆盖同段
    empty = codec_mod.mfsp_frame(0x8F)                         # 零负载（ping）
    assert empty == bytes([0x7E, 0x01, 0x8F, 0xA4])
    assert codec_mod.MfspCodec.encode_ping() == empty


# ---- 共享测试向量（与设备端同源）----

def test_vectors_encode(codec_mod):
    """宿主→设备编码与向量表逐字节一致（含 CRC8）。"""
    data = json.loads(_VECTORS.read_text(encoding="utf-8"))
    for v in data["encode"]:
        payload = _hex(v["payload"]) if v["payload"] else b""
        assert codec_mod.mfsp_frame(int(v["cmd"], 16), payload) == _hex(v["frame"]), v["name"]


def test_vectors_decode(codec_mod):
    """设备→宿主解码与向量表逐字节一致（心跳/debug 断言侧通道）。"""
    data = json.loads(_VECTORS.read_text(encoding="utf-8"))
    c = codec_mod.MfspCodec()
    for v in data["decode"]:
        msgs = c.feed(_hex(v["frame"]))
        if "expect_uptime" in v:
            assert msgs == [] and c.device_uptime == v["expect_uptime"]
            assert c.last_heartbeat is not None
        elif "expect_debug" in v:
            assert msgs == [] and list(c.debug_lines) == [v["expect_debug"]]
        else:
            assert len(msgs) == 1, v["name"]
            m = msgs[0]
            assert m.type == v["expect_type"] and m.source == "serial"
            assert m.channel == v["expect_channel"]
            assert m.values == v["expect_values"]
            assert m.raw_hex == v["frame"]


# ---- 状态机鲁棒性 ----

def test_feed_arbitrary_splits(codec_mod):
    """整段 / 1 字节粒度 / 随机切分喂入，结果一致。"""
    import random
    random.seed(42)
    frames = b"".join([
        codec_mod.mfsp_frame(0x10, [60, 100]),
        codec_mod.mfsp_frame(0x50, [2, 0xFF, 0x0F]),
        codec_mod.mfsp_frame(0x51, [7, 1]),
    ])
    whole = codec_mod.MfspCodec().feed(frames)
    bytewise = codec_mod.MfspCodec().feed(bytes(frames))
    assert [m.values for m in whole] == [m.values for m in bytewise]
    assert len(whole) == 3
    for _ in range(20):                    # 随机切分
        chunks, i = [], 0
        while i < len(frames):
            n = random.randint(1, 5)
            chunks.append(frames[i:i + n])
            i += n
        c = codec_mod.MfspCodec()
        got = []
        for ch in chunks:
            got.extend(c.feed(ch))
        assert [m.values for m in got] == [m.values for m in whole]


def test_garbage_and_bad_crc_recovery(codec_mod):
    """垃圾前缀 + 坏 CRC 帧 + 好帧：好帧恢复解码，错误计数。"""
    good = codec_mod.mfsp_frame(0x20, [7, 100])
    bad = bytearray(codec_mod.mfsp_frame(0x10, [60, 100]))
    bad[-1] ^= 0xFF                        # 破坏 CRC
    c = codec_mod.MfspCodec()
    msgs = c.feed(b"\x00\x13\xAB" + bytes(bad) + good)   # 垃圾 + 坏帧 + 好帧
    assert len(msgs) == 1
    assert msgs[0].values == {"control": 7, "value": 100}
    assert c.errors >= 1                   # 至少计了坏 CRC


def test_bad_crc_rescan_inner_sync(codec_mod):
    """坏帧负载里嵌着下一个真帧的 SYNC：重扫后恢复（不从帧尾丢帧）。"""
    inner = codec_mod.mfsp_frame(0x10, [60, 100])
    # 构造坏帧：LEN 合法但 CRC 错，且负载中包含 inner 的 SYNC（0x7E）
    body = bytes([0x10]) + bytes([0x7E]) + bytes([0x33])   # CMD + 含 SYNC 的假负载
    bad = bytes([0x7E, len(body)]) + body + bytes([0x00])  # CRC 必错
    c = codec_mod.MfspCodec()
    msgs = c.feed(bad + inner)
    assert len(msgs) == 1
    assert msgs[0].type == "note_on" and msgs[0].values == {"note": 60, "velocity": 100}


def test_sync_inside_body_realigns(codec_mod):
    """帧中途出现 SYNC：放弃残帧重新对齐，后续帧正常解码。"""
    good = codec_mod.mfsp_frame(0x11, [60])
    truncated = bytes([0x7E, 0x05, 0x20, 0x01])            # 声明 LEN=5 但没发完
    c = codec_mod.MfspCodec()
    msgs = c.feed(truncated + good)
    assert len(msgs) == 1
    assert msgs[0].type == "note_off" and msgs[0].values == {"note": 60, "velocity": 0}
    assert c.errors >= 1


def test_invalid_length_resync(codec_mod):
    """非法 LEN（0 或 >33）丢弃重新扫描，后续好帧不受影响。"""
    good = codec_mod.mfsp_frame(0x20, [7, 100])
    c = codec_mod.MfspCodec()
    msgs = c.feed(bytes([0x7E, 0x00, 0x7E, 0x7F]) + good)
    assert len(msgs) == 1 and msgs[0].values == {"control": 7, "value": 100}
    assert c.errors >= 1


def test_unknown_cmd_counted(codec_mod):
    """未知 CMD / 已知 CMD 长度不符 → 计错误、不产出消息、不崩。"""
    c = codec_mod.MfspCodec()
    assert c.feed(codec_mod.mfsp_frame(0x77, [1, 2])) == []
    assert c.feed(codec_mod.mfsp_frame(0x10, [60])) == []  # note_on 缺 velocity
    assert c.errors >= 2


# ---- 侧通道与通道改写 ----

def test_heartbeat_and_debug(codec_mod):
    c = codec_mod.MfspCodec()
    out = c.feed(codec_mod.mfsp_frame(0x40, [0x11, 0x22]) +
                 codec_mod.mfsp_frame(0xF0, "中文调试".encode("utf-8")))
    assert out == []                       # 心跳/debug 不产生 MIDI 消息
    assert c.device_uptime == 0x2211
    assert c.last_heartbeat is not None
    assert time.monotonic() - c.last_heartbeat < 1.0
    assert list(c.debug_lines) == ["中文调试"]


def test_channel_override(codec_mod):
    """通道不进协议：默认 ch0，配置改写后统一到指定通道。"""
    frame = codec_mod.mfsp_frame(0x10, [60, 100])
    assert codec_mod.MfspCodec().feed(frame)[0].channel == 0
    assert codec_mod.MfspCodec(channel_override=5).feed(frame)[0].channel == 5


# ---- 虚拟端口映射 ----

def test_map_in_analog_cc_stream(codec_mod):
    """analog→cc 连续流：12bit 右移 5 成 7bit，每帧都发。"""
    rows = [{"kind": "analog", "port": 2, "target": "cc", "num": 20, "enabled": True}]
    src = codec_mod.ParsedMessage(type="analog_in", channel=None,
                                  values={"port": 2, "value": 2048},
                                  description="模拟端口 2 = 2048", source="serial")
    out = codec_mod.map_in(src, rows, 0, {})
    assert len(out) == 1
    assert out[0].type == "cc" and out[0].values == {"control": 20, "value": 64}
    assert out[0].source == "serial"
    assert "模拟端口 2" in out[0].description            # 映射产物标注来源
    # 未映射端口/停用行/未命中行 → 不产消息
    src2 = codec_mod.ParsedMessage(type="analog_in", channel=None,
                                   values={"port": 9, "value": 100}, source="serial")
    assert codec_mod.map_in(src2, rows, 0, {}) == []


def test_map_in_analog_note_threshold_edge(codec_mod):
    """analog→note 阈值边缘：≥2048 按下，只在与上次状态不同时报。"""
    rows = [{"kind": "analog", "port": 0, "target": "note", "num": 60, "enabled": True}]
    edges = {}
    def feed(val):
        src = codec_mod.ParsedMessage(type="analog_in", channel=None,
                                      values={"port": 0, "value": val}, source="serial")
        return codec_mod.map_in(src, rows, 0, edges)
    assert [m.type for m in feed(4095)] == ["note_on"]
    assert feed(3000) == []                # 同在阈值上 → 不重发
    assert [m.type for m in feed(100)] == ["note_off"]
    assert feed(100) == []


def test_map_in_digital_edges(codec_mod):
    """digital→note/cc 边缘触发：变化才发。"""
    rows = [{"kind": "digital", "port": 7, "target": "note", "num": 36, "enabled": True}]
    edges = {}
    def feed(state):
        src = codec_mod.ParsedMessage(type="digital_in", channel=None,
                                      values={"port": 7, "state": state}, source="serial")
        return codec_mod.map_in(src, rows, 0, edges)
    on = feed(1)
    assert [m.type for m in on] == ["note_on"] and on[0].values["velocity"] == 127
    assert feed(1) == []                   # 电平保持不重发
    assert [m.type for m in feed(0)] == ["note_off"]

    rows_cc = [{"kind": "digital", "port": 1, "target": "cc", "num": 64, "enabled": True}]
    edges2 = {}
    def feed_cc(state):
        src = codec_mod.ParsedMessage(type="digital_in", channel=None,
                                      values={"port": 1, "state": state}, source="serial")
        return codec_mod.map_in(src, rows_cc, 0, edges2)
    assert feed_cc(1)[0].values == {"control": 64, "value": 127}
    assert feed_cc(1) == []
    assert feed_cc(0)[0].values == {"control": 64, "value": 0}


def test_map_out_frames(codec_mod):
    """出向映射：cc→analog 左移 5；cc→digital 阈值 64；note→digital 开关。"""
    rows = [
        {"type": "cc", "num": 7, "target": "analog", "port": 3, "enabled": True},
        {"type": "note", "num": 60, "target": "digital", "port": 0, "enabled": True},
    ]
    cc = codec_mod.ParsedMessage(type="cc", channel=0,
                                 values={"control": 7, "value": 100}, source="midi_in")
    fs = codec_mod.map_out_frames(cc, rows)
    assert fs == [codec_mod.MfspCodec.encode_analog_out(3, 100 << 5)]

    cc_off = codec_mod.ParsedMessage(type="cc", channel=0,
                                     values={"control": 7, "value": 30}, source="midi_in")
    assert codec_mod.map_out_frames(cc_off, rows) == \
        [codec_mod.MfspCodec.encode_analog_out(3, 30 << 5)]   # 仍命中 cc→analog 行

    non = codec_mod.ParsedMessage(type="cc", channel=0,
                                  values={"control": 8, "value": 100}, source="midi_in")
    assert codec_mod.map_out_frames(non, rows) == []      # 未命中任何行

    on = codec_mod.ParsedMessage(type="note_on", channel=0,
                                 values={"note": 60, "velocity": 100}, source="midi_in")
    assert codec_mod.map_out_frames(on, rows) == \
        [codec_mod.MfspCodec.encode_digital_out(0, 1)]
    off = codec_mod.ParsedMessage(type="note_off", channel=0,
                                  values={"note": 60, "velocity": 0}, source="midi_in")
    assert codec_mod.map_out_frames(off, rows) == \
        [codec_mod.MfspCodec.encode_digital_out(0, 0)]

    disabled = [dict(rows[0], enabled=False)]
    assert codec_mod.map_out_frames(cc, disabled) == []


def test_encode_out_standard_cmds(codec_mod):
    """未命中映射的 note/cc 走标准 CMD 直通；其余类型不编。"""
    c = codec_mod.MfspCodec()
    m = codec_mod.ParsedMessage(type="note_on", channel=0,
                                values={"note": 60, "velocity": 100}, source="midi_in")
    assert c.encode_out(m) == codec_mod.mfsp_frame(0x80, [60, 100])
    m_off = codec_mod.ParsedMessage(type="note_off", channel=0,
                                    values={"note": 60, "velocity": 0}, source="midi_in")
    assert c.encode_out(m_off) == codec_mod.mfsp_frame(0x81, [60])
    m_cc = codec_mod.ParsedMessage(type="cc", channel=0,
                                   values={"control": 7, "value": 100}, source="midi_in")
    assert c.encode_out(m_cc) == codec_mod.mfsp_frame(0x82, [7, 100])
    m_pb = codec_mod.ParsedMessage(type="pitch_bend", channel=0,
                                   values={"pitch": 100}, source="midi_in")
    assert c.encode_out(m_pb) == b""      # 协议未定义出向弯音


# ---- 插件接线（MFSP 模式）----

class _FakeWorker:
    def __init__(self, chunks=()):
        self.out_queue = queue.Queue()
        for c in chunks:
            self.out_queue.put(c)
        self.sent = []
        self.last_state = "在线"

    def write(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def stop(self) -> None:
        pass


def test_plugin_mfsp_inbound_map(qapp, tmp_path, codec_mod):
    """串口 MFSP 帧 → codec → map_in → engine.inject 统一分发。"""
    plugin_mod = _load("plugin.py", "serial_bridge_plugin_mfsp_test")
    app = api.create_app()
    got = []
    app.subscribe("midi.cc", lambda p: got.append(p))
    bridge = plugin_mod.SerialBridge()
    bridge.on_activate(app)
    try:
        bridge._config_path = tmp_path / "config.json"
        bridge.config["to_midi"] = True   # 隔离开发机真实 config.json 的方向开关
        bridge.config["protocol"] = "mfsp"
        bridge.config["map_in"] = [{"kind": "analog", "port": 2,
                                    "target": "cc", "num": 20, "enabled": True}]
        bridge._protocol = "mfsp"
        bridge._worker = _FakeWorker([codec_mod.mfsp_frame(0x50, [2, 0xFF, 0x0F])])
        bridge._decoder = codec_mod.MfspCodec()
        bridge._tick()
        for parsed in app.engine.drain():
            app._on_parsed(parsed)
        assert len(got) == 1
        assert got[0].source == "serial"
        assert got[0].values == {"control": 20, "value": 127}   # 4095 >> 5
    finally:
        bridge.on_deactivate()


def test_plugin_mfsp_outbound_and_ping(qapp, tmp_path, codec_mod):
    """出向映射改道（cc→analog）+ 未命中直通 + 在线转变自动 ping。"""
    plugin_mod = _load("plugin.py", "serial_bridge_plugin_mfsp_out_test")
    from midi.parser import ParsedMessage
    app = api.create_app()
    bridge = plugin_mod.SerialBridge()
    bridge.on_activate(app)
    try:
        bridge._config_path = tmp_path / "config.json"
        bridge.config["protocol"] = "mfsp"
        bridge.config["map_out"] = [{"type": "cc", "num": 7,
                                     "target": "analog", "port": 3, "enabled": True}]
        bridge._protocol = "mfsp"
        fw = _FakeWorker()
        bridge._worker = fw
        bridge._decoder = codec_mod.MfspCodec()
        bridge.config["to_serial"] = True

        bridge._on_midi_all(ParsedMessage(type="cc", channel=0,
                                          values={"control": 7, "value": 100},
                                          source="midi_in"))
        assert fw.sent == [codec_mod.MfspCodec.encode_analog_out(3, 100 << 5)]

        fw.sent.clear()
        bridge._on_midi_all(ParsedMessage(type="cc", channel=0,
                                          values={"control": 9, "value": 100},
                                          source="midi_in"))
        assert fw.sent == [codec_mod.mfsp_frame(0x82, [9, 100])]   # 未命中 → 直通

        # 在线状态转变 → 自动 ping（心跳探测）
        fw.sent.clear()
        bridge._last_worker_state = None
        fw.last_state = "在线"
        bridge._tick()
        assert fw.sent == [codec_mod.MfspCodec.encode_ping()]
        bridge._tick()
        assert fw.sent == [codec_mod.MfspCodec.encode_ping()]      # 状态没变不再发
    finally:
        bridge.on_deactivate()
