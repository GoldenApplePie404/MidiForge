"""Practice — 竖向钢琴帘练习插件（最小可用版本）。

这次不搞大而全，先跑通:
  1. 竖向钢琴帘（61 键 C2-C6）
  2. 音符从上方飘到判定线
  3. 判定用户按键（perfect/good/ok/miss）
  4. 控制面板 + 硬编码 demo 练习

后续再加：谱子、模式切换、打击垫练习、MIDI 导入...
"""
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# sys.path 修复 — 宿主用 spec_from_file_location 加载 plugin.py，
# 不会自动把 plugins/ 父目录放 sys.path。
_PLUGINS_PARENT = Path(__file__).resolve().parent.parent
if str(_PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_PARENT))

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPalette, QPen
from PyQt6.QtCore import QUrl as _QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QSlider, QSplitter, QVBoxLayout, QWidget,
)
import mido

from core.plugin import Plugin


# ============================================================
# 数据模型
# ============================================================

@dataclass
class PianoNote:
    """钢琴帘里的一个音符。"""
    id: int
    midi: int            # MIDI note number (60 = C4)
    time: float          # 飘到判定线的目标时间（秒）
    duration: float = 0.5  # 持续时间（秒），决定音符块高度
    velocity: int = 80
    track: str = "main"  # 轨道标识（demo="main"；MIDI 导入=轨道名）
    rated: bool = False
    rating: str = ""     # perfect / good / ok / miss
    hit_velocity: int = 0
    hit_delta_ms: float = 0.0


@dataclass
class HitResult:
    note_id: int
    midi: int
    delta_ms: float
    rating: str          # perfect / good / ok / miss
    pitch_ok: bool
    at: float            # play_time at judgment


# ============================================================
# PianoLaneWidget — 核心：竖向钢琴帘 + 飘音符 + 判定
# ============================================================

class PianoLaneWidget(QWidget):
    """竖向钢琴帘。时间从上方"过去"流向底部"现在"。

    坐标系:
        Y=0      → 屏幕顶部（音符还在很"远"）
        Y=H-1    → 屏幕底部
        判定线   → judgment_line_y (默认底部往上 80px)
        音符块   → 上边缘对应 note.time，下边缘对应 note.time + duration

    音符飘到判定线底部的时刻 === note.time。
    """

    NOTE_COLORS = {
        "": "#60a5fa",          # 未判定默认: 蓝
        "perfect": "#22c55e",
        "good": "#3b82f6",
        "ok": "#f59e0b",
        "miss": "#ef4444",
    }

    # 轨道显色（未判定时按 track 分色）
    TRACK_COLORS = [
        "#60a5fa",  # 蓝
        "#a78bfa",  # 紫
        "#34d399",  # 绿
        "#f472b6",  # 粉
        "#fbbf24",  # 黄
        "#38bdf8",  # 青
        "#fb923c",  # 橙
        "#e879f9",  # 洋红
    ]

    # 显示范围: 61 键 C2(36) ~ C7(96) — 匹配 KL Essential M3
    RANGE_START = 36
    RANGE_END = 97
    NUM_KEYS = RANGE_END - RANGE_START

    # 容差
    PERFECT_MS = 30
    GOOD_MS = 80
    OK_MS = 150
    MISS_WINDOW_MS = 250   # 超过 target_time + 这个算漏按

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(500)
        self.setMinimumWidth(600)
        self.setBackgroundRole(QPalette.ColorRole.Window)

        # 练习数据
        self._notes: List[PianoNote] = []
        self._playing = False
        self._paused = False
        self._play_time = 0.0       # 秒
        self._bpm = 120.0
        self._pixels_per_second = 200.0  # 飘速（像素/秒）

        # 动画定时器 (60fps)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._last_ts = 0.0

        # 判定结果回调
        self.on_judged = None   # cb(HitResult)
        self.on_finished = None # cb()

        # 倒计时准备（毫秒）
        self._prep_total_ms = 3000
        self._prep_remaining_ms = 0
        self._prep_last_tick_sec = 0.0

        # 当前按住的琴键（用于绘制按键反馈）
        self._pressed_notes: set = set()

        # 导入状态（None = 还没导入，list = 已导入的 notes）
        self._imported_notes: Optional[list] = None

        # 浮动分数特效 [{text, color, age_sec, x_ratio, base_y}]
        self._floats = []

        # 轨道名 → 颜色（稳定映射，供音符显色）
        self._track_colors: dict = {}
        self._track_order: list = []

        # 模式: "auto"=自动下落（默认）, "semi"=半自动（弹对才推进）
        self._mode = "auto"

        # 统计
        self._hits = {"perfect": 0, "good": 0, "ok": 0, "miss": 0}
        self._hit_count = 0

    # -------- 外部接口 --------

    def set_notes(self, notes: List[PianoNote], bpm: float = 120.0):
        self._notes = [PianoNote(
            id=i, midi=n.midi, time=n.time, duration=n.duration,
            velocity=n.velocity, track=n.track,
        ) for i, n in enumerate(notes)]
        self._bpm = bpm
        self._play_time = 0.0
        self._floats = []
        self._hits = {"perfect": 0, "good": 0, "ok": 0, "miss": 0}
        self._hit_count = 0
        # 建立轨道 → 颜色映射（保持稳定）
        track_idxs: dict = {}
        for n in self._notes:
            if n.track not in track_idxs:
                track_idxs[n.track] = len(track_idxs)
        self._track_order = list(track_idxs.keys())
        self._track_colors = {
            name: self.TRACK_COLORS[i % len(self.TRACK_COLORS)]
            for i, name in enumerate(self._track_order)
        }
        self.update()

    def track_color(self, name: str) -> str:
        """轨道名 → 颜色（含未登记的兜底）。"""
        return self._track_colors.get(name, self.NOTE_COLORS[""])

    def set_mode(self, mode: str):
        """切换模式: "auto" 或 "semi"。"""
        if mode in ("auto", "semi"):
            self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

    def set_pixels_per_second(self, pps: float):
        """调飘速——越大飘越快。"""
        self._pixels_per_second = max(50.0, min(600.0, pps))

    def set_pressed_notes(self, notes: set):
        """Plugin 层传入当前按住的 MIDI note 集合。"""
        self._pressed_notes = notes
        self.update()

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def in_prep(self) -> bool:
        return self._prep_remaining_ms > 0

    def start(self):
        # 先进入倒计时 prep 状态
        self._prep_remaining_ms = self._prep_total_ms
        self._prep_last_tick_sec = self._prep_remaining_ms
        self._playing = False  # prep 期间不判定
        self._paused = False
        self._play_time = 0.0
        self._last_ts = time.monotonic()
        self._timer.start()

    def pause(self):
        """暂停：冻结时间推进与判定，保留现场。"""
        if self._playing and not self._paused:
            self._paused = True
            self._last_ts = time.monotonic()

    def resume(self):
        if self._paused:
            self._paused = False
            self._last_ts = time.monotonic()

    @property
    def paused(self) -> bool:
        return self._paused

    def stop(self):
        self._playing = False
        self._paused = False
        self._timer.stop()

    def reset(self):
        self.stop()
        self._play_time = 0.0
        self._floats = []
        for n in self._notes:
            n.rated = False
            n.rating = ""
        self._hits = {"perfect": 0, "good": 0, "ok": 0, "miss": 0}
        self.update()

    def judge_note_on(self, midi_note: int) -> Optional[HitResult]:
        """用户按下一个 MIDI note — 找判定线附近的匹配音符，返回判定结果。

        semi 模式：找下一个该弹的音符（time 排序，第一个未判定且 time >= play_time），
        弹对则推进 play_time 到该音符 time。
        """
        if not self._playing or not self._notes:
            return None

        # === 半自动模式 ===
        if self._mode == "semi":
            # 找下一个该弹的：按 time 排序，第一个未判定的
            pending = sorted(
                (n for n in self._notes if not n.rated),
                key=lambda n: (n.time, n.id),
            )
            if not pending:
                return None
            target = pending[0]

            if midi_note == target.midi:
                # 弹对：推进 play_time 到这个音符，让下一个开始飘
                self._play_time = target.time
                # rating：半自动不看 timing，弹对就是 perfect
                target.rated = True
                target.rating = "perfect"
                target.hit_delta_ms = 0.0
                self._hits["perfect"] += 1
                self._hit_count += 1
                result = HitResult(
                    note_id=target.id, midi=target.midi, delta_ms=0.0,
                    rating="perfect", pitch_ok=True, at=self._play_time,
                )
                self._floats.append({
                    'text': 'PERFECT', 'color': '#22c55e',
                    'age_sec': 0.0, 'x_ratio': 0.5, 'base_y': self._judgment_line_y,
                })
                if self.on_judged:
                    self.on_judged(result)
                self.update()
                return result
            else:
                # 弹错：不推进，仅记录 miss
                self._hits["miss"] += 1
                self._hit_count += 1
                self._floats.append({
                    'text': 'WRONG', 'color': '#ef4444',
                    'age_sec': 0.0, 'x_ratio': 0.5, 'base_y': self._judgment_line_y,
                })
                self.update()
                # 返回 None 让上层知道没匹配到（不是完全忽略）
                return None

        # === 自动模式：原有判定逻辑 ===
        # 找所有未判定 + 音高匹配 + 在判定窗口内的音符
        candidates = []
        for n in self._notes:
            if n.rated or n.midi != midi_note:
                continue
            # 在判定线附近：play_time 距离 n.time 正负 MISS_WINDOW_MS
            delta = (self._play_time - n.time) * 1000.0
            if abs(delta) <= self.MISS_WINDOW_MS:
                candidates.append((abs(delta), delta, n))

        if not candidates:
            return None  # 没匹配的，不判定（多余按键忽略）

        # 选 delta 最小的
        candidates.sort(key=lambda x: x[0])
        _, delta_ms, note = candidates[0]

        # rating
        if abs(delta_ms) < self.PERFECT_MS:
            rating = "perfect"
        elif abs(delta_ms) < self.GOOD_MS:
            rating = "good"
        elif abs(delta_ms) < self.OK_MS:
            rating = "ok"
        else:
            rating = "miss"

        note.rated = True
        note.rating = rating
        note.hit_delta_ms = delta_ms

        self._hits[rating] += 1
        self._hit_count += 1

        result = HitResult(
            note_id=note.id, midi=midi_note, delta_ms=delta_ms,
            rating=rating, pitch_ok=True, at=self._play_time,
        )
        rating_text = {"perfect": "PERFECT +100", "good": "GOOD +80", "ok": "OK +50", "miss": "MISS"}
        rating_color = {"perfect": "#22c55e", "good": "#3b82f6", "ok": "#f59e0b", "miss": "#ef4444"}
        self._floats.append({
            'text': rating_text.get(rating, rating),
            'color': rating_color.get(rating, '#fff'),
            'age_sec': 0.0, 'x_ratio': 0.5, 'base_y': self._judgment_line_y,
        })
        if self.on_judged:
            self.on_judged(result)
        return result

    # -------- 动画循环 --------

    def _tick(self):
        now = time.monotonic()
        dt = now - self._last_ts
        self._last_ts = now

        # 暂停：冻结（不推进 play_time、不判定）
        if self._paused:
            return

        # === 倒计时 prep 阶段 ===
        if self._prep_remaining_ms > 0:
            self._prep_remaining_ms -= dt * 1000
            cur = int(self._prep_remaining_ms / 1000)
            last = int(self._prep_last_tick_sec / 1000)
            self._prep_last_tick_sec = self._prep_remaining_ms
            if cur != last:
                self.update()
            if self._prep_remaining_ms <= 0:
                self._prep_remaining_ms = 0
                self._playing = True
                self._play_time = 0.0
            return  # prep 阶段不走后面的判定/漏按

        # 半自动模式：play_time 由正确按键驱动，timer 只刷新浮动特效 + 结束检查
        if self._mode == "semi":
            # 更新浮动分数特效
            alive = []
            for f in self._floats:
                f['age_sec'] += dt
                if f['age_sec'] < 1.2:
                    alive.append(f)
            self._floats = alive

            # 检查是否结束
            total_notes = len(self._notes)
            all_rated = all(n.rated for n in self._notes) if self._notes else True
            if all_rated and self._hit_count >= total_notes and total_notes > 0:
                self._playing = False
                self._timer.stop()
                if self.on_finished:
                    self.on_finished()

            self.update()
            return

        # === 自动模式：正常推进 ===
        self._play_time += dt

        # 检查漏按：play_time 已经超过 note.time + MISS_WINDOW_MS 还没判定
        miss_count = 0
        for n in self._notes:
            if not n.rated and self._play_time - n.time > self.MISS_WINDOW_MS / 1000.0:
                n.rated = True
                n.rating = "miss"
                miss_count += 1

        if miss_count > 0:
            self._hits["miss"] += miss_count
            self._hit_count += miss_count
            # 给每个 miss 发回调（简化：只发最后一个）
            last_miss = next((n for n in reversed(self._notes) if n.rated and n.rating == "miss"), None)
            if last_miss and self.on_judged:
                self.on_judged(HitResult(
                    note_id=last_miss.id, midi=last_miss.midi,
                    delta_ms=self.MISS_WINDOW_MS, rating="miss",
                    pitch_ok=True, at=self._play_time,
                ))
                self._floats.append({
                    'text': 'MISS', 'color': '#ef4444',
                    'age_sec': 0.0, 'x_ratio': 0.5, 'base_y': self._judgment_line_y,
                })

        # 更新浮动分数特效
        alive = []
        for f in self._floats:
            f['age_sec'] += dt
            if f['age_sec'] < 1.2:
                alive.append(f)
        self._floats = alive

        # 检查是否结束
        total_notes = len(self._notes)
        all_rated = all(n.rated for n in self._notes) if self._notes else True
        if all_rated and self._hit_count >= total_notes and total_notes > 0:
            self._playing = False
            self._timer.stop()
            if self.on_finished:
                self.on_finished()

        self.update()

    # -------- 绘制 --------

    def _note_x(self, midi: int) -> float:
        """MIDI note → 琴键左边缘 X 坐标。"""
        if midi < self.RANGE_START or midi >= self.RANGE_END:
            return -1
        w = self.width()
        white_count = sum(
            1 for m in range(self.RANGE_START, self.RANGE_END)
            if m % 12 in (0, 2, 4, 5, 7, 9, 11))
        white_w = w / max(white_count, 1)
        # 白键宽度基础上按黑键偏移
        white_idx = sum(
            1 for m in range(self.RANGE_START, midi)
            if m % 12 in (0, 2, 4, 5, 7, 9, 11))
        return white_idx * white_w

    def _white_key_width(self) -> float:
        white_count = sum(
            1 for m in range(self.RANGE_START, self.RANGE_END)
            if m % 12 in (0, 2, 4, 5, 7, 9, 11))
        return self.width() / max(white_count, 1)

    def _black_key_width(self) -> float:
        return self._white_key_width() * 0.6

    def _pressed_key_rect(self, midi: int, y_top: float, h: float):
        """给定 midi 和 y 区间，返回该键对应要画的矩形（按键反馈用）。"""
        key_w = self._white_key_width()
        is_black = midi % 12 in (1, 3, 6, 8, 10)
        if is_black:
            prev_white = midi - 1 if midi % 12 != 0 else midi - 12 + 11
            x_center = self._note_x(prev_white) + key_w
            w = self._black_key_width()
            x = x_center - w / 2
        else:
            x = self._note_x(midi)
            w = key_w
        if x < -10 or x > self.width() + 10:
            return None
        return QRectF(x + 1, y_top, w - 2, h)

    @property
    def _judgment_line_y(self) -> float:
        """判定线 Y 坐标（底部往上 60px）。"""
        H = self.height()
        strip_top = H - max(60.0, H * 0.35)
        return strip_top - 2.0

    def _note_rect(self, n: PianoNote) -> Optional[QRectF]:
        """根据 play_time 计算音符块当前应该画在哪个位置。"""
        key_w = self._white_key_width()

        # X 位置
        is_black = n.midi % 12 in (1, 3, 6, 8, 10)
        if is_black:
            prev_white = n.midi - 1 if n.midi % 12 != 0 else n.midi - 12 + 11
            x_center = self._note_x(prev_white) + key_w
            w = self._black_key_width()
            x = x_center - w / 2
        else:
            x = self._note_x(n.midi)
            w = key_w

        # Y 位置 — 音符上边缘对应 note.time 到达判定线时刻
        delta = self._play_time - n.time
        y_top = self._judgment_line_y + delta * self._pixels_per_second
        h = max(12.0, n.duration * self._pixels_per_second)

        if y_top > self.height() + h * 2:
            return None

        return QRectF(x, y_top, w * 0.92, h)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        W = self.width()
        H = self.height()

        # 背景
        p.fillRect(QRectF(0, 0, W, H), QColor("#1a1a2e"))

        # 画黑白键背景
        self._draw_key_background(p, W, H)

        # 轨道图例（左上角）：当前练习有几个轨道，各是什么颜色
        if self._track_order:
            font = p.font()
            font.setPointSize(9)
            font.setBold(False)
            p.setFont(font)
            lx = 8
            ly = 14
            for ti, name in enumerate(self._track_order):
                color = QColor(self._track_colors[name])
                p.fillRect(QRectF(lx, ly - 8, 12, 12), color)
                p.setPen(QPen(QColor("#ddd")))
                label = name if len(self._track_order) <= 4 else f"#{ti + 1}"
                p.drawText(QRectF(lx + 15, ly - 10, 160, 16),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           label)
                lx += 185

        # 画判定线 — 贴底部，醒目
        jy = self._judgment_line_y
        # 先画粗发光带（判定区背景）
        p.fillRect(QRectF(0, jy - 6, W, 10), QColor(251, 191, 36, 50))
        # 再画亮黄粗线
        pen = QPen(QColor("#fbbf24"))
        pen.setWidth(3)
        p.setPen(pen)
        p.drawLine(QPointF(0, jy), QPointF(W, jy))
        # 判定线下方高亮（已过的区域）
        p.fillRect(QRectF(0, jy, W, H - jy), QColor(251, 191, 36, 20))

        # 按键反馈：判定线正上方 22px 宽的绿条
        feedback_h = 18.0
        fy_top = jy - feedback_h
        for midi in self._pressed_notes:
            xr = self._pressed_key_rect(midi, fy_top, feedback_h)
            if xr is not None:
                p.fillRect(xr, QColor(34, 197, 94, 180))

        # 画音符 — 判定后按结果色，未判定按轨道色
        for n in self._notes:
            rect = self._note_rect(n)
            if rect is None:
                continue
            if n.rated or n.rating:
                color = self.NOTE_COLORS.get(n.rating, self.NOTE_COLORS[""])
            else:
                color = self.track_color(n.track)
            p.fillRect(rect, QColor(color))
            p.setPen(QPen(QColor("white")))
            p.drawRect(rect)

        # 浮动分数特效
        for f in self._floats:
            t = min(f['age_sec'] / 1.2, 1.0)
            dy = -50 * t
            font = p.font()
            font.setPointSize(20)
            font.setBold(True)
            p.setFont(font)
            p.setPen(QPen(QColor(f['color'])))
            x = self.width() * f['x_ratio']
            y = f['base_y'] + dy
            saved_op = p.opacity()
            p.setOpacity(1 - t)
            p.drawText(QRectF(x - 100, y - 15, 200, 30),
                       Qt.AlignmentFlag.AlignCenter, f['text'])
            p.setOpacity(saved_op)

        # prep 倒计时大字
        if self._prep_remaining_ms > 0:
            sec = max(0, int(self._prep_remaining_ms / 1000) + 1)
            if self._prep_remaining_ms <= 500:
                label = 'GO!'
                color = QColor('#22c55e')
            else:
                label = str(sec)
                color = QColor('#fbbf24')
            font = p.font()
            font.setPointSize(min(120, int(H / 4)))
            font.setBold(True)
            p.setFont(font)
            p.setPen(QPen(color))
            p.drawText(QRectF(0, 0, W, H),
                       Qt.AlignmentFlag.AlignCenter, label)

        p.end()

    def _draw_key_background(self, p: QPainter, W: int, H: int):
        """画底部钢琴卷帘区（整个面板底部 35% 是键位区）。"""
        key_w = self._white_key_width()
        black_w = self._black_key_width()
        strip_h = max(60.0, H * 0.35)  # 底部 35% 是键位区
        strip_top = H - strip_h

        # 键位区背景色（稍亮）
        p.fillRect(QRectF(0, strip_top, W, strip_h), QColor("#1f1f35"))

        # 白键先画 — 只在底部 strip 区域
        for midi in range(self.RANGE_START, self.RANGE_END):
            if midi % 12 not in (0, 2, 4, 5, 7, 9, 11):
                continue
            x = self._note_x(midi)
            p.fillRect(QRectF(x, strip_top, key_w - 1, strip_h), QColor("#e8e8e8"))
            # 键底部贴判定线区域
            p.fillRect(QRectF(x, self._judgment_line_y - 6, key_w - 1, 10), QColor("#b0b0b0"))

        # 黑键后画
        for midi in range(self.RANGE_START, self.RANGE_END):
            if midi % 12 not in (1, 3, 6, 8, 10):
                continue
            prev_white = midi - 1 if midi % 12 != 0 else midi - 12 + 11
            x_center = self._note_x(prev_white) + key_w
            x = x_center - black_w / 2
            # 黑键只画在 strip 上半部（不贴到底，模拟真实钢琴）
            p.fillRect(QRectF(x, strip_top, black_w, strip_h * 0.65), QColor("#0d0d14"))

        # 分隔线：键位区和音符下落区之间
        p.setPen(QPen(QColor("#3a3a5e")))
        p.drawLine(QPointF(0, strip_top), QPointF(W, strip_top))

# ============================================================
# Demo 练习 — 硬编码一段 C 大调音阶
# ============================================================

def make_demo_c_major_scale(bpm: float = 120.0) -> List[PianoNote]:
    """C 大调音阶上升: C4 D4 E4 F4 G4 A4 B4 C5 — 每个音一拍。"""
    beat_sec = 60.0 / bpm
    notes = [
        PianoNote(id=i, midi=midi, time=0.5 + i * beat_sec, duration=beat_sec * 0.8)
        for i, midi in enumerate((60, 62, 64, 65, 67, 69, 71, 72))
    ]
    return notes


def make_demo_simple_twinkle(bpm: float = 120.0) -> List[PianoNote]:
    """一闪一闪小星星: C C G G A A G — 四分音符。"""
    beat_sec = 60.0 / bpm
    half = beat_sec / 2
    melody = [
        (60, half), (60, half), (67, half), (67, half),
        (69, half), (69, half), (67, half),
        (65, half), (65, half), (64, half), (64, half),
        (62, half), (62, half), (60, half),
    ]
    notes = []
    t = 0.5  # 留出 prep 时间
    for i, (midi, dur) in enumerate(melody):
        notes.append(PianoNote(id=i, midi=midi, time=t, duration=dur * 0.9))
        t += dur
    return notes


DEMO_PRESETS = {
    "C大调音阶": make_demo_c_major_scale,
    "小星星": make_demo_simple_twinkle,
}


# ============================================================
# Plugin
# ============================================================

class PracticePlugin(Plugin):
    """竖向钢琴帘练习 Plugin。"""

    name = "practice"
    version = "0.2"
    status = "dev"   # dev = 开发中

    def __init__(self):
        super().__init__()
        self.app = None
        self.log = None
        self._lane: Optional[PianoLaneWidget] = None
        self._vexview: Optional[QWebEngineView] = None
        self._state = "idle"   # idle | playing | finished
        self._pressed = set()  # 当前按住的 note（去重 note_off）
        self._score_mode = "treble"   # treble | jianpu
        self._vex_ready = False
        self._vex_pending = None

    def on_activate(self, app):
        self.app = app
        self.log = app.log
        app.subscribe("midi.note_on", self._on_note_on)
        app.subscribe("midi.note_off", self._on_note_off)
        self.log.info("activated")

    def on_deactivate(self):
        if self._lane:
            self._lane.stop()
        self.log.info("deactivated")

    # -------- MIDI 回调 --------

    def _on_note_on(self, parsed):
        midi = parsed.values.get("note")
        if midi is None:
            return
        self._pressed.add(midi)
        if self._lane:
            self._lane.set_pressed_notes(self._pressed.copy())
            if self._lane.playing:
                self._lane.judge_note_on(midi)

    def _on_note_off(self, parsed):
        midi = parsed.values.get("note")
        if midi is None:
            return
        self._pressed.discard(midi)
        if self._lane:
            self._lane.set_pressed_notes(self._pressed.copy())

    # -------- UI --------

    def create_panel(self) -> QWidget:
        return self._build_panel()

    def _build_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 标题
        title = QLabel("🎹 Practice — 竖向钢琴帘")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #e0e0e0;")
        layout.addWidget(title)

        # 控制行
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("练习:"))
        self._combo_ex = QComboBox()
        self._combo_ex.addItems(list(DEMO_PRESETS.keys()))
        ctrl.addWidget(self._combo_ex)

        ctrl.addSpacing(8)
        ctrl.addWidget(QLabel("模式:"))
        self._combo_mode = QComboBox()
        self._combo_mode.addItems(["自动下落", "半自动识谱"])
        self._combo_mode.setFixedWidth(110)
        self._combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        ctrl.addWidget(self._combo_mode)

        ctrl.addSpacing(8)
        ctrl.addWidget(QLabel("谱面:"))
        self._combo_score = QComboBox()
        self._combo_score.addItems(["五线谱", "简谱"])
        self._combo_score.setFixedWidth(92)
        self._combo_score.currentIndexChanged.connect(self._on_score_mode_changed)
        ctrl.addWidget(self._combo_score)

        ctrl.addSpacing(12)
        ctrl.addWidget(QLabel("BPM:"))
        self._slider_bpm = QSlider(Qt.Orientation.Horizontal)
        self._slider_bpm.setRange(60, 220)
        self._slider_bpm.setValue(120)
        self._slider_bpm.setFixedWidth(120)
        ctrl.addWidget(self._slider_bpm)
        self._lbl_bpm = QLabel("120")
        self._slider_bpm.valueChanged.connect(lambda v: self._lbl_bpm.setText(str(v)))
        ctrl.addWidget(self._lbl_bpm)

        ctrl.addSpacing(12)
        ctrl.addWidget(QLabel("飘速:"))
        self._slider_pps = QSlider(Qt.Orientation.Horizontal)
        self._slider_pps.setRange(80, 500)
        self._slider_pps.setValue(200)
        self._slider_pps.setFixedWidth(120)
        ctrl.addWidget(self._slider_pps)

        ctrl.addStretch()

        self._btn_import = QPushButton("导入 MIDI")
        self._btn_import.setMinimumHeight(28)
        self._btn_import.clicked.connect(self._on_import_midi)
        ctrl.addWidget(self._btn_import)

        self._btn_start = QPushButton("开始")
        self._btn_start.setMinimumHeight(28)
        self._btn_start.clicked.connect(self._on_start)
        ctrl.addWidget(self._btn_start)

        self._btn_pause = QPushButton("暂停")
        self._btn_pause.setMinimumHeight(28)
        self._btn_pause.setEnabled(False)
        self._btn_pause.clicked.connect(self._on_pause)
        ctrl.addWidget(self._btn_pause)

        self._btn_reset = QPushButton("重置")
        self._btn_reset.setMinimumHeight(28)
        self._btn_reset.clicked.connect(self._on_reset)
        ctrl.addWidget(self._btn_reset)

        layout.addLayout(ctrl)

        # 可拖动分隔器：上方谱面区 / 下方钢琴帘
        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.setChildrenCollapsible(True)
        self._splitter.setHandleWidth(6)

        # VexFlow 乐谱（五线谱 + 简谱）— 高度可拖
        from plugins.practice._vex_html import VEXFLOW_HTML, VEXFLOW_BASE_URL
        self._vexview = QWebEngineView()
        self._vexview.setHtml(VEXFLOW_HTML, VEXFLOW_BASE_URL)
        self._vexview.loadFinished.connect(self._on_vex_loaded)
        self._vex_ready = False     # loadFinished 后置 True
        self._vex_pending = None    # loadFinished 前缓存的 render JS
        self._vexview.setMinimumHeight(80)
        self._splitter.addWidget(self._vexview)

        # 钢琴帘
        self._lane = PianoLaneWidget()
        self._slider_pps.valueChanged.connect(self._lane.set_pixels_per_second)
        self._lane.on_judged = self._on_judged
        self._lane.on_finished = self._on_finished
        self._lane.setMinimumHeight(200)
        self._splitter.addWidget(self._lane)

        # 演奏跟随定时器：周期性把当前拍喂给谱面做小节跳转
        # 注意：PracticePlugin 不是 QObject，QTimer 不能挂 self，挂到 splitter 上
        self._follow_timer = QTimer(self._splitter)
        self._follow_timer.setInterval(120)
        self._follow_timer.timeout.connect(self._on_follow_tick)
        self._last_follow_bar = -1

        # 初始比例：谱面 1 : 钢琴帘 2
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 2)
        layout.addWidget(self._splitter, 1)

        # 简化状态栏
        self._stats = QLabel('选择练习 → 导入 MIDI 或选 demo → 点 开始')
        self._stats.setStyleSheet('color:#aaa; padding:2px 8px; font-size:12px; background:transparent;')
        self._stats.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._stats.setFixedHeight(22)
        layout.addWidget(self._stats)
        return root

    # -------- 事件 --------

    def _on_start(self):
        if not self._lane:
            return
        bpm = float(self._slider_bpm.value())

        if getattr(self, "_imported_notes", None):
            # 优先用已导入的 MIDI
            notes = self._imported_notes
            self._stats.setText(f"用已导入的 {len(notes)} 个音符 @ {bpm:.0f} BPM → 开始")
            key = getattr(self, "_imported_key", "C")
            meter = getattr(self, "_imported_meter", "4/4")
        else:
            # 用 demo
            name = self._combo_ex.currentText()
            factory = DEMO_PRESETS.get(name)
            if not factory:
                return
            notes = factory(bpm)
            self._stats.setText(f"🎹 练习: {name} ({len(notes)} 音) → 开始!")
            key = "C"
            meter = "4/4"

        self._lane.set_notes(notes, bpm=bpm)
        self._render_score(notes, bpm, key, meter)
        self._stats.setText("🎯 进行中... 得分: 0 | P0 G0 O0 M0")
        self._lane.start()
        self._btn_start.setText("进行中")
        self._btn_start.setEnabled(False)
        self._btn_pause.setText("暂停")
        self._btn_pause.setEnabled(True)
        self._follow_timer.start()

    def _on_follow_tick(self):
        """演奏时周期性把当前拍喂给谱面 → 按小节自动跳转。"""
        if not self._lane or not self._lane.playing or self._lane.paused:
            return
        if not (getattr(self, "_vexview", None) and getattr(self, "_vex_ready", False)):
            return
        bpm = float(self._slider_bpm.value())
        meter = getattr(self, "_imported_meter", "4/4") or "4/4"
        beats_per_bar = int(meter.split("/")[0]) or 4
        beat = self._lane._play_time * bpm / 60.0
        bar = int(beat // beats_per_bar)
        if bar == self._last_follow_bar:
            return
        self._last_follow_bar = bar
        self._vexview.page().runJavaScript(
            "if (typeof setFollow === 'function') setFollow(%s);" % round(beat, 2))

    def _on_pause(self):
        if not self._lane:
            return
        if self._lane.paused:
            self._lane.resume()
            self._btn_pause.setText("暂停")
            self._btn_start.setText("进行中")
            self._follow_timer.start()
        else:
            self._lane.pause()
            self._btn_pause.setText("继续")
            self._btn_start.setText("已暂停")
            self._follow_timer.stop()
            self._stats.setText("已暂停 — 点「继续」回到演奏")

    def _on_reset(self):
        if not self._lane:
            return
        self._lane.stop()
        self._lane.reset()
        self._btn_start.setText("开始")
        self._btn_start.setEnabled(True)
        self._btn_pause.setText("暂停")
        self._btn_pause.setEnabled(False)
        self._stats.setText("已重置 — 选择练习 → 点开始")
        self._clear_vex_highlight()
        self._follow_timer.stop()
        # 回到完整谱面视图
        if getattr(self, "_vexview", None) and getattr(self, "_vex_ready", False):
            self._vexview.page().runJavaScript(
                "if (typeof clearFollow === 'function') clearFollow();")

    def _on_judged(self, result: HitResult):
        h = self._lane._hits
        total = sum(h.values())
        if total > 0:
            score = (h["perfect"] * 100 + h["good"] * 80 + h["ok"] * 50) / total
            self._stats.setText(
                f"[{result.rating.upper()}] Δ{result.delta_ms:+.0f}ms → "
                f"得分 {score:.0f} | P{h['perfect']} G{h['good']} O{h['ok']} M{h['miss']}")
        # 谱面高亮刚判定的音符
        color = {"perfect": "#22c55e", "good": "#3b82f6",
                 "ok": "#f59e0b", "miss": "#ef4444"}.get(result.rating, "#fbbf24")
        self._update_vex_highlight(result.note_id, color)

    def _on_finished(self):
        h = self._lane._hits
        total = sum(h.values())
        score = (h["perfect"] * 100 + h["good"] * 80 + h["ok"] * 50) / max(total, 1)
        self._btn_start.setText("开始")
        self._btn_start.setEnabled(True)
        self._btn_pause.setText("暂停")
        self._btn_pause.setEnabled(False)
        self._follow_timer.stop()
        self._stats.setText(f"✅ 练习结束！总分 {score:.0f}/100 — P{h['perfect']} G{h['good']} O{h['ok']} M{h['miss']}")


    # -------- MIDI 导入 --------

    def _on_import_midi(self):
        path, _ = QFileDialog.getOpenFileName(
            self._lane, "导入 MIDI 文件", "", "MIDI (*.mid *.midi);;All (*)"
        )
        if not path:
            return
        try:
            notes, bpm, key, meter, tracks = self._parse_midi(path)
        except Exception as e:
            self.log.error(f"MIDI 解析失败: {e}")
            return
        self._apply_notes(notes, bpm, key, meter, tracks)

    @staticmethod
    def _parse_midi(path: str):
        """解析 .mid 文件 → (List[PianoNote], bpm, key, meter, tracks)。

        tracks: 有序轨道名列表（如 ["Track 1", "melody"]），notes[i].track 引用它。
        """
        mid = mido.MidiFile(path)
        # 取 tempo / 调号 / 拍号（从 meta message）
        bpm = 120.0
        key = "C"
        meter = "4/4"
        for msg in mido.merge_tracks(mid.tracks):
            if msg.type == 'set_tempo':
                bpm = mido.tempo2bpm(msg.tempo)
            elif msg.type == 'key_signature':
                key = PracticePlugin._vex_key(msg.key)
            elif msg.type == 'time_signature':
                meter = f"{msg.numerator}/{msg.denominator}"
        # tick → 秒
        tpb = mid.ticks_per_beat

        # 各轨道名（mido 的 track name meta 或按序号），重名时加序号后缀保证唯一
        track_names = []
        seen = {}
        for i, tr in enumerate(mid.tracks):
            name = f"Track {i + 1}"
            for msg in tr:
                if msg.type == 'track_name' and msg.name:
                    name = msg.name
                    break
            if name in seen:
                seen[name] += 1
                name = f"{name} #{seen[name] + 1}"
            else:
                seen[name] = 1
            track_names.append(name)

        # 逐轨道解析 note（保留轨道归属）
        raw = []     # (midi, start_sec, duration_sec, velocity, track_idx)
        for ti, track in enumerate(mid.tracks):
            tick = 0
            active = {}  # note → start_tick
            for msg in track:
                tick += msg.time   # 先累加 delta，使 tick 指向本消息发生时
                if msg.type == 'note_on' and msg.velocity > 0:
                    active[msg.note] = tick
                elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                    if msg.note in active:
                        st = active.pop(msg.note)
                        sec_per_tick = mido.tick2second(1, tpb, mido.bpm2tempo(bpm))
                        start_sec = st * sec_per_tick
                        dur_sec = (tick - st) * sec_per_tick
                        if dur_sec > 0:
                            raw.append((msg.note, start_sec, dur_sec, 80, ti))

        raw.sort(key=lambda x: x[1])
        # 时间偏移到 0.5 秒开始（留 prep）
        t0 = raw[0][1] if raw else 0
        notes = [PianoNote(
            id=i, midi=n, time=max(0.5, s - t0 + 0.5), duration=max(0.1, d),
            velocity=v, track=track_names[ti],
        ) for i, (n, s, d, v, ti) in enumerate(raw)]
        return notes, bpm, key, meter, track_names

    @staticmethod
    def _vex_key(key_str: str) -> str:
        """mido key_signature 字符串 → VexFlow 调号（小调 → 关系大调）。"""
        s = (key_str or "C").strip()
        minor = s.lower().endswith("min") or s.lower().endswith("m")
        root = s.split()[0]
        if minor and root[-1].lower() == "m":
            root = root[:-1]
        # MIDI 音名 → 半音数（同音异名统一归一）
        name_map = {
            "C": 0, "B#": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3,
            "E": 4, "FB": 4, "F": 5, "E#": 5, "F#": 6, "GB": 6, "G": 7,
            "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11, "CB": 11,
        }
        pc = name_map.get(root.upper(), 0)
        if minor:
            pc = (pc + 3) % 12   # 关系大调 = 小调根 + 小三度
        # 黑键调号取 VexFlow 认识的常用拼写
        names = {0: "C", 1: "Db", 2: "D", 3: "Eb", 4: "E", 5: "F",
                 6: "F#", 7: "G", 8: "Ab", 9: "A", 10: "Bb", 11: "B"}
        return names.get(pc, "C")

    def _apply_notes(self, notes, bpm, key="C", meter="4/4", tracks=None):
        if not self._lane:
            return
        self._imported_notes = list(notes)  # 记住已导入的
        self._imported_bpm = bpm
        self._imported_key = key
        self._imported_meter = meter
        self._imported_tracks = tracks or [n.track for n in notes]
        self._lane.set_notes(notes, bpm=bpm)
        self._render_score(notes, bpm, key, meter, tracks)
        n_tracks = len(dict.fromkeys(tracks)) if tracks else 1
        self._stats.setText(
            f"已导入 {len(notes)} 个音符 / {n_tracks} 轨道 @ {bpm:.0f} BPM — 点 开始")

    def _render_score(self, notes, bpm, key="C", meter="4/4", tracks=None):
        """把练习音符喂给 VexFlow 谱面（含 time 用于小节切分、track 分行）。"""
        if not getattr(self, "_vexview", None):
            return
        import json as _json
        jp_notes = [{"midi": n.midi, "duration": n.duration,
                     "time": n.time, "track": n.track} for n in notes]
        payload = {"notes": jp_notes, "clef": "treble",
                   "key": key, "meter": meter, "bpm": float(bpm)}
        js = "render(%s);" % _json.dumps(payload)
        mode_js = "setMode('%s');" % ("jianpu" if self._score_mode == "jianpu" else "treble")
        if getattr(self, "_vex_ready", False):
            self._vexview.page().runJavaScript(js)
            self._vexview.page().runJavaScript(mode_js)
        else:
            self._vex_pending = js + " " + mode_js   # loadFinished 后重放

    def _on_score_mode_changed(self, index: int):
        """谱面模式切换：五线谱 / 简谱。"""
        self._score_mode = "jianpu" if index == 1 else "treble"
        if getattr(self, "_vexview", None) and getattr(self, "_vex_ready", False):
            self._vexview.page().runJavaScript(
                "setMode('%s');" % self._score_mode)

    def _on_mode_changed(self, index: int):
        """练习模式切换：0=自动下落, 1=半自动识谱。"""
        mode = "semi" if index == 1 else "auto"
        self._lane.set_mode(mode)
        # 如果正在练习中，重置让新模式生效
        if self._lane.playing or self._lane._prep_remaining_ms > 0:
            self._on_reset()
            self.log.info("模式已切换为 %s，请重新开始练习", mode)

    def _on_vex_loaded(self, ok: bool):
        """VexFlow 页面加载完成后触发——重放缓存里的 render。"""
        self._vex_ready = True
        if not ok:
            self.log.warning("谱面页面加载未完成")
            return
        if self._vex_pending:
            js, self._vex_pending = self._vex_pending, None
            self._vexview.page().runJavaScript(js)

    def _vex_highlight_js(self, flat_index: int, color: str) -> str:
        return (f"if (typeof highlightVf === 'function') "
                f"highlightVf({int(flat_index)}, '{color}');")

    def _update_vex_highlight(self, flat_index: int, color: str = "#fbbf24"):
        if getattr(self, "_vexview", None) and getattr(self, "_vex_ready", False):
            self._vexview.page().runJavaScript(self._vex_highlight_js(flat_index, color))

    def _clear_vex_highlight(self):
        if getattr(self, "_vexview", None) and getattr(self, "_vex_ready", False):
            self._vexview.page().runJavaScript(
                "if (typeof clearHighlight === 'function') clearHighlight();")


def _note_name(midi: int) -> str:
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    octave = midi // 12 - 1
    return f"{names[midi % 12]}{octave}"



