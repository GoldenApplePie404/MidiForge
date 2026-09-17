from typing import List

from PyQt6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton

from midi.virtual_port import detect_virtual_out_port, setup_guide
from ui import style_qss as QSS


class PortPanel(QFrame):
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.engine = engine
        self.input_combo = QComboBox()
        self.output_combo = QComboBox()
        self.virtual_label = QLabel("虚拟端口: 未检测")
        self.virtual_label.setObjectName("virtualStatus")
        self.refresh_btn = QPushButton("刷新设备")
        self.refresh_btn.clicked.connect(self.refresh)
        self._build()
        self.refresh()

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addWidget(QLabel("输入设备"))
        layout.addWidget(self.input_combo, 3)
        layout.addWidget(QLabel("输出设备"))
        layout.addWidget(self.output_combo, 3)
        self.virtual_label.setStyleSheet(f"color: {QSS.MUTED};")
        layout.addWidget(self.virtual_label, 2)
        layout.addWidget(self.refresh_btn)

    def refresh(self) -> None:
        inputs: List[str] = self.engine.list_inputs()
        outputs: List[str] = self.engine.list_outputs()
        for combo, names in ((self.input_combo, inputs), (self.output_combo, outputs)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("（无）")
            combo.addItems(names)
            combo.blockSignals(False)
        virtual = detect_virtual_out_port(outputs, inputs)
        if virtual:
            self.virtual_label.setText(f"虚拟端口: {virtual}")
            self.virtual_label.setStyleSheet(f"color: {QSS.PRIMARY};")
        else:
            self.virtual_label.setText(setup_guide())
            self.virtual_label.setStyleSheet(f"color: {QSS.ERROR};")