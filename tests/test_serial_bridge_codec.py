"""serial_bridge 透传协议编解码器测试（不依赖真实串口与 Qt）。

按文件路径加载 plugins/serial_bridge/_codec.py（插件模块不是包，
运行时由 plugin_loader 以 spec 方式加载，测试保持同一思路）。
覆盖：任意切包一致性、Running Status、断流重对齐、实时字节穿透、
SysEx 完整/损坏/超长、通道改写、编码往返、回环来源标记。
"""

import importlib.util
import random
from pathlib import Path

from midi.parser import ParsedMessage

_HERE = Path(__file__).parent.parent / "plugins" / "serial_bridge"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "serial_bridge_codec", _HERE / "_codec.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


codec = _load_module()


def _feed_all(raw: bytes, **kwargs) -> list:
    d = codec.PassthroughDecoder(**kwargs)
    return d.feed(raw), d


def _signature(msgs) -> list:
    return [(m.type, m.channel, dict(m.values)) for m in msgs]


# ---- 基础解码：整段喂入 == 1 字节粒度 == 任意切包 ----

def test_decode_all_channel_types():
    raw = bytes([
        0x90, 60, 100,          # note_on
        0x80, 60, 0,            # note_off
        0xB0, 7, 64,            # cc
        0xE0, 0, 32,            # pitch_bend：14bit=4096 → -8192+4096 = -4096
        0xC0, 5,                # program_change
        0xD0, 90,               # aftertouch
        0xA0, 62, 33,           # poly_aftertouch
    ])
    msgs, d = _feed_all(raw)
    sig = _signature(msgs)
    assert sig == [
        ("note_on", 0, {"note": 60, "velocity": 100}),
        ("note_off", 0, {"note": 60, "velocity": 0}),
        ("cc", 0, {"control": 7, "value": 64}),
        ("pitch_bend", 0, {"pitch": -4096}),
        ("program_change", 0, {"program": 5}),
        ("aftertouch", 0, {"value": 90}),
        ("poly_aftertouch", 0, {"note": 62, "value": 33}),
    ]
    assert d.errors == 0
    for m in msgs:
        assert m.source == "serial"


def test_bytewise_feed_equals_whole():
    raw = bytes([0x93, 60, 100, 0xB3, 7, 64, 0xE3, 0, 96])
    whole, _ = _feed_all(raw)
    d = codec.PassthroughDecoder()
    part = []
    for b in raw:
        part.extend(d.feed(bytes([b])))
    assert _signature(part) == _signature(whole)


def test_random_split_feed_equals_whole():
    rng = random.Random(42)
    raw = bytes([0x90, 60, 100, 0x80, 60, 0, 0xB0, 7, 64,
                 0xE0, 0, 64, 0xC0, 5, 0xD0, 90, 0xA0, 62, 33])
    whole, _ = _feed_all(raw)
    for _ in range(20):
        d = codec.PassthroughDecoder()
        out = []
        i = 0
        while i < len(raw):
            n = rng.randint(1, 5)
            out.extend(d.feed(raw[i:i + n]))
            i += n
        assert _signature(out) == _signature(whole)


# ---- Running Status 与重对齐 ----

def test_running_status():
    # 90 3C 64 → note_on(60,100)；后续 40 3C 复用状态 → note_on(64,60)
    msgs, d = _feed_all(bytes([0x90, 0x3C, 0x64, 0x40, 0x3C]))
    assert _signature(msgs) == [
        ("note_on", 0, {"note": 60, "velocity": 100}),
        ("note_on", 0, {"note": 64, "velocity": 60}),
    ]
    assert d.errors == 0


def test_note_on_velocity_zero_stays_note_on():
    msgs, _ = _feed_all(bytes([0x90, 60, 0]))
    assert _signature(msgs) == [("note_on", 0, {"note": 60, "velocity": 0})]


def test_realtime_bytes_do_not_break_running_status():
    # 0xFE（active sensing）插在消息中间/数据字节之间：静默忽略
    msgs, d = _feed_all(bytes([0x90, 0xFE, 0x3C, 0x64, 0xF8, 0x40, 0x3C, 0xFF]))
    assert _signature(msgs) == [
        ("note_on", 0, {"note": 60, "velocity": 100}),
        ("note_on", 0, {"note": 64, "velocity": 60}),
    ]
    assert d.errors == 0


def test_garbage_leading_data_bytes_realigned():
    # 断流残留的数据字节：无状态可用 → 丢弃计数；随后的好消息正常解析
    msgs, d = _feed_all(bytes([0x00, 0x7F, 0x3C, 0x90, 60, 100]))
    assert _signature(msgs) == [("note_on", 0, {"note": 60, "velocity": 100})]
    assert d.errors == 3


def test_system_common_clears_running_status():
    # F3（song select，v1 不支持）：清 Running Status；其后的 0x40 成垃圾
    msgs, d = _feed_all(bytes([0x90, 60, 100, 0xF3, 0x40]))
    assert _signature(msgs) == [("note_on", 0, {"note": 60, "velocity": 100})]
    assert d.errors == 1


# ---- SysEx ----

def test_sysex_complete():
    msgs, d = _feed_all(bytes([0xF0, 0x7E, 0x7F, 0x09, 0x01, 0xF7]))
    assert len(msgs) == 1
    m = msgs[0]
    assert m.type == "system" and m.channel is None
    assert m.values == {"raw_type": "sysex"}
    assert m.raw_hex == "F0 7E 7F 09 01 F7"
    assert m.source == "serial"
    assert d.errors == 0


def test_sysex_interrupted_by_status_byte():
    # SysEx 未等到 F7 就来了状态字节 → 丢弃计数，后续消息正常
    msgs, d = _feed_all(bytes([0xF0, 0x7E, 0x90, 60, 100]))
    assert _signature(msgs) == [("note_on", 0, {"note": 60, "velocity": 100})]
    assert d.errors == 1


def test_sysex_overlong_guard():
    d = codec.PassthroughDecoder()
    d.feed(bytes([0xF0]) + b"\x01" * 4096)
    assert d.errors > 0          # 超长被掐断计数，不崩溃不膨胀


# ---- 通道改写 ----

def test_channel_override_keeps_raw_hex():
    msgs, _ = _feed_all(bytes([0x9A, 60, 100]), channel_override=5)
    m = msgs[0]
    assert m.channel == 5
    assert m.raw_hex == "9A 3C 64"       # 线上原始字节不动
    assert m.event_key() == "note_on:5:60"


def test_no_override_event_key():
    msgs, _ = _feed_all(bytes([0x90, 60, 100]))
    assert msgs[0].event_key() == "note_on:0:60"


# ---- 编码（MIDI→串口）----

def test_encode_roundtrip_all_types():
    samples = [
        bytes([0x80, 60, 64]),
        bytes([0x90, 60, 100]),
        bytes([0xA5, 62, 33]),
        bytes([0xB2, 7, 100]),
        bytes([0xC0, 5]),
        bytes([0xD3, 90]),
        bytes([0xE0, 0, 32]),
    ]
    for raw in samples:
        d = codec.PassthroughDecoder()
        msgs = d.feed(raw)
        assert len(msgs) == 1, raw
        assert codec.encode_parsed(msgs[0]) == raw, raw


def test_encode_manual_parsed():
    p = ParsedMessage(type="cc", channel=2, values={"control": 7, "value": 100})
    assert codec.encode_parsed(p) == bytes([0xB2, 7, 100])

    p = ParsedMessage(type="pitch_bend", channel=0, values={"pitch": 8191})
    assert codec.encode_parsed(p) == bytes([0xE0, 0x7F, 0x7F])

    p = ParsedMessage(type="pitch_bend", channel=0, values={"pitch": 99999})
    assert codec.encode_parsed(p) == bytes([0xE0, 0x7F, 0x7F])   # 夹到量程

    p = ParsedMessage(type="note_on", channel=15, values={"note": 60, "velocity": 1})
    assert codec.encode_parsed(p) == bytes([0x9F, 60, 1])


def test_encode_skips_system_and_junk():
    p = ParsedMessage(type="system", channel=None, values={"raw_type": "sysex"})
    assert codec.encode_parsed(p) == b""
    p = ParsedMessage(type="unknown_kind", channel=0, values={})
    assert codec.encode_parsed(p) == b""


# ---- 边界 ----

def test_empty_feed():
    d = codec.PassthroughDecoder()
    assert d.feed(b"") == []
    assert d.errors == 0


def test_decoder_reset():
    d = codec.PassthroughDecoder()
    d.feed(bytes([0x90, 60]))          # 半截消息
    d.reset()
    msgs = d.feed(bytes([0x90, 60, 100]))
    assert _signature(msgs) == [("note_on", 0, {"note": 60, "velocity": 100})]
