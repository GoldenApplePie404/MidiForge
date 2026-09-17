from types import SimpleNamespace
import sys

from midi.keyboard_input import KeyboardWatcher, normalize_key


def test_qt_key_names():
    assert normalize_key("Qt.Key_Left") == "left"
    assert normalize_key("Qt.Key_Space") == "space"
    assert normalize_key("Qt.Key_A") == "a"


def test_keyboard_lib_names():
    assert normalize_key("left") == "left"
    assert normalize_key("SPACE") == "space"


def test_single_char():
    assert normalize_key("w") == "w"


def test_normalize_alias_names():
    assert normalize_key("PageUp") == "page_up"
    assert normalize_key("space bar") == "space"
    assert normalize_key("up arrow") == "up"
    assert normalize_key("Key.space") == "space"


class FakeKeyboard:
    """假的 keyboard 模块：hook 返回句柄，unhook 记录调用。"""

    def __init__(self, hook_exc=None):
        self.hook_exc = hook_exc
        self.called_hook = False
        self.unhooked = []

    def hook(self, cb):
        self.called_hook = True
        if self.hook_exc is not None:
            raise self.hook_exc
        return "h1"

    def unhook(self, handle):
        self.unhooked.append(handle)


def test_watcher_global_hook_success(monkeypatch):
    fake = FakeKeyboard()
    monkeypatch.setitem(sys.modules, "keyboard", fake)

    received = []
    watcher = KeyboardWatcher(received.append, use_global_hook=True)

    assert fake.called_hook is True
    assert watcher.global_active is True
    assert watcher._global_hook == "h1"

    watcher.stop_global()
    assert fake.unhooked == ["h1"]
    assert watcher.global_active is False


def test_watcher_global_hook_fails_downgrades(monkeypatch):
    fake = FakeKeyboard(hook_exc=PermissionError("no permission"))
    monkeypatch.setitem(sys.modules, "keyboard", fake)

    watcher = KeyboardWatcher(lambda k: None, use_global_hook=True)

    assert watcher.global_active is False
    assert watcher._global_hook is None


def test_watcher_focus_capture():
    received = []
    watcher = KeyboardWatcher(received.append, use_global_hook=False)
    watcher.qevent_key("Qt.Key_Left")
    assert received == ["left"]


def test_global_callback_only_down():
    received = []
    watcher = KeyboardWatcher(received.append, use_global_hook=False)

    watcher._global_callback(SimpleNamespace(event_type="down", name="left"))
    assert received == ["left"]

    watcher._global_callback(SimpleNamespace(event_type="up", name="left"))
    assert received == ["left"]  # up 事件不触发