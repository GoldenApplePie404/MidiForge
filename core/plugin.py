"""插件基类。"""

from abc import ABC
from pathlib import Path


class Plugin(ABC):
    """插件基类。

    使用模式::

        class MyPlugin(Plugin):
            name = "my_plugin"

            def on_activate(self, app):
                self.app = app
                # 插件数据目录（自动创建，推荐用它）
                self.data_dir = app.data_dir(self.name)
                # 插件私有 logger（自动带 [my_plugin] 前缀）
                self.log = app.log
                # 订阅细分 topic
                app.subscribe("midi.cc", self._on_cc)
                # 用状态快照查当前按住的 note
                pressed = app.state.pressed_notes(channel=0)

            def on_deactivate(self): ...
            def create_panel(self): ...
    """

    name: str = ""
    version: str = "0.1"
    # status: "stable" 稳定 / "dev" 开发中 / "deprecated" 已废弃
    status: str = "stable"

    def on_activate(self, app) -> None:
        """激活时调用。app 是 api.AppContext，提供 state/log/bindings/audio/clock/data_dir 等子 API。"""

    def on_deactivate(self) -> None:
        """停用时调用，用于清理。"""

    def create_panel(self):
        """可选：返回一个 QWidget 挂进主窗口"插件"Tab；无面板插件返回 None。"""
        return None
