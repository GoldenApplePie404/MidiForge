"""消息发送面板：选择通道/类型/参数并触发发送回调。"""

from typing import Callable, Dict

from PyQt6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSpinBox

from ui import style_qss as QSS

_TYPE_DEFS = {
    "note_on": {"p1": "音符", "p2": "力度", "p1_max": 127, "p2_max": 127, "kw": ("note", "velocity")},
    "note_off": {"p1": "音符", "p2": "力度", "p1_max": 127, "p2_max": 127, "kw": ("note", "velocity")},
    "cc": {"p1": "CC号", "p2": "值", "p1_max": 127, "p2_max": 127, "kw": ("control", "value")},
    "pitch_bend": {"p1": "弯音", "p2": None, "p1_max": 16383, "p2_max": 1, "kw": ("pitch",)},
    "program_change": {"p1": "音色", "p2": None, "p1_max": 127, "p2_max": 1, "kw": ("program",)},
}


class SendPanel(QFrame):
    def __init__(self, send_cb: Callable[[str, int, Dict[str, int]], None], parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.send_cb = send_cb
        self.channel_spin = QSpinBox()
        self.channel_spin.setRange(0, 15)
        self.channel_spin.setValue(0)
        self.type_combo = QComboBox()
        self.type_combo.addItems(list(_TYPE_DEFS.keys()))
        self.param1_spin = QSpinBox()
        self.param2_spin = QSpinBox()
        self.send_btn = QPushButton("发送")
        self.send_btn.setStyleSheet(f"background-color:{QSS.PRIMARY}; color:{QSS.BG}; font-weight:600;")
        self.send_btn.clicked.connect(self._do_send)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addWidget(QLabel("发送测试:"))
        layout.addWidget(QLabel("通道"))
        layout.addWidget(self.channel_spin)
        layout.addWidget(self.type_combo)
        layout.addWidget(self.param1_spin)
        layout.addWidget(self.param2_spin)
        layout.addWidget(self.send_btn)
        layout.addStretch(1)
        self.type_combo.currentTextChanged.connect(self._update_params)
        self._update_params()

    def _update_params(self) -> None:
        d = _TYPE_DEFS[self.type_combo.currentText()]
        self.param1_spin.setRange(0, d["p1_max"])
        self.param1_spin.setValue(0)
        if d["p2"] is None:
            self.param2_spin.setEnabled(False)
            self.param2_spin.setValue(0)
        else:
            self.param2_spin.setEnabled(True)
            self.param2_spin.setRange(0, d["p2_max"])
            self.param2_spin.setValue(0)

    def _do_send(self) -> None:
        d = _TYPE_DEFS[self.type_combo.currentText()]
        kw = {d["kw"][0]: self.param1_spin.value()}
        if d["p2"] is not None:
            kw[d["kw"][1]] = self.param2_spin.value()
        self.send_cb(self.type_combo.currentText(), self.channel_spin.value(), kw)