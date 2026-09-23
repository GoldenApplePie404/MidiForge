"""MIDI Recorder — 录制 MIDI 输入 → 保存 .mid → 加载 → 回放到输出端口。

展示了 midi_file service 完整用法：load/save/play，以及 EventBus 实时消费。
"""

import sys
import threading
import time
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.plugin import Plugin
from ui import style_qss as QSS


def _fmt(seconds: float) -> str:
    """秒 → mm:ss.ms。"""
    if seconds < 0:
        seconds = 0
    m, s = divmod(int(seconds), 60)
    ms = int((seconds - int(seconds)) * 100)
    return f"{m:02d}:{s:02d}.{ms:02d}"


class RecorderPlugin(Plugin):
    name = "recorder"
    version = "0.1"
    status = "dev"

    def __init__(self):
        super().__init__()
        self.app = None
        self.log = None

        # 录制状态
        self._recording = False
        self._record_start = 0.0
        self._events = []             # List[ParsedMessage] 带 tick_time
        self._timer = QTimer()
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._update_timer_label)
        self._playing = False

    # ============ 生命周期 ============

    def on_activate(self, app) -> None:
        self.app = app
        self.log = app.log
        app.subscribe("midi.all", self._on_midi_all)

    def on_deactivate(self) -> None:
        self._timer.stop()

    # ============ 订阅回调 ============

    def _on_midi_all(self, parsed) -> None:
        if not self._recording or self._panel is None:
            return
        # 跳过虚拟回声和 sysex（sysex 由 sysex_editor 抓包器处理）
        if parsed.source == "virtual":
            return
        # 给 parsed 加 tick_time 属性
        parsed.tick_time = time.monotonic() - self._record_start
        self._events.append(parsed)
        self._append_row(parsed)

    # ============ UI ============

    def create_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)

        title = QLabel("MIDI 录制 / 回放")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # ---- 工具栏 ----
        bar = QHBoxLayout()

        self._btn_record = QPushButton("● 录制")
        self._btn_record.setObjectName("recordBtn")
        self._btn_record.clicked.connect(self._toggle_record)
        bar.addWidget(self._btn_record)

        self._btn_play = QPushButton("▶ 回放")
        self._btn_play.clicked.connect(self._toggle_play)
        bar.addWidget(self._btn_play)

        bar.addSpacing(12)
        bar.addWidget(QLabel("速度:"))
        self._spin_speed = QSpinBox()
        self._spin_speed.setRange(1, 400)
        self._spin_speed.setValue(100)
        self._spin_speed.setSuffix(" %")
        bar.addWidget(self._spin_speed)

        bar.addWidget(QLabel("BPM:"))
        self._spin_bpm = QSpinBox()
        self._spin_bpm.setRange(30, 300)
        self._spin_bpm.setValue(120)
        bar.addWidget(self._spin_bpm)

        bar.addStretch(1)

        self._lbl_timer = QLabel("00:00.00")
        mono = QFont("Consolas")
        mono.setPointSize(14)
        self._lbl_timer.setFont(mono)
        bar.addWidget(self._lbl_timer)

        btn_save = QPushButton("保存 .mid")
        btn_save.clicked.connect(self._on_save)
        bar.addWidget(btn_save)

        btn_load = QPushButton("加载 .mid")
        btn_load.clicked.connect(self._on_load)
        bar.addWidget(btn_load)

        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._on_clear)
        bar.addWidget(btn_clear)

        layout.addLayout(bar)

        # ---- 消息列表 ----
        frame = QFrame()
        frame.setObjectName("panel")
        row = QVBoxLayout(frame)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["时间", "类型", "Channel", "Details"])
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(0, self._table.horizontalHeader().ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, self._table.horizontalHeader().ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, self._table.horizontalHeader().ResizeMode.ResizeToContents)
        row.addWidget(self._table)

        layout.addWidget(frame, 1)

        root.setStyleSheet(
            f'QLabel#sectionTitle {{ color: {QSS.PRIMARY}; font-size: 13px; font-weight: 600; }}'
            f'QPushButton#recordBtn {{ background-color: #D32F2F; color: white; font-weight: 600; padding: 4px 12px; }}'
            f'QPushButton#recordBtn:checked {{ background-color: {QSS.ERROR}; }}'
            f'QLabel {{ color: {QSS.TEXT}; }}'
        )

        self._panel = root
        self._update_buttons()
        return root

    # ============ 录制 ============

    def _toggle_record(self) -> None:
        if not self._recording:
            self._start_record()
        else:
            self._stop_record()

    def _start_record(self) -> None:
        self._events.clear()
        self._table.setRowCount(0)
        self._record_start = time.monotonic()
        self._recording = True
        self._timer.start()
        self._btn_record.setText("■ 停止")
        self._btn_record.setStyleSheet(
            "QPushButton { background-color: #444; color: #FFF; font-weight: 600; padding: 4px 12px; }")
        self._update_buttons()
        self.log.info("开始录制 MIDI")

    def _stop_record(self) -> None:
        self._recording = False
        self._timer.stop()
        self._btn_record.setText("● 录制")
        self._btn_record.setStyleSheet("")
        self._update_buttons()
        self.log.info("录制停止，共 %d 条消息，时长 %s",
                      len(self._events), _fmt(self._current_duration()))

    def _current_duration(self) -> float:
        if self._events:
            return self._events[-1].tick_time
        return time.monotonic() - self._record_start if self._recording else 0

    def _update_timer_label(self) -> None:
        self._lbl_timer.setText(_fmt(self._current_duration()))
        # 轮询回放结束状态（主定时器在 50ms 间隔也负责刷回放按钮状态）
        if self._playing and not self._play_alive():
            self._playing = False
            self._update_buttons()

    def _play_alive(self) -> bool:
        """后台线程是否还活着。回放线程退出后返回 False。"""
        t = getattr(self, "_play_thread", None)
        return t is not None and t.is_alive()

    def _update_buttons(self) -> None:
        playing = self._playing
        recording = self._recording
        self._btn_play.setEnabled(not recording)
        self._btn_record.setEnabled(not playing)
        self._btn_play.setText("■ 停止" if playing else "▶ 回放")

    # ============ 消息列表 ============

    def _append_row(self, parsed) -> None:
        t = getattr(parsed, "tick_time", 0)
        type_label = parsed.type
        ch = parsed.channel if parsed.channel is not None else "-"
        v = parsed.values or {}
        detail = ""
        if parsed.type in ("note_on", "note_off"):
            detail = f"note={v.get('note')} vel={v.get('velocity')}"
        elif parsed.type == "cc":
            detail = f"cc={v.get('control')} val={v.get('value')}"
        elif parsed.type == "program_change":
            detail = f"prog={v.get('program')}"
        elif parsed.type in ("pitch_bend", "pitchwheel"):
            detail = f"pitch={v.get('pitch')}"
        elif parsed.type in ("channel_aftertouch", "aftertouch"):
            detail = f"at={v.get('value')}"
        elif parsed.type in ("poly_aftertouch", "polytouch"):
            detail = f"note={v.get('note')} at={v.get('value')}"
        elif parsed.type == "system":
            detail = parsed.values.get("raw_type", "system")
        else:
            detail = str(v)[:30]

        r = self._table.rowCount()
        self._table.insertRow(r)
        for i, val in enumerate([_fmt(t), type_label, str(ch), detail]):
            self._table.setItem(r, i, QTableWidgetItem(val))
        self._table.scrollToBottom()

    # ============ 回放 ============

    def _toggle_play(self) -> None:
        if self._playing:
            # v1: 不支持中途停止（play 阻塞），等自然结束
            return
        if not self._events:
            return
        self._playing = True
        self._update_buttons()

        events_copy = list(self._events)
        speed = self._spin_speed.value() / 100.0
        send_fn = self.app.send_midi

        def _worker():
            try:
                self.app.midi_file.play(events_copy, send_midi_fn=send_fn, speed=speed)
            except Exception as exc:
                self.log.warning("回放异常: %s", exc)
            # 线程结束后主 QTimer 会检测 is_alive() 并更新按钮

        self._play_thread = threading.Thread(target=_worker, daemon=True)
        self._play_thread.start()

    # ============ 保存 / 加载 ============

    def _on_save(self) -> None:
        if not self._events:
            return
        path, _ = QFileDialog.getSaveFileName(
            self._panel, "保存为 MIDI", "recording.mid",
            "MIDI 文件 (*.mid *.midi)")
        if not path:
            return
        bpm = float(self._spin_bpm.value())
        try:
            self.app.midi_file.save(self._events, path, tempo=bpm)
            self.log.info("已保存: %s (%d 条, %d BPM)", path, len(self._events), int(bpm))
        except Exception as exc:
            self.log.warning("保存失败: %s", exc)

    def _on_load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self._panel, "打开 MIDI", "",
            "MIDI 文件 (*.mid *.midi)")
        if not path:
            return
        try:
            events = self.app.midi_file.load(path)
            self._events = events
            self._table.setRowCount(0)
            for ev in events:
                self._append_row(ev)
            self._lbl_timer.setText(_fmt(self._current_duration()))
            self.log.info("已加载: %s (%d 条)", path, len(events))
        except Exception as exc:
            self.log.warning("加载失败: %s", exc)

    def _on_clear(self) -> None:
        if self._recording:
            self._stop_record()
        self._events.clear()
        self._table.setRowCount(0)
        self._lbl_timer.setText("00:00.00")
        self._update_buttons()
