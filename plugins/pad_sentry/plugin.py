"""PadSentry：2×4 打击垫可视化 + 内置 wav 采样发声插件。

监听连续 CC 号范围（如 102-109），踩哪个 pad 哪个格子发光 + 播放对应鼓组 wav。
首次运行自动合成 GM 鼓组采样到 samples/ 目录。
"""

import json
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pygame
from PyQt6.QtCore import QTimer, Qt, pyqtProperty
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget
)

from core.plugin import Plugin
from ui import style_qss as QSS


# ---- 默认配置 ----
DEFAULT_CONFIG = {
    "cc_range": [102, 109],
    "volume": 0.75,
    "enabled": True,
    "pad_pulse_ms": 300,
    "pads": [
        {"cc": 102, "name": "吊镲",     "wav": "pad1_crash.wav"},
        {"cc": 103, "name": "通鼓(左)", "wav": "pad2_tom_left.wav"},
        {"cc": 104, "name": "通鼓(右)", "wav": "pad3_tom_right.wav"},
        {"cc": 105, "name": "节奏镲",   "wav": "pad4_ride.wav"},
        {"cc": 106, "name": "闭镲",     "wav": "pad5_hihat_closed.wav"},
        {"cc": 107, "name": "开镲",     "wav": "pad6_hihat_open.wav"},
        {"cc": 108, "name": "军鼓",     "wav": "pad7_snare.wav"},
        {"cc": 109, "name": "底鼓",     "wav": "pad8_kick.wav"},
    ],
}


def _ensure_valid_wav(path: Path) -> bool:
    """检查 wav 是否能被 pygame 正常加载。不做任何自动转码/覆盖——用户文件神圣不可侵犯。"""
    try:
        with wave.open(str(path), "rb") as w:
            w.getnframes()  # 能打开就是合法 wav
            return True
    except wave.Error:
        pass
    try:
        # 只检查 header 完整性，不修 header
        with open(path, "rb") as f:
            raw = f.read(12)
        if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
            return True  # pygame 能处理非标准 tag
    except OSError:
        pass
    return False


def _ensure_samples(samples_dir: Path) -> None:
    """确保采样目录存在可用的 wav：自动合成缺失文件 + 修复非标准 header。"""
    # 扫描 + 修复已有 wav
    if samples_dir.exists():
        for wav in samples_dir.glob("*.wav"):
            _ensure_valid_wav(wav)

    # 合成缺失文件（不会覆盖已有）
    if not samples_dir.exists() or not any(samples_dir.glob("*.wav")):
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            import _synth
            _synth.generate_all(samples_dir, skip_existing=True)
        except Exception as e:
            print(f"[PadSentry] 合成采样失败: {e}")


class _PadButton(QFrame):
    """单个 pad 的可视化：触发时亮度脉冲，自然衰减。"""

    def __init__(self, label: str, note_name: str):
        super().__init__()
        self.setObjectName("padCell")
        self.setFixedSize(88, 88)
        self._brightness = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._decay)
        self._pulse_ms = 300

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(2)

        self.id_label = QLabel(label)
        self.id_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.id_label.setStyleSheet(f"color: {QSS.TEXT}; font-size: 11px; font-weight: 700;")

        self.name_label = QLabel(note_name)
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name_label.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")

        layout.addWidget(self.id_label)
        layout.addWidget(self.name_label)
        layout.addStretch(1)

    @pyqtProperty(float)
    def brightness(self):
        return self._brightness

    @brightness.setter
    def brightness(self, v):
        self._brightness = v
        self._refresh_style()

    def _refresh_style(self):
        base_bg = QColor(QSS.CARD)
        glow = QColor(QSS.ACCENT)
        r = int(base_bg.red() * (1 - self._brightness) + glow.red() * self._brightness)
        g = int(base_bg.green() * (1 - self._brightness) + glow.green() * self._brightness)
        b = int(base_bg.blue() * (1 - self._brightness) + glow.blue() * self._brightness)
        self.setStyleSheet(
            f"QFrame#padCell {{ border: 1px solid {QSS.BORDER}; "
            f"border-radius: 8px; background-color: rgb({r},{g},{b}); }}"
        )

    def pulse(self, duration_ms: int = 300):
        self._pulse_ms = duration_ms
        self._brightness = 1.0
        self._refresh_style()
        interval = max(8, duration_ms // 20)
        self._timer.start(interval)

    def _decay(self):
        step = 0.05
        self._brightness = max(0.0, self._brightness - step)
        self._refresh_style()
        if self._brightness <= 0:
            self._timer.stop()


class PadSentry(Plugin):
    """2×4 打击垫可视化 + 内置 wav 采样发声。"""

    name = "pad_sentry"
    version = "1.0"
    description = "2x4 打击垫可视化网格，踩 pad 发光 + 内置 GM 鼓组采样发声"

    def on_activate(self, app):
        self.app = app
        self._plugin_dir = Path(__file__).parent
        self._config_path = self._plugin_dir / "config.json"
        self._samples_dir = self._plugin_dir / "samples"

        # 确保采样文件存在 + 修复格式
        _ensure_samples(self._samples_dir)

        self.config = self._load_config()
        self._pad_map = {p["cc"]: p for p in self.config["pads"]}

        # 初始化 pygame mixer（多轨播放，支持同时踩多个 pad）
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
        except pygame.error as e:
            print(f"[PadSentry] pygame.mixer 初始化失败: {e}")

        # 每个 pad 独占 4 条 channel（连打/并发不丢音）
        CHANNELS_PER_PAD = 4
        self._channels_per_pad = CHANNELS_PER_PAD
        total = len(self.config["pads"]) * CHANNELS_PER_PAD
        pygame.mixer.set_num_channels(total)

        # 预加载 Sound 对象 + 为每个 pad 分配 channel 池
        self._sfx = {}            # cc -> pygame.mixer.Sound
        self._pad_channels = {}   # cc -> list[Channel]
        self._pad_chan_idx = {}   # cc -> int（round-robin 索引）
        for i, pad in enumerate(self.config["pads"]):
            wav_path = self._samples_dir / pad["wav"]
            base_ch = i * CHANNELS_PER_PAD
            self._pad_channels[pad["cc"]] = [
                pygame.mixer.Channel(base_ch + j) for j in range(CHANNELS_PER_PAD)
            ]
            self._pad_chan_idx[pad["cc"]] = 0
            if wav_path.exists():
                try:
                    sound = pygame.mixer.Sound(str(wav_path))
                    sound.set_volume(self.config.get("volume", 0.75))
                    self._sfx[pad["cc"]] = sound
                except pygame.error as e:
                    print(f"[PadSentry] 加载 {wav_path.name} 失败: {e}")

        # 订阅 MIDI
        app.subscribe("midi.message", self._on_message)

    def on_deactivate(self):
        for s in self._sfx.values():
            s.stop()
        self._sfx.clear()
        # 不 quit mixer——可能其他 PyQt 部件也在用

    # ---- 消息处理 ----
    def _on_message(self, parsed):
        if parsed.type != "cc":
            return
        cc = parsed.values.get("control")
        val = parsed.values.get("value", 0)
        if cc not in self._pad_map:
            return
        if val <= 0:
            return  # 松开忽略

        pad = self._pad_map[cc]

        # UI 脉冲
        pad_btn = getattr(self, "_pad_btns", {}).get(cc)
        if pad_btn is not None:
            pad_btn.pulse(self.config.get("pad_pulse_ms", 300))

        # 播放采样（每个 pad 独占 4 channel，round-robin 轮换，永不丢音）
        if self.config.get("enabled", True):
            sfx = self._sfx.get(cc)
            if sfx is not None:
                sfx.set_volume(self.config.get("volume", 0.75))
                channels = self._pad_channels[cc]
                idx = self._pad_chan_idx[cc]
                self._pad_chan_idx[cc] = (idx + 1) % len(channels)
                channels[idx].play(sfx)  # channel 正忙则自动打断（打击乐自然 roll 效果）

    # ---- UI ----
    def create_panel(self) -> QWidget:
        box = QWidget()
        box.setStyleSheet(f"background-color: {QSS.BG}; border-radius: 8px;")
        root = QVBoxLayout(box)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # 标题
        title = QLabel("🎛 PadSentry · 指鼓演奏")
        title.setStyleSheet(f"color: {QSS.TEXT}; font-size: 14px; font-weight: 700;")
        root.addWidget(title)

        # CC 范围提示
        rng = self.config.get("cc_range", [0, 0])
        hint = QLabel(f"监听 CC #{rng[0]}~#{rng[1]}（共 {len(self.config['pads'])} 个 pad） · 内置 GM 鼓组")
        hint.setStyleSheet(f"color: {QSS.MUTED}; font-size: 11px;")
        root.addWidget(hint)

        # 2×4 网格
        grid_wrap = QGridLayout()
        grid_wrap.setSpacing(8)
        self._pad_btns = {}
        for i, pad in enumerate(self.config["pads"]):
            btn = _PadButton(f"Pad {i + 1}", pad["name"])
            self._pad_btns[pad["cc"]] = btn
            grid_wrap.addWidget(btn, i // 4, i % 4)

        row_wrap = QWidget()
        row_wrap.setLayout(grid_wrap)
        root.addWidget(row_wrap)

        # 控制条
        ctrl = QHBoxLayout()

        self.enable_cb = QCheckBox("发声")
        self.enable_cb.setChecked(self.config.get("enabled", True))
        self.enable_cb.setStyleSheet(f"color: {QSS.TEXT};")
        self.enable_cb.toggled.connect(self._toggle_sound)
        ctrl.addWidget(self.enable_cb)

        ctrl.addSpacing(16)

        vol_label = QLabel("音量")
        vol_label.setStyleSheet(f"color: {QSS.MUTED}; font-size: 11px;")
        ctrl.addWidget(vol_label)

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(int(self.config.get("volume", 0.75) * 100))
        self.vol_slider.setFixedWidth(120)
        self.vol_slider.valueChanged.connect(self._on_volume)
        ctrl.addWidget(self.vol_slider)

        self.vol_val = QLabel(f"{self.vol_slider.value()}%")
        self.vol_val.setStyleSheet(f"color: {QSS.MUTED}; font-size: 11px;")
        self.vol_val.setFixedWidth(30)
        ctrl.addWidget(self.vol_val)

        ctrl.addStretch(1)

        root.addLayout(ctrl)

        # 采样状态提示
        loaded = len(self._sfx)
        total = len(self.config["pads"])
        status = QLabel(f"采样就绪 {loaded}/{total}")
        status.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        root.addWidget(status)

        return box

    # ---- 控制条回调 ----
    def _toggle_sound(self, on: bool):
        self.config["enabled"] = on
        self._save_config()

    def _on_volume(self, v: int):
        self.config["volume"] = v / 100.0
        self.vol_val.setText(f"{v}%")
        for sfx in self._sfx.values():
            sfx.set_volume(self.config["volume"])
        self._save_config()

    # ---- 配置持久化 ----
    def _load_config(self) -> dict:
        if self._config_path.exists():
            try:
                with open(self._config_path, encoding="utf-8") as f:
                    data = json.load(f)
                merged = {**DEFAULT_CONFIG, **data}
                if "pads" not in data:
                    merged["pads"] = DEFAULT_CONFIG["pads"]
                return merged
            except (json.JSONDecodeError, OSError):
                pass
        self._save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG

    def _save_config(self, data: dict = None):
        cfg = data if data is not None else self.config
        try:
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
