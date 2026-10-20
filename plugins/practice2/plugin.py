"""Practice2 — 横向钢琴练习插件（仿 pianodrift.com，旧 practice 的 2.0）。

用法一句话：打开 MIDI → 选模式 → 播放，音符落到键盘上时跟弹。

架构：
    plugin.py   本文件：面板工具条 + 宿主接线（订阅 MIDI / 音频 / 配置）
    _engine.py  传输引擎（纯逻辑）：时间锚点、等待模式、分组判定、统计
    _canvas.py  画布：下落音符 + 88 键钢琴 + 电脑键盘/鼠标弹奏
    _sound.py   音色合成：每键 wav 按需生成（三角钢琴/电钢琴/八音盒）

线程模型：MIDI 订阅回调只写引擎状态（引擎内部加锁），
音频与 UI 全部在主线程 QTimer 里消费，不跨线程碰 Qt。

分发契约（把 plugins/practice2/ 整个目录拷进其他 MidiForge 即可用）：
    宿主 API    必需：app.audio.{init_if_needed, inited, load, play}、
                app.{subscribe, log, data_dir}、core.plugin.Plugin、
                core.midi_file（load_song 解析 .mid 用，缺失只影响导曲）；
                可选：ensure_channels / allocate_pool / release_pool /
                fadeout_if / play 的 plugin_name 参数，缺失时自动降级
                （见 _play_channel / _fade_channel）
    第三方依赖  PyQt6、numpy、pygame（缺 pygame 只是无声，不崩）
    自包含资源  sounds/ 首次弹奏自动合成，data/ 走 app.data_dir
"""

import inspect
import sys
import time
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from core.plugin import Plugin
from plugins.practice2 import _sound
from plugins.practice2._canvas import DriftCanvas
from plugins.practice2._compat import QSS
from plugins.practice2._engine import (MODE_FOLLOW, MODE_LISTEN, MODE_WAIT,
                                         Song, SongNote, DriftEngine, load_song)

try:
    import pygame
except ImportError:
    pygame = None


DEFAULT_CONFIG = {
    "tone": "grand",
    "speed": 1.0,
    "mode": MODE_FOLLOW,
    "part": "all",
    "tail": 0.6,
    "count_in": True,
    "fall_speed": 160,
}


# ============================================================
# 内置示范曲（直接构造音符，不走文件）
# ============================================================

def _demo_twinkle(bpm: float = 100.0) -> Song:
    """小星星（第一乐句）。"""
    q = 60.0 / bpm
    seq = [(60, 1), (60, 1), (67, 1), (67, 1), (69, 1), (69, 2), (67, 2),
           (65, 1), (65, 1), (64, 1), (64, 1), (62, 1), (62, 2), (60, 2)]
    return _build_demo("小星星", seq, q, bpm)


def _demo_scale(bpm: float = 90.0) -> Song:
    """C 大调音阶上下行。"""
    q = 60.0 / bpm
    up = list(range(60, 72))
    seq = [(p, 1) for p in up + up[-2:0:-1]]
    return _build_demo("C大调音阶", seq, q, bpm)


def _build_demo(name: str, seq, q: float, bpm: float) -> Song:
    notes, t = [], 0.0
    for i, (pitch, beats) in enumerate(seq):
        notes.append(SongNote(id=i, pitch=pitch, start=t,
                              end=t + beats * q * 0.92, velocity=90,
                              channel=0, color_idx=0))
        t += beats * q
    return Song(notes=notes,
                tracks=[type("T", (), {"name": name, "count": len(notes)})()],
                bpm=bpm, duration=t)


DEMOS = {"小星星": _demo_twinkle, "C大调音阶": _demo_scale}


# ============================================================
# Plugin
# ============================================================

class Practice2Plugin(Plugin):
    """Practice2 练习插件（2.0）。"""

    name = "practice2"
    version = "2.0"
    status = "dev"

    POOL_CHANNELS = 64    # 独占音频通道池（复音上限由它决定）

    def __init__(self):
        super().__init__()
        self.app = None
        self.log = None
        self.engine = DriftEngine()
        self._config = dict(DEFAULT_CONFIG)
        self._panel = None
        self._canvas: DriftCanvas = None
        self._plugin_dir = Path(__file__).parent   # 采样目录依赖它，越早越好
        self._sfx = {}          # (tone, pitch) → _SfxHandle
        self._click = None      # (hi, lo)
        self._parts = []        # [(value, label), ...]
        self._save_timer = None
        # 用户弹的音的通道管理：不登记的话松键没人止音，
        # 同音重按会在多个通道叠加采样（低音 4.5s 长尾），听起来"一直响"
        self._user_ch: dict = {}    # pitch → 当前发声通道
        self._pedal_held: set = set()   # 被延音踏板延住的 pitch
        self._kw_play = False       # 宿主 play 是否支持 plugin_name（on_activate 探测）

    # ---------------- 生命周期 ----------------

    def on_activate(self, app):
        self.app = app
        self.log = app.log
        self._load_config()

        audio = app.audio
        audio.init_if_needed()
        if not audio.inited:
            self.log.warning("音频后端初始化失败，钢琴不会出声：%s", audio.last_error)
        else:
            # 池相关是宿主较新的 API，旧宿主没有就退回全局通道
            ensure = getattr(audio, "ensure_channels", None)
            if ensure is not None:
                ensure(self.POOL_CHANNELS + 16)
            alloc = getattr(audio, "allocate_pool", None)
            if alloc is not None:
                alloc(self.name, self.POOL_CHANNELS)
        # play 签名探测：旧宿主的 play 不接受 plugin_name 池参数
        self._kw_play = "plugin_name" in inspect.signature(audio.play).parameters

        self.engine.cb_sound = self._on_engine_sound
        self.engine.cb_stop = self._on_engine_stop
        self.engine.cb_click = self._on_engine_click
        self.engine.cb_pedal = self._on_pedal_changed

        app.subscribe("midi.note_on", self._on_note_on)
        app.subscribe("midi.note_off", self._on_note_off)
        app.subscribe("midi.cc", self._on_cc)
        self.log.info("activated")

    def on_deactivate(self):
        self.engine.pause()
        self._sfx.clear()
        if self.app:
            release = getattr(self.app.audio, "release_pool", None)
            if release is not None:
                release(self.name)

    # ---------------- 音频兼容层（旧宿主降级） ----------------

    def _play_channel(self, sfx, volume: float):
        """播放并返回通道号。旧宿主 play 不含 plugin_name 参数时退回普通播放。"""
        if self._kw_play:
            return self.app.audio.play(sfx, plugin_name=self.name, volume=volume)
        return self.app.audio.play(sfx, volume=volume)

    def _fade_channel(self, ch: int, snd, ms: int) -> None:
        """止音。优先宿主 fadeout_if（带归属校验防误杀）；
        旧宿主没有时退化为直接淡出（最坏情况误杀复用通道，但不崩）。"""
        fader = getattr(self.app.audio, "fadeout_if", None)
        if fader is not None:
            fader(ch, snd, ms)
            return
        if pygame is not None:
            try:
                pygame.mixer.Channel(ch).fadeout(max(30, ms))
            except Exception:
                pass

    # ---------------- 引擎 → 宿主回调 ----------------

    def _get_sfx(self, pitch: int):
        tone = self._config["tone"]
        key = (tone, pitch)
        if key in self._sfx:
            return self._sfx[key]
        try:
            path = _sound.ensure_sample(self._plugin_dir / "sounds", tone, pitch)
            sfx = self.app.audio.load(str(path))
            if sfx is None:
                # 加载失败不缓存，下次按键重试（缓存 None 会永久静音）
                self.log.warning("音色采样加载失败 tone=%s pitch=%s path=%s",
                                 tone, pitch, path)
                return None
            self._sfx[key] = sfx
            return sfx
        except Exception as exc:
            self.log.warning("音色采样生成/加载失败 pitch=%s: %s", pitch, exc)
            return None

    def _on_engine_sound(self, note, is_practice: bool):
        """引擎调度出声（主线程）。返回 (通道, 声音) 作为 voice 句柄。"""
        sfx = self._get_sfx(note.pitch)
        if sfx is None:
            return None
        vol = _sound.TIMBRE_GAIN[self._config["tone"]] * (note.velocity / 127.0) ** 1.35
        ch = self._play_channel(sfx, vol)
        if ch is None:
            return None
        return (ch, sfx.sound)

    def _on_engine_stop(self, handle):
        """引擎止音（主线程）：fadeout 避免咔哒声。句柄带声音归属防误杀。"""
        if handle is None:
            return
        ch, snd = handle
        self._fade_channel(ch, snd, 150)

    def _on_engine_click(self, beats_left: int):
        """预备拍 click。"""
        if self._click is None:
            try:
                self._click = tuple(
                    self.app.audio.load(str(p))
                    for p in _sound.ensure_click(self._plugin_dir / "sounds"))
            except Exception:
                self._click = (None, None)
        sfx = self._click[0] if beats_left >= 2 else self._click[1]
        if sfx:
            self._play_channel(sfx, 0.6)

    # ---------------- 用户输入 ----------------

    def _strike(self, pitch: int, velocity: int):
        """用户发声（MIDI 线程/主线程均可）：出声 + 判定。"""
        sfx = self._get_sfx(pitch)
        if sfx is not None:
            vol = _sound.TIMBRE_GAIN[self._config["tone"]] * (velocity / 127.0) ** 1.35
            ch = self._play_channel(sfx, vol)
            if ch is not None:
                old = self._user_ch.get(pitch)
                if old is not None and old[0] != ch:
                    self._fade_channel(old[0], old[1], 80)  # 同音重按：止掉上一次的
                self._user_ch[pitch] = (ch, sfx.sound)
                self._pedal_held.discard(pitch)
        self.engine.user_note_on(pitch, velocity)

    def _handle_note_off(self, pitch: int):
        """松键收尾：踏板按住 → 延住；否则按延音滑条淡出。"""
        self.engine.user_note_off(pitch)
        if self.engine._pedal_active():
            self._pedal_held.add(pitch)
        else:
            self._release_user(pitch)

    def _release_user(self, pitch: int):
        """止掉某个用户音，淡出时长 = 延音滑条（秒）。

        句柄是 (通道, 声音)：池轮转后通道可能已被引擎音复用，
        fadeout_if 会先验证归属，防止误杀别人的声音。
        """
        h = self._user_ch.pop(pitch, None)
        if h is not None:
            self._fade_channel(h[0], h[1], int(self._config["tail"] * 1000))

    def _on_note_on(self, parsed):
        vel = parsed.values.get("velocity", 0)
        pitch = parsed.values.get("note")
        if pitch is None:
            return
        if vel <= 0:                      # 力度 0 的 note_on = note_off
            self._handle_note_off(pitch)
            return
        self._strike(pitch, vel)

    def _on_note_off(self, parsed):
        pitch = parsed.values.get("note")
        if pitch is not None:
            self._handle_note_off(pitch)

    def _on_cc(self, parsed):
        if parsed.values.get("control") == 64:
            self.engine.set_pedal(parsed.values.get("value", 0) >= 64)

    def _on_pedal_changed(self, down: bool):
        """踏板完全松开（CC64 与 Shift 都抬）→ 收尾被延住的音。"""
        if not down and not self.engine._pedal_active() and self._pedal_held:
            for pitch in list(self._pedal_held):
                self._release_user(pitch)
            self._pedal_held.clear()

    # ---------------- 配置（400ms 防抖落盘）----------------

    def _config_path(self) -> Path:
        return self.app.data_dir(self.name) / "config.json"

    def _load_config(self):
        import json
        path = self._config_path()
        if path.exists():
            try:
                self._config.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:
                self.log.warning("config.json 解析失败，用默认配置: %s", exc)

    def _mark_dirty(self, key: str, value):
        self._config[key] = value
        if self._save_timer is None:
            self._save_timer = QTimer(self._panel)
            self._save_timer.setSingleShot(True)
            self._save_timer.setInterval(400)
            self._save_timer.timeout.connect(self._save_config)
        self._save_timer.start()

    def _save_config(self):
        import json
        try:
            self._config_path().parent.mkdir(parents=True, exist_ok=True)
            self._config_path().write_text(
                json.dumps(self._config, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as exc:
            self.log.warning("保存 config.json 失败: %s", exc)

    # ---------------- UI ----------------

    def create_panel(self) -> QWidget:
        self._panel = self._build_panel()
        # 应用已保存的配置到控件
        self._apply_config_to_ui()
        # 主循环定时器：60fps 推进引擎 + 重绘
        self._timer = QTimer(self._panel)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        # 曲子播完后同步播放按钮状态（面板已建好才能接管）
        self.engine.cb_finished = self._sync_play_button
        return self._panel

    def _build_panel(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        title = QLabel("🎹 Practice2 — 下落音符钢琴练习（88 键）")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # ---- 工具条第 1 行：曲目 / 播放 / 进度 / 循环 ----
        row1 = QHBoxLayout()
        self._btn_open = QPushButton("打开 MIDI…")
        self._btn_open.clicked.connect(self._on_open_midi)
        row1.addWidget(self._btn_open)

        self._combo_demo = QComboBox()
        self._combo_demo.addItem("示范曲…")
        for name in DEMOS:
            self._combo_demo.addItem(name)
        self._combo_demo.currentIndexChanged.connect(self._on_demo_selected)
        row1.addWidget(self._combo_demo)

        self._btn_play = QPushButton("▶ 播放")
        self._btn_play.clicked.connect(self._on_toggle_play)
        row1.addWidget(self._btn_play)

        self._btn_restart = QPushButton("↺ 从头")
        self._btn_restart.clicked.connect(self._on_restart)
        row1.addWidget(self._btn_restart)

        self._lbl_time = QLabel("0:00 / 0:00")
        self._lbl_time.setStyleSheet(f"color:{QSS.MUTED};")
        row1.addWidget(self._lbl_time)
        row1.addStretch(1)

        self._chk_loop = QCheckBox("A-B 循环")
        self._chk_loop.toggled.connect(self._on_loop_changed)
        row1.addWidget(self._chk_loop)
        self._btn_set_a = QPushButton("A=此处")
        self._btn_set_a.clicked.connect(lambda: self._on_set_loop_point(0))
        row1.addWidget(self._btn_set_a)
        self._spin_a = self._make_spin()
        row1.addWidget(self._spin_a)
        self._btn_set_b = QPushButton("B=此处")
        self._btn_set_b.clicked.connect(lambda: self._on_set_loop_point(1))
        row1.addWidget(self._btn_set_b)
        self._spin_b = self._make_spin()
        row1.addWidget(self._spin_b)
        layout.addLayout(row1)

        # ---- 工具条第 2 行：模式 / 声部 / 速度 / 音色 / 预备拍 / 延音 ----
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("模式:"))
        self._combo_mode = QComboBox()
        self._combo_mode.addItem("跟随演奏", MODE_FOLLOW)
        self._combo_mode.addItem("等待按对", MODE_WAIT)
        self._combo_mode.addItem("试听所选声部", MODE_LISTEN)
        self._combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        row2.addWidget(self._combo_mode)

        row2.addWidget(QLabel("声部:"))
        self._combo_part = QComboBox()
        self._fill_parts()
        self._combo_part.currentIndexChanged.connect(self._on_part_changed)
        row2.addWidget(self._combo_part)

        row2.addWidget(QLabel("速度:"))
        self._combo_speed = QComboBox()
        for pct in (25, 50, 75, 100, 125, 150):
            self._combo_speed.addItem(f"{pct}%", pct / 100.0)
        self._combo_speed.setCurrentIndex(3)
        self._combo_speed.currentIndexChanged.connect(self._on_speed_changed)
        row2.addWidget(self._combo_speed)

        row2.addWidget(QLabel("音色:"))
        self._combo_tone = QComboBox()
        for t in _sound.TIMBRES:
            self._combo_tone.addItem(_sound.TIMBRE_LABELS[t], t)
        self._combo_tone.currentIndexChanged.connect(self._on_tone_changed)
        row2.addWidget(self._combo_tone)

        self._chk_count = QCheckBox("两拍预备")
        self._chk_count.toggled.connect(
            lambda v: (self.engine.__setattr__("count_in", v),
                       self._mark_dirty("count_in", v)))
        row2.addWidget(self._chk_count)

        row2.addWidget(QLabel("延音:"))
        self._slider_tail = QSlider(Qt.Orientation.Horizontal)
        self._slider_tail.setRange(0, 200)
        self._slider_tail.setValue(60)
        self._slider_tail.setFixedWidth(90)
        self._slider_tail.valueChanged.connect(self._on_tail_changed)
        row2.addWidget(self._slider_tail)
        self._lbl_tail = QLabel("0.6s")
        self._lbl_tail.setStyleSheet(f"color:{QSS.MUTED};")
        row2.addWidget(self._lbl_tail)

        row2.addWidget(QLabel("下落:"))
        self._slider_pps = QSlider(Qt.Orientation.Horizontal)
        self._slider_pps.setRange(80, 400)
        self._slider_pps.setValue(160)
        self._slider_pps.setFixedWidth(90)
        self._slider_pps.valueChanged.connect(self._on_pps_changed)
        row2.addWidget(self._slider_pps)
        layout.addLayout(row2)

        # ---- 画布 ----
        self._canvas = DriftCanvas(self.engine)
        self._canvas.cb_strike = self._strike
        self._canvas.cb_note_off = self._handle_note_off
        self._canvas.cb_seek = self._on_seek
        self._canvas.toggle_play_requested.connect(self._on_toggle_play)
        layout.addWidget(self._canvas, 1)

        hint = QLabel("提示：点击画布获得焦点后，可用电脑键盘 a w s e d… 弹奏（C4–E5），"
                      "Shift = 临时延音，空格 = 播放/暂停，Esc = 全部静音。MIDI 键盘即插即弹。")
        hint.setStyleSheet(f"color:{QSS.MUTED}; font-size:11px;")
        layout.addWidget(hint)

        root.setStyleSheet(
            f'QLabel#sectionTitle {{ color: {QSS.PRIMARY}; font-size: 13px; font-weight: 600; }}'
            f'QLabel {{ color: {QSS.TEXT}; }}'
        )
        return root

    def _make_spin(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 3600.0)
        spin.setDecimals(2)
        spin.setSuffix(" s")
        spin.setFixedWidth(88)
        spin.valueChanged.connect(self._on_loop_changed)
        return spin

    def _fill_parts(self):
        """按当前歌曲的通道填充声部下拉。"""
        self._combo_part.blockSignals(True)
        self._combo_part.clear()
        self._parts = [("all", "整首乐曲"), ("high", "高音（C4 及以上）"),
                       ("low", "低音（低于 C4）")]
        if self.engine.song:
            for t in self.engine.song.tracks:
                ch = self._parts_len()
                self._parts.append((f"track:{ch}", f"{t.name}（{t.count} 音）"))
        for value, label in self._parts:
            self._combo_part.addItem(label, value)
        self._combo_part.blockSignals(False)

    def _parts_len(self):
        return len(self._parts) - 3

    def _apply_config_to_ui(self):
        c = self._config
        idx = self._combo_speed.findData(c["speed"])
        self._combo_speed.setCurrentIndex(idx if idx >= 0 else 3)
        ti = self._combo_tone.findData(c["tone"])
        self._combo_tone.setCurrentIndex(ti if ti >= 0 else 0)
        mi = self._combo_mode.findData(c["mode"])
        self._combo_mode.setCurrentIndex(mi if mi >= 0 else 0)
        pi = self._combo_part.findData(c["part"])
        if pi >= 0:
            self._combo_part.setCurrentIndex(pi)
        self._chk_count.setChecked(bool(c["count_in"]))
        self._slider_tail.setValue(int(c["tail"] * 100))
        self._slider_pps.setValue(int(c["fall_speed"]))
        self.engine.speed = float(c["speed"])
        self.engine.set_mode(c["mode"])
        self.engine.part = c["part"]
        self.engine.set_pedal_tail(float(c["tail"]))
        self.engine.count_in = bool(c["count_in"])
        self._canvas.pps = float(c["fall_speed"])

    # ---------------- 槽 ----------------

    def _on_open_midi(self):
        path, _ = QFileDialog.getOpenFileName(
            self._panel, "打开 MIDI 文件", "", "MIDI (*.mid *.midi);;All (*)")
        if not path:
            return
        try:
            song = load_song(path)
        except Exception as exc:
            self.log.warning("MIDI 打开失败: %s", exc)
            return
        self.engine.set_song(song)
        self._combo_demo.blockSignals(True)
        self._combo_demo.setCurrentIndex(0)
        self._combo_demo.blockSignals(False)
        self._fill_parts()
        self._spin_a.setValue(0.0)
        self._spin_b.setValue(round(song.duration, 2))
        self._btn_play.setText("▶ 播放")
        self.log.info("已加载 %s：%d 音 / %.1fs / %.0f BPM",
                      Path(path).name, len(song.notes), song.duration, song.bpm)

    def _on_demo_selected(self, idx):
        if idx <= 0:
            return
        name = self._combo_demo.itemText(idx)
        song = DEMOS[name]()
        self.engine.set_song(song)
        self._fill_parts()
        self._spin_a.setValue(0.0)
        self._spin_b.setValue(round(song.duration, 2))

    def _on_toggle_play(self):
        if self.engine.playing:
            self.engine.pause()
        else:
            self.engine.play()
        self._sync_play_button()

    def _on_restart(self):
        self.engine.restart()
        self._sync_play_button()

    def _sync_play_button(self):
        self._btn_play.setText("Ⅱ 暂停" if self.engine.playing else "▶ 播放")

    def _on_seek(self, pos: float):
        self.engine.seek(pos)

    def _on_mode_changed(self, idx):
        mode = self._combo_mode.itemData(idx)
        if mode:
            self.engine.set_mode(mode)
            self._mark_dirty("mode", mode)

    def _on_part_changed(self, idx):
        part = self._combo_part.itemData(idx)
        if part:
            self.engine.set_part(part)
            self._mark_dirty("part", part)

    def _on_speed_changed(self, idx):
        speed = self._combo_speed.itemData(idx)
        if speed:
            self.engine.set_speed(float(speed))
            self._mark_dirty("speed", float(speed))

    def _on_tone_changed(self, idx):
        tone = self._combo_tone.itemData(idx)
        if tone:
            self._mark_dirty("tone", tone)

    def _on_tail_changed(self, v):
        tail = v / 100.0
        self._lbl_tail.setText(f"{tail:.1f}s")
        self.engine.set_pedal_tail(tail)
        self._mark_dirty("tail", tail)

    def _on_pps_changed(self, v):
        self._canvas.pps = float(v)
        self._mark_dirty("fall_speed", v)

    def _on_set_loop_point(self, which: int):
        pos = max(0.0, self.engine.current_position())
        (self._spin_a if which == 0 else self._spin_b).setValue(round(pos, 2))

    def _on_loop_changed(self, *_):
        a = self._spin_a.value()
        b = max(a + 0.05, self._spin_b.value())
        self.engine.set_loop(self._chk_loop.isChecked(), a, b)

    # ---------------- 主循环 ----------------

    def _tick(self):
        self.engine.tick(time.monotonic())
        # 时间标签
        eng = self.engine
        if eng.song:
            cur = max(0.0, eng.current_position())
            self._lbl_time.setText(
                f"{self._fmt(cur)} / {self._fmt(eng.song.duration)}")
        self._canvas.update()

    @staticmethod
    def _fmt(t: float) -> str:
        m, s = divmod(int(t), 60)
        return f"{m}:{s:02d}"
