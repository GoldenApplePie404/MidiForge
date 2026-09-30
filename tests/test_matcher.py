from core.bindings import Binding, BindingConfig, BindingSource
from core.matcher import Matcher
from midi.parser import ParsedMessage


def make_config(sources, signal="sig"):
    return BindingConfig(bindings=[Binding(signal=signal, sources=[BindingSource.from_dict(s) for s in sources])])


def test_exact_note_on_match():
    cfg = make_config([{"type": "midi", "event": "note_on", "channel": 9, "note": 36}])
    m = Matcher(cfg)
    parsed = ParsedMessage(type="note_on", channel=9, values={"note": 36, "velocity": 100})
    assert m.signals_for_parsed(parsed) == {"sig"}


def test_wildcard_channel():
    cfg = make_config([{"type": "midi", "event": "cc", "cc": 1}])  # channel 未指定 = 任意
    m = Matcher(cfg)
    p1 = ParsedMessage(type="cc", channel=3, values={"control": 1, "value": 64})
    p2 = ParsedMessage(type="cc", channel=7, values={"control": 1, "value": 10})
    assert m.signals_for_parsed(p1) == {"sig"}
    assert m.signals_for_parsed(p2) == {"sig"}


def test_no_match_on_different_note():
    cfg = make_config([{"type": "midi", "event": "note_on", "note": 36}])
    m = Matcher(cfg)
    parsed = ParsedMessage(type="note_on", channel=0, values={"note": 60, "velocity": 1})
    assert m.signals_for_parsed(parsed) == set()


def test_multi_source_one_signal():
    cfg = make_config([
        {"type": "midi", "event": "note_on", "note": 36},
        {"type": "keyboard", "key": "space"},
    ])
    m = Matcher(cfg)
    assert m.signals_for_parsed(ParsedMessage(type="note_on", channel=9, values={"note": 36})) == {"sig"}
    assert m.signals_for_key("space") == {"sig"}


def test_event_type_must_match():
    cfg = make_config([{"type": "midi", "event": "note_on"}])
    m = Matcher(cfg)
    assert m.signals_for_parsed(ParsedMessage(type="cc", channel=0, values={"control": 1})) == set()


def test_keyboard_key_normalized():
    cfg = make_config([{"type": "keyboard", "key": "Left"}])
    m = Matcher(cfg)
    assert m.signals_for_key("left") == {"sig"}


def test_disabled_binding_not_matched_midi():
    """停用的绑定不再监听它绑定的 MIDI 按键。"""
    cfg = make_config([{"type": "midi", "event": "note_on", "channel": 9, "note": 36}])
    cfg.bindings[0].enabled = False
    m = Matcher(cfg)
    assert m.signals_for_parsed(ParsedMessage(type="note_on", channel=9, values={"note": 36})) == set()


def test_disabled_binding_not_matched_key():
    cfg = make_config([{"type": "keyboard", "key": "space"}])
    cfg.bindings[0].enabled = False
    m = Matcher(cfg)
    assert m.signals_for_key("space") == set()


def test_reenable_binding_matches_again():
    cfg = make_config([{"type": "midi", "event": "cc", "cc": 21}])
    cfg.bindings[0].enabled = False
    m = Matcher(cfg)
    parsed = ParsedMessage(type="cc", channel=0, values={"control": 21, "value": 127})
    assert m.signals_for_parsed(parsed) == set()
    cfg.bindings[0].enabled = True
    assert m.signals_for_parsed(parsed) == {"sig"}