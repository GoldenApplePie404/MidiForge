import re
from typing import Callable, List, Optional

# 跨来源键名别名：统一 Qt 驼峰式（PageUp）与 keyboard 库描述式
# （"space bar"/"up arrow"）等形态的差异。
_KEY_ALIASES = {
    "pageup": "page_up",
    "pagedown": "page_down",
    "spacebar": "space",
    "space_bar": "space",
    "up_arrow": "up",
    "down_arrow": "down",
    "left_arrow": "left",
    "right_arrow": "right",
    "arrow_up": "up",
    "arrow_down": "down",
    "arrow_left": "left",
    "arrow_right": "right",
}


def _normalize_single(name: str) -> str:
    """归一化单个键名（不含组合）。"""
    name = name.strip()
    if name.startswith("Qt.Key_"):
        name = name[len("Qt.Key_"):]
    elif name.startswith("Key."):
        name = name[len("Key."):]
    name = name.lower()
    name = re.sub(r"[\s-]+", "_", name)
    name = re.sub(r"(_|\s)+", "_", name).strip("_")
    if name.endswith("_key"):
        name = name[: -len("_key")]
    return _KEY_ALIASES.get(name, name)


def normalize_key(name: str) -> str:
    """把 Qt 键名或 keyboard 库键名规范为小写写法。
    支持组合键："Ctrl+Shift+K" → "ctrl+shift+k"。
    """
    if "+" in name:
        return "+".join(_normalize_single(p) for p in name.split("+"))
    return _normalize_single(name)


def send_key(name: str) -> bool:
    """发送一次按键（或组合键）。返回是否成功。
    依赖 keyboard 库（Windows 上通常不需要管理员权限就能 send）。
    """
    key_name = normalize_key(name)
    try:
        import keyboard  # type: ignore
        keyboard.send(key_name)
        return True
    except Exception:
        return False


def key_hold(name: str, down: bool) -> bool:
    """按下 / 松开某一键（组合键只取第一个分量）。"""
    key_name = normalize_key(name)
    # keyboard 库的 hold_key 需要单键
    first = key_name.split("+")[0] if "+" in key_name else key_name
    try:
        import keyboard  # type: ignore
        if down:
            keyboard.press(first)
        else:
            keyboard.release(first)
        return True
    except Exception:
        return False


class KeyboardWatcher:
    """键盘监听：焦点内捕获（Qt 事件过滤器）与可选全局钩子。"""

    def __init__(self, on_key: Callable[[str], None], use_global_hook: bool = False):
        self.on_key = on_key
        self._global_hook: Optional[object] = None
        if use_global_hook:
            self._try_start_global()

    def _try_start_global(self) -> bool:
        try:
            import keyboard  # type: ignore  # 仅 Windows/Linux 可用

            self._global_hook = keyboard.hook(self._global_callback)
            return True
        except Exception:
            self._global_hook = None
            return False

    def _global_callback(self, event) -> None:
        if event.event_type == "down":
            self.on_key(normalize_key(event.name))

    def qevent_key(self, key_name: str) -> None:
        """由 Qt 事件过滤器在返回之前调用（焦点内捕获）。"""
        self.on_key(normalize_key(key_name))

    def stop_global(self) -> None:
        if self._global_hook is not None:
            import keyboard  # type: ignore

            keyboard.unhook(self._global_hook)
            self._global_hook = None

    @property
    def global_active(self) -> bool:
        return self._global_hook is not None