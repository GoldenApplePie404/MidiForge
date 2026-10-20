"""DriftCanvas — 下落音符 + 88 键钢琴画布。

布局（自上而下）：
    进度条（4px，可点击跳转）
    下落区（音符块按轨道着色，随时间向下飘向键盘线）
    键盘区（88 键 A0–C8，白/黑键分层；按下高亮；等待模式高亮待按键）

坐标模型（与 Practice2 一致）：
    音符底边接触键盘线（下落区底部）的时刻 === note.start
    y_bottom = fall_h - (note.start - position) × pps
    y_top    = fall_h - (note.end   - position) × pps

输入：
    MIDI 键盘 → 引擎（plugin.py 订阅宿主 topic）
    电脑键盘  → keyPressEvent，a w s e d f t g y h u j k o l p ; = C4–E5
    鼠标      → 点击琴键直接发声
    Shift     → 临时延音踏板；Space → 播放/暂停；Esc → 停掉全部声音
"""

import time

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from plugins.practice2._compat import QSS
from plugins.practice2._engine import (C4, MODE_WAIT, NOTE_MAX, NOTE_MIN,
                                         DriftEngine)

# 白/黑键判定（pitch_class = pitch % 12）
WHITE_PC = {0, 2, 4, 5, 7, 9, 11}

# 电脑键盘映射：C4 起 17 个半音（a w s e d f t g y h u j k o l p ;）
KEY_BINDING = "awsedftgyhujkolp;"

# 音符调色板（按通道轮转；伴奏统一用暗色）
PALETTE = ["#00E5FF", "#B388FF", "#34D399", "#F472B6",
           "#FBBF24", "#38BDF8", "#FB923C", "#E879F9"]
ACCOMP_COLOR = "#4A5678"

KB_HEIGHT = 96          # 键盘区高度（px）
PROGRESS_H = 4          # 顶部进度条高度
BLACK_W_RATIO = 0.62    # 黑键宽 / 白键宽


def _is_white(pitch: int) -> bool:
    return (pitch % 12) in WHITE_PC


def _note_name(pitch: int) -> str:
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[pitch % 12]}{pitch // 12 - 1}"


class DriftCanvas(QWidget):
    """钢琴练习画布。engine 为只读消费方；用户输入回调给宿主发声。"""

    toggle_play_requested = pyqtSignal()

    def __init__(self, engine: DriftEngine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.pps = 160.0                      # 下落速度（像素/秒）
        self.setMinimumHeight(280)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(False)

        # 用户按键发声回调（宿主注入）：cb_strike(pitch, velocity)
        # 默认直接走引擎（独立使用画布时可用）；宿主注入自己的回调会整体替换
        self.cb_strike = lambda pitch, vel: self.engine.user_note_on(pitch, vel)
        self.cb_note_off = lambda pitch: self.engine.user_note_off(pitch)
        self.cb_seek = None                  # 宿主注入：点击进度条跳转 (pos)

        # 88 键几何预计算
        self._whites = [p for p in range(NOTE_MIN, NOTE_MAX + 1) if _is_white(p)]
        self._white_idx = {p: i for i, p in enumerate(self._whites)}
        # 电脑键盘按键 → 音高
        self._key_pitch = {}
        for i, ch in enumerate(KEY_BINDING):
            qkey = getattr(Qt.Key, f"Key_{ch.upper()}") if ch != ";" else Qt.Key.Key_Semicolon
            self._key_pitch[qkey] = C4 + i
        self._shift_down = False

    # ---------------- 布局几何 ----------------

    def _kb_rect(self) -> QRectF:
        return QRectF(0, self.height() - KB_HEIGHT, self.width(), KB_HEIGHT)

    def _fall_rect(self) -> QRectF:
        return QRectF(0, PROGRESS_H, self.width(),
                      self.height() - KB_HEIGHT - PROGRESS_H)

    def _white_w(self) -> float:
        return self.width() / len(self._whites)

    def _key_rect(self, pitch: int) -> QRectF:
        """琴键在键盘区的矩形。"""
        kb = self._kb_rect()
        ww = self._white_w()
        if _is_white(pitch):
            x = self._white_idx[pitch] * ww
            return QRectF(x + 0.5, kb.top(), ww - 1.0, kb.height())
        # 黑键：贴在左侧白键的右边界上
        left_white = self._white_idx[pitch - 1] + 1   # 右邻白键序号
        bw = ww * BLACK_W_RATIO
        x = left_white * ww - bw / 2
        return QRectF(x, kb.top(), bw, kb.height() * 0.62)

    def _pitch_at(self, x: float, y: float) -> int:
        """鼠标坐标 → 音高（先查黑键再查白键）。"""
        kb = self._kb_rect()
        if not kb.contains(x, y):
            return -1
        ww = self._white_w()
        for p in range(NOTE_MIN, NOTE_MAX + 1):
            if not _is_white(p):
                r = self._key_rect(p)
                if r.contains(x, y):
                    return p
        idx = int(max(0, min(x, self.width() - 1)) / ww)
        if 0 <= idx < len(self._whites):
            return self._whites[idx]
        return -1

    # ---------------- 绘制 ----------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        eng = self.engine
        pos = eng.current_position()

        self._draw_progress(painter, pos)
        self._draw_fall(painter, pos)
        self._draw_keyboard(painter)
        self._draw_overlays(painter, pos)

    def _draw_progress(self, painter: QPainter, pos: float) -> None:
        eng = self.engine
        w = self.width()
        painter.fillRect(QRectF(0, 0, w, PROGRESS_H), QColor(QSS.BORDER))
        if eng.song and eng.song.duration > 0:
            frac = max(0.0, min(1.0, pos / eng.song.duration))
            painter.fillRect(QRectF(0, 0, w * frac, PROGRESS_H), QColor(QSS.PRIMARY))
            if eng.loop_enabled and eng.song.duration > 0:
                # A-B 区间刻度
                a = eng.loop_a / eng.song.duration
                b = min(1.0, eng.loop_b / eng.song.duration)
                painter.fillRect(QRectF(w * a, 0, max(2.0, w * (b - a)), PROGRESS_H),
                                 QColor(179, 136, 255, 140))

    def _draw_fall(self, painter: QPainter, pos: float) -> None:
        eng = self.engine
        area = self._fall_rect()
        painter.fillRect(area, QColor("#0A0E18"))

        if eng.song:
            fall_h = area.height()
            horizon = fall_h / max(self.pps, 1.0) + 2.0   # 还没进入视野的提前量
            for n in eng.song.notes:
                if n.start - pos > horizon:
                    break                                  # 按 start 排序，后面全部不可见
                y_bottom = fall_h - (n.start - pos) * self.pps
                y_top = fall_h - (n.end - pos) * self.pps
                if y_top > fall_h + 20:
                    continue
                is_practice = n.id in eng._practice_ids
                base = QColor(PALETTE[n.color_idx % len(PALETTE)]) if is_practice \
                    else QColor(ACCOMP_COLOR)
                if not is_practice:
                    base.setAlpha(150)
                if n.id in eng.hit:
                    base = base.lighter(140)
                key_r = self._key_rect(n.pitch)
                rect = QRectF(key_r.left() + 1.5, y_top - 2.0,
                              key_r.width() - 3.0, max(4.0, y_bottom - y_top))
                painter.setPen(QPen(base.darker(150), 1))
                painter.setBrush(base)
                painter.drawRoundedRect(rect, 3, 3)

        # 键盘线（判定线）
        painter.setPen(QPen(QColor(QSS.PRIMARY), 1, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(0, area.bottom()), QPointF(self.width(), area.bottom()))

    def _draw_keyboard(self, painter: QPainter) -> None:
        eng = self.engine
        kb = self._kb_rect()
        expected = eng.expected_pitches()
        now = time.monotonic()
        ww = self._white_w()

        # 白键
        for p in self._whites:
            r = self._key_rect(p)
            color = QColor("#E8ECF4")
            if p in eng.pressed:
                color = QColor(QSS.PRIMARY).lighter(120)
            painter.setPen(QPen(QColor(QSS.BORDER), 1))
            painter.setBrush(color)
            painter.drawRect(r)
            # 等待模式：待按键描边
            if p in expected:
                painter.setPen(QPen(QColor("#22C55E"), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(r.adjusted(1, 1, -1, -1))
            # 命中/漏掉闪灯
            if p in eng.hit_flash and now < eng.hit_flash[p]:
                painter.fillRect(r, QColor(34, 197, 94, 110))
            elif p in eng.miss_flash and now < eng.miss_flash[p]:
                painter.fillRect(r, QColor(255, 82, 82, 110))
            # 标注：C 键名 + 电脑键盘字母
            if p % 12 == 0:
                painter.setPen(QColor("#6B7590"))
                painter.setFont(QFont("Segoe UI", 7))
                painter.drawText(r, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
                                 _note_name(p))
            if C4 <= p <= C4 + len(KEY_BINDING) - 1:
                ch = KEY_BINDING[p - C4]
                painter.setPen(QColor("#303A55" if not _is_white(p) else "#5A647F"))
                painter.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
                painter.drawText(r, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter, ch)

        # 黑键（盖在白键之上）
        bw = ww * BLACK_W_RATIO
        for p in range(NOTE_MIN, NOTE_MAX + 1):
            if _is_white(p):
                continue
            r = self._key_rect(p)
            color = QColor("#141B30")
            if p in eng.pressed:
                color = QColor(QSS.PRIMARY).darker(160)
            painter.setPen(QPen(QColor("#05070D"), 1))
            painter.setBrush(color)
            painter.drawRect(r)
            if p in expected:
                painter.setPen(QPen(QColor("#22C55E"), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(r.adjusted(1, 1, -1, -1))
            if p in eng.hit_flash and now < eng.hit_flash[p]:
                painter.fillRect(r, QColor(34, 197, 94, 130))
            elif p in eng.miss_flash and now < eng.miss_flash[p]:
                painter.fillRect(r, QColor(255, 82, 82, 130))
            if C4 <= p <= C4 + len(KEY_BINDING) - 1:
                ch = KEY_BINDING[p - C4]
                painter.setPen(QColor("#8A94B0"))
                painter.setFont(QFont("Consolas", 7, QFont.Weight.Bold))
                painter.drawText(r, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter, ch)

    def _draw_overlays(self, painter: QPainter, pos: float) -> None:
        eng = self.engine
        # 预备拍倒计时
        if eng.playing and eng.song and pos < 0:
            beats_left = max(1, int(round(-pos / (60.0 / max(eng.song.bpm, 20.0)))))
            painter.setPen(QColor(QSS.PRIMARY))
            painter.setFont(QFont("Microsoft YaHei UI", 52, QFont.Weight.Bold))
            painter.drawText(self._fall_rect(), Qt.AlignmentFlag.AlignCenter, str(beats_left))
            painter.setFont(QFont("Microsoft YaHei UI", 11))
            painter.setPen(QColor(QSS.MUTED))
            painter.drawText(QRectF(0, self._fall_rect().bottom() - 60, self.width(), 24),
                             Qt.AlignmentFlag.AlignHCenter, "预备…")
        # 右上角统计
        if eng.song and eng.mode != "listen":
            hits, missed, acc = eng.stats()
            text = f"正确 {hits}   漏掉 {missed}   准确率 {acc}"
            if eng.loop_count > 0:
                text = f"第 {eng.loop_count + 1} 遍 · {text}"
            painter.setFont(QFont("Microsoft YaHei UI", 9))
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(text) + 16
            rect = QRectF(self.width() - tw - 8, PROGRESS_H + 6, tw, 22)
            painter.fillRect(rect, QColor(20, 26, 42, 200))
            painter.setPen(QColor(QSS.TEXT))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    # ---------------- 输入 ----------------

    def keyPressEvent(self, ev) -> None:
        key = ev.key()
        if key == Qt.Key.Key_Shift:
            self.engine.set_shift_pedal(True)
            ev.accept()
            return
        if key == Qt.Key.Key_Space:
            self.toggle_play_requested.emit()
            ev.accept()
            return
        if key == Qt.Key.Key_Escape:
            self.engine.pressed.clear()
            self.engine.stop_all_sound()
            ev.accept()
            return
        pitch = self._key_pitch.get(key)
        if pitch is not None and not ev.isAutoRepeat():
            self._strike(pitch, 95)
            ev.accept()
            return
        super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_Shift:
            self.engine.set_shift_pedal(False)
            ev.accept()
            return
        pitch = self._key_pitch.get(ev.key())
        if pitch is not None and not ev.isAutoRepeat():
            self.cb_note_off(pitch)
            ev.accept()
            return
        super().keyReleaseEvent(ev)

    def mousePressEvent(self, ev) -> None:
        self.setFocus()
        pos = ev.position()
        # 点顶部进度条 → 跳转
        if pos.y() <= PROGRESS_H + 2 and self.engine.song:
            frac = pos.x() / max(1.0, self.width())
            self._seek_request = frac * self.engine.song.duration
            if hasattr(self, "cb_seek") and self.cb_seek:
                self.cb_seek(self._seek_request)
            ev.accept()
            return
        pitch = self._pitch_at(pos.x(), pos.y())
        if pitch >= 0:
            self._strike(pitch, 95)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        pass

    def mouseReleaseEvent(self, ev) -> None:
        pos = ev.position()
        pitch = self._pitch_at(pos.x(), pos.y())
        if pitch >= 0:
            self.cb_note_off(pitch)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def _strike(self, pitch: int, velocity: int) -> None:
        """用户发声入口：只调回调，由回调负责发声 + 判定（避免双发）。"""
        self.cb_strike(pitch, velocity)
