from abc import ABC


class Plugin(ABC):
    """插件基类。name 必填；子类实现生命周期钩子。"""

    name: str = ""

    def on_activate(self, app) -> None:  # app: api.AppContext
        """激活时调用，可在此订阅事件、访问引擎。"""

    def on_deactivate(self) -> None:
        """停用时调用，用于清理。"""

    def create_panel(self):
        """可选：返回一个 QWidget 挂进主窗口“插件”Tab；无面板插件返回 None。"""
        return None