import mido
import pytest

import api
from midi.parser import ParsedMessage


def test_create_app_context():
    app = api.create_app()
    assert app.engine is not None and app.bus is not None and app.matcher is not None


def test_on_decorator_subscribes_default(tmp_path):
    got = []
    api.create_app(bindings_path=str(tmp_path / "b.json"))
    captured = {}

    @api.on("midi.message")
    def cb(msg):
        got.append(msg)

    captured["parsed"] = ParsedMessage(type="note_on", channel=9, values={"note": 36})
    api._DEFAULT_APP.bus.publish("midi.message", captured["parsed"])
    assert got and got[0].type == "note_on"


def test_dispatch_publishes_signal():
    app = api.create_app()
    from core.bindings import Binding, BindingSource
    app.config.bindings = [Binding(signal="boom", sources=[
        BindingSource.from_dict({"type": "midi", "event": "note_on", "note": 36})])]
    app.matcher = api.Matcher(app.config)
    got = []
    app.bus.subscribe("midi.signal", got.append)
    app._on_parsed(ParsedMessage(type="note_on", channel=0, values={"note": 36, "velocity": 1}))
    assert got and "boom" in got[0]["signals"]


def test_send_midi_publishes_sent(monkeypatch):
    app = api.create_app()
    got = []
    app.bus.subscribe("midi.sent", got.append)
    monkeypatch.setattr(app.engine, "send_message", lambda *args, **kwargs: None)
    app.send_midi("note_on", channel=0, note=60)
    assert got and got[0]["type"] == "note_on"