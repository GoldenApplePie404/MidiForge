"""数据模型 + Judge 判定引擎。

核心原则:
  - Judge 只认 Exercise 数据结构，不管练习从哪来（内置/录制/MIDI 导入）
  - pitch 匹配 + 时间差 delta_ms → 四档 rating
  - 多余输入（Exercise 里没这个音但用户按了）→ rating=miss, pitch_ok=False
  - 漏按（超过 miss_timeout）→ mark_timeout_misses() 强制 miss
"""
import time
from dataclasses import dataclass, field
from typing import List, Optional

from midi.parser import ParsedMessage


# ---- Rating 常量 ----
Rating_PERFECT = "perfect"
Rating_GOOD = "good"
Rating_OK = "ok"
Rating_MISS = "miss"


# ---- 数据模型 ----

@dataclass
class NoteTarget:
    """单条练习目标。"""
    time: float                    # 从练习开始的秒数
    channel: int = 0
    note: Optional[int] = None     # piano note（60=C4）— None 表示不是 piano
    cc: Optional[int] = None       # pad CC（102=pad1）— None 表示不是 pad
    duration: float = 0.5
    velocity: int = 100


@dataclass
class Exercise:
    name: str
    tempo: float = 120.0
    mode: str = "sight_read"       # "sight_read" | "blind"
    scope: str = "both"            # "pad" | "piano" | "both"
    notes: List[NoteTarget] = field(default_factory=list)


@dataclass
class HitResult:
    expected: NoteTarget
    actual_time: float
    delta_ms: float
    pitch_ok: bool
    velocity: int
    rating: str                    # "perfect" | "good" | "ok" | "miss"


@dataclass
class SessionResult:
    exercise: Exercise
    results: List[HitResult] = field(default_factory=list)
    duration: float = 0.0
    bpm_used: float = 120.0
    stats: dict = field(default_factory=dict)


# ---- Judge ----

class Judge:
    """把用户 MIDI 输入跟 Exercise 目标比对。"""

    PERFECT_MS = 30       # ±30ms = perfect
    OK_MS = 200           # ±100ms = good, ±200ms = ok, >200 = miss

    def __init__(self, exercise: Exercise, tolerance_ms: float = 100):
        self._exercise = exercise
        self._tolerance = tolerance_ms
        self._pending: List[NoteTarget] = sorted(exercise.notes, key=lambda n: n.time)
        self._hit: List[HitResult] = []
        self._session_start: Optional[float] = None

    # ---- 外部接口 ----

    def start(self) -> None:
        self._session_start = time.monotonic()

    def stop(self) -> SessionResult:
        """把所有剩余 pending 强制标为 miss，返回 SessionResult。"""
        if self._session_start is None:
            return SessionResult(exercise=self._exercise, bpm_used=self._exercise.tempo)
        now = time.monotonic()
        for t in self._pending[:]:
            actual = now - self._session_start
            self._hit.append(HitResult(
                expected=t, actual_time=actual,
                delta_ms=(actual - t.time) * 1000,
                pitch_ok=False, velocity=0, rating=Rating_MISS))
        self._pending.clear()

        duration = now - self._session_start
        stats = self._compute_stats()
        return SessionResult(
            exercise=self._exercise, results=list(self._hit),
            duration=duration, bpm_used=self._exercise.tempo, stats=stats)

    def on_midi_in(self, parsed: ParsedMessage) -> Optional[HitResult]:
        if parsed.type == "note_on":
            if parsed.values.get("velocity", 0) == 0:
                return None
        elif parsed.type == "cc":
            if parsed.values.get("value", 0) == 0:
                return None
        else:
            return None

        if self._session_start is None:
            self.start()

        actual_time = time.monotonic() - self._session_start

        match_target = self._find_best_match(parsed, actual_time)
        if match_target is None:
            return self._make_extra_input_result(parsed, actual_time)

        delta_ms = (actual_time - match_target.time) * 1000
        pitch_ok = self._check_pitch(match_target, parsed)
        rating = self._rate(delta_ms, pitch_ok)

        self._pending.remove(match_target)
        result = HitResult(
            expected=match_target, actual_time=actual_time,
            delta_ms=delta_ms, pitch_ok=pitch_ok,
            velocity=parsed.values.get("velocity", 0),
            rating=rating)
        self._hit.append(result)
        return result

    def mark_timeout_misses(self, now_seconds: float) -> List[HitResult]:
        """定时调：超过 miss_timeout 还没按的标 miss。now_seconds 是 session 内相对秒数。"""
        timeout = self._tolerance + 200
        new_misses = []
        for t in self._pending[:]:
            if now_seconds - t.time > timeout:
                actual = t.time + timeout / 1000.0
                result = HitResult(
                    expected=t, actual_time=actual,
                    delta_ms=(actual - t.time) * 1000,
                    pitch_ok=False, velocity=0, rating=Rating_MISS)
                self._hit.append(result)
                new_misses.append(result)
                self._pending.remove(t)
        return new_misses

    # ---- 查询 ----

    def pending_count(self) -> int:
        return len(self._pending)

    def hit_count(self) -> int:
        return len(self._hit)

    def is_finished(self) -> bool:
        return len(self._pending) == 0

    # ---- 内部 ----

    def _find_best_match(self, parsed: ParsedMessage, actual_time: float) -> Optional[NoteTarget]:
        """找时间最接近的 pending 目标（不管 pitch，pitch 不匹配也算命中目标但 rating 会是 miss）。"""
        best = None
        best_time_diff = float("inf")

        for t in self._pending:
            if parsed.type == "note_on" and t.note is None:
                continue
            if parsed.type == "cc" and t.cc is None:
                continue
            td = abs(actual_time - t.time)
            if td < best_time_diff:
                best_time_diff = td
                best = t

        return best

    def _check_pitch(self, target: NoteTarget, parsed: ParsedMessage) -> bool:
        if parsed.type == "note_on" and target.note is not None:
            return parsed.values.get("note") == target.note
        if parsed.type == "cc" and target.cc is not None:
            return parsed.values.get("control") == target.cc
        return False

    def _rate(self, delta_ms: float, pitch_ok: bool) -> str:
        if not pitch_ok:
            return Rating_MISS
        ad = abs(delta_ms)
        if ad < self.PERFECT_MS:
            return Rating_PERFECT
        if ad < self._tolerance:
            return Rating_GOOD
        if ad < self.OK_MS:
            return Rating_OK
        return Rating_MISS

    def _make_extra_input_result(self, parsed: ParsedMessage, actual_time: float) -> Optional[HitResult]:
        """多余输入：用户按了 Exercise 里根本没有的音/CC。"""
        # 先确认 pending 里有没有同类型目标（避免 note_on 进来但全是 pad 目标误判）
        has_matching_type = False
        for t in self._pending:
            if parsed.type == "note_on" and t.note is not None:
                has_matching_type = True
                break
            if parsed.type == "cc" and t.cc is not None:
                has_matching_type = True
                break
        if not has_matching_type:
            return None

        closest = min(self._pending, key=lambda t: abs(actual_time - t.time))
        delta = (actual_time - closest.time) * 1000
        return HitResult(
            expected=closest, actual_time=actual_time,
            delta_ms=delta, pitch_ok=False,
            velocity=parsed.values.get("velocity", 0),
            rating=Rating_MISS)

    def _compute_stats(self) -> dict:
        counts = {Rating_PERFECT: 0, Rating_GOOD: 0, Rating_OK: 0, Rating_MISS: 0}
        for h in self._hit:
            counts[h.rating] = counts.get(h.rating, 0) + 1

        total = sum(counts.values())
        score = 0.0
        if total > 0:
            score = (
                counts[Rating_PERFECT] * 100 +
                counts[Rating_GOOD] * 80 +
                counts[Rating_OK] * 60
            ) / total

        return {
            "perfect": counts[Rating_PERFECT],
            "good": counts[Rating_GOOD],
            "ok": counts[Rating_OK],
            "miss": counts[Rating_MISS],
            "total": total,
            "score": round(score, 1),
            "accuracy_pct": round(
                (counts[Rating_PERFECT] + counts[Rating_GOOD]) / total * 100, 1) if total else 0,
        }
