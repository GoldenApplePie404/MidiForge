"""插件管理对话框：列出全部插件，每行一个启用开关，改动即时生效。"""

from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

_STATUS_TEXT = {
    "stable": "稳定",
    "dev": "开发中",
    "deprecated": "已废弃",
}


class PluginManagerDialog(QDialog):
    """插件列表 + 启用开关。

    host: PluginHost；app: 宿主 app（传给 set_enabled）；
    on_changed(name, enabled): 开关成功后回调（宿主刷新对应 tab）。
    """

    def __init__(self, host, app, on_changed, parent=None):
        super().__init__(parent)
        self.setWindowTitle("插件管理")
        self.setMinimumSize(520, 320)
        self._host = host
        self._app = app
        self._on_changed = on_changed
        self._row_of: dict = {}          # name → 行号

        self.table = QTableWidget(len(host.items), 4, self)
        self.table.setHorizontalHeaderLabels(["插件", "版本", "状态", "启用"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, item in enumerate(host.items):
            self._row_of[item["name"]] = row
            self._fill_row(row, item)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(btn_close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addLayout(btn_row)

    def _fill_row(self, row: int, item: dict) -> None:
        err = item.get("error")
        name_item = QTableWidgetItem(item["name"])
        if err:
            name_item.setToolTip(err)
        self.table.setItem(row, 0, name_item)
        self.table.setItem(row, 1, QTableWidgetItem(str(item.get("version", ""))))
        status = _STATUS_TEXT.get(item.get("status", "stable"),
                                  str(item.get("status")))
        if err:
            status = f"加载失败（{err.split(':')[0]}）"
        elif not item.get("enabled"):
            status = "已停用"
        self.table.setItem(row, 2, QTableWidgetItem(status))

        cb = QCheckBox()
        # 先设状态再连信号：_fill_row 重建（回滚/刷新）时不得再触发 _toggle
        cb.blockSignals(True)
        cb.setChecked(bool(item.get("enabled")))
        cb.blockSignals(False)
        if err:
            cb.setEnabled(False)             # 加载失败不可开关
        else:
            cb.toggled.connect(lambda on, it=item: self._toggle(it, on))
        self.table.setCellWidget(row, 3, cb)

    def _toggle(self, item: dict, on: bool) -> None:
        ok, msg = self._host.set_enabled(item["name"], on, self._app)
        row = self._row_of.get(item["name"])
        if ok:
            self._fill_row(row, item)        # 刷新状态列（checkbox 按真实状态）
            self._on_changed(item["name"], on)
            return
        # 失败：按 host 真实状态重建行（开关自动回滚）+ 提示
        self._fill_row(row, item)
        QMessageBox.warning(self, "插件管理", f"{item['name']}: {msg}")
