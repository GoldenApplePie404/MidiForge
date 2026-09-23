"""实时 MIDI 示波器 — CC 热力图 + Pitch Bend 波形 + Aftertouch 曲线。

数据驱动：订阅 "midi.all"，每条消息更新内部 grid/curve 缓存，
QTimer 每 30ms 触发 paintEvent 重绘。
"""

import sys
import time
from collections import deque
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from PyQt6.QtCore import Qt, QRectF, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from core.plugin import Plugin
from ui import style_qss as QSS


# ---- 常量 ----
NUM_CHANNELS = 16
NUM_CCS = 128
WINDOW_SECONDS = 5.0       # 波形滑动窗口
MAX_CURVE_POINTS = 500     # 每条波形最多点数（性能）


class _OscWidget(QWidget):
    """绘制画布：上半 CC 热力图，下半 Pitch + Aftertouch 两条波形。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(560)
        # 数据：热力图（每次收到消息就更新为最新值）
        self.cc_grid = [[0] * NUM_CCS for _ in range(NUM_CHANNELS)]
        # 当前按住的琴键：note_grid[ch][note] = velocity（note_off/超时清 0）
        self.note_grid = [[0] * 128 for _ in range(NUM_CHANNELS)]
        # 波形（deque，自动淘汰旧数据）
        self.pitch_history = deque(maxlen=MAX_CURVE_POINTS)     # [(monotonic, value)]
        self.at_history = deque(maxlen=MAX_CURVE_POINTS)        # [(monotonic, ch, value)]
        self.paused = False
        self.window = WINDOW_SECONDS

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = self.width()
        h = self.height()

        # ---- 背景 ----
        painter.fillRect(self.rect(), QColor(QSS.BG))

        # ---- 分区（全部转 int，Qt 绘制接口只接受整数坐标） ----
        # Note 单条琴键条不需要太多高度；CC 热力图给足；两条波形平分剩余
        note_h = int(min(h * 0.10, 64))
        cc_h = int(min(h * 0.42, 260))
        rest = h - note_h - cc_h - 20
        half = max(rest // 2, 60)
        gap = 5

        cy = note_h + cc_h + gap
        at_y = cy + half + gap
        self._draw_note_activity(painter, 0, 0, w, note_h)
        self._draw_heatmap(painter, 0, note_h, w, cc_h)
        self._draw_waveform(painter, 0, cy, w, half,
                            self.pitch_history, y_range=(-8192, 8191),
                            color=QColor("#00E5FF"), label="Pitch Bend")
        self._draw_waveform(painter, 0, at_y, w, half,
                            self.at_history, y_range=(0, 127),
                            color=QColor("#FF4081"), label="Aftertouch")

    # ---------- Note 活动区 ----------
    def _draw_note_activity(self, p, x, y, w, h) -> None:
        title_font = QFont("Consolas", 10)
        p.setFont(title_font)
        p.setPen(QColor(QSS.PRIMARY))
        p.drawText(x + 4, y + 14, "Note 活动 (按键实时点亮)")

        # 内容区：顶部让出标题行(20px)，底部让出键号刻度行(14px)
        area_x = x + 4
        area_y = y + 20
        area_w = w - area_x - 6
        area_h = h - 20 - 14
        if area_w <= 0 or area_h <= 0:
            return

        # 单条琴键条：128 键横排，任一通道按下即整列点亮（取最大力度）
        cell_w = max(area_w / 128.0, 1.0)

        # 先画空白琴键底（区分黑键更暗）
        p.setPen(Qt.PenStyle.NoPen)
        for note in range(128):
            # C 音（note % 12 == 0）与黑键加暗区分，方便定位
            is_black = note % 12 in (1, 3, 6, 8, 10)
            base = QColor(28, 34, 44, 255) if is_black else QColor(18, 24, 34, 255)
            p.fillRect(QRectF(area_x + note * cell_w, area_y, cell_w - 0.5, area_h), base)

        # 点亮按住的键
        for note in range(128):
            vel = max(self.note_grid[ch][note] for ch in range(NUM_CHANNELS))
            if vel <= 0:
                continue
            ratio = min(vel / 127.0, 1.0)
            g = int(110 + ratio * 130)
            b = int(210 + ratio * 45)
            p.fillRect(QRectF(area_x + note * cell_w, area_y, cell_w - 0.5, area_h),
                       QColor(0, g, b, 235))

        # 琴键边框
        p.setPen(QPen(QColor(QSS.BORDER), 1))
        p.drawRect(QRectF(area_x, area_y, area_w, area_h))

        # 刻度和 C 音标签（每 12 键），画在内容区底部刻度行
        p.setFont(QFont("Consolas", 8))
        tick_y = area_y + area_h + 10
        for note in range(0, 128, 12):
            nx = area_x + note * cell_w
            p.setPen(QColor(QSS.TEXT))
            p.drawText(int(nx) - 4, tick_y, str(note))

    # ---------- CC 热力图 ----------
    def _draw_heatmap(self, p, x, y, w, h) -> None:
        title_font = QFont("Consolas", 10)
        p.setFont(title_font)
        p.setPen(QColor(QSS.PRIMARY))
        p.drawText(x + 4, y + 14, "CC 控制器热力图 (Channel × CC#)")

        # 内容区：顶部让出标题行(20px)，底部让出 CC 刻度行(14px)
        area_x = x + 60   # 留位置画 channel 标签
        area_y = y + 20
        area_w = w - area_x - 10
        area_h = h - 20 - 14

        if area_w <= 0 or area_h <= 0:
            return

        cell_w = max(area_w / NUM_CCS, 1.0)
        cell_h = max(area_h / NUM_CHANNELS, 1.0)

        # 背景网格
        p.setPen(Qt.PenStyle.NoPen)
        for ch in range(NUM_CHANNELS):
            for cc in range(NUM_CCS):
                val = self.cc_grid[ch][cc]
                rect = QRectF(area_x + cc * cell_w, area_y + ch * cell_h,
                              cell_w - 0.5, cell_h - 0.5)
                if val > 0:
                    # val (0-127) → 颜色亮度
                    ratio = val / 127.0
                    # 霓虹蓝绿渐变
                    r = int(0 + ratio * 30)
                    g = int(150 + ratio * 105)
                    b = int(200 + ratio * 55)
                    p.fillRect(rect, QColor(r, g, b, 180))
                else:
                    p.fillRect(rect, QColor(20, 25, 35, 200))

        # Channel 标签
        p.setPen(QColor(QSS.TEXT))
        p.setFont(QFont("Consolas", 8))
        for ch in range(NUM_CHANNELS):
            p.drawText(x + 2, int(area_y + ch * cell_h + cell_h * 0.7), f"Ch{ch}")

        # 边框
        p.setPen(QPen(QColor(QSS.BORDER), 1))
        p.drawRect(QRectF(area_x, area_y, area_w, area_h))

        # CC 刻度（每 16 个一个），画在内容区底部刻度行
        p.setFont(QFont("Consolas", 8))
        tick_y = area_y + area_h + 10
        for cc in range(0, NUM_CCS, 16):
            cx = area_x + cc * cell_w + cell_w * 0.5
            p.setPen(QColor(QSS.TEXT))
            p.drawText(int(cx) - 6, tick_y, f"{cc}")

    # ---------- 通用波形 ----------
    def _draw_waveform(self, p, x, y, w, h, history, y_range, color, label) -> None:
        title_font = QFont("Consolas", 10)
        p.setFont(title_font)
        p.setPen(QColor(QSS.PRIMARY))
        p.drawText(x + 4, y + 14, label)

        area_x = x + 4
        area_y = y + 18
        area_w = w - area_x - 4
        area_h = h - 24

        if area_w <= 0 or area_h <= 0:
            return

        # 背景网格线
        p.setPen(QPen(QColor(QSS.BORDER), 0.5))
        for i in range(5):
            gy = area_y + area_h * i / 4
            p.drawLine(int(area_x), int(gy), int(area_x + area_w), int(gy))
        # 中线
        mid_y = int(area_y + area_h / 2)
        p.setPen(QPen(QColor(QSS.BORDER), 0.7, Qt.PenStyle.DashLine))
        p.drawLine(int(area_x), int(mid_y), int(area_x + area_w), int(mid_y))

        if not history:
            p.setPen(QPen(QColor(QSS.MUTED), 1))
            p.setFont(QFont("Consolas", 9))
            p.drawText(area_x + 8, mid_y, "(等待数据...)")
            p.setPen(QPen(QColor(QSS.BORDER), 1))
            p.drawRect(QRectF(area_x, area_y, area_w, area_h))
            return

        now = time.monotonic()
        # ---- 以最后一个数据点为锚的滚动窗口 ----
        # 旧版用 now 过滤导致"推完杆隔一会儿就全消失"（数据被当成过期清掉）。
        # 现在锚定最后数据点：轨迹保留最近 window 秒，向左滚出画布，回看仍在。
        t_end = now
        if history:
            t_end = max(now, history[-1][0])
        t_min = t_end - self.window
        t_max = t_end + 0.01

        lo, hi = y_range
        y_span = hi - lo

        path = QPainterPath()
        first = True
        for pt in history:
            # 兼容 (ts, val) 与 (ts, ch, val) 两种格式
            ts = pt[0]
            val = pt[1] if len(pt) == 2 else pt[2]
            if ts < t_min:
                continue
            if ts > t_max:
                continue
            # 映射到像素
            px = area_x + (ts - t_min) / self.window * area_w
            py = area_y + area_h - (val - lo) / y_span * area_h
            if first:
                path.moveTo(px, py)
                first = False
            else:
                path.lineTo(px, py)

        p.setPen(QPen(color, 1.5))
        p.drawPath(path)

        # Y 轴标注
        p.setPen(QColor(QSS.MUTED))
        p.setFont(QFont("Consolas", 8))
        p.drawText(area_x + 2, area_y + 10, str(hi))
        p.drawText(area_x + 2, int(mid_y + 10), "0" if lo < 0 else str(lo + y_span // 2))
        p.drawText(area_x + 2, area_y + area_h - 2, str(lo))

        # 边框
        p.setPen(QPen(QColor(QSS.BORDER), 1))
        p.drawRect(QRectF(area_x, area_y, area_w, area_h))


class OscilloscopePlugin(Plugin):
    name = "oscilloscope"
    version = "0.1"
    status = "dev"

    def __init__(self):
        super().__init__()
        self.app = None
        self.log = None
        self._osc = None
        self._timer = QTimer()
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._redraw)

    # ============ 生命周期 ============

    def on_activate(self, app) -> None:
        self.app = app
        self.log = app.log
        app.subscribe("midi.all", self._on_midi_all)
        self._timer.start()

    def on_deactivate(self) -> None:
        self._timer.stop()

    # ============ 订阅回调 ============

    def _on_midi_all(self, parsed) -> None:
        if self._osc is None or self._osc.paused:
            return
        ch = parsed.channel if parsed.channel is not None else 0
        v = parsed.values or {}
        now = time.monotonic()

        if parsed.type == "cc":
            cc_num = v.get("control", 0)
            val = v.get("value", 0)
            if 0 <= cc_num < NUM_CCS and 0 <= ch < NUM_CHANNELS:
                self._osc.cc_grid[ch][cc_num] = val

        elif parsed.type == "note_on":
            note = v.get("note", 0)
            velocity = v.get("velocity", 0)
            if 0 <= note < 128 and 0 <= ch < NUM_CHANNELS:
                self._osc.note_grid[ch][note] = velocity if velocity > 0 else 0

        elif parsed.type == "note_off":
            note = v.get("note", 0)
            if 0 <= note < 128 and 0 <= ch < NUM_CHANNELS:
                self._osc.note_grid[ch][note] = 0

        elif parsed.type in ("pitch_bend", "pitchwheel"):
            self._osc.pitch_history.append((now, v.get("pitch", 0)))

        elif parsed.type in ("channel_aftertouch", "aftertouch"):
            self._osc.at_history.append((now, ch, v.get("value", 0)))

    # ============ UI ============

    def create_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)

        title = QLabel("实时 MIDI 示波器")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # ---- 工具栏 ----
        bar = QHBoxLayout()

        self._chk_pause = QCheckBox("暂停")
        self._chk_pause.toggled.connect(self._on_pause)
        bar.addWidget(self._chk_pause)

        self._chk_freeze = QCheckBox("清空波形")
        self._chk_freeze.toggled.connect(self._on_clear_waves)
        bar.addWidget(self._chk_freeze)

        bar.addSpacing(12)
        bar.addWidget(QLabel("窗口:"))
        self._combo_window = QComboBox()
        self._combo_window.addItems(["2s", "5s", "10s", "20s"])
        self._combo_window.setCurrentIndex(1)
        self._combo_window.currentIndexChanged.connect(self._on_window_change)
        bar.addWidget(self._combo_window)

        bar.addStretch(1)

        self._lbl_status = QLabel("就绪 · 暂停以读取历史数据")
        bar.addWidget(self._lbl_status)

        layout.addLayout(bar)

        # ---- 画布 ----
        self._osc = _OscWidget()
        self._osc.setStyleSheet(
            f'background-color: {QSS.BG}; border: 1px solid {QSS.BORDER}; border-radius: 6px;')
        layout.addWidget(self._osc, 1)

        self._panel = root
        return root

    # ============ 控件回调 ============

    def _on_pause(self, v: bool) -> None:
        if self._osc:
            self._osc.paused = v

    def _on_clear_waves(self, v: bool) -> None:
        if v and self._osc:
            self._osc.pitch_history.clear()
            self._osc.at_history.clear()
            for ch in range(NUM_CHANNELS):
                for cc in range(NUM_CCS):
                    self._osc.cc_grid[ch][cc] = 0
                for note in range(128):
                    self._osc.note_grid[ch][note] = 0
        self._chk_freeze.setChecked(False)

    def _on_window_change(self, idx: int) -> None:
        vals = [2.0, 5.0, 10.0, 20.0]
        if self._osc:
            self._osc.window = vals[idx]

    def _redraw(self) -> None:
        if self._osc and self._osc.parent() is not None:
            self._osc.update()
