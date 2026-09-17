from PyQt6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from core.plugin import Plugin
from ui import style_qss as QSS


class NoteSentry(Plugin):
    """示例插件：显示最后收到的 MIDI 消息，并可点击发送音符。"""

    name = "note_sentry"
    version = "0.1"
    description = "示例插件：显示最后收到的 MIDI 消息，并可点击发送音符"

    def on_activate(self, app):
        self.app = app
        app.subscribe("midi.message", self.on_message)

    def on_message(self, parsed):
        label = getattr(self, "label", None)
        if label is None:
            return  # 面板尚未构造
        label.setText(f"最后消息: ch{parsed.channel + 1 if parsed.channel is not None else '-'} "
                      f"{parsed.type} {parsed.raw_hex}")

    def create_panel(self):
        box = QWidget()
        v = QVBoxLayout(box)
        self.label = QLabel("等待 MIDI 消息…")
        self.label.setStyleSheet(f"color: {QSS.TEXT}; font-size: 14px;")
        hint = QLabel("点击下方按钮向输出端口发送 C4 音符。")
        hint.setStyleSheet(f"color: {QSS.MUTED};")
        self.btn = QPushButton("发送 C4")
        self.btn.setStyleSheet(f"background-color: {QSS.ACCENT}; color: {QSS.BG}; font-weight: 600;")
        self.btn.clicked.connect(lambda: self.app.send_midi("note_on", channel=0, note=60, velocity=100))
        v.addWidget(self.label)
        v.addWidget(hint)
        v.addWidget(self.btn)
        v.addStretch(1)
        return box