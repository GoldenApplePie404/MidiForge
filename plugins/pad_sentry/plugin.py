"""PadSentry — 2×4 打击垫可视化 + 采样发声插件。

架构:
    MIDI CC 消息 → EventBus("midi.cc" 细分 topic) → PadSentry._on_cc()
    → app.audio.play() round-robin 播放（宿主统一管 mixer + channel 池）
    → _PadButton.pulse() 发光衰减

v0.3 迁移:
    - pygame.mixer.init()  → 宿主 app.audio.init_if_needed() 统一管
    - 自己分 channel 池    → app.audio.allocate_pool("pad_sentry", 32)
    - Sound.play() 丢音    → app.audio.play(sfx, plugin_name="pad_sentry")
    - print()              → app.log.info("加载 %s", name)
    - 订阅 "midi.message"  → 订阅 "midi.cc"（宿主按 type 过滤）
"""

import json
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
# 仅在 config.json 不存在时用作兜底；用户改 wav 文件名/CC 号都在 config.json 里。
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
    """检查 wav 是否能被 pygame 正常加载。

    注意：**不做任何自动转码/覆盖**。用户采样文件神圣不可侵犯，
    FL Studio Edison 导出的 Ogg-in-WAV 问题由用户自己在外部脚本处理。
    """
    try:
        with wave.open(str(path), "rb") as w:
            w.getnframes()
            return True
    except wave.Error:
        pass
    try:
        # 退一步：header 是合法 RIFF/WAVE 就算通过，
        # pygame 对部分非标准 format tag（如 0x674F）有一定容忍度
        with open(path, "rb") as f:
            raw = f.read(12)
        if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
            return True
    except OSError:
        pass
    return False


def _ensure_samples(samples_dir: Path) -> None:
    """确保 samples/ 目录下至少有可用 wav。

    已有文件不动（skip_existing=True），只在目录为空时调 _synth 合成 GM 鼓组。
    """
    if samples_dir.exists():
        for wav in samples_dir.glob("*.wav"):
            _ensure_valid_wav(wav)

    if not samples_dir.exists() or not any(samples_dir.glob("*.wav")):
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            import _synth
            _synth.generate_all(samples_dir, skip_existing=True)
        except Exception as e:
            # 此时还没 on_activate，无法用 app.log——保留 print 兜底
            print(f"[PadSentry] 合成采样失败: {e}")


class _PadButton(QFrame):
    """单个 pad 的可视化格子：踩下去发光（亮度=1.0），自然衰减到 0。

    用 `pyqtProperty` 让 `brightness` 可以在 QSS 里被动态设置，
    这里用纯 Python 插值 base_color → glow_color，避免 QSS 重新 parse 的开销。
    """

    def __init__(self, label: str, note_name: str):
        super().__init__()
        self.setObjectName("padCell")
        self.setFixedSize(88, 88)
        self._brightness = 0.0
        # 衰减定时器：interval = duration_ms / 20，每次 step -= 0.05，
        # 所以 300ms 脉光大 约 20 步 × 15ms/步 = 300ms 归零
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
        # 线性插值 base ↔ glow，brightness=1 时完全变成 glow 色
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
        # 20 步衰减，interval 至少 8ms（Qt 定时器下限）
        interval = max(8, duration_ms // 20)
        self._timer.start(interval)

    def _decay(self):
        step = 0.05
        self._brightness = max(0.0, self._brightness - step)
        self._refresh_style()
        if self._brightness <= 0:
            self._timer.stop()


class PadSentry(Plugin):
    """2×4 打击垫插件主类。

    音频播放架构（关键！）:
        pygame.mixer.set_num_channels(32)   # 8 pad × 4 独占 channel
        每个 pad 分配 [base+0, base+1, base+2, base+3] 四条 channel
        播放时 round-robin 选下一条：channel 空闲就立即播，忙则打断最老的。

        为什么不用 Sound.play()？
          Sound.play() 从**全局池**找空闲 channel，找不到 → 静默返回 None → 丢音。
          Channel(idx).play() 强制指定 channel，永远不返回 None。
        为什么每个 pad 4 条？
          极端场景：Crash（1.28s 长音）连打，4 条够塞下 4 层叠加，
          第 5 次触发会打断第 1 层（打击乐自然 roll 效果，人耳听不出违和）。
    """

    name = "pad_sentry"
    version = "1.0"
    description = "2x4 打击垫可视化网格，踩 pad 发光 + 内置 GM 鼓组采样发声"

    def on_activate(self, app):
        self.app = app
        self.log = app.log
        self._plugin_dir = Path(__file__).parent
        self._config_path = self._plugin_dir / "config.json"
        self._samples_dir = self._plugin_dir / "samples"

        # 确保采样文件存在（首次运行自动合成；已有则跳过）
        _ensure_samples(self._samples_dir)

        self.config = self._load_config()
        self._pad_map = {p["cc"]: p for p in self.config["pads"]}

        # ---- 用宿主统一音频服务 ----
        # 宿主统一管理 pygame.mixer.init()，多插件共存不会冲突。
        CHANNELS_PER_PAD = 4
        self._channels_per_pad = CHANNELS_PER_PAD
        total = len(self.config["pads"]) * CHANNELS_PER_PAD

        audio = app.audio
        audio.init_if_needed()                                 # 宿主统一 init
        audio.allocate_pool("pad_sentry", channel_count=total)  # 独占池

        # 预加载 sfx 到宿主音频服务（全局音量自动生效）
        self._sfx = {}  # cc → _SfxHandle
        for pad in self.config["pads"]:
            wav_path = self._samples_dir / pad["wav"]
            if wav_path.exists():
                sfx = audio.load(str(wav_path))
                if sfx is not None:
                    self._sfx[pad["cc"]] = sfx
                    self.log.info("loaded %s (%.2fs)", wav_path.name, sfx.duration)
                else:
                    self.log.warning("load failed: %s", wav_path.name)

        # ---- 订阅细分 topic（宿主已按 type 过滤，直接拿到 cc 类型消息）----
        app.subscribe("midi.cc", self._on_cc)

    def on_deactivate(self):
        for sfx in self._sfx.values():
            sfx.stop()
        self._sfx.clear()
        if self.app:
            self.app.audio.release_pool("pad_sentry")

    # ---- 消息处理 ----
    def _on_cc(self, parsed):
        """EventBus 回调：宿主已按 topic=midi.cc 过滤，进来的都是 CC 消息。

        只响应 value > 0（松开忽略）+ cc 号在 pad_map 范围内的。
        """
        cc = parsed.values.get("control")
        val = parsed.values.get("value", 0)
        if cc not in self._pad_map:
            return
        if val <= 0:
            return

        pad = self._pad_map[cc]

        # UI：找到对应的 _PadButton 做脉冲发光
        pad_btn = getattr(self, "_pad_btns", {}).get(cc)
        if pad_btn is not None:
            pad_btn.pulse(self.config.get("pad_pulse_ms", 300))

        # 音频：交给宿主统一服务 round-robin 播放
        if self.config.get("enabled", True):
            sfx = self._sfx.get(cc)
            if sfx is not None:
                volume = self.config.get("volume", 0.75)
                self.app.audio.play(
                    sfx, plugin_name="pad_sentry", volume=volume
                )

    # ---- UI ----
    def create_panel(self) -> QWidget:
        """插件面板：2×4 网格 + 发声开关 + 音量滑块 + 采样就绪状态。"""
        box = QWidget()
        box.setStyleSheet(f"background-color: {QSS.BG}; border-radius: 8px;")
        root = QVBoxLayout(box)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        title = QLabel("🎛 PadSentry · 指鼓演奏")
        title.setStyleSheet(f"color: {QSS.TEXT}; font-size: 14px; font-weight: 700;")
        root.addWidget(title)

        rng = self.config.get("cc_range", [0, 0])
        hint = QLabel(f"监听 CC #{rng[0]}~#{rng[1]}（共 {len(self.config['pads'])} 个 pad） · 内置 GM 鼓组")
        hint.setStyleSheet(f"color: {QSS.MUTED}; font-size: 11px;")
        root.addWidget(hint)

        # 2×4 网格（i // 4 = 行, i % 4 = 列）
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

        # 控制条：发声开关 + 音量滑块
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

        # 采样就绪提示（loaded/total），能直观看到哪些 wav 缺失
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
        # 所有已加载的 Sound 统一调音量
        for sfx in self._sfx.values():
            sfx.set_volume(self.config["volume"])
        self._save_config()

    # ---- 配置持久化 ----
    def _load_config(self) -> dict:
        """读 config.json；不存在或损坏则用 DEFAULT_CONFIG 写一份。"""
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
