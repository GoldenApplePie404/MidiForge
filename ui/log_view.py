from typing import List, Optional, Set

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QHeaderView,
                             QLabel, QMenu, QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout)

from midi.parser import ParsedMessage
from ui import style_qss as QSS

# 消息类型中文名（用于下拉和日志）
_TYPE_LABELS = {
    "": "全部类型",
    "note_on": "音符开",
    "note_off": "音符关",
    "cc": "控制改变",
    "pitch_bend": "弯音",
    "program_change": "音色切换",
    "aftertouch": "通道触后",
    "poly_aftertouch": "复音触后",
    "system": "系统消息",
}


class LogView(QFrame):
    COLUMNS = ("时间", "来源", "通道", "类型", "数值", "原始 hex", "别名/描述")
    KEYS = ("time", "source", "channel", "type", "value", "hex", "alias")

    # 外部可连接：请求基于某条 MIDI 消息创建绑定
    request_create_binding = pyqtSignal(object)  # ParsedMessage

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self._rows: List[dict] = []
        self._type_filter: Optional[str] = None     # None = 不过滤
        self._channel_filter: Optional[int] = None   # None = 不过滤

        root = QVBoxLayout(self)

        # 顶部栏：标题 + 过滤控件 + 清空
        bar = QHBoxLayout()
        self._title = QLabel("消息日志")
        self._title.setObjectName("sectionTitle")

        # 类型过滤
        self._type_combo = QComboBox()
        for t, label in _TYPE_LABELS.items():
            self._type_combo.addItem(label, t)
        self._type_combo.setMinimumWidth(90)
        self._type_combo.currentIndexChanged.connect(self._on_filter_changed)

        # 通道过滤
        self._channel_combo = QComboBox()
        self._channel_combo.addItem("全部通道", None)
        for ch in range(1, 17):
            self._channel_combo.addItem(f"CH{ch}", ch - 1)  # MIDI 1-16 → 内部 0-15
        self._channel_combo.setMinimumWidth(70)
        self._channel_combo.currentIndexChanged.connect(self._on_filter_changed)

        self.clear_btn = QPushButton("清空")

        bar.addWidget(self._title)
        bar.addSpacing(16)
        bar.addWidget(QLabel("类型:"))
        bar.addWidget(self._type_combo)
        bar.addWidget(QLabel("通道:"))
        bar.addWidget(self._channel_combo)
        bar.addStretch(1)
        bar.addWidget(self.clear_btn)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        root.addWidget(self.table)
        self.clear_btn.clicked.connect(self.clear_log)

    # ---- 过滤 ----
    def _on_filter_changed(self) -> None:
        self._type_filter = self._type_combo.currentData() or None
        ch = self._channel_combo.currentData()
        self._channel_filter = ch if ch is not None else None
        self._rebuild()

    # ---- 数据 ----
    def append_message(self, parsed: ParsedMessage, source: str = "in", alias: Optional[str] = None) -> None:
        value = _format_values(parsed)
        row = {
            "time": _now_str(),
            "source": source,
            "channel": str(parsed.channel) if parsed.channel is not None else "-",
            "_channel_raw": parsed.channel,  # 用于通道过滤（内部 0-15）
            "type": parsed.type,
            "value": value,
            "hex": parsed.raw_hex,
            "alias": alias or parsed.description,
            "_parsed": parsed,  # 原始对象，供右键创建绑定
        }
        self._rows.append(row)
        if not self._passes_filter(row):
            return
        self._add_table_row(row)

    def _passes_filter(self, row: dict) -> bool:
        if self._type_filter and row["type"] != self._type_filter:
            return False
        if self._channel_filter is not None:
            if row["_channel_raw"] != self._channel_filter:
                return False
        return True

    def clear_log(self) -> None:
        self._rows.clear()
        self.table.setRowCount(0)

    def _add_table_row(self, row: dict) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for i, key in enumerate(self.KEYS):
            item = QTableWidgetItem(row[key])
            if key == "channel":
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(r, i, item)

    def _rebuild(self) -> None:
        self.table.setRowCount(0)
        for row in self._rows:
            if not self._passes_filter(row):
                continue
            self._add_table_row(row)

    # ---- 右键菜单 ----
    def _show_context_menu(self, pos) -> None:
        table_row = self.table.rowAt(pos.y())
        if table_row < 0:
            return
        # _rows 和 table 行数因为过滤可能不一致，从 table 找对应的原始 row
        # 简单方式：当前行显示的数据和 _rows 里的 dict 有对应的 _parsed
        # 用 item 的数据反查
        item_time = self.table.item(table_row, 0).text()
        item_type = self.table.item(table_row, 3).text()
        row = next((r for r in self._rows if r["time"] == item_time and r["type"] == item_type), None)
        if row is None or "_parsed" not in row:
            return
        parsed: ParsedMessage = row["_parsed"]

        menu = QMenu(self)
        label = f"{parsed.type} ch{parsed.channel if parsed.channel is not None else '*'}"
        act_bind = QAction(f"基于此消息创建绑定 ({label})", self)
        act_bind.triggered.connect(lambda: self.request_create_binding.emit(parsed))
        menu.addAction(act_bind)
        menu.exec(self.table.viewport().mapToGlobal(pos))


def _now_str() -> str:
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _format_values(parsed: ParsedMessage) -> str:
    excluded = {"velocity"} if parsed.type in ("note_on", "note_off") else set()
    parts = [f"{k}={v}" for k, v in parsed.values.items() if k not in excluded]
    return " ".join(parts)