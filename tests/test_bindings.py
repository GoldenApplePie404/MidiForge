import json

import pytest

from core.bindings import Binding, BindingConfig, BindingSource


def test_source_from_dict_midi():
    s = BindingSource.from_dict({"type": "midi", "channel": 9, "event": "note_on", "note": 36})
    assert s.type == "midi" and s.channel == 9 and s.note == 36


def test_source_from_dict_keyboard():
    s = BindingSource.from_dict({"type": "keyboard", "key": "space"})
    assert s.type == "keyboard" and s.key == "space"


def test_source_to_dict_roundtrip():
    s = BindingSource.from_dict({"type": "midi", "channel": 9, "event": "note_on", "note": 36})
    assert BindingSource.from_dict(s.to_dict()) == s


def test_binding_to_dict_roundtrip():
    b = Binding(signal="drum_hit", sources=[
        BindingSource.from_dict({"type": "midi", "channel": 9, "event": "note_on", "note": 36}),
        BindingSource.from_dict({"type": "keyboard", "key": "space"}),
    ], virtual_midi={"type": "note_on", "channel": 0, "note": 60, "velocity": 100})
    assert Binding.from_dict(b.to_dict()) == b


def test_config_roundtrip_file(tmp_path):
    cfg = BindingConfig(bindings=[
        Binding(signal="drum_hit", sources=[BindingSource.from_dict({"type": "keyboard", "key": "space"})])
    ])
    path = tmp_path / "bindings.json"
    cfg.to_file(str(path))
    again = BindingConfig.from_file(str(path))
    assert again == cfg


def test_config_corrupt_file_backs_up(tmp_path):
    path = tmp_path / "bindings.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="绑定配置损坏"):
        BindingConfig.from_file(str(path))
    assert (tmp_path / "bindings.json.bak").exists()


def test_source_from_dict_missing_type():
    with pytest.raises(ValueError, match="缺少字段"):
        BindingSource.from_dict({"channel": 9})


def test_source_from_dict_invalid_type():
    with pytest.raises(ValueError, match="未知绑定源类型"):
        BindingSource.from_dict({"type": "usb"})


def test_binding_sources_must_be_dict():
    with pytest.raises(ValueError, match="绑定源必须是对象"):
        Binding.from_dict({"signal": "x", "sources": ["bad"]})


def test_config_bindings_must_be_dict():
    with pytest.raises(ValueError, match="绑定项必须是对象"):
        BindingConfig.from_dict({"bindings": [42]})


def test_binding_virtual_midi_empty_dict_serialized():
    assert Binding(signal="x", sources=[], virtual_midi={}).to_dict()["virtual_midi"] == {}