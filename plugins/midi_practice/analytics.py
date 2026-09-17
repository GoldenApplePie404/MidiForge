"""成绩分析：薄弱项识别 + 建议 + 图表数据。"""
from collections import defaultdict
from typing import List

from midi_practice.engine import SessionResult, HitResult


def analyze(session: SessionResult) -> dict:
    """返回 {weaknesses, suggestions, chart_data}。"""
    results = session.results
    if not results:
        return {"weaknesses": [], "suggestions": [], "chart_data": None}

    weaknesses = _find_weaknesses(results)
    suggestions = _make_suggestions(session, weaknesses)

    return {
        "weaknesses": weaknesses,
        "suggestions": suggestions,
        "chart_data": {
            "velocity": _velocity_chart(results),
            "timing": _timing_chart(results),
        },
    }


def _find_weaknesses(results: List[HitResult]) -> List[str]:
    stats: dict = defaultdict(lambda: {"total": 0, "miss": 0, "delta_sum": 0.0, "delta_n": 0})
    for r in results:
        target = r.expected
        label = f"note={target.note}" if target.note is not None else f"cc={target.cc}"
        s = stats[label]
        s["total"] += 1
        if r.rating == "miss":
            s["miss"] += 1
        if abs(r.delta_ms) > 50:
            s["delta_sum"] += r.delta_ms
            s["delta_n"] += 1

    out = []
    for label, s in sorted(stats.items(), key=lambda kv: kv[1]["miss"], reverse=True):
        if s["miss"] >= max(1, s["total"] * 0.3):
            hit = s["total"] - s["miss"]
            out.append(f"{label} 命中率 {hit}/{s['total']}")
        elif s["delta_n"] >= 2:
            avg = s["delta_sum"] / s["delta_n"]
            direction = "提前" if avg < 0 else "延后"
            out.append(f"{label} 总是{direction} 平均 {abs(avg):.0f}ms")
    return out[:5]


def _make_suggestions(session: SessionResult, weaknesses: List[str]) -> List[str]:
    score = session.stats.get("score", 0)
    sugs = []

    if score >= 90:
        sugs.append("🎉 太强了！试试更快 BPM 或 blind 闭卷模式")
    elif score >= 75:
        sugs.append("💪 不错！试试 blind 闭卷模式")
    elif score >= 50:
        sugs.append(f"👍 降 BPM 到 {int(session.exercise.tempo * 0.8)} 再慢速练准")
    else:
        sugs.append(f"🐢 不急，先降到 {int(session.exercise.tempo * 0.6)} 慢速")

    if weaknesses:
        sugs.append(f"🎯 薄弱: {'; '.join(weaknesses[:2])}")
    return sugs


def _velocity_chart(results: List[HitResult]) -> dict:
    return {
        "target": [r.expected.velocity for r in results],
        "actual": [r.velocity for r in results],
    }


def _timing_chart(results: List[HitResult]) -> dict:
    return {"deltas": [round(r.delta_ms, 1) for r in results]}
