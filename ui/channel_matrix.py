from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QFrame, QGridLayout, QLabel

from ui import style_qss as QSS


class ChannelMatrix(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        grid = QGridLayout(self)
        self._cells = []
        self._timers: dict = {}
        for ch in range(16):
            cell = QLabel(f"CH{ch + 1}")
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setMinimumSize(44, 28)
            cell.setStyleSheet(self._dull_style())
            grid.addWidget(cell, ch // 4, ch % 4)
            self._cells.append(cell)

    @staticmethod
    def _dull_style() -> str:
        return f"background-color: {QSS.PANEL}; color: {QSS.MUTED}; border-radius:5px; border:1px solid {QSS.BORDER};"

    @staticmethod
    def _lit_style() -> str:
        return (f"background-color: {QSS.PRIMARY}; color: {QSS.BG}; font-weight:600;"
                f"border-radius:5px; border:1px solid {QSS.PRIMARY};")

    def pulse(self, channel: int) -> None:
        if not 0 <= channel < 16:
            return
        cell = self._cells[channel]
        cell.setStyleSheet(self._lit_style())
        if channel in self._timers:
            self._timers[channel].stop()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._dim(channel))
        self._timers[channel] = timer
        timer.start(500)

    def _dim(self, channel: int) -> None:
        self._timers.pop(channel, None)
        self._cells[channel].setStyleSheet(self._dull_style())