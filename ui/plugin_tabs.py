from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QLabel, QMenu, QTabWidget, QWidget

from ui import style_qss as QSS


_STATUS_LABEL = {
    "dev":        "开发中",
    "deprecated": "已废弃",
}


class PluginTabs(QTabWidget):
    """承载"主界面"与各插件面板的 Tab 容器。

    支持插件可插拔：tab 可被替换为"已停用"占位页，右键标签发出
    toggle_requested(name, enable) 由宿主执行真正的启停。
    """

    toggle_requested = pyqtSignal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._name_widget: dict = {}   # name → 当前 tab 上的 widget
        self._enabled: dict = {}       # name → 当前启用态（右键菜单文案用）
        tb = self.tabBar()
        tb.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tb.customContextMenuRequested.connect(self._on_tab_menu)

    # ---- 挂载 / 刷新 ----

    def mount(self, name: str, widget: QWidget, status: str = "stable",
              enabled: bool = True) -> None:
        self.set_view(name, widget, status, enabled)

    def mount_disabled(self, name: str, status: str = "stable") -> None:
        self.set_view(name, None, status, enabled=False)

    def show_error(self, name: str, error: str) -> None:
        box = QLabel(f"插件 {name} 加载失败：{error}")
        box.setWordWrap(True)
        box.setStyleSheet(f"color: {QSS.ERROR}; padding: 8px;")
        self.set_view(name, box, "stable", enabled=False)

    def set_view(self, name: str, widget, status: str = "stable",
                 enabled: bool = True, info: str = "") -> None:
        """把 name 的 tab 切到指定面板（幂等）。widget=None 显示占位页。"""
        if widget is None:
            widget = self._placeholder(name, info)
        old = self._name_widget.get(name)
        title = self._title(name, status, enabled)
        self._name_widget[name] = widget
        self._enabled[name] = enabled
        if old is None:
            self.addTab(widget, title)
            return
        idx = self.indexOf(old)
        self.removeTab(idx)
        self.insertTab(idx, widget, title)
        if old is not widget:
            old.deleteLater()

    def _placeholder(self, name: str, info: str = "") -> QLabel:
        text = f"插件 {name} 已停用。\n右键点击此标签可重新启用。"
        if info:
            text += f"\n\n{info}"
        box = QLabel(text)
        box.setWordWrap(True)
        box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.setStyleSheet(f"color: {QSS.MUTED}; padding: 12px;")
        return box

    def _title(self, name: str, status: str, enabled: bool) -> str:
        title = name
        if status in _STATUS_LABEL:
            title += f" [{_STATUS_LABEL[status]}]"
        if not enabled:
            title += " [已停用]"
        return title

    # ---- 右键菜单 ----

    def _on_tab_menu(self, pos) -> None:
        tb = self.tabBar()
        idx = tb.tabAt(pos)
        if idx < 0:
            return
        name = next((n for n, w in self._name_widget.items()
                     if self.indexOf(w) == idx), None)
        if name is None:
            return
        enabled = self._enabled.get(name, True)
        menu = QMenu(self)
        act = menu.addAction("停用此插件" if enabled else "启用此插件")
        if menu.exec(tb.mapToGlobal(pos)) == act:
            self.toggle_requested.emit(name, not enabled)
