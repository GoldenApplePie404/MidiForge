"""宿主兼容层：宿主缺 ui.style_qss 或缺个别颜色常量时回退内置色板。

插件要求可整目录拷贝到其他 MidiForge 使用，因此对宿主 UI 模块
只做"能用就用、不能用就内置"的弱依赖，不作为加载前提。
"""
from types import SimpleNamespace

try:
    from ui import style_qss as _qss
except Exception:
    _qss = None

QSS = SimpleNamespace(
    PRIMARY=getattr(_qss, "PRIMARY", "#00E5FF"),
    TEXT=getattr(_qss, "TEXT", "#E6E9F0"),
    MUTED=getattr(_qss, "MUTED", "#7A8499"),
    BORDER=getattr(_qss, "BORDER", "#232D47"),
)
