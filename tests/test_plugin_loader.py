import sys
import textwrap
import types

from core.plugin import Plugin
from core.plugin_loader import PluginHost


def write_plugin(tmp_path, name, source):
    d = tmp_path / name
    d.mkdir()
    (d / "plugin.py").write_text(source, encoding="utf-8")
    return d


GOOD_SOURCE = textwrap.dedent('''
    from core.plugin import Plugin

    class GoodPlugin(Plugin):
        name = "good"
        def on_activate(self, app):
            pass
''')

BAD_SOURCE = textwrap.dedent('''
    from core.plugin import Plugin

    class BadPlugin(Plugin):
        name = "bad"
        def on_activate(self, app):
            raise RuntimeError("boom")
''')


def test_discover_finds_plugins(tmp_path):
    write_plugin(tmp_path, "a", GOOD_SOURCE)
    host = PluginHost(str(tmp_path))
    host.load_and_activate(app=None)
    assert {i["name"] for i in host.items} == {"good"}


def test_failure_isolated(tmp_path):
    write_plugin(tmp_path, "a", GOOD_SOURCE)
    write_plugin(tmp_path, "b", BAD_SOURCE)
    host = PluginHost(str(tmp_path))
    host.load_and_activate(app=None)
    by_name = {i["name"]: i for i in host.items}
    assert by_name["bad"]["disabled"] is True
    assert "boom" in by_name["bad"]["error"]
    assert by_name["good"]["disabled"] is False


def test_module_missing_plugin_class(tmp_path):
    write_plugin(tmp_path, "a", "import math\n")
    host = PluginHost(str(tmp_path))
    host.load_and_activate(app=None)
    assert host.items == []


def test_filter_ignores_imported_plugin_subclass(tmp_path):
    # 构造一个外部模块，其中定义了一个产自其它模块的 Plugin 子类
    evil = types.ModuleType("_evil_helper")
    class EvilPlugin(Plugin):
        name = "evil"
    evil.EvilPlugin = EvilPlugin
    sys.modules["_evil_helper"] = evil
    try:
        source = textwrap.dedent('''
            from _evil_helper import EvilPlugin
            from core.plugin import Plugin

            class OnlyPlugin(Plugin):
                name = "only"
        ''')
        write_plugin(tmp_path, "a", source)
        host = PluginHost(str(tmp_path))
        host.load_and_activate(app=None)
        # 只应识别本模块定义的 OnlyPlugin，而非导入的 EvilPlugin
        assert {i["name"] for i in host.items} == {"only"}
    finally:
        del sys.modules["_evil_helper"]


def test_deactivate_all(tmp_path):
    write_plugin(tmp_path, "a", GOOD_SOURCE)
    host = PluginHost(str(tmp_path))
    host.load_and_activate(app=None)
    host.deactivate_all()
    assert host.items == []