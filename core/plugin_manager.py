"""插件管理器 — 发现、加载、查询插件。

=== 和 core/plugin_loader.py 的关系 ===
    plugin_loader.py 是底层的"扫描目录 + import"。
    plugin_manager.py 在它之上提供:
      - 生命周期管理（on_init 一次 vs on_activate 每次）
      - 插件注册表（其他插件能 query 已加载的插件）
      - 错误隔离（插件崩了自动标记 disabled）
"""

import importlib
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.plugin import Plugin


@dataclass
class _PluginEntry:
    name: str
    module_name: str       # import 名 (e.g. plugins.pad_sentry.plugin)
    cls: Optional[type] = None   # Plugin 子类（加载后才有）
    instance: Optional[Plugin] = None
    enabled: bool = True
    error: Optional[str] = None    # 最近一次错误
    init_called: bool = False      # on_init 是否调过


class PluginManager:
    """插件注册表 + 生命周期管理。"""

    def __init__(self, plugin_dirs: List[Path], app=None):
        self._dirs = plugin_dirs
        self._app = app
        self._entries: Dict[str, _PluginEntry] = {}
        self._scan()

    # ---- 扫描 ----

    def _scan(self) -> None:
        for d in self._dirs:
            if not d.exists():
                continue
            for subdir in d.iterdir():
                if not subdir.is_dir():
                    continue
                plugin_file = subdir / "plugin.py"
                if plugin_file.exists():
                    name = subdir.name
                    module_name = f"{subdir.parent.name}.{subdir.name}.plugin"
                    self._entries[name] = _PluginEntry(name=name, module_name=module_name)

    # ---- 导入 ----

    def load(self, name: str) -> Optional[Plugin]:
        entry = self._entries.get(name)
        if not entry or not entry.enabled:
            return None
        try:
            mod = importlib.import_module(entry.module_name)
            # 找 Plugin 子类
            cls = None
            for attr in dir(mod):
                obj = getattr(mod, attr)
                if isinstance(obj, type) and issubclass(obj, Plugin) and obj is not Plugin:
                    cls = obj
                    break
            if cls is None:
                raise RuntimeError("未找到 Plugin 子类")
            entry.cls = cls
            entry.instance = cls()

            # on_init — 第一次 load 时调一次
            if not entry.init_called:
                init = getattr(entry.instance, "on_init", None)
                if callable(init):
                    init(self._app)
                entry.init_called = True

            entry.error = None
            return entry.instance
        except Exception as e:
            entry.enabled = False
            entry.error = f"{e}\n{traceback.format_exc()}"
            return None

    def activate(self, name: str) -> bool:
        """加载（如果没加载过）+ on_activate。"""
        inst = self.load(name)
        if inst is None:
            return False
        try:
            inst.on_activate(self._app)
            return True
        except Exception as e:
            self._entries[name].error = str(e)
            return False

    def deactivate(self, name: str) -> bool:
        entry = self._entries.get(name)
        if not entry or not entry.instance:
            return False
        try:
            entry.instance.on_deactivate()
            return True
        except Exception as e:
            entry.error = str(e)
            return False

    def activate_all(self) -> Dict[str, bool]:
        return {name: self.activate(name) for name in self._entries}

    def deactivate_all(self) -> None:
        for entry in self._entries.values():
            if entry.instance:
                try:
                    entry.instance.on_deactivate()
                except Exception:
                    pass

    # ---- 查询 ----

    def list(self) -> List[_PluginEntry]:
        """返回所有发现的插件（含 disabled 的）。"""
        return list(self._entries.values())

    def get(self, name: str) -> Optional[Plugin]:
        entry = self._entries.get(name)
        return entry.instance if entry else None

    def is_enabled(self, name: str) -> bool:
        return self._entries.get(name, _PluginEntry(name="x", module_name="x", enabled=False)).enabled

    def enable(self, name: str) -> None:
        entry = self._entries.get(name)
        if entry:
            entry.enabled = True
            entry.error = None

    def disable(self, name: str) -> None:
        entry = self._entries.get(name)
        if entry:
            entry.enabled = False

    # ---- 插件间协作 ----

    def find_by_name(self, name: str) -> Optional[Plugin]:
        """其他插件可以通过名字查询已加载的插件。"""
        return self.get(name)

    def get_all_instances(self) -> List[Plugin]:
        """返回所有已加载的插件实例。"""
        return [e.instance for e in self._entries.values() if e.instance]
