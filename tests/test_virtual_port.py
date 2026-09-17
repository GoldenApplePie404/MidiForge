from midi.virtual_port import detect_virtual_out_port, setup_guide


def test_detect_loopmidi():
    names = ["Microsoft GS Wavetable Synth", "loopMIDI Port", "MIDI 4x4"]
    assert detect_virtual_out_port(names) == "loopMIDI Port"


def test_detect_other_virtual():
    names = ["VirtualMIDISynth", "foo"]
    assert detect_virtual_out_port(names) == "VirtualMIDISynth"


def test_no_virtual():
    assert detect_virtual_out_port(["Microsoft GS Wavetable Synth"]) is None


def test_guide_has_download_hint():
    assert "loopMIDI" in setup_guide() and "http" in setup_guide()