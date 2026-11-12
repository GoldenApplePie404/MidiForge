"""serial_bridge 链路冒烟测试：虚拟串口（loop://）+ 插件接线（不依赖真实硬件）。

两条链路：
  1. SerialWorker on loop:// —— write 出去的字节从读循环回来（验证传输层读写闭环）
  2. 插件接线（假 worker）—— 串口字节 → codec → engine.inject → 统一分发；
     MIDI→串口 方向编码写出 + serial/virtual 来源防回环
"""

import importlib.util
import queue
import time
from pathlib import Path

import pytest

import api
from midi.parser import ParsedMessage

_HERE = Path(__file__).parent.parent / "plugins" / "serial_bridge"


def _load(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeWorker:
    """替身传输层：预置入站字节，记录写出字节。"""

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


# ---- 传输层：loop:// 回环 ----

def test_serial_worker_loop_url():
    mod = _load("_serial.py", "serial_bridge_serial_test")
    if not mod._HAS_SERIAL:
        pytest.skip("pyserial 未安装")
    w = mod.SerialWorker("loop://", 115200)
    w.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and w.last_state != "在线":
            time.sleep(0.02)
        assert w.last_state == "在线"

        w.write(bytes([0x90, 60, 100]))          # 写出去 → 从读循环回来
        buf = b""
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(buf) < 3:
            try:
                buf += w.out_queue.get(timeout=0.2)
            except queue.Empty:
                pass
        assert bytes([0x90, 60, 100]) in buf
    finally:
        w.stop()


# ---- 插件接线：串口 → MIDI 统一分发 ----

def test_plugin_inbound_chain(qapp, tmp_path):
    app = api.create_app()
    got = []
    app.subscribe("midi.note_on", lambda p: got.append(p))

    plugin_mod = _load("plugin.py", "serial_bridge_plugin_test")
    bridge = plugin_mod.SerialBridge()
    bridge.on_activate(app)
    try:
        bridge._config_path = tmp_path / "config.json"   # 别把测试配置写进插件目录
        bridge.config["to_midi"] = True   # 隔离开发机真实 config.json 的方向开关
        bridge._worker = _FakeWorker([bytes([0x90, 60, 100])])
        bridge._decoder = plugin_mod._codec.PassthroughDecoder()
        bridge._tick()                                    # 主线程消费一次

        # engine 队列 → AppContext 统一分发（等价 _loop 单次迭代）
        for parsed in app.engine.drain():
            app._on_parsed(parsed)

        assert len(got) == 1
        assert got[0].source == "serial"
        assert got[0].channel == 0
        assert got[0].values == {"note": 60, "velocity": 100}
    finally:
        bridge.on_deactivate()


# ---- 插件接线：MIDI → 串口 + 防回环 ----

def test_plugin_outbound_and_echo_guard(qapp, tmp_path):
    app = api.create_app()
    plugin_mod = _load("plugin.py", "serial_bridge_plugin_test")
    bridge = plugin_mod.SerialBridge()
    bridge.on_activate(app)
    try:
        bridge._config_path = tmp_path / "config.json"
        fw = _FakeWorker()
        bridge._worker = fw
        bridge._decoder = plugin_mod._codec.PassthroughDecoder()
        bridge.config["to_serial"] = True

        bridge._on_midi_all(ParsedMessage(type="note_off", channel=0,
                                          values={"note": 60, "velocity": 0},
                                          source="midi_in"))
        assert fw.sent == [bytes([0x80, 60, 0])]

        # 防回环：serial / virtual 来源不转发
        fw.sent.clear()
        bridge._on_midi_all(ParsedMessage(type="note_off", channel=0,
                                          values={"note": 60, "velocity": 0},
                                          source="serial"))
        bridge._on_midi_all(ParsedMessage(type="note_off", channel=0,
                                          values={"note": 60, "velocity": 0},
                                          source="virtual"))
        assert fw.sent == []

        # 方向关：不转发
        bridge.config["to_serial"] = False
        bridge._on_midi_all(ParsedMessage(type="note_off", channel=0,
                                          values={"note": 60, "velocity": 0},
                                          source="midi_in"))
        assert fw.sent == []
    finally:
        bridge.on_deactivate()
