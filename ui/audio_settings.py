"""音频设置对话框：输出设备 / 驱动后端 / 采样率 / 缓冲区，应用即重启 mixer。"""

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout,
)


class AudioSettingsDialog(QDialog):
    """音频输出设置。

    app: AppContext（用 app.audio.restart + app.save_audio_config）。
    「应用」成功后落盘 config/audio.json 并广播 audio.restarted（重启链内部），
    插件自行清缓存重载采样。
    """

    DRIVERS = [("自动（推荐）", None), ("WASAPI", "wasapi"),
               ("WinMM", "winmm"), ("DirectSound", "directsound")]
    SAMPLE_RATES = [("44100 Hz", 44100), ("48000 Hz", 48000)]
    BUFFERS = [("256（低延迟，要求高）", 256), ("512", 512),
               ("1024（默认）", 1024), ("2048（稳定，延迟高）", 2048)]

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.setWindowTitle("音频设置")
        self.setMinimumWidth(420)
        self._app = app
        cur = app.audio.current_config()

        form = QFormLayout()

        # ---- 输出设备 ----
        self._device = QComboBox()
        self._device.addItem("系统默认", None)
        devices = app.audio.list_output_devices()
        for name in devices:
            self._device.addItem(name, name)
        if cur.get("device"):
            idx = self._device.findData(cur["device"])
            if idx >= 0:
                self._device.setCurrentIndex(idx)
            else:
                # 配置里的设备当前不在线：置顶显示并标记
                self._device.insertItem(1, f"{cur['device']}（当前不在线）", cur["device"])
                self._device.setCurrentIndex(1)
        elif devices:
            self._device.setToolTip("\n".join(devices))
        form.addRow("输出设备", self._device)

        # ---- 驱动 ----
        self._driver = QComboBox()
        for label, val in self.DRIVERS:
            self._driver.addItem(label, val)
        for i, (_, val) in enumerate(self.DRIVERS):
            if val == cur.get("driver"):
                self._driver.setCurrentIndex(i)
                break
        form.addRow("驱动后端", self._driver)

        # ---- 采样率 / 缓冲区 ----
        self._rate = QComboBox()
        for label, val in self.SAMPLE_RATES:
            self._rate.addItem(label, val)
        self._rate.setCurrentIndex(max(0, self._rate.findData(
            cur.get("sample_rate") or 44100)))
        form.addRow("采样率", self._rate)

        self._buffer = QComboBox()
        for label, val in self.BUFFERS:
            self._buffer.addItem(label, val)
        self._buffer.setCurrentIndex(max(0, self._buffer.findData(
            cur.get("buffer") or 1024)))
        form.addRow("缓冲区", self._buffer)

        hint = QLabel("切换会重建音频引擎，已加载的采样将自动重新加载。\n"
                      "配置保存在 config/audio.json，下次启动直接生效。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")

        btn_apply = QPushButton("应用并重启音频")
        btn_apply.clicked.connect(self._apply)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addWidget(btn_apply)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_close)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addLayout(btn_row)

    def _apply(self) -> None:
        overrides = {
            "device": self._device.currentData(),
            "driver": self._driver.currentData(),
            "sample_rate": self._rate.currentData(),
            "buffer": self._buffer.currentData(),
        }
        self.setEnabled(False)
        try:
            ok, err = self._app.audio.restart(**overrides)
        finally:
            self.setEnabled(True)
        if ok:
            self._app.save_audio_config(self._app.audio.current_config())
            self._app.log.info("音频设置已应用: %s", overrides)
            win = self.parent()
            if hasattr(win, "statusBar"):
                win.statusBar().showMessage("音频设置已生效", 4000)
            self.accept()
            return
        # 失败不落盘（audio.restart 已按旧参数回退，保持可用）
        QMessageBox.warning(
            self, "音频设置",
            f"切换失败，已回退到原设置。\n\n原因：{err or '未知'}\n\n"
            "可尝试更换驱动后端（WinMM 兼容性最好）。")
