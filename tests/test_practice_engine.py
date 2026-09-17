"""Judge 判定引擎单元测试。"""
import sys
import time
from pathlib import Path

# 让 midi_practice 包可导入
_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "plugins"))

from midi_practice.engine import (
    Exercise, NoteTarget, HitResult, Judge,
    Rating_PERFECT, Rating_GOOD, Rating_OK, Rating_MISS,
)
from midi.parser import ParsedMessage


def _msg_cc(control: int, value: int = 127) -> ParsedMessage:
    return ParsedMessage(type="cc", channel=0, values={"control": control, "value": value}, raw_hex="")


def _msg_note(note: int, vel: int = 100) -> ParsedMessage:
    return ParsedMessage(type="note_on", channel=0, values={"note": note, "velocity": vel}, raw_hex="")


def test_judge_piano_perfect():
    """±30ms 内 + 音高对 → perfect。"""
    targets = [NoteTarget(time=1.0, note=60, velocity=100)]
    judge = Judge(Exercise(name="test", tempo=120.0, notes=targets))
    judge._session_start = time.monotonic() - 1.0

    result = judge.on_midi_in(_msg_note(60))

    assert result is not None
    assert result.rating == Rating_PERFECT
    assert result.pitch_ok is True
    assert abs(result.delta_ms) < 30


def test_judge_piano_miss_wrong_note():
    """音高错 → miss + pitch_ok=False。"""
    targets = [NoteTarget(time=1.0, note=60, velocity=100)]
    judge = Judge(Exercise(name="test", tempo=120.0, notes=targets))
    judge._session_start = time.monotonic() - 1.0

    result = judge.on_midi_in(_msg_note(62))  # D4 instead of C4

    assert result is not None
    assert result.rating == Rating_MISS
    assert result.pitch_ok is False


def test_judge_pad_good():
    """Pad 输入（CC 102）在 ±100ms 内 + 对 → good。"""
    targets = [NoteTarget(time=2.0, cc=102)]
    judge = Judge(Exercise(name="test", tempo=120.0, notes=targets))
    judge._session_start = time.monotonic() - 2.05  # 50ms late

    result = judge.on_midi_in(_msg_cc(102))

    assert result is not None
    assert result.rating == Rating_GOOD
    assert result.pitch_ok is True


def test_judge_ignores_note_off():
    """note_on velocity=0 → 忽略。"""
    targets = [NoteTarget(time=1.0, note=60)]
    judge = Judge(Exercise(name="test", tempo=120.0, notes=targets))
    judge._session_start = time.monotonic() - 1.0

    msg = ParsedMessage(type="note_on", channel=0,
                        values={"note": 60, "velocity": 0}, raw_hex="")
    result = judge.on_midi_in(msg)

    assert result is None


def test_judge_pending_count():
    targets = [NoteTarget(time=1.0, note=60), NoteTarget(time=2.0, note=62), NoteTarget(time=3.0, note=64)]
    judge = Judge(Exercise(name="test", tempo=120.0, notes=targets))
    assert judge.pending_count() == 3


def test_judge_extra_input():
    """按了 Exercise 里没有的音但类型匹配 → miss（多余输入）。"""
    targets = [NoteTarget(time=1.0, note=60)]
    judge = Judge(Exercise(name="test", tempo=120.0, notes=targets))
    judge._session_start = time.monotonic() - 0.5

    result = judge.on_midi_in(_msg_note(62))  # 错音 + 时间相近

    assert result is not None
    assert result.rating == Rating_MISS
    assert result.pitch_ok is False


def test_judge_stop_auto_misses_remaining():
    """stop() 时把所有 pending 强制标 miss。"""
    targets = [NoteTarget(time=1.0, note=60), NoteTarget(time=2.0, note=62)]
    judge = Judge(Exercise(name="test", tempo=120.0, notes=targets))
    judge.start()

    # 只按对第一个
    time.sleep(1.01)
    judge.on_midi_in(_msg_note(60))

    session = judge.stop()
    assert session.stats["perfect"] + session.stats["good"] + session.stats["ok"] == 1
    assert session.stats["miss"] == 1  # 第二个没按 → miss
    assert judge.is_finished()
