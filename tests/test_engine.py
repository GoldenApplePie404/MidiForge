import re
import time

import pytest
import mido

from midi.engine import MidiEngine


def _base(name: str) -> str:
    """去掉 rtmidi 在 Windows 上附加的末尾索引号。"""
    return re.sub(r"\s+\d+$", "", name)


def _find_loopback_pair() -> tuple[str, str] | None:
    """找一对 base 名相同的 (输出端口, 输入端口)。"""
    ins = mido.get_input_names()
    outs = mido.get_output_names()
    for out in outs:
        base = _base(out)
        if any(_base(inp) == base for inp in ins):
            inp = next(i for i in ins if _base(i) == base)
            return out, inp
    return None


_LOOPBACK_PORTS = _find_loopback_pair()


@pytest.mark.skipif(_LOOPBACK_PORTS is None, reason="需要可回环的虚拟端口")
def test_loopback_roundtrip():
    out_port, in_port = _LOOPBACK_PORTS
    eng = MidiEngine()
    eng.open_input(in_port)
    time.sleep(0.15)  # 等 loopMIDI 端口稳定

    # 用独立 mido 输出端口发——避免被 engine 的 echo 过滤当作自己发的
    import mido
    sender = mido.open_output(out_port)
    msgs = []
    for attempt in range(3):
        sender.send(mido.Message("note_on", channel=9, note=36, velocity=100))
        deadline = time.time() + 2.0
        while time.time() < deadline:
            msgs = eng.drain()
            if msgs:
                break
            time.sleep(0.05)
        if msgs:
            break
    sender.close()

    assert msgs, "回环超时未收到任何消息"
    assert any(p.values.get("note") == 36 for p in msgs)
    eng.close_inputs()


def test_send_missing_output_raises():
    eng = MidiEngine()
    with pytest.raises(RuntimeError, match="未打开输出端口"):
        eng.send_message("note_on", channel=0, note=60)


def test_receive_path_with_fake_port(monkeypatch):
    captured = {}

    class FakePort:
        def __init__(self, name, callback):
            self.name = name
            self.callback = callback
            self.closed = False

        def close(self):
            self.closed = True

    def fake_factory(name, callback=None, **kwargs):
        port = FakePort(name, callback)
        captured["port"] = port
        return port

    monkeypatch.setattr("mido.open_input", fake_factory)

    error_log = []
    eng = MidiEngine()
    eng.open_input("fake", on_error=error_log.append)

    port = captured["port"]
    port.callback(mido.Message("note_on", channel=9, note=36, velocity=100))
    port.callback(mido.Message("songpos", pos=3))

    msgs = eng.drain()
    assert len(msgs) == 2
    first = msgs[0]
    assert first.type == "note_on"
    assert first.channel == 9
    assert first.values["note"] == 36
    # songpos 被解析为系统消息并入队
    assert msgs[1].type == "system"
    assert msgs[1].values["raw_type"] == "songpos"
    # 解析全程未触发错误回调
    assert error_log == []
    eng.close_inputs()
    assert port.closed