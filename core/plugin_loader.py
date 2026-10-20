import importlib.util
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple, Union

from core.plugin import Plugin


class PluginHost:
    """扫描插件目录、加载并激活插件；异常隔离，不影响宿主。

    插件可插拔：item["enabled"] 表示当前是否激活。
    启用 = 实例化 + on_activate；停用 = on_deactivate + 丢弃实例。
    每次启用都重建实例——现有插件的 on_deactivate 不退订总线，
    复用旧实例二次 on_activate 会重复订阅导致消息重复分发。
    禁用列表持久化在 state_path（json），新插件默认启用。
    """

    def __init__(self, plugins_dir: Union[str, List[str]],
                 state_path: Optional[Union[str, Path]] = None):
        if isinstance(plugins_dir, str):
            plugins_dir = [plugins_dir]
        self._dirs = [Path(d) for d in plugins_dir]
        self.state_path = Path(state_path) if state_path else None
        self.items: List[dict] = []  # {"name","instance","error","disabled","enabled","version","status","_cls"}
        self._disabled_state: set = set()

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
        self._load_state()
        for plugin_py in self._discover():
            item = {"name": plugin_py.parent.name, "instance": None,
                    "error": None, "disabled": False, "enabled": False,
                    "version": "0.1", "status": "stable", "_cls": None}
            try:
                mod = self._import_plugin(plugin_py)
                cls = next((c for c in mod.__dict__.values()
                            if isinstance(c, type) and issubclass(c, Plugin) and c is not Plugin
                            and getattr(c, "__module__", None) == mod.__name__), None)
                if cls is None:
                    continue  # 未定义插件类：不记录，跳过
                item["name"] = cls.name or item["name"]
                item["version"] = getattr(cls, "version", "0.1")
                item["status"] = getattr(cls, "status", "stable")
                item["_cls"] = cls
                if item["name"] in self._disabled_state:
                    pass  # 禁用：不实例化、不激活，保留类引用供启用
                else:
                    plugin = cls()
                    plugin.on_activate(app)
                    item["instance"] = plugin
                    item["enabled"] = True
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
                item["disabled"] = True
            self.items.append(item)

    def set_enabled(self, name: str, enabled: bool, app) -> Tuple[bool, str]:
        """动态启停插件（即时生效）。返回 (成功与否, 消息)。"""
        item = next((i for i in self.items if i["name"] == name), None)
        if item is None:
            return False, "未找到插件"
        if item.get("error"):
            return False, f"插件加载失败，无法启用: {item['error']}"
        if enabled == bool(item.get("enabled")):
            return True, "已启用" if enabled else "已停用"   # 幂等：状态没变不动
        if enabled:
            try:
                self._deactivate_item(item)     # 幂等：已激活先干净停用
                plugin = item["_cls"]()
                plugin.on_activate(app)
                item["instance"] = plugin
                item["enabled"] = True
            except Exception as exc:
                item["instance"] = None
                item["enabled"] = False
                item["error"] = f"{type(exc).__name__}: {exc}"
                self._save_state()
                return False, f"启用失败: {item['error']}"
        else:
            try:
                self._deactivate_item(item)
            except Exception as exc:
                return False, f"停用失败: {exc}"
        if item["enabled"]:
            self._disabled_state.discard(name)
        else:
            self._disabled_state.add(name)
        self._save_state()
        return True, "已启用" if enabled else "已停用"

    def _deactivate_item(self, item: dict) -> None:
        inst = item.get("instance")
        if inst is not None:
            try:
                inst.on_deactivate()
            except Exception as exc:
                print(f"插件 {item['name']} 清理失败: {exc}", file=sys.stderr)
        item["instance"] = None
        item["enabled"] = False

    def deactivate_all(self) -> None:
        for item in self.items:
            self._deactivate_item(item)
        self.items.clear()

    # ---- 禁用列表持久化 ----

    def _load_state(self) -> None:
        self._disabled_state = set()
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            disabled = data.get("disabled", [])
            if isinstance(disabled, list):
                self._disabled_state = {str(x) for x in disabled}
        except Exception:
            pass  # 文件损坏当空处理

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"disabled": sorted(self._disabled_state)},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _import_plugin(path: Path):
        module_name = f"_plugin_{path.parent.name}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载插件模块: {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
