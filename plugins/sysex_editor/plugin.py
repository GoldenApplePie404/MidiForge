"""SysEx 抓包器 — 只读查看设备私有消息（接收 + 分类 + 复制）。

设计定位：调试阶段让开发者"听"设备在发什么。发送功能已由平台
api.send_sysex() 安全网关接管，开发者如需主动发 SysEx 应写自己的
脚本（或等待后续新增专用发送插件）。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.plugin import Plugin
from midi import sysex as L
from ui import style_qss as QSS


DEFAULT_CONFIG = {
    "capture_max": 500,
}


def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class SysExEditorPlugin(Plugin):
    name = "sysex_editor"
    version = "0.2"
    status = "dev"

    def __init__(self):
        super().__init__()
        self.app = None
        self.log = None
        self._config = json.loads(json.dumps(DEFAULT_CONFIG))
        self._panel = None
        self._capture_paused = False

    # ============ 生命周期 ============

    def on_activate(self, app) -> None:
        self.app = app
        self.log = app.log
        self._load_config()
        app.subscribe("midi.all", self._on_midi_all)

    def on_deactivate(self) -> None:
        pass

    # ============ 配置 ============

    def _config_path(self) -> Path:
        d = self.app.data_dir(self.name) if self.app else Path("plugins") / self.name / "data"
        return d / "config.json"

    def _load_config(self) -> None:
        path = self._config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._config = _deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), data)
            except Exception as exc:
                self.log.warning("config.json 解析失败，用默认配置: %s", exc)
        else:
            self._save_config()

    def _save_config(self) -> None:
        try:
            self._config_path().parent.mkdir(parents=True, exist_ok=True)
            self._config_path().write_text(
                json.dumps(self._config, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.log.warning("保存 config.json 失败: %s", exc)

    # ============ 订阅回调 ============

    def _on_midi_all(self, parsed) -> None:
        if self._panel is None or self._capture_paused:
            return
        if parsed.type != "system" or parsed.values.get("raw_type") != "sysex":
            return
        if parsed.source == "virtual":
            return
        if parsed.raw_hex:
            self._append_capture(parsed)

    # ============ UI ============

    def create_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)

        title = QLabel("SysEx 抓包器 — 查看设备私有消息（只读）")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # ---- 抓包区 ----
        capture_frame = QFrame()
        capture_frame.setObjectName("panel")
        cap_row = QVBoxLayout(capture_frame)

        cap_bar = QHBoxLayout()
        cap_title = QLabel("接收抓包")
        cap_title.setObjectName("sectionTitle")
        self._chk_pause = QCheckBox("暂停")
        self._chk_pause.toggled.connect(lambda v: setattr(self, "_capture_paused", v))
        btn_copy = QPushButton("复制 hex")
        btn_copy.clicked.connect(self._on_copy_selected)
        btn_save = QPushButton("导出 CSV")
        btn_save.clicked.connect(self._on_export_csv)
        btn_clear_cap = QPushButton("清空")
        btn_clear_cap.clicked.connect(self._on_clear_capture)
        cap_bar.addWidget(cap_title)
        cap_bar.addStretch(1)
        cap_bar.addWidget(self._chk_pause)
        cap_bar.addWidget(btn_copy)
        cap_bar.addWidget(btn_save)
        cap_bar.addWidget(btn_clear_cap)
        cap_row.addLayout(cap_bar)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["时间", "摘要", "风险", "字节数", "hex"])
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(0, self._table.horizontalHeader().ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, self._table.horizontalHeader().ResizeMode.Stretch)
        self._table.doubleClicked.connect(lambda _i: self._on_copy_selected())
        cap_row.addWidget(self._table)

        layout.addWidget(capture_frame, 1)

        root.setStyleSheet(
            f'QLabel#sectionTitle {{ color: {QSS.PRIMARY}; font-size: 13px; font-weight: 600; }}'
            f'QLabel {{ color: {QSS.TEXT}; }}'
        )

        self._panel = root
        return root

    # ============ 接收抓包 ============

    def _append_capture(self, parsed) -> None:
        max_rows = self._config.get("capture_max", 500)
        if self._table.rowCount() >= max_rows:
            self._table.removeRow(0)

        try:
            p = [int(x, 16) for x in parsed.raw_hex.split()]
            core = p[:]
            if core and core[0] == 0xF0:
                core = core[1:]
            if core and core[-1] == 0xF7:
                core = core[:-1]
            level, reason, _ = L.classify(core)
            summary = L.summarize(core)
        except Exception:
            core, level, reason, summary = [], L.RISK_OK, "解析失败", parsed.raw_hex

        colors = {L.RISK_OK: QSS.PRIMARY, L.RISK_WARN: "#FFB300", L.RISK_HIGH: QSS.ERROR}
        r = self._table.rowCount()
        self._table.insertRow(r)
        values = [_now_str(), summary, L.RANK_TEXT.get(level, "?"), str(len(p)), parsed.raw_hex]
        mono_font = QFont("Consolas")
        mono_font.setPointSize(9)
        for i, v in enumerate(values):
            item = QTableWidgetItem(str(v))
            if i == 2:
                item.setForeground(QColor(colors.get(level, QSS.PRIMARY)))
            if i == 4:
                item.setFont(mono_font)
            self._table.setItem(r, i, item)
        self._table.item(r, 0).setData(Qt.ItemDataRole.UserRole, core)
        self._table.scrollToBottom()

    def _selected_core(self):
        row = self._table.currentRow()
        if row < 0:
            return None
        it = self._table.item(row, 0)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _on_copy_selected(self) -> None:
        core = self._selected_core()
        if core is None:
            return
        try:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(L.bytes_to_hex_str(core, wrap_f0f7=True))
        except Exception:
            pass

    def _on_export_csv(self) -> None:
        """导出全部抓包为 CSV。"""
        try:
            from PyQt6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getSaveFileName(
                self._panel, "导出 SysEx 抓包",
                f"sysex_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                "CSV 文件 (*.csv)")
            if not path:
                return
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write("时间,摘要,风险,字节数,hex\n")
                for r in range(self._table.rowCount()):
                    row_vals = [self._table.item(r, c).text() for c in range(5)]
                    f.write(",".join(row_vals) + "\n")
            self.log.info("SysEx 抓包导出: %s (%d 条)", path, self._table.rowCount())
        except Exception as exc:
            self.log.warning("导出 CSV 失败: %s", exc)

    def _on_clear_capture(self) -> None:
        self._table.setRowCount(0)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
