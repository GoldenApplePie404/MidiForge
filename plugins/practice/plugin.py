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
    QPlainTextEdit, QPushButton, QSlider, QVBoxLayout, QWidget,
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
        "": "#60a5fa",          # 未判定: 蓝
        "perfect": "#22c55e",
        "good": "#3b82f6",
        "ok": "#f59e0b",
        "miss": "#ef4444",
    }

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

        # 统计
        self._hits = {"perfect": 0, "good": 0, "ok": 0, "miss": 0}
        self._hit_count = 0

    # -------- 外部接口 --------

    def set_notes(self, notes: List[PianoNote], bpm: float = 120.0):
        self._notes = [PianoNote(
            id=i, midi=n.midi, time=n.time, duration=n.duration,
            velocity=n.velocity,
        ) for i, n in enumerate(notes)]
        self._bpm = bpm
        self._play_time = 0.0
        self._floats = []
        self._hits = {"perfect": 0, "good": 0, "ok": 0, "miss": 0}
        self._hit_count = 0
        self.update()

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
        self._play_time = 0.0
        self._last_ts = time.monotonic()
        self._timer.start()

    def stop(self):
        self._playing = False
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
        """用户按下一个 MIDI note — 找判定线附近的匹配音符，返回判定结果。"""
        if not self._playing or not self._notes:
            return None

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

        # 画音符
        for n in self._notes:
            rect = self._note_rect(n)
            if rect is None:
                continue
            color = self.NOTE_COLORS.get(n.rating, self.NOTE_COLORS[""])
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
        PianoNote(id=0, midi=60, time=1 * beat_sec, duration=beat_sec * 0.8),  # C4
        PianoNote(id=1, midi=62, time=2 * beat_sec, duration=beat_sec * 0.8),  # D4
        PianoNote(id=2, midi=64, time=3 * beat_sec, duration=beat_sec * 0.8),  # E4
        PianoNote(id=3, midi=65, time=4 * beat_sec, duration=beat_sec * 0.8),  # F4
        PianoNote(id=4, midi=67, time=5 * beat_sec, duration=beat_sec * 0.8),  # G4
        PianoNote(id=5, midi=69, time=6 * beat_sec, duration=beat_sec * 0.8),  # A4
        PianoNote(id=6, midi=71, time=7 * beat_sec, duration=beat_sec * 0.8),  # B4
        PianoNote(id=7, midi=72, time=8 * beat_sec, duration=beat_sec * 0.8),  # C5
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
    version = "0.1"

    def __init__(self):
        super().__init__()
        self.app = None
        self.log = None
        self._lane: Optional[PianoLaneWidget] = None
        self._vexview: Optional[QWebEngineView] = None
        self._state = "idle"   # idle | playing | finished
        self._pressed = set()  # 当前按住的 note（去重 note_off）

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

        self._btn_import = QPushButton("📂 导入 MIDI")
        self._btn_import.setMinimumHeight(28)
        self._btn_import.clicked.connect(self._on_import_midi)
        ctrl.addWidget(self._btn_import)

        self._btn_start = QPushButton("▶ 开始")
        self._btn_start.setMinimumHeight(28)
        self._btn_start.clicked.connect(self._on_start)
        ctrl.addWidget(self._btn_start)

        self._btn_reset = QPushButton("🔄 重置")
        self._btn_reset.setMinimumHeight(28)
        self._btn_reset.clicked.connect(self._on_reset)
        ctrl.addWidget(self._btn_reset)

        layout.addLayout(ctrl)

        # VexFlow 乐谱（五线谱 + 简谱）
        from plugins.practice._vex_html import VEXFLOW_HTML, VEXFLOW_BASE_URL
        self._vexview = QWebEngineView()
        # 用本地 file:// URL 作为 base，使 HTML 里的 src="vexflow.js" 指向 assets/
        self._vexview.setHtml(VEXFLOW_HTML, VEXFLOW_BASE_URL)
        self._vexview.setFixedHeight(160)
        layout.addWidget(self._vexview)

        # 钢琴帘
        self._lane = PianoLaneWidget()
        self._slider_pps.valueChanged.connect(self._lane.set_pixels_per_second)
        self._lane.on_judged = self._on_judged
        self._lane.on_finished = self._on_finished
        layout.addWidget(self._lane, 1)

        # 简化状态栏
        self._stats = QLabel('🎵 选择练习 → 导入 MIDI 或选 demo → ▶ 开始')
        self._stats.setStyleSheet('color:#aaa; padding:2px 8px; font-size:12px; background:transparent;')
        self._stats.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._stats.setFixedHeight(22)
        layout.addWidget(self._stats)
        return root
        return root

    # -------- 事件 --------

    def _on_start(self):
        if not self._lane:
            return
        bpm = float(self._slider_bpm.value())

        if self._imported_notes:
            # 优先用已导入的 MIDI
            notes = self._imported_notes
            self._stats.setText(f"📂 用已导入的 {len(notes)} 个音符 @ {bpm:.0f} BPM → 开始!")
        else:
            # 用 demo
            name = self._combo_ex.currentText()
            factory = DEMO_PRESETS.get(name)
            if not factory:
                return
            notes = factory(bpm)
            self._stats.setText(f"🎹 练习: {name} ({len(notes)} 音) → 开始!")

        self._lane.set_notes(notes, bpm=bpm)
        self._stats.setText("🎯 进行中... 得分: 0 | P0 G0 O0 M0")
        self._lane.start()
        self._btn_start.setText("⏸ 进行中...")
        self._btn_start.setEnabled(False)

    def _on_reset(self):
        if not self._lane:
            return
        self._lane.stop()
        self._lane.reset()
        self._btn_start.setText("▶ 开始")
        self._btn_start.setEnabled(True)
        self._stats.setText("已重置 — 选择练习 → 点开始")

    def _on_judged(self, result: HitResult):
        h = self._lane._hits
        total = sum(h.values())
        if total > 0:
            score = (h["perfect"] * 100 + h["good"] * 80 + h["ok"] * 50) / total
            self._stats.setText(
                f"[{result.rating.upper()}] Δ{result.delta_ms:+.0f}ms → "
                f"得分 {score:.0f} | P{h['perfect']} G{h['good']} O{h['ok']} M{h['miss']}")

    def _on_finished(self):
        h = self._lane._hits
        total = sum(h.values())
        score = (h["perfect"] * 100 + h["good"] * 80 + h["ok"] * 50) / max(total, 1)
        self._btn_start.setText("▶ 开始")
        self._btn_start.setEnabled(True)
        self._stats.setText(f"✅ 练习结束！总分 {score:.0f}/100 — P{h['perfect']} G{h['good']} O{h['ok']} M{h['miss']}")


    # -------- MIDI 导入 --------

    def _on_import_midi(self):
        path, _ = QFileDialog.getOpenFileName(
            self._lane, "导入 MIDI 文件", "", "MIDI (*.mid *.midi);;All (*)"
        )
        if not path:
            return
        try:
            notes, bpm = self._parse_midi(path)
        except Exception as e:
            self.log.error(f"MIDI 解析失败: {e}")
            return
        self._apply_notes(notes, bpm)

    @staticmethod
    def _parse_midi(path: str):
        """解析 .mid 文件 → (List[PianoNote], bpm)。"""
        mid = mido.MidiFile(path)
        # 取 tempo 和 拍号（从 meta message）
        bpm = 120.0
        for msg in mido.merge_tracks(mid.tracks):
            if msg.type == 'set_tempo':
                bpm = mido.tempo2bpm(msg.tempo)
                break
        # tick → 秒
        tpb = mid.ticks_per_beat

        # 合并所有 track 的 note
        active = {}  # channel → note → start_tick
        raw = []     # (midi, start_sec, duration_sec, velocity)
        for track in mid.tracks:
            tick = 0
            for msg in track:
                if msg.type == 'set_tempo':
                    tick += msg.time
                    continue
                if msg.type == 'note_on' and msg.velocity > 0:
                    start_tick = tick
                    active.setdefault(msg.channel, {})[msg.note] = start_tick
                elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                    ch = msg.channel
                    if ch in active and msg.note in active[ch]:
                        st = active[ch].pop(msg.note)
                        sec_per_tick = mido.tick2second(1, tpb, mido.bpm2tempo(bpm))
                        start_sec = st * sec_per_tick
                        dur_sec = (tick - st) * sec_per_tick
                        raw.append((msg.note, start_sec, max(0.1, dur_sec), 80))
                tick += msg.time

        raw.sort(key=lambda x: x[1])
        # 时间偏移到 0.5 秒开始（留 prep）
        t0 = raw[0][1] if raw else 0
        notes = [PianoNote(
            id=i, midi=n, time=max(0.5, s - t0 + 0.5), duration=d, velocity=v
        ) for i, (n, s, d, v) in enumerate(raw)]
        return notes, bpm

    def _apply_notes(self, notes, bpm):
        if not self._lane:
            return
        self._imported_notes = list(notes)  # 记住已导入的
        self._imported_bpm = bpm
        self._lane.set_notes(notes, bpm=bpm)
        # 喂给 VexFlow
        if self._vexview:
            import json as _json
            jp_notes = [{"midi": n.midi, "duration": n.duration} for n in notes]
            # vexflow_jianpu.js 需要 CDN 加载；先尝试，失败就静默
            js = f"try {{ render({{notes: {_json.dumps(jp_notes)}, clef:'treble', key:'C', meter:'4/4'}}); }} catch(e) {{ console.log('vexflow render failed:', e); }}"
            self._vexview.page().runJavaScript(js)
        self._stats.setText(f"📂 已导入 {len(notes)} 个音符 @ {bpm:.0f} BPM — 点 ▶ 开始")

    def _update_vex_highlight(self, flat_index: int, color: str = "#fbbf24"):
        if self._vexview:
            self._vexview.page().runJavaScript(
                f"if (typeof highlightVf === 'function') highlightVf({flat_index}, '{color}');"
            )


def _note_name(midi: int) -> str:
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    octave = midi // 12 - 1
    return f"{names[midi % 12]}{octave}"



