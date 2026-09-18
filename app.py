import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

import api
from ui import style_qss as QSS
from ui.main_window import MainWindow

BINDINGS_PATH = Path(__file__).parent / "config" / "bindings.json"
PLUGIN_DIRS = [
    Path(__file__).parent / "plugins",
    Path(__file__).parent / "examples",  # 内置示例插件
]


def create_binding_config_path(path: Path) -> None:
    """确保默认绑定配置文件存在（空配置）。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"version": 1, "bindings": []}', encoding="utf-8")


def main() -> int:
    create_binding_config_path(BINDINGS_PATH)
    app = api.create_app(bindings_path=str(BINDINGS_PATH))
    # QtWebEngine 需要这个 flag 在 QApplication 之前设置
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    qapp = QApplication(sys.argv)
    qapp.setStyleSheet(QSS.GLOBAL_QSS)
    win = MainWindow(app=app, plugins_dirs=[str(p) for p in PLUGIN_DIRS])
    win.show()
    return qapp.exec()


if __name__ == "__main__":
    raise SystemExit(main())