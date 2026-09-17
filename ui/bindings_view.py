import json
from typing import Optional

from PyQt6.QtCore import QEvent, QTimer, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                             QFormLayout, QFrame, QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QMessageBox, QPushButton,
                             QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from core.bindings import Binding, BindingConfig, BindingSource


# 匹配用的 MIDI 事件类型下拉选项
_MIDI_EVENTS = [
    ("通配 (任意)", None),
    ("音符开 note_on", "note_on"),
    ("音符关 note_off", "note_off"),
    ("控制改变 cc", "cc"),
    ("弯音 pitch_bend", "pitch_bend"),
    ("音色切换 program_change", "program_change"),
    ("通道触后 aftertouch", "aftertouch"),
    ("复音触后 poly_aftertouch", "poly_aftertouch"),
]


def _action_desc(b: Binding) -> str:
    """把 Binding 的动作 (virtual_midi + key_out) 显示成字符串。"""
    parts = []
    if b.virtual_midi:
        vm = b.virtual_midi
        type_ = vm.get("type", "(从源推断)")
        ch = vm.get("channel", "同通道")
        detail = []
        if vm.get("note") is not None:
            detail.append(f"note={vm['note']}")
        if vm.get("control") is not None:
            detail.append(f"cc={vm['control']}")
        d = f"virtual_midi → {type_} ch{ch}"
        if detail:
            d += " (" + " ".join(detail) + ")"
        parts.append(d)
    if b.key_out:
        parts.append(f"key_out → {b.key_out.get('key', '?')}")
    return " | ".join(parts) if parts else "-"


def _source_desc(s: BindingSource) -> str:
    if s.type == "keyboard":
        return f"按键 {s.key or '?'}"
    parts = []
    if s.channel is not None:
        parts.append(f"ch{s.channel + 1}")
    else:
        parts.append("ch*")
    parts.append(s.event or "*")
    if s.note is not None:
        parts.append(f"note={s.note}")
    elif s.cc is not None:
        parts.append(f"cc={s.cc}")
    if s.value_min is not None:
        parts.append(f"≥{s.value_min}")
    return " ".join(parts)


# ---- 键名录制对话框 ----
_RECORD_MODIFIERS = {
    Qt.KeyboardModifier.ControlModifier: "ctrl",
    Qt.KeyboardModifier.AltModifier: "alt",
    Qt.KeyboardModifier.ShiftModifier: "shift",
    Qt.KeyboardModifier.MetaModifier: "win",
}

# Qt 键名到我们用的小写名
_QT_KEY_TO_NAME = {
    Qt.Key.Key_Space: "space",
    Qt.Key.Key_Return: "enter",
    Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Escape: "esc",
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Delete: "delete",
    Qt.Key.Key_Insert: "insert",
    Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "page_up",
    Qt.Key.Key_PageDown: "page_down",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_F1: "f1", Qt.Key.Key_F2: "f2", Qt.Key.Key_F3: "f3", Qt.Key.Key_F4: "f4",
    Qt.Key.Key_F5: "f5", Qt.Key.Key_F6: "f6", Qt.Key.Key_F7: "f7", Qt.Key.Key_F8: "f8",
    Qt.Key.Key_F9: "f9", Qt.Key.Key_F10: "f10", Qt.Key.Key_F11: "f11", Qt.Key.Key_F12: "f12",
}


def _qt_event_to_key_str(event: QKeyEvent) -> str:
    """把 Qt 按键事件转成我们用的 key 字符串（如 'ctrl+shift+f1'）。"""
    mods = event.modifiers()
    parts = []
    for mod, name in _RECORD_MODIFIERS.items():
        if mods & mod:
            parts.append(name)
    key = event.key()
    if key in _QT_KEY_TO_NAME:
        parts.append(_QT_KEY_TO_NAME[key])
    elif Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
        parts.append(chr(key).lower())
    elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
        parts.append(chr(key))
    elif key == Qt.Key.Key_unknown:
        return ""
    else:
        text = event.text().strip().lower()
        if text:
            parts.append(text)
        else:
            return ""
    return "+".join(parts)


class RecordKeyDialog(QDialog):
    """录制一次按键（含组合键），用 keyboard 库做全局钩子，避免 Qt 焦点问题。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("录制按键")
        self.setFixedSize(400, 180)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._result_key: str = ""
        self._pending_release = False
        self._last_state = {}  # key -> bool down

        root = QVBoxLayout(self)
        tip = QLabel("按下你想绑定的按键或组合键...\n（按 Esc 取消）")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tip.setStyleSheet("font-size: 14px; color: #00f5ff; padding: 20px;")
        root.addWidget(tip)

        self._preview = QLabel("")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet("font-size: 20px; font-weight: bold; color: #ff4488;")
        root.addWidget(self._preview)

        # 启动全局键盘钩子（后台线程）+ Qt 定时器轮询同步到 UI
        try:
            import keyboard  # type: ignore
            self._hook = keyboard.hook(self._global_callback)
            self._supported = True
        except Exception:
            self._hook = None
            self._supported = False
            tip.setText("⚠ keyboard 库不可用，录制失败")

        self._poll = QTimer(self)
        self._poll.setInterval(30)
        self._poll.timeout.connect(self._sync)
        self._poll.start()

    def _global_callback(self, event) -> None:
        """keyboard 库钩子线程：记录当前按下去的键。"""
        if not self._supported:
            return
        name = event.name
        if event.event_type == "down":
            self._last_state[name] = True
            self._pending_release = True
        elif event.event_type == "up":
            self._last_state[name] = False

    def _sync(self) -> None:
        """主线程定时轮询：把当前按键状态拼成组合键字符串。"""
        if not self._supported:
            return
        # 取所有正在按下的键
        held = [k for k, v in self._last_state.items() if v]
        if not held:
            return
        key_str = "+".join(held)
        self._preview.setText(key_str)
        self._result_key = key_str
        # 等松开所有键后关闭
        if self._pending_release and all(not v for v in self._last_state.values()):
            self.accept()

    def closeEvent(self, event) -> None:
        self._poll.stop()
        if self._hook is not None:
            try:
                import keyboard  # type: ignore
                keyboard.unhook(self._hook)
            except Exception:
                pass
        super().closeEvent(event)

    def recorded_key(self) -> str:
        return self._result_key


class BindingEditDialog(QDialog):
    """编辑单个 Binding（含 sources 列表 + 动作）。"""

    def __init__(self, binding: Optional[Binding] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑绑定")
        self.setMinimumSize(540, 480)
        self.resize(560, 620)
        self._binding = binding or Binding(signal="新信号", sources=[BindingSource(type="midi")])
        self._sources: list = [BindingSource.from_dict(s.to_dict()) for s in self._binding.sources]

        root = QVBoxLayout(self)

        # 中间主体内容进 QScrollArea
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_content = QWidget()
        sroot = QVBoxLayout(scroll_content)

        # 信号名
        form = QFormLayout()
        self.signal_edit = QLineEdit(self._binding.signal)
        form.addRow("信号名:", self.signal_edit)
        sroot.addLayout(form)

        # ---- 源列表 ----
        sroot.addWidget(QLabel("<b>匹配源（任一命中即触发）</b>"))
        self.source_table = QTableWidget(0, 3)
        self.source_table.setHorizontalHeaderLabels(("类型", "参数", "预览"))
        self.source_table.verticalHeader().setVisible(False)
        self.source_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.source_table.setMinimumHeight(160)
        sroot.addWidget(self.source_table)

        src_bar = QHBoxLayout()
        self.add_src_btn = QPushButton("+ MIDI 源")
        self.add_kbd_src_btn = QPushButton("+ 键盘源")
        self.del_src_btn = QPushButton("− 删除选中源")
        src_bar.addWidget(self.add_src_btn)
        src_bar.addWidget(self.add_kbd_src_btn)
        src_bar.addWidget(self.del_src_btn)
        src_bar.addStretch(1)
        sroot.addLayout(src_bar)

        # ---- MIDI 源参数编辑 ----
        self.src_edit = QWidget()
        src_form = QFormLayout(self.src_edit)
        src_form.setContentsMargins(0, 0, 0, 0)

        self.src_type_combo = QComboBox()
        self.src_type_combo.addItem("MIDI", "midi")
        self.src_type_combo.addItem("键盘", "keyboard")
        self.src_type_combo.currentIndexChanged.connect(self._on_src_type_changed)
        src_form.addRow("源类型:", self.src_type_combo)

        # MIDI 源参数
        self.midi_group = QWidget()
        mf = QFormLayout(self.midi_group)
        mf.setContentsMargins(0, 0, 0, 0)
        self.midi_channel = QSpinBox()
        self.midi_channel.setRange(-1, 15)
        self.midi_channel.setValue((self._binding.sources[0].channel
                                    if self._binding.sources and self._binding.sources[0].channel is not None
                                    else -1))
        self.midi_channel.setSpecialValueText("通配 *")
        mf.addRow("通道 (0-15, -1=通配):", self.midi_channel)

        self.midi_event_combo = QComboBox()
        for label, val in _MIDI_EVENTS:
            self.midi_event_combo.addItem(label, val)
        mf.addRow("事件类型:", self.midi_event_combo)

        self.midi_note = QSpinBox()
        self.midi_note.setRange(-1, 127)
        self.midi_note.setSpecialValueText("通配 *")
        mf.addRow("音符号 (note_on/off, -1=通配):", self.midi_note)

        self.midi_cc = QSpinBox()
        self.midi_cc.setRange(-1, 127)
        self.midi_cc.setSpecialValueText("通配 *")
        mf.addRow("CC 号 (-1=通配):", self.midi_cc)

        self.midi_value_min = QSpinBox()
        self.midi_value_min.setRange(-1, 127)
        self.midi_value_min.setSpecialValueText("不限")
        mf.addRow("数值下限 (≥, -1=不限):", self.midi_value_min)

        # 键盘源参数
        self.kbd_group = QWidget()
        kf = QFormLayout(self.kbd_group)
        kf.setContentsMargins(0, 0, 0, 0)
        kbd_row = QHBoxLayout()
        self.kbd_key_edit = QLineEdit()
        self.kbd_key_edit.setPlaceholderText("如 f1 / ctrl+k / ctrl+shift+s")
        self.kbd_record_btn = QPushButton("🎤 录制")
        self.kbd_record_btn.setFixedWidth(70)
        self.kbd_record_btn.clicked.connect(lambda: self._record_into(self.kbd_key_edit))
        kbd_row.addWidget(self.kbd_key_edit, 1)
        kbd_row.addWidget(self.kbd_record_btn)
        kf.addRow("按键名:", self._wrap(kbd_row))

        src_form.addRow(self.midi_group)
        src_form.addRow(self.kbd_group)
        self._on_src_type_changed(0)

        self.save_src_btn = QPushButton("保存到选中源")
        sroot.addWidget(self.src_edit)
        sroot.addWidget(self.save_src_btn)

        # ---- 动作 ----
        sroot.addSpacing(8)
        sroot.addWidget(QLabel("<b>触发后动作</b>"))

        act_form = QFormLayout()
        act_form.setContentsMargins(0, 0, 0, 0)

        # virtual_midi
        self.vm_enable = QPushButton("切换 virtual_midi")
        self.vm_enable.setCheckable(True)
        self.vm_enable.setChecked(bool(self._binding.virtual_midi))
        self.vm_enable.toggled.connect(lambda on: self.vm_group.setEnabled(on))
        act_form.addRow("转发到虚拟 MIDI:", self.vm_enable)

        self.vm_group = QWidget()
        vf = QFormLayout(self.vm_group)
        vf.setContentsMargins(0, 0, 0, 0)
        self.vm_type = QComboBox()
        self.vm_type.addItem("从源推断", None)
        for label, val in _MIDI_EVENTS[1:]:
            self.vm_type.addItem(label, val)
        # 从 binding 恢复：virtual_midi["type"] 存在则选中对应项；None 则保持"从源推断"
        vm_type_val = self._binding.virtual_midi.get("type") if self._binding.virtual_midi else None
        idx = self.vm_type.findData(vm_type_val)
        if idx >= 0:
            self.vm_type.setCurrentIndex(idx)
        vf.addRow("消息类型:", self.vm_type)
        self.vm_channel = QSpinBox()
        self.vm_channel.setRange(0, 15)
        self.vm_channel.setValue(self._binding.virtual_midi.get("channel", 0) if self._binding.virtual_midi else 0)
        vf.addRow("通道:", self.vm_channel)
        self.vm_note = QSpinBox()
        self.vm_note.setRange(-1, 127)
        self.vm_note.setSpecialValueText("从源推断")
        self.vm_note.setValue(self._binding.virtual_midi.get("note", -1) if self._binding.virtual_midi else -1)
        vf.addRow("音符号:", self.vm_note)
        self.vm_cc = QSpinBox()
        self.vm_cc.setRange(-1, 127)
        self.vm_cc.setSpecialValueText("从源推断")
        self.vm_cc.setValue(self._binding.virtual_midi.get("control", -1) if self._binding.virtual_midi else -1)
        vf.addRow("CC 号:", self.vm_cc)
        self.vm_value = QSpinBox()
        self.vm_value.setRange(-1, 127)
        self.vm_value.setSpecialValueText("从源推断")
        self.vm_value.setValue(self._binding.virtual_midi.get("value", -1) if self._binding.virtual_midi else -1)
        vf.addRow("数值:", self.vm_value)
        self.vm_group.setEnabled(self.vm_enable.isChecked())
        act_form.addRow(self.vm_group)

        # key_out
        self.key_enable = QPushButton("切换 key_out")
        self.key_enable.setCheckable(True)
        self.key_enable.setChecked(bool(self._binding.key_out))
        act_form.addRow("模拟键盘按键:", self.key_enable)

        self.key_group = QWidget()
        kof = QFormLayout(self.key_group)
        kof.setContentsMargins(0, 0, 0, 0)
        key_row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("如 f1 / ctrl+k")
        if self._binding.key_out:
            self.key_edit.setText(self._binding.key_out.get("key", ""))
        self.key_record_btn = QPushButton("🎤 录制")
        self.key_record_btn.setFixedWidth(70)
        self.key_record_btn.clicked.connect(lambda: self._record_into(self.key_edit))
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(self.key_record_btn)
        kof.addRow("按键:", self._wrap(key_row))
        # 让 key_edit / 录制按钮始终可操作；"切换 key_out" 只控制保存时是否包含
        act_form.addRow(self.key_group)

        sroot.addLayout(act_form)

        # 完成 scroll 内容，添加到 root
        scroll.setWidget(scroll_content)
        root.addWidget(scroll, 1)

        # 底部按钮固定
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        # 信号连接
        self.add_src_btn.clicked.connect(lambda: self._add_source("midi"))
        self.add_kbd_src_btn.clicked.connect(lambda: self._add_source("keyboard"))
        self.del_src_btn.clicked.connect(self._remove_source)
        self.save_src_btn.clicked.connect(self._save_source_edits)
        self.source_table.currentCellChanged.connect(self._on_source_selected)

        self._rebuild_source_table()
        # 自动选中第一行 → 触发 currentCellChanged → _on_source_selected 加载参数到表单
        if self._sources:
            self.source_table.setCurrentCell(0, 0)

        # 录制模式
        self._recording_target: Optional[QLineEdit] = None
        self._recording_held: set = set()
        self._recording_original_text = ""
        self._recording_btn: Optional[QPushButton] = None
        self.installEventFilter(self)

    def _on_src_type_changed(self, idx: int) -> None:
        is_midi = self.src_type_combo.currentData() == "midi"
        self.midi_group.setVisible(is_midi)
        self.kbd_group.setVisible(not is_midi)

    @staticmethod
    def _wrap(layout) -> QWidget:
        """把 QHBoxLayout 包成一个 QWidget，方便放进 QFormLayout。"""
        w = QWidget()
        w.setLayout(layout)
        return w

    def _record_into(self, target: QLineEdit) -> None:
        """进入录制模式：点击按钮后在对话框上按任意键会被捕获。"""
        if self._recording_target is not None:
            # 已经在录制 → 取消
            self._cancel_recording()
            return
        self._recording_target = target
        self._recording_held.clear()
        self._recording_original_text = target.text()
        target.setText("按下按键... (Esc 取消)")
        target.setStyleSheet("background-color: #3a1a2a; color: #ff4488;")
        # 找到对应的录制按钮，改文字
        btn = self.key_record_btn if target is self.key_edit else self.kbd_record_btn
        self._recording_btn = btn
        btn.setText("⏹ 停止")
        btn.setStyleSheet("background-color: #ff4488; color: white;")
        self.activateWindow()
        self.setFocus()

    def _cancel_recording(self) -> None:
        if self._recording_target is None:
            return
        self._recording_target.setText(self._recording_original_text)
        self._recording_target.setStyleSheet("")
        if self._recording_btn:
            self._recording_btn.setText("🎤 录制")
            self._recording_btn.setStyleSheet("")
        self._recording_target = None
        self._recording_btn = None
        self._recording_held.clear()

    def _finish_recording(self, key_str: str) -> None:
        if self._recording_target is None:
            return
        self._recording_target.setText(key_str)
        self._recording_target.setStyleSheet("")
        if self._recording_btn:
            self._recording_btn.setText("🎤 录制")
            self._recording_btn.setStyleSheet("")
        self._recording_target = None
        self._recording_btn = None
        self._recording_held.clear()
        # 录完自动勾选对应的 enable 按钮（如果存在）
        if self._recording_target is self.key_edit:
            self.key_enable.setChecked(True)
        elif self._recording_target is self.kbd_key_edit:
            pass  # kbd_key_edit 是源参数，不受影响

    def eventFilter(self, obj, event) -> bool:
        """捕获对话框上的按键，用于录制模式。"""
        if self._recording_target is not None and event.type() == QEvent.Type.KeyPress:
            ev = event  # QKeyEvent
            key = ev.key()
            if key == Qt.Key.Key_Escape and ev.modifiers() == Qt.KeyboardModifier.NoModifier:
                self._cancel_recording()
                return True
            key_str = _qt_event_to_key_str(ev)
            if key_str:
                self._recording_held.add(key_str)
                # 实时预览
                preview = "+".join(sorted(self._recording_held))
                self._recording_target.setText(preview)
                self._recording_target.setStyleSheet("background-color: #2a3a1a; color: #44ff88;")
                return True
        elif self._recording_target is not None and event.type() == QEvent.Type.KeyRelease:
            # 等所有键松开后提交结果
            ev = event
            key_str = _qt_event_to_key_str(ev)
            if key_str and key_str in self._recording_held:
                self._recording_held.discard(key_str)
                if not self._recording_held:
                    result = self._recording_target.text()
                    if result and result != self._recording_original_text:
                        self._finish_recording(result)
                    else:
                        self._cancel_recording()
                return True
        return super().eventFilter(obj, event)

    def _add_source(self, type_: str) -> None:
        self._sources.append(BindingSource(type=type_))
        self._rebuild_source_table()

    def _remove_source(self) -> None:
        row = self.source_table.currentRow()
        if 0 <= row < len(self._sources):
            self._sources.pop(row)
            self._rebuild_source_table()

    def _on_source_selected(self, row: int, col: int, p_row: int, p_col: int) -> None:
        if 0 <= row < len(self._sources):
            s = self._sources[row]
            if s.type == "midi":
                self.src_type_combo.setCurrentIndex(0)
                self.midi_channel.setValue(s.channel if s.channel is not None else -1)
                idx = self.midi_event_combo.findData(s.event)
                self.midi_event_combo.setCurrentIndex(idx if idx >= 0 else 0)
                self.midi_note.setValue(s.note if s.note is not None else -1)
                self.midi_cc.setValue(s.cc if s.cc is not None else -1)
                self.midi_value_min.setValue(s.value_min if s.value_min is not None else -1)
            else:
                self.src_type_combo.setCurrentIndex(1)
                self.kbd_key_edit.setText(s.key or "")

    def _save_source_edits(self) -> None:
        row = self.source_table.currentRow()
        if row < 0:
            return
        type_ = self.src_type_combo.currentData()
        if type_ == "midi":
            self._sources[row] = BindingSource(
                type="midi",
                channel=self.midi_channel.value() if self.midi_channel.value() >= 0 else None,
                event=self.midi_event_combo.currentData(),
                note=self.midi_note.value() if self.midi_note.value() >= 0 else None,
                cc=self.midi_cc.value() if self.midi_cc.value() >= 0 else None,
                value_min=self.midi_value_min.value() if self.midi_value_min.value() >= 0 else None,
            )
        else:
            self._sources[row] = BindingSource(type="keyboard", key=self.kbd_key_edit.text().strip() or None)
        self._rebuild_source_table()

    def _rebuild_source_table(self) -> None:
        self.source_table.setRowCount(0)
        for s in self._sources:
            r = self.source_table.rowCount()
            self.source_table.insertRow(r)
            self.source_table.setItem(r, 0, QTableWidgetItem(s.type))
            self.source_table.setItem(r, 1, QTableWidgetItem(_source_desc(s)))
            self.source_table.setItem(r, 2, QTableWidgetItem("← 选中后在下方编辑"))

    def _accept(self) -> None:
        if not self._sources:
            QMessageBox.warning(self, "源为空", "至少需要一个匹配源")
            return
        signal = self.signal_edit.text().strip()
        if not signal:
            QMessageBox.warning(self, "信号名", "信号名不能为空")
            return

        vm = None
        if self.vm_enable.isChecked():
            vm = {"channel": self.vm_channel.value()}
            t = self.vm_type.currentData()  # None = 从源推断
            if t is not None:
                vm["type"] = t
            # 根据消息类型只存相关的数值字段（互斥）
            if t in ("note_on", "note_off"):
                if self.vm_note.value() >= 0:
                    vm["note"] = self.vm_note.value()
                # velocity 没有独立 spinbox，从源推断
            elif t == "cc":
                if self.vm_cc.value() >= 0:
                    vm["control"] = self.vm_cc.value()
                if self.vm_value.value() >= 0:
                    vm["value"] = self.vm_value.value()
            elif t == "pitch_bend":
                if self.vm_value.value() >= 0:
                    vm["pitch"] = self.vm_value.value()
            elif t == "program_change":
                if self.vm_value.value() >= 0:
                    vm["program"] = self.vm_value.value()
            else:
                # 未知类型：保守全存（不常见）
                for k, w in (("note", self.vm_note), ("control", self.vm_cc), ("value", self.vm_value)):
                    if w.value() >= 0:
                        vm[k] = w.value()

        key_out = None
        if self.key_enable.isChecked():
            k = self.key_edit.text().strip()
            if k:
                key_out = {"key": k}

        self._binding = Binding(
            signal=signal,
            sources=[BindingSource.from_dict(s.to_dict()) for s in self._sources],
            virtual_midi=vm,
            key_out=key_out,
        )
        self.accept()

    def result_binding(self) -> Binding:
        return self._binding


class BindingsView(QFrame):
    """绑定配置编辑器：表格 + 新增/编辑/删除 + 导入导出。"""

    def __init__(self, config: BindingConfig, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self._config = config
        self._app = None  # 可由外部注入

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        title = QLabel("信号绑定")
        title.setObjectName("sectionTitle")
        self.add_btn = QPushButton("新增绑定")
        self.edit_btn = QPushButton("编辑")
        self.test_btn = QPushButton("测试触发")
        self.del_btn = QPushButton("删除")
        self.export_btn = QPushButton("导出 JSON")
        self.import_btn = QPushButton("导入 JSON")

        bar.addWidget(title)
        bar.addStretch(1)
        for w in (self.add_btn, self.edit_btn, self.test_btn, self.del_btn, self.export_btn, self.import_btn):
            bar.addWidget(w)
        root.addLayout(bar)

        # 表格：第 0 列 checkbox，1=信号名，2=匹配源，3=动作
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("✓", "信号名", "匹配源", "触发后动作"))
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for c in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(lambda _: self._edit_row())
        root.addWidget(self.table)

        self.add_btn.clicked.connect(self._add_row)
        self.edit_btn.clicked.connect(self._edit_row)
        self.test_btn.clicked.connect(self._test_row)
        self.del_btn.clicked.connect(self._remove_row)
        self.export_btn.clicked.connect(self._export)
        self.import_btn.clicked.connect(self._import)
        self._rebuild()

    def set_app(self, app) -> None:
        """注入 AppContext，用于测试触发。"""
        self._app = app

    # ---- 展示 ----
    def _rebuild(self) -> None:
        self.table.setRowCount(0)
        for b in self._config.bindings:
            r = self.table.rowCount()
            self.table.insertRow(r)
            # 第 0 列：checkbox
            cb = QTableWidgetItem()
            cb.setFlags(cb.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            cb.setCheckState(Qt.CheckState.Unchecked)
            cb.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(r, 0, cb)
            # 第 1-3 列：内容
            self.table.setItem(r, 1, QTableWidgetItem(b.signal))
            self.table.setItem(r, 2, QTableWidgetItem(" 或  ".join(_source_desc(s) for s in b.sources)))
            self.table.setItem(r, 3, QTableWidgetItem(_action_desc(b)))

    def _selected_rows(self) -> list:
        """返回所有勾选的行索引。"""
        rows = []
        for r in range(self.table.rowCount()):
            cb = self.table.item(r, 0)
            if cb and cb.checkState() == Qt.CheckState.Checked:
                rows.append(r)
        return rows

    # ---- 操作 ----
    def _current_binding_idx(self) -> int:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._config.bindings):
            return -1
        return row

    def _add_row(self) -> None:
        dlg = BindingEditDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._config.bindings.append(dlg.result_binding())
            self._rebuild()
            self._save()

    def _edit_row(self) -> None:
        idx = self._current_binding_idx()
        if idx < 0:
            return
        dlg = BindingEditDialog(self._config.bindings[idx], parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._config.bindings[idx] = dlg.result_binding()
            self._rebuild()
            self._save()

    def _test_row(self) -> None:
        """模拟一次该 binding 命中（用 source 里第一条构造）。"""
        idx = self._current_binding_idx()
        if idx < 0:
            return
        if self._app is None:
            QMessageBox.information(self, "提示", "测试触发需要先让工具处于运行状态（请先启动 GUI）")
            return
        b = self._config.bindings[idx]
        try:
            self._app.execute_actions({b.signal})
            QMessageBox.information(self, "测试成功", f"信号 '{b.signal}' 的动作已执行")
        except Exception as exc:
            QMessageBox.warning(self, "测试失败", str(exc))

    def _remove_row(self) -> None:
        # 优先批量：勾了 checkbox 就批量删，否则删当前行
        selected = self._selected_rows()
        if selected:
            signals = [self._config.bindings[r].signal for r in selected]
            msg = f"删除选中的 {len(selected)} 条绑定?\n" + "\n".join(f"  · {s}" for s in signals[:8])
            if len(signals) > 8:
                msg += f"\n  ... 还有 {len(signals) - 8} 条"
            if QMessageBox.question(self, "确认删除", msg,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                    != QMessageBox.StandardButton.Yes:
                return
            for r in sorted(selected, reverse=True):
                del self._config.bindings[r]
        else:
            idx = self._current_binding_idx()
            if idx < 0:
                return
            signal = self.table.item(idx, 1).text()
            if QMessageBox.question(self, "确认", f"删除信号 '{signal}' 及其所有源?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                    != QMessageBox.StandardButton.Yes:
                return
            del self._config.bindings[idx]
        self._rebuild()
        self._save()

    def _save(self) -> None:
        if self._app:
            self._app.save_bindings()
            self._app.bus.publish("binding.changed", None)

    def current_config(self) -> BindingConfig:
        return self._config

    def save_to(self, path: str) -> None:
        """兼容旧 API：把当前配置写到指定路径。"""
        self._config.to_file(path)

    # ---- 导入导出 ----
    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出绑定配置", "bindings.json", "JSON 文件 (*.json)")
        if path:
            self._config.to_file(path)

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入绑定配置", "", "JSON 文件 (*.json)")
        if not path:
            return
        try:
            new_cfg = BindingConfig.from_file(path)
        except ValueError as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        # 原地更新，保留旧对象引用（matcher / app.config 都指着同一个对象）
        self._config.bindings.clear()
        self._config.bindings.extend(new_cfg.bindings)
        self._config.version = new_cfg.version
        self._rebuild()
        self._save()
