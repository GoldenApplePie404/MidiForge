from PyQt6.QtWidgets import QLabel, QTabWidget

from ui import style_qss as QSS


class PluginTabs(QTabWidget):
    """承载“主界面”与各插件面板的 Tab 容器。"""

    def mount(self, name: str, widget) -> None:
        self.addTab(widget, name)

    def show_error(self, name: str, error: str) -> None:
        box = QLabel(f"插件 {name} 加载失败：{error}")
        box.setWordWrap(True)
        box.setStyleSheet(f"color: {QSS.ERROR}; padding: 8px;")
        self.addTab(box, name)