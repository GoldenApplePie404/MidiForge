"""MIDI Practice — 综合 MIDI 设备练习插件。

架构见 docs/superpowers/specs/2026-09-17-midi-practice-design.md
"""
import json
import sys
import time
from pathlib import Path

# ⚠️ 必须在 import 自己包内的模块之前加 — 宿主用 spec_from_file_location 加载，
# 不会自动把插件父目录放进 sys.path，导致 from midi_practice.engine import ... 找不到。
_PLUGINS_PARENT = Path(__file__).resolve().parent.parent
if str(_PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_PARENT))
from typing import List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy,
    QSlider, QVBoxLayout, QWidget,
)

from core.plugin import Plugin
from midi_practice.analytics import analyze
from midi_practice.engine import (
    Exercise, HitResult, Judge, NoteTarget, Rating_GOOD, Rating_MISS,
    Rating_OK, Rating_PERFECT, SessionResult,
)
from midi_practice.exercises import BuiltInScales, UserRecording


class PracticePlugin(Plugin):
    """MIDI Practice — 钢琴 + 打击垫综合练习。"""

    name = "midi_practice"
    version = "0.1"

    def __init__(self):
        super().__init__()
        self.app = None
        self.log = None
        self.data_dir: Optional[Path] = None
        self._builtin = BuiltInScales()
        self._exercise: Optional[Exercise] = None
        self._judge: Optional[Judge] = None
        self._state = "idle"     # idle | playing | recording
        self._rec_msgs: list = []
        self._rec_start = 0.0
        self._config_path: Optional[Path] = None
        self._config = {"bpm": 120, "tolerance_ms": 100}

    # ---- 生命周期 ----

    def on_activate(self, app):
        self.app = app
        self.log = app.log
        self.data_dir = app.data_dir("midi_practice")
        self._config_path = self.data_dir / "config.json"
        self._config = self._load_config()

        # 订阅 MIDI + clock
        app.subscribe("midi.note_on", self._on_note_on)
        app.subscribe("midi.cc", self._on_cc)
        app.subscribe("clock.tick", self._on_clock_tick)

        self.log.info("activated")

    def on_deactivate(self):
        self._state = "idle"
        if self._judge:
            self._judge.stop()
        self.log.info("deactivated")

    # ---- 配置 ----

    def _load_config(self) -> dict:
        if self._config_path and self._config_path.exists():
            try:
                return json.loads(self._config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"bpm": 120, "tolerance_ms": 100}

    def _save_config(self) -> None:
        if self._config_path is None:
            return
        try:
            self._config_path.write_text(
                json.dumps(self._config, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            self.log.warning("save config failed: %s", e)

    # ---- MIDI / Clock 回调 ----

    def _on_note_on(self, parsed):
        if self._state == "recording":
            self._rec_msgs.append((time.monotonic(), parsed))
            return
        if self._state != "playing" or self._judge is None:
            return
        self._feed_judge(parsed)

    def _on_cc(self, parsed):
        if self._state == "recording":
            self._rec_msgs.append((time.monotonic(), parsed))
            return
        if self._state != "playing" or self._judge is None:
            return
        self._feed_judge(parsed)

    def _feed_judge(self, parsed):
        result = self._judge.on_midi_in(parsed)
        if result:
            self._handle_hit(result)
        if self._judge.is_finished():
            self._finish_exercise()

    def _on_clock_tick(self, info):
        if self._state != "playing" or self._judge is None:
            return
        # 每秒检查一次超时漏按（tick 是 16th note 精度）
        if info.get("tick", 0) % 4 == 0:
            now = time.monotonic()
            now_sec = 0.0
            if self._judge._session_start:
                now_sec = now - self._judge._session_start
            misses = self._judge.mark_timeout_misses(now_sec)
            for m in misses:
                self._handle_hit(m)
            if self._judge.is_finished():
                self._finish_exercise()

    # ---- 判定结果处理 ----

    def _handle_hit(self, result: HitResult):
        """一条 HitResult 出来时：UI 更新（日志 + 得分 + 命中动画 + 视奏高亮下一个）。"""
        if not hasattr(self, "_log_view"):
            return  # UI 还没建或已关闭

        color_map = {
            Rating_PERFECT: "#4ade80",
            Rating_GOOD: "#60a5fa",
            Rating_OK: "#fbbf24",
            Rating_MISS: "#ef4444",
        }
        color = color_map.get(result.rating, "#ddd")
        t = result.expected
        tgt = f"note={t.note}({_note_name(t.note)})" if t.note else f"cc={t.cc}"
        icon = "✅" if result.pitch_ok else "❌"
        self._log_view.appendPlainText(
            f"[{result.rating.upper():7s}] Δ{result.delta_ms:+.0f}ms  {tgt}  {icon}"
        )

        # 得分
        if self._judge:
            s = self._judge._compute_stats()
            self._lbl_score.setText(
                f"得分: {s['score']}/100  P:{s['perfect']} G:{s['good']} O:{s['ok']} M:{s['miss']}")

        # 命中动画
        self._flash_target(t, color)

        # 视奏模式：高亮下一个未判定目标
        if self._exercise and self._exercise.mode == "sight_read" and self._judge:
            self._reset_sight_highlight()
            if self._judge._pending:
                next_t = self._judge._pending[0]
                self._highlight_target(next_t, "#22c55e")

    # ---- UI: 命中动画 / 高亮 ----

    def _flash_target(self, t: NoteTarget, color: str):
        if t.note is not None and hasattr(self, "_piano_keys") and t.note in self._piano_keys:
            btn = self._piano_keys[t.note]
            btn.setStyleSheet(f"background-color: {color}; color: white; font-weight: bold;")
            QTimer.singleShot(200, lambda n=t.note: self._reset_key(n))
        elif t.cc is not None and hasattr(self, "_pad_btns") and t.cc in self._pad_btns:
            btn = self._pad_btns[t.cc]
            btn.setStyleSheet(
                f"QFrame {{ background-color: {color}; border: 2px solid {color}; border-radius: 6px; }}")
            QTimer.singleShot(200, lambda c=t.cc: self._reset_pad(c))

    def _reset_key(self, midi_note: int):
        btn = self._piano_keys.get(midi_note) if hasattr(self, "_piano_keys") else None
        if btn is None:
            return
        if midi_note % 12 in (0, 2, 4, 5, 7, 9, 11):
            btn.setStyleSheet("")
        else:
            btn.setStyleSheet("background-color: #333; color: #aaa;")

    def _reset_pad(self, cc: int):
        btn = self._pad_btns.get(cc) if hasattr(self, "_pad_btns") else None
        if btn is None:
            return
        btn.setStyleSheet(
            "QFrame { background-color: #222; border: 2px solid #444; border-radius: 6px; }")

    def _reset_sight_highlight(self):
        if hasattr(self, "_piano_keys"):
            for n in list(self._piano_keys.keys()):
                self._reset_key(n)
        if hasattr(self, "_pad_btns"):
            for cc in list(self._pad_btns.keys()):
                self._reset_pad(cc)

    def _highlight_target(self, t: NoteTarget, color: str = "#22c55e"):
        if t.note is not None and hasattr(self, "_piano_keys") and t.note in self._piano_keys:
            self._piano_keys[t.note].setStyleSheet(
                f"background-color: {color}; color: white; font-weight: bold; border: 2px solid {color};")
        elif t.cc is not None and hasattr(self, "_pad_btns") and t.cc in self._pad_btns:
            self._pad_btns[t.cc].setStyleSheet(
                f"QFrame {{ background-color: {color}; border: 2px solid {color}; border-radius: 6px; }}")

    # ---- 练习控制 ----

    def _start_exercise(self, exercise: Exercise) -> None:
        self._exercise = exercise
        tol = self._config.get("tolerance_ms", 100)
        self._judge = Judge(exercise, tolerance_ms=tol)
        self._state = "playing"
        self._judge.start()
        # 清空日志
        if hasattr(self, "_log_view"):
            self._log_view.clear()
            self._log_view.appendPlainText(f"▶ 开始: {exercise.name} ({len(exercise.notes)} 目标, BPM {exercise.tempo})")
        self.log.info("start: %s (%d targets)", exercise.name, len(exercise.notes))

    def _finish_exercise(self) -> Optional[SessionResult]:
        if self._judge is None:
            return None
        session = self._judge.stop()
        self._state = "idle"
        self.log.info("finish: score=%.1f accuracy=%.1f%%",
                      session.stats.get("score", 0),
                      session.stats.get("accuracy_pct", 0))

        # UI 更新
        if hasattr(self, "_btn_start"):
            self._btn_start.setEnabled(True)
            self._btn_stop.setEnabled(False)
        if hasattr(self, "_lbl_status"):
            self._lbl_status.setText(
                f"完成！得分 {session.stats.get('score', 0)}/100")

        # 分析弹窗
        try:
            analysis = analyze(session)
            self._show_analysis(session, analysis)
        except Exception as e:
            self.log.warning("analysis failed: %s", e)

        return session

    def _show_analysis(self, session: SessionResult, analysis: dict):
        s = session.stats
        text = (
            f"<h3>练习完成！</h3>"
            f"<p><b>总分</b>: {s.get('score', 0)}/100 &nbsp;&nbsp; "
            f"<b>准确率</b>: {s.get('accuracy_pct', 0)}%</p>"
            f"<p>Perfect: {s.get('perfect', 0)} &nbsp; "
            f"Good: {s.get('good', 0)} &nbsp; "
            f"OK: {s.get('ok', 0)} &nbsp; "
            f"Miss: {s.get('miss', 0)}</p>"
        )
        ws = analysis.get("weaknesses", [])
        if ws:
            text += f"<p><b>🎯 薄弱项:</b><br>{'<br>'.join(ws)}</p>"
        sugs = analysis.get("suggestions", [])
        if sugs:
            text += f"<p><b>💡 建议:</b><br>{'<br>'.join(sugs)}</p>"

        box = QMessageBox()
        box.setWindowTitle("MIDI Practice — 成绩")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(text)
        box.addButton("🔄 重练", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        box.exec()

    # ---- 录制 ----

    def _on_record_toggled(self, checked: bool):
        if checked:
            self._state = "recording"
            self._rec_msgs = []
            self._rec_start = time.monotonic()
            self._log_view.appendPlainText("🎙 录制开始...")
            self._lbl_status.setText("录制中... 再次点击停止")
        else:
            self._stop_recording()

    def _stop_recording(self):
        self._state = "idle"
        if not self._rec_msgs:
            self._log_view.appendPlainText("❌ 录制为空")
            self._btn_record.setChecked(False)
            self._btn_record.setEnabled(True)
            return

        ex = UserRecording.from_messages(
            self._rec_msgs, bpm=float(self._slider_bpm.value()),
            name=f"用户录制 {time.strftime('%H:%M:%S')}")

        # 存盘
        try:
            import json as _j
            rec_dir = self.data_dir / "recordings" if self.data_dir else Path("recordings")
            rec_dir.mkdir(parents=True, exist_ok=True)
            path = rec_dir / f"{int(time.time())}.json"
            path.write_text(_j.dumps({
                "name": ex.name, "tempo": ex.tempo, "scope": ex.scope,
                "notes": [{"time": n.time, "note": n.note, "cc": n.cc,
                           "velocity": n.velocity} for n in ex.notes],
            }, ensure_ascii=False, indent=2))
        except Exception as e:
            self.log.warning("save recording failed: %s", e)

        self._log_view.appendPlainText(
            f"✅ 录制完成: {len(ex.notes)} 个目标 ({ex.scope})")
        self._btn_record.setChecked(False)
        self._btn_record.setEnabled(True)
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._lbl_status.setText("录制完成 — 点开始练刚录的")

        # 自动启动刚录的
        self._start_exercise(ex)

    # ---- UI ----

    def create_panel(self) -> QWidget:
        return self._build_panel()

    def _build_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # ---- 标题 ----
        title = QLabel("🎵 MIDI Practice")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        # ---- 练习选择 ----
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("练习:"))
        self._combo_ex = QComboBox()
        for key, name, scope in self._builtin.list_available():
            self._combo_ex.addItem(f"{name} [{scope}]", key)
        sel_row.addWidget(self._combo_ex, 1)
        layout.addLayout(sel_row)

        # ---- 设置行 ----
        set_row = QHBoxLayout()
        self._cb_sight = QCheckBox("视奏")
        self._cb_blind = QCheckBox("闭卷")
        self._cb_sight.setChecked(True)
        self._cb_blind.setChecked(False)
        set_row.addWidget(QLabel("模式:"))
        set_row.addWidget(self._cb_sight)
        set_row.addWidget(self._cb_blind)
        set_row.addSpacing(16)
        set_row.addWidget(QLabel("BPM:"))
        self._slider_bpm = QSlider(Qt.Orientation.Horizontal)
        self._slider_bpm.setRange(60, 220)
        self._slider_bpm.setValue(int(self._config.get("bpm", 120)))
        self._lbl_bpm = QLabel(str(self._slider_bpm.value()))
        self._slider_bpm.valueChanged.connect(
            lambda v: (self._lbl_bpm.setText(str(v)),
                       self._config.__setitem__("bpm", v)))
        set_row.addWidget(self._slider_bpm, 1)
        set_row.addWidget(self._lbl_bpm)
        set_row.addSpacing(16)
        set_row.addWidget(QLabel("容差(ms):"))
        self._slider_tol = QSlider(Qt.Orientation.Horizontal)
        self._slider_tol.setRange(30, 250)
        self._slider_tol.setValue(int(self._config.get("tolerance_ms", 100)))
        self._lbl_tol = QLabel(str(self._slider_tol.value()))
        self._slider_tol.valueChanged.connect(
            lambda v: (self._lbl_tol.setText(str(v)),
                       self._config.__setitem__("tolerance_ms", v)))
        set_row.addWidget(self._slider_tol, 1)
        set_row.addWidget(self._lbl_tol)
        layout.addLayout(set_row)

        # ---- 按钮行 ----
        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("▶ 开始")
        self._btn_start.setMinimumHeight(30)
        self._btn_start.clicked.connect(self._on_start_clicked)
        self._btn_stop = QPushButton("■ 停止")
        self._btn_stop.setMinimumHeight(30)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop_clicked)
        self._btn_record = QPushButton("🎙 录制")
        self._btn_record.setMinimumHeight(30)
        self._btn_record.setCheckable(True)
        self._btn_record.toggled.connect(self._on_record_toggled)
        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_stop)
        btn_row.addWidget(self._btn_record)
        btn_row.addStretch()
        self._lbl_status = QLabel("就绪")
        btn_row.addWidget(self._lbl_status)
        layout.addLayout(btn_row)

        # ---- 钢琴键盘 ----
        piano_frame = QFrame()
        piano_frame.setFrameShape(QFrame.Shape.StyledPanel)
        piano_layout = QVBoxLayout(piano_frame)
        piano_layout.addWidget(QLabel("🎹 钢琴键盘（视奏: 绿色高亮 = 下一个目标）"))

        self._piano_keys = {}
        piano_row = QHBoxLayout()
        for midi_note in range(36, 85):  # C2(36) ~ C6(84)
            name = _note_name(midi_note)
            is_white = midi_note % 12 in (0, 2, 4, 5, 7, 9, 11)
            btn = QPushButton(name)
            if is_white:
                btn.setFixedWidth(14)
                btn.setFixedHeight(70)
                btn.setStyleSheet("")  # 白键默认
            else:
                btn.setFixedWidth(12)
                btn.setFixedHeight(45)
                btn.setStyleSheet("background-color: #333; color: #aaa; border: 1px solid #222;")
            btn.setEnabled(False)  # 练习器只显示，不接收点击
            btn.setToolTip(name)
            self._piano_keys[midi_note] = btn
            piano_row.addWidget(btn)
        piano_layout.addLayout(piano_row)
        layout.addWidget(piano_frame)

        # ---- 打击垫网格 ----
        pad_frame = QFrame()
        pad_frame.setFrameShape(QFrame.Shape.StyledPanel)
        pad_layout = QVBoxLayout(pad_frame)
        pad_layout.addWidget(QLabel("🥁 打击垫"))

        self._pad_btns = {}
        pad_grid = QGridLayout()
        pad_labels = ["Crash", "Tom L", "Tom R", "Ride", "HH", "OH", "Snare", "Kick"]
        pad_ccs = [102, 103, 104, 105, 106, 107, 108, 109]
        for i, (cc, label) in enumerate(zip(pad_ccs, pad_labels)):
            row, col = divmod(i, 4)
            box = QFrame()
            box.setFixedSize(90, 90)
            box.setStyleSheet(
                "QFrame { background-color: #222; border: 2px solid #444; border-radius: 6px; }")
            inner = QVBoxLayout(box)
            inner.addWidget(QLabel(f"P{i+1}"))
            inner.addWidget(QLabel(f"{label}"))
            inner.addWidget(QLabel(f"CC {cc}"))
            for lbl in box.findChildren(QLabel):
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._pad_btns[cc] = box
            pad_grid.addWidget(box, row, col)
        pad_layout.addLayout(pad_grid)
        layout.addWidget(pad_frame)

        # ---- 实时反馈 ----
        log_frame = QFrame()
        log_frame.setFrameShape(QFrame.Shape.StyledPanel)
        log_layout = QVBoxLayout(log_frame)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("📋 实时反馈"))
        top_row.addStretch()
        self._lbl_score = QLabel("得分: —")
        self._lbl_score.setStyleSheet("font-weight: bold;")
        top_row.addWidget(self._lbl_score)
        log_layout.addLayout(top_row)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(200)
        self._log_view.setFixedHeight(100)
        font = QFont("Consolas")
        font.setPointSize(9)
        self._log_view.setFont(font)
        self._log_view.setStyleSheet("color: #ddd; background-color: #111;")
        log_layout.addWidget(self._log_view)
        layout.addWidget(log_frame)

        return root

    # ---- 按钮事件 ----

    def _on_start_clicked(self):
        key = self._combo_ex.currentData()
        ex = self._builtin.load(key, tempo=float(self._slider_bpm.value()))
        if self._cb_blind.isChecked():
            ex.mode = "blind"
        self._config["bpm"] = self._slider_bpm.value()
        self._config["tolerance_ms"] = self._slider_tol.value()
        self._save_config()

        self._start_exercise(ex)
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_record.setEnabled(False)
        self._lbl_status.setText(f"进行中: {ex.name}")

    def _on_stop_clicked(self):
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_record.setEnabled(True)
        self._finish_exercise()


def _note_name(midi_note: int) -> str:
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    octave = midi_note // 12 - 1
    return f"{names[midi_note % 12]}{octave}"
