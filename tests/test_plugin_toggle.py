"""PluginHost 可插拔（启用/停用/持久化）测试。

不依赖 Qt：直接造临时插件目录，fake app 只要有 subscribe 等空方法即可。
"""
import json
import sys
from pathlib import Path

import pytest

from core.plugin_loader import PluginHost

_PLUGIN_SRC = '''
from core.plugin import Plugin


class {cls}(Plugin):
    name = "{name}"
    version = "1.0"
    calls = {{"activate": 0, "deactivate": 0}}

    def on_activate(self, app):
        type(self).calls["activate"] += 1

    def on_deactivate(self):
        type(self).calls["deactivate"] += 1
'''


class FakeApp:
    def subscribe(self, *a, **k):
        pass

    def data_dir(self, name="host"):
        return Path(".")


def write_plugin(plugins_dir: Path, dirname: str, name: str, cls: str) -> None:
    d = plugins_dir / dirname
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(
        _PLUGIN_SRC.format(cls=cls, name=name), encoding="utf-8")


@pytest.fixture
def setup(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    write_plugin(plugins_dir, "plug_a", "plug_a", "PluginA")
    write_plugin(plugins_dir, "plug_b", "plug_b", "PluginB")
    state = tmp_path / "plugins.json"
    app = FakeApp()
    return plugins_dir, state, app


def _host(plugins_dir, state):
    return PluginHost([str(plugins_dir)], state_path=state)


def _item(host, name):
    return next(i for i in host.items if i["name"] == name)


# ---- 启动时按 state 禁用 ----

def test_state_disables_plugin_at_load(setup):
    plugins_dir, state, app = setup
    state.write_text(json.dumps({"disabled": ["plug_a"]}), encoding="utf-8")
    host = _host(plugins_dir, state)
    host.load_and_activate(app)

    a, b = _item(host, "plug_a"), _item(host, "plug_b")
    assert a["enabled"] is False and a["instance"] is None
    assert a["_cls"] is not None                    # 保留类引用供启用
    assert b["enabled"] is True and b["instance"] is not None
    assert b["instance"].calls["activate"] == 1


# ---- 动态启用 / 停用 ----

def test_enable_activates_and_persists(setup):
    plugins_dir, state, app = setup
    state.write_text(json.dumps({"disabled": ["plug_a"]}), encoding="utf-8")
    host = _host(plugins_dir, state)
    host.load_and_activate(app)

    ok, msg = host.set_enabled("plug_a", True, app)
    assert ok
    item = _item(host, "plug_a")
    assert item["enabled"] is True and item["instance"] is not None
    assert item["instance"].calls["activate"] == 1
    assert json.loads(state.read_text(encoding="utf-8"))["disabled"] == []


def test_disable_deactivates_and_persists(setup):
    plugins_dir, state, app = setup
    host = _host(plugins_dir, state)
    host.load_and_activate(app)

    ok, _ = host.set_enabled("plug_b", False, app)
    assert ok
    item = _item(host, "plug_b")
    assert item["enabled"] is False and item["instance"] is None
    assert item["_cls"].calls["deactivate"] == 1
    assert json.loads(state.read_text(encoding="utf-8"))["disabled"] == ["plug_b"]


def test_reenable_creates_new_instance(setup):
    """启用必须重建实例：旧实例可能残留订阅，复用会重复分发。"""
    plugins_dir, state, app = setup
    host = _host(plugins_dir, state)
    host.load_and_activate(app)
    inst1 = _item(host, "plug_b")["instance"]

    host.set_enabled("plug_b", False, app)
    host.set_enabled("plug_b", True, app)
    inst2 = _item(host, "plug_b")["instance"]

    assert inst2 is not inst1
    # calls 是类级计数：首次加载 + 重建启用 = 2 次激活、1 次停用
    assert inst2.calls["activate"] == 2
    assert inst2.calls["deactivate"] == 1


def test_enable_is_idempotent(setup):
    plugins_dir, state, app = setup
    host = _host(plugins_dir, state)
    host.load_and_activate(app)

    ok, _ = host.set_enabled("plug_b", True, app)   # 已启用再启用
    assert ok
    assert _item(host, "plug_b")["instance"].calls["activate"] == 1


# ---- 异常隔离 ----

def test_broken_plugin_cannot_be_enabled(setup, tmp_path):
    plugins_dir, state, app = setup
    bad = plugins_dir / "plug_bad"
    bad.mkdir()
    (bad / "plugin.py").write_text("raise RuntimeError('boom')\n",
                                   encoding="utf-8")
    host = _host(plugins_dir, state)
    host.load_and_activate(app)

    item = _item(host, "plug_bad")
    assert item["error"] and item["disabled"]

    ok, msg = host.set_enabled("plug_bad", True, app)
    assert not ok and "无法启用" in msg


def test_corrupt_state_treated_as_empty(setup):
    plugins_dir, state, app = setup
    state.write_text("{not json", encoding="utf-8")
    host = _host(plugins_dir, state)
    host.load_and_activate(app)                     # 不崩
    assert all(i["enabled"] for i in host.items)


def test_activate_failure_isolated(setup):
    """启用时 on_activate 抛错：不崩、状态回退、error 记录。"""
    plugins_dir, state, app = setup
    d = plugins_dir / "plug_flip"
    d.mkdir()
    (d / "plugin.py").write_text('''
from core.plugin import Plugin

N = {"n": 0}


class PluginFlip(Plugin):
    name = "plug_flip"

    def on_activate(self, app):
        N["n"] += 1
        if N["n"] > 1:
            raise ValueError("第二次激活失败")
''', encoding="utf-8")
    host = _host(plugins_dir, state)
    host.load_and_activate(app)

    ok, _ = host.set_enabled("plug_flip", False, app)
    assert ok
    ok, msg = host.set_enabled("plug_flip", True, app)
    assert not ok and "启用失败" in msg
    item = _item(host, "plug_flip")
    assert item["enabled"] is False and item["instance"] is None
    assert item["error"]
