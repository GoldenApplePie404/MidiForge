import mido
import pytest
from midi.parser import MIDO_TYPE_MAP, parse


def test_system_event_keys_distinct():
    assert parse(mido.Message("clock")).event_key() == "system:clock"
    assert parse(mido.Message("start")).event_key() == "system:start"
    assert parse(mido.Message("clock")).event_key() != parse(mido.Message("start")).event_key()


def test_mido_type_map_reverse():
    assert MIDO_TYPE_MAP["cc"] == "control_change"
    assert MIDO_TYPE_MAP["pitch_bend"] == "pitchwheel"


def test_values_excludes_channel():
    p = parse(mido.Message("control_change", channel=1, control=7, value=64))
    assert p.values == {"control": 7, "value": 64}
    assert "channel" not in p.values


def test_pitch_bend_no_main_segment():
    p = parse(mido.Message("pitchwheel", channel=3, pitch=100))
    assert p.type == "pitch_bend"
    assert p.event_key() == "pitch_bend:3"


def test_sysex_system_description():
    p = parse(mido.Message("sysex", data=[0x7E, 0x00, 0x02]))
    assert p.type == "system"
    assert "sysex" in p.description
    assert "sysex" in p.event_key()


def test_note_on():
    p = parse(mido.Message("note_on", channel=9, note=36, velocity=100))
    assert p.type == "note_on"
    assert p.channel == 9
    assert p.values["note"] == 36
    assert p.values["velocity"] == 100
    assert p.event_key() == "note_on:9:36"


def test_note_off():
    p = parse(mido.Message("note_off", channel=0, note=60))
    assert p.type == "note_off"
    assert p.event_key() == "note_off:0:60"


def test_cc():
    p = parse(mido.Message("control_change", channel=1, control=7, value=64))
    assert p.type == "cc"
    assert p.event_key() == "cc:1:7"


def test_pitch_bend():
    p = parse(mido.Message("pitchwheel", channel=3, pitch=8191))
    assert p.type == "pitch_bend"
    assert p.values["pitch"] == 8191
    assert p.event_key() == "pitch_bend:3"


def test_program_change():
    p = parse(mido.Message("program_change", channel=5, program=10))
    assert p.type == "program_change"
    assert p.event_key() == "program_change:5:10"


def test_channel_aftertouch():
    p = parse(mido.Message("aftertouch", channel=4, value=50))
    assert p.type == "aftertouch"
    assert p.event_key() == "aftertouch:4"


def test_poly_aftertouch():
    p = parse(mido.Message("polytouch", channel=6, note=60, value=40))
    assert p.type == "poly_aftertouch"
    assert p.event_key() == "poly_aftertouch:6:60"


def test_system_message():
    p = parse(mido.Message("clock"))
    assert p.type == "system"
    assert p.channel is None


def test_parse_handles_songpos():
    p = parse(mido.Message("songpos", pos=1))
    assert p.type == "system"
    assert p.values["raw_type"] == "songpos"


def test_raw_hex():
    p = parse(mido.Message("note_on", channel=9, note=36, velocity=100))
    assert p.raw_hex == "99 24 64"


def test_description_contains_info():
    p = parse(mido.Message("note_on", channel=9, note=36, velocity=100))
    assert "36" in p.description
    assert "ch9" in p.description