"""SynthLab — 插件内实时减法合成器（numpy 内核 + sounddevice 输出）。

定位（硬约束）：**完全自包含在 plugins/synth/ 内**，不改动 core/ / api.py / ui/。
    - 不用宿主 pygame.mixer（那是采样播放器），改用 numpy 自建 DSP + sounddevice 流
    - 对宿主只做两件只读的事：订阅 "midi.all"、用 app.log 打日志
    - MIDI 只接收键盘发声，**不对外转发**

线程模型（关键）：
    MIDI 轮询线程 → _on_midi_all() → engine.push_*（入队，线程安全）
    PortAudio 回调线程 → AudioOut._callback() → engine.process()
    Qt 主线程 → 面板控件；MIDI 线程的 UI 更新一律经 _UiBridge 信号排队回主线程

    Qt 槽内任何未捕获异常都会毒死事件循环 → 所有槽体 try/except 兜底。
"""

import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from core.plugin import Plugin
from ui import style_qss as QSS

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _audio          # noqa: E402
import _dsp            # noqa: E402


DEFAULT_CONFIG = {
    "params": dict(_dsp.DEFAULT_PARAMS),
    "cc_map": dict(_dsp.DEFAULT_CC_MAP),
    "device": None,
    "channel": None,          # None = 全部通道
    "enabled": True,
}

# 面板一行的旋钮格数（各组最多 5 个参数，正好一行）
COLS_PER_ROW = 5

# 空闲多久没按键就自动释放音频设备（秒）。用户手动开的流不受此限。
IDLE_RELEASE_SEC = 10.0

# 能画缩略图的波形集合（与 _WaveShape._ys 支持的形状一一对应）
_WAVE_KINDS = frozenset(("saw", "square", "triangle", "sine"))

# 全局 QSS 里有 `QWidget { background-color: BG }`，会让每个容器小格在卡片上糊出
# 一块深色方块。这里用 id 选择器把自建的容器格清成透明（选择器只命中容器本身，
# 不会波及里面的旋钮和按钮）。
_FLAT_QSS = "QWidget#synthFlat { background: transparent; border: none; }"


def _flat(w: QWidget) -> QWidget:
    """把自建容器格清成透明，避免在卡片上留下深色方块。"""
    w.setObjectName("synthFlat")
    w.setStyleSheet(_FLAT_QSS)
    return w


class _UiBridge(QObject):
    """MIDI 线程 → Qt 主线程 的 UI 更新通道。

    EventBus.publish 在 MIDI 轮询线程执行，Qt 控件只能在主线程碰。
    PyQt 对"非 QObject 的槽"会建代理 QObject，其线程归属 = connect() 时所在线程，
    所以这里在主线程 connect，信号就会以队列方式投递回主线程。
    """

    cc_received = pyqtSignal(int, int, str)   # control, value, 命中参数 key（无则 ""）
    learn_done = pyqtSignal(int, str)         # control, 参数 key
    need_audio = pyqtSignal()                 # MIDI 线程 → 主线程：请开音频流（可重试）


class _Knob(QWidget):
    """自绘旋钮：270° 圆弧 + 指针 + 数值。

    内部只持有 0-127 的整数刻度 t（与 MIDI CC 同域），
    与参数实际值的换算交给 _dsp.value_to_cc() / cc_to_value()，
    这样曲线（lin/log）与整型参数的取整只在一处实现。

    交互：上下拖拽 / 滚轮调节、双击复位、右键菜单（由宿主面板挂入）。
    """

    tChanged = pyqtSignal(int)

    W, H = 62, 78

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._label = label
        self._t = 0
        self._default_t = 0
        self._text = "—"
        self._drag_y = None
        self._drag_t = 0
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip(label)

    # ---- 数据 ----

    def t(self) -> int:
        return self._t

    def set_t(self, t: int, emit: bool = False) -> None:
        t = int(max(0, min(127, t)))
        self._t = t
        self.update()
        if emit:
            self.tChanged.emit(t)

    def set_default_t(self, t: int) -> None:
        self._default_t = int(max(0, min(127, t)))

    def set_text(self, s: str) -> None:
        self._text = s
        self.update()

    # ---- 绘制 ----

    def paintEvent(self, event):                      # noqa: N802
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            w = float(self.width())
            cx, cy, r = w / 2.0, 40.0, 19.0
            rect = QRectF(cx - r, cy - r, 2.0 * r, 2.0 * r)

            p.setPen(QPen(QColor(QSS.BORDER), 3))
            p.drawArc(rect, 225 * 16, -270 * 16)
            span = int(-270 * 16 * self._t / 127.0)
            if span != 0:
                p.setPen(QPen(QColor(QSS.PRIMARY), 3))
                p.drawArc(rect, 225 * 16, span)

            ang = math.radians(225.0 - 270.0 * self._t / 127.0)
            ca, sa = math.cos(ang), math.sin(ang)
            p.setPen(QPen(QColor(QSS.TEXT), 2))
            p.drawLine(QPointF(cx + (r - 7.0) * ca, cy - (r - 7.0) * sa),
                       QPointF(cx + (r - 1.0) * ca, cy - (r - 1.0) * sa))

            f = p.font()
            f.setPointSize(7)
            p.setFont(f)
            align = int(Qt.AlignmentFlag.AlignCenter)
            p.setPen(QColor(QSS.MUTED))
            p.drawText(QRectF(0.0, 0.0, w, 13.0), align, self._label)
            p.setPen(QColor(QSS.TEXT))
            p.drawText(QRectF(0.0, cy + r + 4.0, w, 14.0), align, self._text)
        finally:
            p.end()

    # ---- 交互 ----

    def mousePressEvent(self, event):                 # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_y = event.position().y()
            self._drag_t = self._t

    def mouseMoveEvent(self, event):                  # noqa: N802
        if self._drag_y is None:
            return
        delta = self._drag_y - event.position().y()
        step = 0.6 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
        self.set_t(self._drag_t + delta * step, emit=True)

    def mouseReleaseEvent(self, event):               # noqa: N802
        self._drag_y = None

    def wheelEvent(self, event):                      # noqa: N802
        d = event.angleDelta().y()
        if d:
            self.set_t(self._t + (2 if d > 0 else -2), emit=True)

    def mouseDoubleClickEvent(self, event):           # noqa: N802
        self.set_t(self._default_t, emit=True)


class _Scope(QWidget):
    """实时输出波形（示波器）。

    数据来自 engine.scope_snapshot()——音频回调线程写环形缓冲，这里只读一份快照。
    画法是"每列取该列窗口内的最大/最小值，画一条竖线"，也就是音频示波器惯用的
    包络法：比逐采样折线更能看清高频内容，且列数固定、开销可控。
    """

    MAX_COLS = 320                     # 竖线列数上限，防止超宽面板里白烧 CPU

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._data = np.zeros(2, dtype=np.float32)
        self._peak = 0.0
        self.setMinimumHeight(104)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._timer = QTimer(self)
        self._timer.setInterval(33)     # ≈30fps，够顺滑又不抢音频线程
        self._timer.timeout.connect(self.refresh)

    # 面板不可见时停表，别在后台空转重绘
    def showEvent(self, event):                       # noqa: N802
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):                       # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def stop(self) -> None:
        """插件停用时停表（宿主可能仍留着面板，不能靠 hideEvent 兜底）。"""
        self._timer.stop()

    def refresh(self) -> None:
        try:
            data = self._engine.scope_snapshot()
            self._data = data
            self._peak = float(np.max(np.abs(data))) if data.size else 0.0
        except Exception:
            pass
        self.update()

    def paintEvent(self, event):                      # noqa: N802
        p = QPainter(self)
        try:
            w = float(self.width())
            h = float(self.height())
            p.fillRect(QRectF(0.0, 0.0, w, h), QColor(QSS.BG))

            p.setPen(QPen(QColor(QSS.BORDER), 1))
            for frac in (0.25, 0.5, 0.75):
                p.drawLine(QPointF(0.0, h * frac), QPointF(w, h * frac))

            n = self._data.size
            if n >= 2:
                mid = h / 2.0
                amp = mid - 4.0
                cols = min(self.MAX_COLS, max(2, int(w)))
                step = n / float(cols)
                p.setPen(QPen(QColor(QSS.PRIMARY), 1))
                for i in range(cols):
                    lo = int(i * step)
                    hi = max(lo + 1, int((i + 1) * step))
                    seg = self._data[lo:hi]
                    if seg.size == 0:
                        continue
                    x = (i + 0.5) * (w / cols)
                    p.drawLine(QPointF(x, mid - float(seg.max()) * amp),
                               QPointF(x, mid - float(seg.min()) * amp))

            f = p.font()
            f.setPointSize(7)
            p.setFont(f)
            p.setPen(QColor(QSS.MUTED))
            p.drawText(QRectF(6.0, 3.0, w - 12.0, 12.0),
                       int(Qt.AlignmentFlag.AlignLeft),
                       f"峰值 {self._peak:.3f}  窗口 {n} 采样")
        finally:
            p.end()


class _AdsrGraph(QWidget):
    """ADSR 包络图示。

    时间轴做对数压缩：Attack 默认 0.01s、Release 上限 8s，线性映射的话短包络
    全挤在左边缘看不出形状。所以这里是"形状示意"，不标真实时间刻度。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(190, 96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._att, self._dec, self._sus, self._rel = 0.01, 0.35, 0.6, 0.25

    def set_params(self, att: float, dec: float, sus: float, rel: float) -> None:
        self._att, self._dec, self._sus, self._rel = float(att), float(dec), float(sus), float(rel)
        self.update()

    @staticmethod
    def _norm(t: float, top: float) -> float:
        """把时间压进 0~1（对数），t=0 得 0、t=top 得 1。"""
        return math.log1p(max(0.0, t)) / math.log1p(top)

    def paintEvent(self, event):                      # noqa: N802
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            w = float(self.width())
            h = float(self.height())
            p.fillRect(QRectF(0.0, 0.0, w, h), QColor(QSS.BG))

            left, right = 7.0, w - 7.0
            top, bot = 10.0, h - 16.0
            gw, gh = right - left, bot - top
            if gw <= 8.0 or gh <= 8.0:
                return

            # 四段宽度：A/D/R 按时间对数缩放，S 段固定（它表示的是"持续"，无时长）
            x0 = left
            x1 = x0 + 0.30 * gw * self._norm(self._att, 5.0)
            x2 = x1 + 0.24 * gw * self._norm(self._dec, 5.0)
            x3 = x2 + 0.20 * gw
            x4 = min(right, x3 + 0.24 * gw * self._norm(self._rel, 8.0))
            sy = bot - gh * max(0.0, min(1.0, self._sus))

            pts = [QPointF(x0, bot), QPointF(x1, top),
                   QPointF(x2, sy), QPointF(x3, sy), QPointF(x4, bot)]

            fill = QColor(QSS.PRIMARY)
            fill.setAlpha(48)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawPolygon(QPolygonF(pts + [QPointF(x0, bot)]))

            # 坐标轴
            p.setPen(QPen(QColor(QSS.BORDER), 1))
            p.drawLine(QPointF(left, bot), QPointF(right, bot))
            p.drawLine(QPointF(left, top), QPointF(left, bot))

            # 包络曲线 + 拐点
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(QSS.PRIMARY), 2))
            p.drawPolyline(QPolygonF(pts))
            p.setBrush(QColor(QSS.ACCENT))
            p.setPen(Qt.PenStyle.NoPen)
            for pt in pts[1:4]:
                p.drawEllipse(pt, 2.4, 2.4)

            # 阶段标签
            f = p.font()
            f.setPointSize(7)
            p.setFont(f)
            p.setPen(QColor(QSS.MUTED))
            align = int(Qt.AlignmentFlag.AlignCenter)
            for cx, tag in (((x0 + x1) / 2.0, "A"), ((x1 + x2) / 2.0, "D"),
                            ((x2 + x3) / 2.0, "S"), ((x3 + x4) / 2.0, "R")):
                p.drawText(QRectF(cx - 8.0, bot + 2.0, 16.0, 12.0), align, tag)
        finally:
            p.end()


class _WaveShape(QWidget):
    """一个周期的小波形缩略图，跟着振荡器的波形下拉实时变。"""

    _N = 128

    def __init__(self, kind: str = "sine", parent=None):
        super().__init__(parent)
        self._kind = kind
        self.setFixedSize(66, 30)
        self.setToolTip("当前波形形状")

    def set_kind(self, kind: str) -> None:
        if kind != self._kind:
            self._kind = kind
            self.update()

    def _ys(self) -> np.ndarray:
        ph = np.linspace(0.0, 1.0, self._N, endpoint=False)
        kind = self._kind
        if kind == "square":
            return np.where(ph < 0.5, 1.0, -1.0)
        if kind == "triangle":
            return 1.0 - 4.0 * np.abs(ph - 0.5)
        if kind == "saw":
            return 2.0 * ph - 1.0
        return np.sin(2.0 * np.pi * ph)     # sine / 未知波形

    def paintEvent(self, event):                      # noqa: N802
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            w = float(self.width())
            h = float(self.height())
            p.fillRect(QRectF(0.0, 0.0, w, h), QColor(QSS.BG))
            p.setPen(QPen(QColor(QSS.BORDER), 1))
            p.drawLine(QPointF(0.0, h / 2.0), QPointF(w, h / 2.0))

            ys = self._ys()
            mid = h / 2.0
            amp = mid - 4.0
            xs = np.linspace(2.0, w - 2.0, ys.size)
            p.setPen(QPen(QColor(QSS.PRIMARY), 1.6))
            p.drawPolyline(QPolygonF(
                [QPointF(float(x), float(mid - v * amp)) for x, v in zip(xs, ys)]))
        finally:
            p.end()


class SynthLab(Plugin):
    """合成器插件主类。"""

    name = "synth"
    version = "0.1"
    status = "dev"
    description = "插件内实时减法合成器（2 OSC + ADSR + 低通 + LFO），可被 MIDI 键盘直接驱动"

    # ============ 生命周期 ============

    def on_activate(self, app) -> None:
        self.app = app
        self.log = app.log
        self._plugin_dir = Path(__file__).parent
        self._config_path = self._plugin_dir / "config.json"

        self._panel = None
        self._knobs = {}
        self._combos = {}
        self._learn_btns = {}
        self._shapes = {}          # 波形 key → _WaveShape（OSC1/OSC2/LFO 共用的缩略图）
        self._adsr = None          # ADSR 图示，面板建好后才有
        self._scope = None         # 输出波形示波器
        self._learn_key = None
        self._sid = None

        # ---- 音频引擎 + 输出流 ----
        self._engine = _dsp.SynthEngine()
        self._audio = _audio.AudioOut(self._engine)
        self.config = self._load_config()
        self._apply_config()

        # ---- 主线程 UI 桥 ----
        self._bridge = _UiBridge()
        self._bridge.cc_received.connect(self._on_cc_received)
        self._bridge.learn_done.connect(self._on_learn_done)
        self._bridge.need_audio.connect(self._ensure_audio)

        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_now)

        self._tick_timer = QTimer()
        self._tick_timer.setInterval(200)
        self._tick_timer.timeout.connect(self._tick)

        # ---- 订阅：只读全量消息，不对外转发 ----
        self._sid = app.subscribe("midi.all", self._on_midi_all)

        # ---- 音频流策略：按需开 + 失败可重试 + 空闲释放 ----
        # 不在激活时就抢设备。实测原因：本机 SDL 会退到 DirectSound 独占输出，
        # 之后 PortAudio 的 MME/WASAPI 全部开不了流；若 synth 一激活就常驻一条流，
        # pad_sentry 这类用 pygame 播采样的插件会被一起拖哑。
        # 顺带修掉老缺陷：开流失败（设备被占/参数不接受）不再是永久哑掉——
        # 下一次按键会重新尝试，用户也能在面板上换设备后手动重开。
        self._last_note_ts = time.monotonic()
        self._keep_audio = False        # True = 用户手动开过，不参与空闲释放
        self._audio_pending = False     # 已向主线程排队开流，防止连击重复请求
        # 面板没打开也要跑 tick（空闲释放不依赖面板），所以在这里就启动定时器
        self._tick_timer.start()

    def on_deactivate(self) -> None:
        try:
            self._save_now()
            self._save_timer.stop()
            self._tick_timer.stop()
            if self._scope is not None:
                self._scope.stop()
            self._audio.stop()
            if self.app is not None and self._sid is not None:
                self.app.bus.unsubscribe(self._sid)
        except Exception as exc:
            print(f"[synth] 清理失败: {exc}", file=sys.stderr)

    # ============ 配置 ============

    def _load_config(self) -> dict:
        """读 config.json；缺失/损坏则回落默认值（不写盘，等真正改动再写）。"""
        data = {}
        if self._config_path.exists():
            try:
                with open(self._config_path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = {}
        if not isinstance(data, dict):
            data = {}

        cfg = dict(DEFAULT_CONFIG)
        params = dict(_dsp.DEFAULT_PARAMS)
        raw_params = data.get("params")
        if isinstance(raw_params, dict):
            for k, v in raw_params.items():
                if k in _dsp.PARAM_BY_KEY:
                    params[k] = _dsp.clamp_param(k, v)
        cfg["params"] = params

        raw_map = data.get("cc_map")
        if isinstance(raw_map, dict):
            cmap = {}
            for c, k in raw_map.items():
                try:
                    c = int(c)
                except (TypeError, ValueError):
                    continue
                if k in _dsp.PARAM_BY_KEY:
                    cmap[c] = k
            cfg["cc_map"] = cmap

        dev = data.get("device")
        cfg["device"] = int(dev) if isinstance(dev, int) else None
        ch = data.get("channel")
        cfg["channel"] = int(ch) if isinstance(ch, int) and 0 <= ch <= 15 else None
        cfg["enabled"] = bool(data.get("enabled", True))
        return cfg

    def _apply_config(self) -> None:
        for key, val in self.config["params"].items():
            self._engine.set_param(key, val)
        self._engine.set_cc_map(self.config["cc_map"])

    def _schedule_save(self) -> None:
        """400ms 防抖落盘（只在主线程调用）。"""
        if self._save_timer is not None:
            self._save_timer.start(400)

    def _save_now(self) -> None:
        payload = {
            "params": {k: self._engine.get_param(k) for k in _dsp.PARAM_BY_KEY},
            "cc_map": {str(c): k for c, k in self._engine.cc_map.items()},
            "device": self.config.get("device"),
            "channel": self.config.get("channel"),
            "enabled": bool(self.config.get("enabled", True)),
        }
        self.config.update(payload)
        try:
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.log.warning("配置写入失败: %s", exc)

    # ============ MIDI（MIDI 线程）============

    def _on_midi_all(self, parsed) -> None:
        try:
            self._handle(parsed)
        except Exception as exc:
            # 事件总线上的异常绝不能外溢到 MIDI 线程
            self.log.warning("synth 消息处理失败: %s", exc)

    def _handle(self, parsed) -> None:
        if parsed.source == "virtual":       # 虚拟端口回环回声，必须过滤
            return
        if not self.config.get("enabled", True):
            return
        eng = self._engine
        ch = self.config.get("channel")
        if ch is not None and parsed.channel is not None and parsed.channel != ch:
            return

        v = parsed.values or {}
        if parsed.type == "note_on":
            vel = int(v.get("velocity", 0))
            note = int(v.get("note", 0))
            self._last_note_ts = time.monotonic()
            if vel > 0:
                eng.push_note_on(note, vel)
                self._request_audio()
            else:
                eng.push_note_off(note)      # velocity=0 的 note_on 即 note_off
        elif parsed.type == "note_off":
            self._last_note_ts = time.monotonic()
            eng.push_note_off(int(v.get("note", 0)))
        elif parsed.type == "cc":
            self._handle_cc(int(v.get("control", 0)), int(v.get("value", 0)))
        elif parsed.type == "pitch_bend":
            bent = float(v.get("pitch", 0)) / 8192.0
            eng.push_pitch_bend(max(-1.0, min(1.0, bent)))

    def _handle_cc(self, control: int, value: int) -> None:
        eng = self._engine
        if control == _dsp.SUSTAIN_CC:
            eng.push_sustain(value >= 64)
            self._bridge.cc_received.emit(control, value, "")
            return

        key = self._learn_key
        if key:
            # 学习模式：把这一条 CC 绑到待学习参数（绑定+落盘在主线程完成）
            cmap = dict(eng.cc_map)
            for c, k in list(cmap.items()):
                if k == key or c == control:
                    cmap.pop(c, None)        # 一个参数只留一条映射，一个 CC 只绑一个参数
            cmap[control] = key
            eng.set_cc_map(cmap)
            self._learn_key = None
            self._bridge.learn_done.emit(control, key)
            return

        hit = eng.cc_map.get(control)
        if hit:
            eng.set_param(hit, _dsp.cc_to_value(hit, value))
        self._bridge.cc_received.emit(control, value, hit or "")

    # ============ UI 桥槽（主线程）============

    def _on_cc_received(self, control: int, value: int, key: str) -> None:
        try:
            if self._panel is None:
                return
            if key:
                knob = self._knobs.get(key)
                if knob is not None:
                    knob.set_t(value, emit=False)
                    knob.set_text(self._fmt_value(_dsp.PARAM_BY_KEY[key],
                                                  self._engine.get_param(key)))
                if _dsp.PARAM_BY_KEY[key]["group"] == "ENVELOPE":
                    # 外部推杆也能推包络，图示得跟着动
                    self._refresh_adsr()
                self._schedule_save()
            self._lbl_midi.setText(f"最近 CC：CC{control} = {value}"
                                   + (f" → {_dsp.PARAM_BY_KEY[key]['label']}" if key else ""))
        except Exception as exc:
            self.log.warning("CC 回显失败: %s", exc)

    def _on_learn_done(self, control: int, key: str) -> None:
        try:
            btn = self._learn_btns.get(key)
            if btn is not None:
                btn.setChecked(False)
            if self._panel is not None:
                self._lbl_midi.setText(
                    f"已绑定 CC{control} → {_dsp.PARAM_BY_KEY[key]['label']}"
                    f"（共 {len(self._engine.cc_map)} 个映射）")
            self._schedule_save()
        except Exception as exc:
            self.log.warning("学习绑定回显失败: %s", exc)

    # ============ 音频流开关（按需开 + 可重试）============

    def _request_audio(self) -> None:
        """在 MIDI 线程调用：需要发声但流没起来 → 请主线程去开。

        不直接 start()，因为 OutputStream 的创建/启动要走 Qt 主线程，
        且失败后下一次按键还要能重试（_audio_pending 只为合并连击时的重复请求）。
        """
        if self._audio.ok or self._audio_pending:
            return
        if not self.config.get("enabled", True):
            return
        self._audio_pending = True
        self._bridge.need_audio.emit()

    def _ensure_audio(self) -> None:
        """主线程槽：真正开流。上一次失败也可以再试。"""
        self._audio_pending = False
        try:
            if self._audio.ok or not self.config.get("enabled", True):
                return
            ok = self._audio.start(device=self.config.get("device"))
            if ok:
                self.log.info("synth 音频流已就绪（%s）",
                              self._audio.info["device"])
            if self._panel is not None:
                self._btn_audio.setText("停止音频" if ok else "启动音频")
                if not ok:
                    self._lbl_audio.setText(
                        f"音频启动失败：{self._audio.info['last_error']}")
        except Exception as exc:
            self.log.warning("音频启动失败: %s", exc)

    def _tick(self) -> None:
        try:
            # 空闲释放放在最前面：面板没打开时 _panel 是 None，但音频流照样要按时
            # 放掉，不能因为"没开面板"就一直占着设备。
            if (self._audio.ok and not self._keep_audio
                    and self._engine.active_voices == 0
                    and time.monotonic() - self._last_note_ts > IDLE_RELEASE_SEC):
                self._audio.stop()
                if self._panel is not None:
                    self._btn_audio.setText("启动音频")
            if self._panel is None:
                return
            info = self._audio.info
            backend = info["device"] if info["ok"] else "未启动"
            self._lbl_audio.setText(
                f"音频：{backend} · {info['sample_rate']} Hz · 缓冲 {info['block_size']}"
                f"（{info['latency_ms']} ms）· 错误 {info['errors']}")
            eng = self._engine
            self._lbl_play.setText(
                f"声部 {eng.active_voices} · 峰值 {eng.peak:.3f} · 丢帧 {eng.dropouts}"
                f" · 延音 {'开' if eng.sustain_on else '关'}")
            if not info["ok"] and info["last_error"]:
                self._lbl_audio.setText(f"音频启动失败：{info['last_error']}")
        except Exception as exc:
            self.log.warning("状态刷新失败: %s", exc)

    # ============ 面板 ============

    def create_panel(self) -> QWidget:
        box = QWidget()
        box.setStyleSheet(f"background-color: {QSS.BG}; border-radius: 8px;")
        root = QVBoxLayout(box)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        title = QLabel("🎹 SynthLab · 实时减法合成器")
        title.setStyleSheet(f"color: {QSS.TEXT}; font-size: 14px; font-weight: 700;")
        root.addWidget(title)

        hint = QLabel("只接收 MIDI 键盘发声，不对外转发 · 旋钮：拖拽/滚轮调节，双击复位，"
                      "右键可解绑 CC 或恢复默认 · 点 “L” 后再推一下推杆即可学习绑定")
        hint.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        root.addLayout(self._build_toolbar())

        self._lbl_audio = QLabel("音频：—")
        self._lbl_audio.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        self._lbl_audio.setWordWrap(True)
        root.addWidget(self._lbl_audio)

        self._lbl_midi = QLabel("最近 CC：—")
        self._lbl_midi.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        root.addWidget(self._lbl_midi)

        # ---- 输出波形（常驻顶部，一眼看到键盘推出来的声音长什么样）----
        self._lbl_play = QLabel("声部 0")
        self._lbl_play.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        self._panel = box        # 先置位，_Scope.showEvent 里的回调才敢碰 _lbl_play
        root.addWidget(self._build_scope_card())

        # ---- 参数区（可滚动，避免窗口小的时候被截断）----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)

        # 两个振荡器左右并排——它们是并列关系，不该上下堆叠
        col.addWidget(self._row(self._make_osc_card("OSC1"),
                                self._make_osc_card("OSC2")))
        col.addWidget(self._make_env_card())
        col.addWidget(self._row(self._make_group_card("FILTER"),
                                self._make_group_card("LFO")))
        col.addWidget(self._make_group_card("MASTER"))
        col.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self._tick_timer.start()
        self._refresh_adsr()
        self._tick()
        return box

    # ---- 卡片组装 ----

    def _row(self, *cards) -> QWidget:
        """把若干张卡片横向并排（等宽拉伸）。"""
        holder = _flat(QWidget())
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        for card in cards:
            lay.addWidget(card, 1)
        return holder

    def _card(self, title: str) -> tuple:
        """统一卡片外壳：圆角底 + 左侧主色竖条 + 标题。返回 (卡片, 内容布局, 标题行)。"""
        card = QFrame()
        card.setObjectName("synthCard")
        card.setStyleSheet(
            f"QFrame#synthCard {{ background-color: {QSS.CARD}; "
            f"border: 1px solid {QSS.BORDER}; border-radius: 10px; }}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        bar = QFrame()
        bar.setFixedSize(3, 11)
        bar.setStyleSheet(f"background-color: {QSS.PRIMARY}; border-radius: 1px;")
        head.addWidget(bar)
        lbl = QLabel(title)
        lbl.setStyleSheet(f"color: {QSS.TEXT}; font-size: 11px; font-weight: 700;")
        head.addWidget(lbl)
        head.addStretch(1)
        lay.addLayout(head)
        return card, lay, head

    def _build_scope_card(self) -> QFrame:
        card, lay, head = self._card("输出波形")
        hint = QLabel("实时")
        hint.setStyleSheet(f"color: {QSS.ACCENT}; font-size: 9px;")
        head.addWidget(hint)
        self._scope = _Scope(self._engine)
        lay.addWidget(self._scope)
        lay.addWidget(self._lbl_play)
        return card

    def _make_osc_card(self, group: str) -> QFrame:
        """振荡器卡片：波形下拉 + 波形缩略图在上，四个旋钮在下。"""
        card, lay, head = self._card(group)

        wave_spec = next(s for s in _dsp.PARAM_SPECS
                         if s["group"] == group and s["kind"] == "choice")
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self._with_wave_shape(wave_spec), 0, Qt.AlignmentFlag.AlignTop)
        top.addStretch(1)
        lay.addLayout(top)

        knobs = QHBoxLayout()
        knobs.setSpacing(8)
        for spec in _dsp.PARAM_SPECS:
            if spec["group"] == group and spec["kind"] != "choice":
                knobs.addWidget(self._make_knob_cell(spec))
        lay.addLayout(knobs)
        return card

    def _with_wave_shape(self, spec: dict) -> QWidget:
        """波形下拉 + 紧跟一个波形缩略图（OSC1/OSC2/LFO 通用）。"""
        holder = _flat(QWidget())
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self._make_combo_cell(spec), 0, Qt.AlignmentFlag.AlignTop)
        shape = _WaveShape(self._combos[spec["key"]].currentText())
        self._shapes[spec["key"]] = shape
        lay.addWidget(shape, 0, Qt.AlignmentFlag.AlignVCenter)
        return holder

    def _make_env_card(self) -> QFrame:
        """包络卡片：左侧 ADSR 图示，右侧 A/D/S/R 四个旋钮。"""
        card, lay, head = self._card("ENVELOPE")

        body = QHBoxLayout()
        body.setSpacing(10)
        self._adsr = _AdsrGraph()
        body.addWidget(self._adsr, 1)

        knobs = QHBoxLayout()
        knobs.setSpacing(8)
        for spec in _dsp.PARAM_SPECS:
            if spec["group"] == "ENVELOPE":
                knobs.addWidget(self._make_knob_cell(spec))
        body.addLayout(knobs, 0)
        lay.addLayout(body)
        return card

    def _make_group_card(self, group: str) -> QFrame:
        """通用参数卡片：组内所有参数按 COLS_PER_ROW 折行摆放。"""
        card, lay, head = self._card(group)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        row = col = 0
        for spec in _dsp.PARAM_SPECS:
            if spec["group"] != group:
                continue
            if spec["kind"] == "choice":
                # 波形类下拉旁边挂缩略图，其它下拉（如 LFO 目标）保持原样
                cell = (self._with_wave_shape(spec)
                        if set(spec["choices"]) <= _WAVE_KINDS
                        else self._make_combo_cell(spec))
            else:
                cell = self._make_knob_cell(spec)
            grid.addWidget(cell, row, col, Qt.AlignmentFlag.AlignTop)
            col += 1
            if col >= COLS_PER_ROW:
                col = 0
                row += 1
        lay.addLayout(grid)
        lay.addStretch(1)
        return card

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self._chk_enabled = QCheckBox("发声")
        self._chk_enabled.setChecked(bool(self.config.get("enabled", True)))
        self._chk_enabled.setStyleSheet(f"color: {QSS.TEXT};")
        self._chk_enabled.toggled.connect(self._on_enabled)
        bar.addWidget(self._chk_enabled)

        bar.addSpacing(8)
        bar.addWidget(self._mk_label("音频输出:"))
        self._combo_dev = QComboBox()
        self._combo_dev.setMinimumWidth(180)
        self._combo_dev.addItem("系统默认", None)
        for idx, name in _audio.list_output_devices():
            self._combo_dev.addItem(f"{idx}: {name}", idx)
        saved = self.config.get("device")
        pos = self._combo_dev.findData(saved)
        self._combo_dev.setCurrentIndex(pos if pos >= 0 else 0)
        bar.addWidget(self._combo_dev)

        self._btn_audio = QPushButton("停止音频" if self._audio.ok else "启动音频")
        self._btn_audio.clicked.connect(self._on_audio_button)
        bar.addWidget(self._btn_audio)

        bar.addSpacing(8)
        bar.addWidget(self._mk_label("通道:"))
        self._combo_ch = QComboBox()
        self._combo_ch.addItem("全部", None)
        for i in range(16):
            self._combo_ch.addItem(f"ch{i + 1}", i)
        pos = self._combo_ch.findData(self.config.get("channel"))
        self._combo_ch.setCurrentIndex(pos if pos >= 0 else 0)
        self._combo_ch.currentIndexChanged.connect(self._on_channel)
        bar.addWidget(self._combo_ch)

        bar.addStretch(1)

        btn_panic = QPushButton("Panic")
        btn_panic.setToolTip("立刻释放所有声部")
        btn_panic.clicked.connect(self._on_panic)
        bar.addWidget(btn_panic)

        btn_reset = QPushButton("恢复默认")
        btn_reset.clicked.connect(self._on_reset_all)
        bar.addWidget(btn_reset)

        return bar

    @staticmethod
    def _mk_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {QSS.MUTED}; font-size: 11px;")
        return lbl

    def _make_knob_cell(self, spec: dict) -> QWidget:
        key = spec["key"]
        cell = _flat(QWidget())
        v = QVBoxLayout(cell)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        knob = _Knob(spec["label"])
        knob.set_default_t(_dsp.value_to_cc(key, spec["default"]))
        knob.set_t(_dsp.value_to_cc(key, self._engine.get_param(key)))
        knob.set_text(self._fmt_value(spec, self._engine.get_param(key)))
        knob.tChanged.connect(lambda t, k=key: self._on_knob(k, t))
        knob.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        knob.customContextMenuRequested.connect(
            lambda pos, s=spec, w=knob: self._knob_menu(s, w, pos))
        v.addWidget(knob, 0, Qt.AlignmentFlag.AlignHCenter)

        btn = QPushButton("L")
        btn.setFixedSize(_Knob.W, 16)
        btn.setCheckable(True)
        btn.setToolTip(f"学习：点击后再推一下要绑定到「{spec['label']}」的推杆/旋钮")
        btn.clicked.connect(lambda checked=False, s=spec, b=btn: self._on_learn(s, b, checked))
        v.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)

        self._knobs[key] = knob
        self._learn_btns[key] = btn
        return cell

    def _make_combo_cell(self, spec: dict) -> QWidget:
        key = spec["key"]
        cell = _flat(QWidget())
        v = QVBoxLayout(cell)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        lbl = QLabel(spec["label"])
        lbl.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(lbl, 0, Qt.AlignmentFlag.AlignHCenter)

        combo = QComboBox()
        combo.setFixedWidth(88)
        combo.addItems([str(c) for c in spec["choices"]])
        combo.setCurrentText(str(self._engine.get_param(key)))
        combo.currentTextChanged.connect(lambda txt, k=key: self._on_choice(k, txt))
        v.addWidget(combo, 0, Qt.AlignmentFlag.AlignHCenter)

        self._combos[key] = combo
        return cell

    # ============ 面板回调（主线程）============

    def _fmt_value(self, spec: dict, value) -> str:
        if spec["kind"] == "int":
            text = str(int(value))
        else:
            top = abs(float(spec["max"]))
            text = f"{value:.0f}" if top >= 1000 else (f"{value:.1f}" if top >= 20 else f"{value:.2f}")
        unit = spec.get("unit", "")
        return f"{text} {unit}".strip()

    def _refresh_adsr(self) -> None:
        """把引擎里当前的 A/D/S/R 同步到图示上。"""
        if self._adsr is None:
            return
        try:
            g = self._engine.get_param
            self._adsr.set_params(g("attack"), g("decay"), g("sustain"), g("release"))
        except Exception as exc:
            self.log.warning("包络图示刷新失败: %s", exc)

    def _on_knob(self, key: str, t: int) -> None:
        try:
            spec = _dsp.PARAM_BY_KEY.get(key)
            if spec is None:
                return
            self._engine.set_param(key, _dsp.cc_to_value(key, t))
            knob = self._knobs.get(key)
            if knob is not None:
                knob.set_text(self._fmt_value(spec, self._engine.get_param(key)))
            if spec["group"] == "ENVELOPE":
                self._refresh_adsr()
            self._schedule_save()
        except Exception as exc:
            self.log.warning("旋钮回调失败: %s", exc)

    def _on_choice(self, key: str, text: str) -> None:
        try:
            self._engine.set_param(key, text)
            shape = self._shapes.get(key)
            if shape is not None:
                shape.set_kind(text)
            self._schedule_save()
        except Exception as exc:
            self.log.warning("下拉回调失败: %s", exc)

    def _on_learn(self, spec: dict, btn: QPushButton, checked: bool) -> None:
        try:
            key = spec["key"]
            if not checked:
                if self._learn_key == key:
                    self._learn_key = None
                return
            for k, b in self._learn_btns.items():
                if k != key and b.isChecked():
                    b.setChecked(False)
            self._learn_key = key
            if self._panel is not None:
                self._lbl_midi.setText(
                    f"学习中：请推一下要绑定到「{spec['label']}」的 CC（踏板 CC64 不可绑）…")
        except Exception as exc:
            self.log.warning("学习模式切换失败: %s", exc)

    def _knob_menu(self, spec: dict, knob: _Knob, pos) -> None:
        try:
            key = spec["key"]
            ccs = sorted(c for c, k in self._engine.cc_map.items() if k == key)
            menu = QMenu(knob)
            if ccs:
                act = menu.addAction(f"解绑 CC{ccs[0]}")
                act.triggered.connect(lambda: self._unbind(key))
            else:
                act = menu.addAction("未绑定 CC")
                act.setEnabled(False)
            menu.addSeparator()
            act2 = menu.addAction("恢复默认值")
            act2.triggered.connect(lambda: self._reset_param(key))
            menu.exec(knob.mapToGlobal(pos))
        except Exception as exc:
            self.log.warning("旋钮菜单失败: %s", exc)

    def _unbind(self, key: str) -> None:
        cmap = {c: k for c, k in self._engine.cc_map.items() if k != key}
        self._engine.set_cc_map(cmap)
        if self._panel is not None:
            self._lbl_midi.setText(f"已解绑「{_dsp.PARAM_BY_KEY[key]['label']}」的 CC")
        self._schedule_save()

    def _reset_param(self, key: str) -> None:
        val = _dsp.DEFAULT_PARAMS[key]
        self._engine.set_param(key, val)
        knob = self._knobs.get(key)
        if knob is not None:
            knob.set_t(_dsp.value_to_cc(key, self._engine.get_param(key)), emit=False)
            knob.set_text(self._fmt_value(_dsp.PARAM_BY_KEY[key],
                                          self._engine.get_param(key)))
        combo = self._combos.get(key)
        if combo is not None:
            combo.setCurrentText(str(self._engine.get_param(key)))
        self._refresh_adsr()
        self._schedule_save()

    def _on_panic(self) -> None:
        try:
            self._engine.push_panic()
        except Exception as exc:
            self.log.warning("Panic 失败: %s", exc)

    def _on_reset_all(self) -> None:
        try:
            for key, val in _dsp.DEFAULT_PARAMS.items():
                self._engine.set_param(key, val)
            self._engine.set_cc_map(_dsp.DEFAULT_CC_MAP)
            for key, knob in self._knobs.items():
                knob.set_t(_dsp.value_to_cc(key, self._engine.get_param(key)), emit=False)
                knob.set_text(self._fmt_value(_dsp.PARAM_BY_KEY[key],
                                              self._engine.get_param(key)))
            for key, combo in self._combos.items():
                combo.setCurrentText(str(self._engine.get_param(key)))
            self._refresh_adsr()
            self._save_now()
            if self._panel is not None:
                self._lbl_midi.setText("已恢复默认参数与 CC 映射")
        except Exception as exc:
            self.log.warning("恢复默认失败: %s", exc)

    def _on_enabled(self, on: bool) -> None:
        try:
            self.config["enabled"] = bool(on)
            if not on:
                self._engine.push_panic()
            self._schedule_save()
        except Exception as exc:
            self.log.warning("发声开关失败: %s", exc)

    def _on_channel(self, index: int) -> None:
        try:
            self.config["channel"] = self._combo_ch.itemData(index)
            self._schedule_save()
        except Exception as exc:
            self.log.warning("通道过滤失败: %s", exc)

    def _on_audio_button(self) -> None:
        try:
            if self._audio.ok:
                self._audio.stop()
                self._keep_audio = False        # 手动关了，回到按需模式
                self._btn_audio.setText("启动音频")
            else:
                dev = self._combo_dev.currentData()
                self.config["device"] = dev
                ok = self._audio.start(device=dev)
                self._keep_audio = ok            # 手动开的常驻，不参与空闲释放
                self._btn_audio.setText("停止音频" if ok else "启动音频")
                if not ok:
                    self._lbl_audio.setText(
                        f"音频启动失败：{self._audio.info['last_error']}")
            self._schedule_save()
        except Exception as exc:
            self.log.warning("音频开关失败: %s", exc)