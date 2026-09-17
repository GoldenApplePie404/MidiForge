import importlib.util
import sys
from pathlib import Path
from typing import List, Union

from core.plugin import Plugin


class PluginHost:
    """扫描插件目录、加载并激活插件；异常隔离，不影响宿主。"""

    def __init__(self, plugins_dir: Union[str, List[str]]):
        if isinstance(plugins_dir, str):
            plugins_dir = [plugins_dir]
        self._dirs = [Path(d) for d in plugins_dir]
        self.items: List[dict] = []  # {"name","instance","error","disabled"}

    def _discover(self) -> List[Path]:
        found = []
        seen = set()
        for d in self._dirs:
            if not d.exists():
                continue
            # 子目录里的 plugin.py
            for p in d.iterdir():
                pp = p / "plugin.py"
                if p.is_dir() and pp.exists() and str(pp) not in seen:
                    seen.add(str(pp))
                    found.append(pp)
            # 当前目录直接有 plugin.py 也支持
            here = d / "plugin.py"
            if here.exists() and str(here) not in seen:
                seen.add(str(here))
                found.append(here)
        return found

    def load_and_activate(self, app) -> None:
        for plugin_py in self._discover():
            item = {"name": plugin_py.parent.name, "instance": None,
                    "error": None, "disabled": False}
            try:
                mod = self._import_plugin(plugin_py)
                inst = next((c for c in mod.__dict__.values()
                             if isinstance(c, type) and issubclass(c, Plugin) and c is not Plugin
                             and getattr(c, "__module__", None) == mod.__name__), None)
                if inst is None:
                    continue  # 未定义插件类：不记录，跳过
                item["name"] = inst.name or item["name"]
                plugin = inst()
                plugin.on_activate(app)
                item["instance"] = plugin
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
                item["disabled"] = True
            self.items.append(item)

    def deactivate_all(self) -> None:
        for item in self.items:
            inst = item["instance"]
            if inst is not None and not item.get("disabled"):
                try:
                    inst.on_deactivate()
                except Exception as exc:
                    print(f"插件 {item['name']} 清理失败: {exc}", file=sys.stderr)
        self.items.clear()

    @staticmethod
    def _import_plugin(path: Path):
        module_name = f"_plugin_{path.parent.name}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载插件模块: {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod