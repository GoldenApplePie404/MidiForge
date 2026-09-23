from PyQt6.QtWidgets import QLabel, QTabWidget

from ui import style_qss as QSS


_STATUS_LABEL = {
    "dev":        "开发中",
    "deprecated": "已废弃",
}


class PluginTabs(QTabWidget):
    """承载"主界面"与各插件面板的 Tab 容器。"""

    def mount(self, name: str, widget, status: str = "stable") -> None:
        title = name
        if status in _STATUS_LABEL:
            title = f"{name} [{_STATUS_LABEL[status]}]"
        self.addTab(widget, title)

    def show_error(self, name: str, error: str) -> None:
        box = QLabel(f"插件 {name} 加载失败：{error}")
        box.setWordWrap(True)
        box.setStyleSheet(f"color: {QSS.ERROR}; padding: 8px;")
        self.addTab(box, name)