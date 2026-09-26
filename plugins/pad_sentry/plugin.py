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
    QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QMenu, QMessageBox, QSlider, QVBoxLayout, QWidget
)

from core.plugin import Plugin
from ui import style_qss as QSS


# ---- 默认配置 ----
# 仅在 config.json 不存在时用作兜底；用户改 wav 文件名/CC 号都在 config.json 里。
DEFAULT_CONFIG = {
    "cc_range": [102, 109],
    "volume": 0.75,
    "enabled": True,
    # pedal_kick: 是否让备选 CC（延音踏板 CC64）也打底鼓。
    # 弹钢琴时关掉 → 踏板只做延音，不会误触发底鼓。
    "pedal_kick": True,
    "pad_pulse_ms": 300,
    # pads[].alt_cc（可选）：备选触发 CC。典型用法是延音踏板（CC64）——
    # 踩下发 127、松开发 0，配合 _on_cc 里的 val<=0 过滤即为"踩一下响一下"。
    # pads[].volume（可选，0.0~1.0）：该 pad 的独立音量，与总音量相乘，默认 1.0。
    # pads[].custom_wav（可选）：用户自选采样的原路径，存在时优先于内置 wav。
    "pads": [
        {"cc": 102, "name": "吊镲",     "wav": "pad1_crash.wav"},
        {"cc": 103, "name": "通鼓(左)", "wav": "pad2_tom_left.wav"},
        {"cc": 104, "name": "通鼓(右)", "wav": "pad3_tom_right.wav"},
        {"cc": 105, "name": "节奏镲",   "wav": "pad4_ride.wav"},
        {"cc": 106, "name": "闭镲",     "wav": "pad5_hihat_closed.wav"},
        {"cc": 107, "name": "开镲",     "wav": "pad6_hihat_open.wav"},
        {"cc": 108, "name": "军鼓",     "wav": "pad7_snare.wav"},
        {"cc": 109, "name": "底鼓",     "wav": "pad8_kick.wav", "alt_cc": 64},
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

        # 独立音量标记：100% 时留空，只有调过才显示，避免视觉噪音
        self.vol_label = QLabel("")
        self.vol_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.vol_label.setStyleSheet(f"color: {QSS.ACCENT}; font-size: 9px;")

        layout.addWidget(self.id_label)
        layout.addWidget(self.name_label)
        layout.addWidget(self.vol_label)
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

    def set_volume_pct(self, pct: int):
        """显示该 pad 的独立音量（100% 时留空）。"""
        self.vol_label.setText("" if pct == 100 else f"{pct}%")


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
    version = "1.3"
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
        # 备选触发 CC → pad（如延音踏板 CC64 → 底鼓）
        self._alt_map = {}
        for pad in self.config["pads"]:
            alt = pad.get("alt_cc")
            if alt is not None:
                self._alt_map[alt] = pad

        # ---- 用宿主统一音频服务 ----
        # 宿主统一管理 pygame.mixer.init()，多插件共存不会冲突。
        CHANNELS_PER_PAD = 4
        self._channels_per_pad = CHANNELS_PER_PAD
        total = len(self.config["pads"]) * CHANNELS_PER_PAD

        audio = app.audio
        audio.init_if_needed()                                 # 宿主统一 init
        if not audio.inited:
            # 后端没起来时，下面每个 pad 都会报"采样加载失败"——先把根因说清楚
            self.log.warning("音频后端初始化失败，采样无法播放：%s", audio.last_error)
        audio.allocate_pool("pad_sentry", channel_count=total)  # 独占池

        # 预加载 sfx 到宿主音频服务（全局音量自动生效）
        self._sfx = {}  # cc → _SfxHandle
        for pad in self.config["pads"]:
            self._load_pad_sfx(pad)

        # ---- 订阅细分 topic（宿主已按 type 过滤，直接拿到 cc 类型消息）----
        app.subscribe("midi.cc", self._on_cc)

    def on_deactivate(self):
        for sfx in self._sfx.values():
            sfx.stop()
        self._sfx.clear()
        if self.app:
            self.app.audio.release_pool("pad_sentry")

    # ---- 采样加载 ----
    def _resolve_wav_path(self, pad) -> Path:
        """pad 的采样路径：custom_wav（用户自选）优先，否则用内置 samples/ 里的 wav。"""
        custom = pad.get("custom_wav")
        if custom:
            return Path(custom)
        return self._samples_dir / pad["wav"]

    def _load_pad_sfx(self, pad) -> bool:
        """加载/重载某个 pad 的采样；失败时清掉该 pad 的句柄并返回 False。"""
        path = self._resolve_wav_path(pad)
        if not path.exists():
            self._sfx.pop(pad["cc"], None)
            self.log.warning("采样缺失: %s", path)
            return False
        sfx = self.app.audio.load(str(path))
        if sfx is None:
            self._sfx.pop(pad["cc"], None)
            self.log.warning("采样加载失败: %s", path)
            return False
        self._sfx[pad["cc"]] = sfx
        self.log.info("loaded %s (%.2fs)", path.name, sfx.duration)
        return True

    # ---- 消息处理 ----
    def _on_cc(self, parsed):
        """EventBus 回调：宿主已按 topic=midi.cc 过滤，进来的都是 CC 消息。

        只响应 value > 0（松开忽略）+ cc 号命中主 CC 或备选 CC（如踏板）。
        """
        cc = parsed.values.get("control")
        val = parsed.values.get("value", 0)
        if val <= 0:
            return
        pad = self._pad_map.get(cc)
        if pad is None:
            # 备选 CC（延音踏板）路径：受 pedal_kick 开关控制，
            # 弹钢琴时关掉就不会误触发底鼓。
            if not self.config.get("pedal_kick", True):
                return
            pad = self._alt_map.get(cc)
            if pad is None:
                return

        # UI：找到对应的 _PadButton 做脉冲发光（按 pad 的主 CC 索引）
        pad_btn = getattr(self, "_pad_btns", {}).get(pad["cc"])
        if pad_btn is not None:
            pad_btn.pulse(self.config.get("pad_pulse_ms", 300))

        # 音频：交给宿主统一服务 round-robin 播放（按 pad 的主 CC 索引）
        if self.config.get("enabled", True):
            sfx = self._sfx.get(pad["cc"])
            if sfx is not None:
                # 最终音量 = 总音量 × 该 pad 的独立音量
                volume = self.config.get("volume", 0.75) * pad.get("volume", 1.0)
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
        hint_text = f"监听 CC #{rng[0]}~#{rng[1]}（共 {len(self.config['pads'])} 个 pad）"
        for alt_cc, alt_pad in sorted(self._alt_map.items()):
            hint_text += f" · 踏板 CC#{alt_cc} → {alt_pad['name']}"
        hint = QLabel(hint_text + " · 内置 GM 鼓组")
        hint.setStyleSheet(f"color: {QSS.MUTED}; font-size: 11px;")
        root.addWidget(hint)

        # 2×4 网格（i // 4 = 行, i % 4 = 列）
        grid_wrap = QGridLayout()
        grid_wrap.setSpacing(8)
        self._pad_btns = {}
        for i, pad in enumerate(self.config["pads"]):
            btn = _PadButton(f"Pad {i + 1}", pad["name"])
            btn.set_volume_pct(int(pad.get("volume", 1.0) * 100))
            # 右键菜单：换采样 / 调独立音量 / 恢复默认
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda pos, p=pad, b=btn: self._pad_menu(p, b, pos)
            )
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

        # 踏板开关：关掉后延音踏板只做延音，不再模拟底鼓
        if self._alt_map:
            self.pedal_cb = QCheckBox("踏板打底鼓")
            self.pedal_cb.setChecked(self.config.get("pedal_kick", True))
            self.pedal_cb.setStyleSheet(f"color: {QSS.TEXT};")
            self.pedal_cb.setToolTip("关闭后延音踏板恢复纯延音用途，不会触发底鼓")
            self.pedal_cb.toggled.connect(self._toggle_pedal_kick)
            ctrl.addWidget(self.pedal_cb)

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
        self._status_label = QLabel()
        self._status_label.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        root.addWidget(self._status_label)
        self._refresh_status()

        return box

    # ---- 控制条回调 ----
    def _toggle_sound(self, on: bool):
        self.config["enabled"] = on
        self._save_config()

    def _toggle_pedal_kick(self, on: bool):
        """踏板 → 底鼓 开关：关掉后踏板恢复纯延音用途。"""
        self.config["pedal_kick"] = on
        self._save_config()

    def _on_volume(self, v: int):
        """总音量。播放时实时读 config["volume"]（见 _on_cc），所以只需改配置。

        注意：_SfxHandle 只有 play()/stop()，没有 set_volume()，
        之前这里遍历调用 set_volume 会抛 AttributeError 卡死事件循环。
        """
        self.config["volume"] = v / 100.0
        self.vol_val.setText(f"{v}%")
        self._save_config()

    # ---- pad 右键菜单 ----
    def _pad_menu(self, pad, btn, pos) -> None:
        """右键 pad：换采样 / 调独立音量 / 恢复默认采样。"""
        pct = int(pad.get("volume", 1.0) * 100)
        menu = QMenu(btn)
        act_sample = menu.addAction("更换采样…")
        act_volume = menu.addAction(f"调整音量…（当前 {pct}%）")
        menu.addSeparator()
        act_reset = menu.addAction("恢复默认采样")
        act_reset.setEnabled(bool(pad.get("custom_wav")))

        chosen = menu.exec(btn.mapToGlobal(pos))
        if chosen is act_sample:
            self._change_sample(pad, btn)
        elif chosen is act_volume:
            self._adjust_pad_volume(pad, btn)
        elif chosen is act_reset:
            self._reset_sample(pad)

    def _change_sample(self, pad, btn) -> None:
        """选一个音频文件替换该 pad 的采样（引用原路径，不动用户文件）。"""
        if pad.get("custom_wav"):
            start_dir = str(Path(pad["custom_wav"]).parent)
        else:
            start_dir = str(self._samples_dir)
        path, _ = QFileDialog.getOpenFileName(
            btn, f"{pad['name']} — 选择采样", start_dir, "音频文件 (*.wav *.ogg)"
        )
        if not path:
            return
        old = pad.get("custom_wav")
        pad["custom_wav"] = path
        if not self._load_pad_sfx(pad):
            # 加载失败则回滚，避免坏路径留在配置里
            if old:
                pad["custom_wav"] = old
            else:
                pad.pop("custom_wav", None)
            self._load_pad_sfx(pad)
            QMessageBox.warning(btn, "采样加载失败",
                                f"无法加载：\n{path}\n\n已保留原采样。")
            return
        self._save_config()
        self._refresh_status()

    def _adjust_pad_volume(self, pad, btn) -> None:
        """调该 pad 的独立音量（与总音量相乘）。"""
        cur = int(pad.get("volume", 1.0) * 100)
        val, ok = QInputDialog.getInt(
            btn, f"{pad['name']} — 独立音量", "音量 %：", cur, 0, 100, 5
        )
        if not ok:
            return
        pad["volume"] = val / 100.0
        self._pad_btns[pad["cc"]].set_volume_pct(val)
        self._save_config()

    def _reset_sample(self, pad) -> None:
        """清掉自选采样，回到内置采样。"""
        pad.pop("custom_wav", None)
        self._load_pad_sfx(pad)
        self._save_config()
        self._refresh_status()

    def _refresh_status(self) -> None:
        """刷新采样就绪状态，并提示右键可编辑。"""
        total = len(self.config["pads"])
        loaded = len(self._sfx)
        custom = sum(1 for p in self.config["pads"] if p.get("custom_wav"))
        text = f"采样就绪 {loaded}/{total}"
        if custom:
            text += f" · 自选采样 {custom} 个"
        if loaded == 0 and self.app is not None and not self.app.audio.inited:
            text += f" · 音频后端未就绪：{self.app.audio.last_error}"
        text += " · 右键 pad 可换采样 / 调独立音量"
        self._status_label.setText(text)

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
