"""ExerciseSource 单元测试。"""
import sys
from pathlib import Path

_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "plugins"))

from midi_practice.exercises import BuiltInScales, MidiFileImport, UserRecording
from midi_practice.engine import Exercise
from midi.parser import ParsedMessage


def test_builtin_has_items():
    scales = BuiltInScales()
    items = scales.list_available()
    assert len(items) == 5
    for key, name, scope in items:
        assert scope in ("piano", "pad", "both")


def test_builtin_load_c_major():
    scales = BuiltInScales()
    ex = scales.load("c_major_ascending")
    assert isinstance(ex, Exercise)
    assert len(ex.notes) == 8
    notes = [n.note for n in ex.notes]
    assert notes == [60, 62, 64, 65, 67, 69, 71, 72]


def test_builtin_load_pad_cc():
    scales = BuiltInScales()
    ex = scales.load("pad_cc_identification")
    assert isinstance(ex, Exercise)
    assert ex.scope == "pad"
    assert len(ex.notes) == 8
    ccs = [n.cc for n in ex.notes]
    assert set(ccs) == {102, 103, 104, 105, 106, 107, 108, 109}


def test_user_recording_from_messages():
    msgs = [
        (0.0, ParsedMessage(type="note_on", channel=0,
                            values={"note": 60, "velocity": 100}, raw_hex="")),
        (0.5, ParsedMessage(type="note_on", channel=0,
                            values={"note": 62, "velocity": 95}, raw_hex="")),
        (1.0, ParsedMessage(type="cc", channel=0,
                            values={"control": 102, "value": 127}, raw_hex="")),
    ]
    ex = UserRecording.from_messages(msgs, bpm=120.0, name="test")
    assert ex.name == "test"
    assert len(ex.notes) == 3
    assert ex.scope == "both"
    assert ex.notes[0].note == 60
    assert ex.notes[1].time == 0.5
    assert ex.notes[2].cc == 102
