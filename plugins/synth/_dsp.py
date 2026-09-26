"""SynthLab DSP 内核 —— 纯 numpy 实时减法合成（不依赖 Qt，也不依赖宿主）。

=== 线程模型（关键） ===
    主线程 / MIDI 线程：只调用 push_note_on() / push_cc() / push_pitch_bend() 等，
        事件进 queue.Queue（线程安全），不碰音频状态。
    音频回调线程：调用 process(outdata)，块首一次性取空事件队列。
    参数：普通 dict 存 Python float/int（主线程写、回调线程读，GIL 下原子），无锁。
    process() 内不分配长生命周期对象，临时数组复用。

=== 信号链（每块 N 个采样） ===
    事件 → 声部分配 → 每声部 2 振荡器（PolyBLEP 抗混叠）
        → 每声部独立线性 ADSR → 混音 → 全局滤波包络 + LFO
        → TPT 状态变量低通（逐采样标量递归）→ 主音量 → 软削波 → 立体声
"""

from __future__ import annotations

import math
import queue

import numpy as np

WAVES = ("saw", "square", "triangle", "sine")

# 声部包络阶段
STAGE_IDLE, STAGE_ATT, STAGE_DEC, STAGE_SUS, STAGE_REL = 0, 1, 2, 3, 4

# ---- 参数表：UI 旋钮 / config 持久化 / CC 映射全部从这里派生 ----
# kind: choice | float | int      curve: lin | log（log 用于频率/时间类参数）
PARAM_SPECS = (
    # OSC1
    {"key": "osc1_wave", "label": "波形", "group": "OSC1", "kind": "choice",
     "choices": WAVES, "default": "saw"},
    {"key": "osc1_level", "label": "电平", "group": "OSC1", "kind": "float",
     "min": 0.0, "max": 1.0, "default": 0.8, "curve": "lin"},
    {"key": "osc1_detune", "label": "失谐", "group": "OSC1", "kind": "float",
     "min": -50.0, "max": 50.0, "default": 0.0, "curve": "lin", "unit": "ct"},
    {"key": "osc1_octave", "label": "八度", "group": "OSC1", "kind": "int",
     "min": -2, "max": 2, "default": 0},
    {"key": "osc1_semi", "label": "半音", "group": "OSC1", "kind": "int",
     "min": -12, "max": 12, "default": 0},
    # OSC2
    {"key": "osc2_wave", "label": "波形", "group": "OSC2", "kind": "choice",
     "choices": WAVES, "default": "square"},
    {"key": "osc2_level", "label": "电平", "group": "OSC2", "kind": "float",
     "min": 0.0, "max": 1.0, "default": 0.35, "curve": "lin"},
    {"key": "osc2_detune", "label": "失谐", "group": "OSC2", "kind": "float",
     "min": -50.0, "max": 50.0, "default": 0.0, "curve": "lin", "unit": "ct"},
    {"key": "osc2_octave", "label": "八度", "group": "OSC2", "kind": "int",
     "min": -2, "max": 2, "default": 0},
    {"key": "osc2_semi", "label": "半音", "group": "OSC2", "kind": "int",
     "min": -12, "max": 12, "default": 0},
    # FILTER
    {"key": "cutoff", "label": "Cutoff", "group": "FILTER", "kind": "float",
     "min": 20.0, "max": 18000.0, "default": 1600.0, "curve": "log",
     "unit": "Hz", "cc": 74},
    {"key": "reso", "label": "Resonance", "group": "FILTER", "kind": "float",
     "min": 0.5, "max": 12.0, "default": 1.2, "curve": "lin", "cc": 71},
    {"key": "filter_env", "label": "包络量", "group": "FILTER", "kind": "float",
     "min": -1.0, "max": 1.0, "default": 0.45, "curve": "lin"},
    {"key": "keytrack", "label": "键盘跟踪", "group": "FILTER", "kind": "float",
     "min": 0.0, "max": 1.0, "default": 0.3, "curve": "lin"},
    # ENVELOPE
    {"key": "attack", "label": "Attack", "group": "ENVELOPE", "kind": "float",
     "min": 0.001, "max": 5.0, "default": 0.01, "curve": "log",
     "unit": "s", "cc": 73},
    {"key": "decay", "label": "Decay", "group": "ENVELOPE", "kind": "float",
     "min": 0.001, "max": 5.0, "default": 0.35, "curve": "log", "unit": "s"},
    {"key": "sustain", "label": "Sustain", "group": "ENVELOPE", "kind": "float",
     "min": 0.0, "max": 1.0, "default": 0.6, "curve": "lin"},
    {"key": "release", "label": "Release", "group": "ENVELOPE", "kind": "float",
     "min": 0.005, "max": 8.0, "default": 0.25, "curve": "log",
     "unit": "s", "cc": 72},
    # LFO
    {"key": "lfo_wave", "label": "波形", "group": "LFO", "kind": "choice",
     "choices": ("sine", "triangle"), "default": "sine"},
    {"key": "lfo_rate", "label": "速率", "group": "LFO", "kind": "float",
     "min": 0.05, "max": 20.0, "default": 4.0, "curve": "log",
     "unit": "Hz", "cc": 76},
    {"key": "lfo_depth", "label": "深度", "group": "LFO", "kind": "float",
     "min": 0.0, "max": 1.0, "default": 0.0, "curve": "lin", "cc": 1},
    {"key": "lfo_target", "label": "目标", "group": "LFO", "kind": "choice",
     "choices": ("cutoff", "pitch", "amp"), "default": "cutoff"},
    # MASTER
    {"key": "volume", "label": "主音量", "group": "MASTER", "kind": "float",
     "min": 0.0, "max": 1.0, "default": 0.7, "curve": "lin", "cc": 7},
    {"key": "polyphony", "label": "复音数", "group": "MASTER", "kind": "int",
     "min": 1, "max": 16, "default": 8},
    {"key": "bend_range", "label": "弯音范围", "group": "MASTER", "kind": "float",
     "min": 0.0, "max": 24.0, "default": 2.0, "curve": "lin", "unit": "st"},
)

PARAM_BY_KEY = {s["key"]: s for s in PARAM_SPECS}
PARAM_GROUPS = tuple(dict.fromkeys(s["group"] for s in PARAM_SPECS))
DEFAULT_PARAMS = {s["key"]: s["default"] for s in PARAM_SPECS}

# 预置 CC 映射（通用 Sound Controller 号；实际推杆号用面板"学习"按钮校准）
DEFAULT_CC_MAP = {int(s["cc"]): s["key"] for s in PARAM_SPECS if s.get("cc") is not None}

SUSTAIN_CC = 64


# ============================================================
# 参数值 ↔ CC 值（0-127）换算
# ============================================================

def value_to_cc(key: str, value: float) -> int:
    """把参数值映射到 0-127（供界面回显）。"""
    spec = PARAM_BY_KEY.get(key)
    if spec is None or spec["kind"] == "choice":
        return 0
    lo, hi = float(spec["min"]), float(spec["max"])
    if spec.get("curve") == "log":
        v = max(lo, min(hi, float(value)))
        t = math.log(v / lo) / math.log(hi / lo) if hi > lo > 0 else 0.0
    else:
        t = (float(value) - lo) / (hi - lo) if hi > lo else 0.0
    return int(round(max(0.0, min(1.0, t)) * 127))


def cc_to_value(key: str, cc_value: int) -> float:
    """把 0-127 映射回参数值。"""
    spec = PARAM_BY_KEY.get(key)
    if spec is None or spec["kind"] == "choice":
        return 0.0
    lo, hi = float(spec["min"]), float(spec["max"])
    t = max(0.0, min(1.0, float(cc_value) / 127.0))
    if spec["kind"] == "int":
        return int(round(lo + (hi - lo) * t))
    if spec.get("curve") == "log":
        return lo * (hi / lo) ** t if hi > lo > 0 else lo
    return lo + (hi - lo) * t


def clamp_param(key: str, value):
    """把值夹到 spec 范围内并做类型规整。"""
    spec = PARAM_BY_KEY.get(key)
    if spec is None:
        return value
    if spec["kind"] == "choice":
        return value if value in spec["choices"] else spec["default"]
    lo, hi = float(spec["min"]), float(spec["max"])
    if spec["kind"] == "int":
        return int(max(lo, min(hi, int(round(float(value))))))
    v = max(lo, min(hi, float(value)))
    return v if math.isfinite(v) else spec["default"]


# ============================================================
# 波形 / 包络 工具
# ============================================================

def _polyblep(t, dt):
    """PolyBLEP 抗混叠修正量。t: 相位 [0,1)；dt: 每采样相位增量。支持广播。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        tl = t / dt
        left = 2.0 * tl - tl * tl - 1.0
        tr = (t - 1.0) / dt
        right = tr * tr + 2.0 * tr + 1.0
    return np.where(t < dt, left, np.where(t > 1.0 - dt, right, 0.0))


def _wave(kind: str, phase, dt):
    """按相位生成波形。phase/dt 形状需可广播。"""
    if kind == "sine":
        return np.sin(2.0 * math.pi * phase)
    if kind == "saw":
        return 2.0 * phase - 1.0 - _polyblep(phase, dt)
    if kind == "square":
        sq = np.where(phase < 0.5, 1.0, -1.0)
        sq = sq + _polyblep(phase, dt) - _polyblep((phase + 0.5) % 1.0, dt)
        return sq
    # triangle：无阶跃跳变，朴素实现已足够
    return 4.0 * np.abs(phase - 0.5) - 1.0


def _env_segments(level, t, stage, n, att, dec, sus, rel, out):
    """生成 n 点线性 ADSR 包络（块内可跨阶段）。

    level/t/stage 是标量状态，返回更新后的 (level, t, stage)。
    out 是长度 ≥ n 的缓冲区，函数写入 out[:n]。
    """
    i = 0
    while i < n:
        if stage == STAGE_IDLE:
            out[i:n] = 0.0
            return 0.0, 0.0, STAGE_IDLE
        if stage == STAGE_SUS:
            out[i:n] = sus
            return sus, 0.0, STAGE_SUS

        if stage == STAGE_ATT:
            dur, target, nxt = att, 1.0, STAGE_DEC
        elif stage == STAGE_DEC:
            dur, target, nxt = dec, sus, STAGE_SUS
        else:  # STAGE_REL
            dur, target, nxt = rel, 0.0, STAGE_IDLE

        remain = dur - t
        if remain <= 1e-9:
            level, stage, t = target, nxt, 0.0
            continue
        seg = int(min(n - i, remain))
        if seg <= 0:
            break
        end = level + (target - level) * (seg / remain)
        out[i:i + seg] = np.linspace(level, end, seg, endpoint=False)
        level, t, i = end, t + seg, i + seg
    return level, t, stage


# ============================================================
# 合成引擎
# ============================================================

# 面板示波器的取样窗口长度（采样数）。1024 @44.1k ≈ 23ms，够看清低音区的波形形状。
SCOPE_LEN = 1024


class SynthEngine:
    """实时减法合成引擎。所有 push_* 线程安全；process() 仅在音频线程调用。"""

    def __init__(self, sample_rate: int = 44100, block_size: int = 256,
                 max_voices: int = 16):
        self.sample_rate = int(sample_rate)
        self.block_size = int(block_size)
        self.max_voices = int(max_voices)

        self.params = dict(DEFAULT_PARAMS)
        self.cc_map = dict(DEFAULT_CC_MAP)

        self._events: "queue.Queue[tuple]" = queue.Queue()

        v, n = self.max_voices, self.block_size
        self._ramp = np.arange(1, n + 1, dtype=np.float64)

        # 示波器取样：环形缓冲，process() 末尾写入最新一块，面板读快照画图
        self.scope = np.zeros(SCOPE_LEN, dtype=np.float32)
        self._scope_w = 0

        # 声部状态
        self._stage = np.zeros(v, dtype=np.int8)
        self._held = np.zeros(v, dtype=bool)
        self._pending = np.zeros(v, dtype=bool)     # 延音踏板挂起的 note_off
        self._note = np.zeros(v, dtype=np.int16) - 1
        self._vel = np.zeros(v, dtype=np.float64)
        self._t = np.zeros(v, dtype=np.float64)
        self._level = np.zeros(v, dtype=np.float64)
        self._ph1 = np.zeros(v, dtype=np.float64)
        self._ph2 = np.zeros(v, dtype=np.float64)
        self._age = np.zeros(v, dtype=np.int64)
        self._age_counter = 0

        # 全局状态
        self._bend = 0.0
        self._sustain = False
        self._last_note = 60
        self._f_stage = STAGE_IDLE
        self._f_t = 0.0
        self._f_level = 0.0
        self._lfo_phase = 0.0
        self._ic1 = 0.0
        self._ic2 = 0.0

        # 复用缓冲
        self._env_buf = np.zeros(n, dtype=np.float64)
        self._f_env_buf = np.zeros(n, dtype=np.float64)

        # 观测
        self.peak = 0.0
        self.dropouts = 0

    # ---- 参数 ----

    def set_sample_rate(self, sr: int) -> None:
        self.sample_rate = int(sr)

    def set_param(self, key: str, value) -> None:
        if key not in PARAM_BY_KEY:
            return
        val = clamp_param(key, value)
        if key == "polyphony":
            poly = int(val)
            # 超出复音上限的声部直接释放，避免"卡住不出声也不释放"
            for vi in range(poly, self.max_voices):
                if self._stage[vi] != STAGE_IDLE:
                    self._stage[vi] = STAGE_REL
                    self._t[vi] = 0.0
        self.params[key] = val

    def get_param(self, key: str):
        return self.params.get(key, DEFAULT_PARAMS.get(key))

    def set_cc_map(self, cc_map: dict) -> None:
        clean = {}
        for cc, key in dict(cc_map or {}).items():
            try:
                cc_i = int(cc)
            except (TypeError, ValueError):
                continue
            if 0 <= cc_i <= 127 and cc_i != SUSTAIN_CC and key in PARAM_BY_KEY \
                    and PARAM_BY_KEY[key]["kind"] != "choice":
                clean[cc_i] = key
        self.cc_map = clean

    # ---- 事件入队（线程安全）----

    def push_note_on(self, note: int, velocity: int = 100) -> None:
        self._events.put(("on", int(note), int(velocity)))

    def push_note_off(self, note: int) -> None:
        self._events.put(("off", int(note)))

    def push_pitch_bend(self, normalized: float) -> None:
        self._events.put(("bend", float(normalized)))

    def push_sustain(self, on: bool) -> None:
        self._events.put(("sus", bool(on)))

    def push_panic(self) -> None:
        self._events.put(("panic",))

    # ---- 状态查询（UI 只读）----

    @property
    def active_voices(self) -> int:
        return int(np.count_nonzero(self._stage != STAGE_IDLE))

    @property
    def sustain_on(self) -> bool:
        return self._sustain

    # ---- 内部：事件处理 ----

    def _drain_events(self) -> None:
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                return
            kind = ev[0]
            if kind == "on":
                self._note_on(ev[1], ev[2])
            elif kind == "off":
                self._note_off(ev[1])
            elif kind == "bend":
                self._bend = max(-1.0, min(1.0, ev[1]))
            elif kind == "sus":
                self._set_sustain(ev[1])
            elif kind == "panic":
                self._panic()

    def _note_on(self, note: int, velocity: int) -> None:
        poly = max(1, min(int(self.params["polyphony"]), self.max_voices))
        same = np.nonzero((self._note == note) & (self._stage != STAGE_IDLE))[0]
        if same.size:
            vi = int(same[0])                      # 同音重触发
        else:
            free = np.nonzero(self._stage[:poly] == STAGE_IDLE)[0]
            if free.size:
                vi = int(free[0])
            else:                                   # 抢占最老的声部
                vi = int(np.argmin(self._age[:poly]))

        self._age_counter += 1
        self._age[vi] = self._age_counter
        self._note[vi] = note
        self._vel[vi] = max(0.05, min(1.0, velocity / 127.0))
        self._stage[vi] = STAGE_ATT
        self._t[vi] = 0.0
        self._level[vi] = 0.0
        self._held[vi] = True
        self._pending[vi] = False
        self._ph1[vi] = 0.0
        self._ph2[vi] = 0.0
        self._last_note = note
        # 触发全局滤波包络
        self._f_stage = STAGE_ATT
        self._f_t = 0.0
        self._f_level = 0.0

    def _note_off(self, note: int) -> None:
        for vi in np.nonzero((self._note == note) & self._held)[0]:
            vi = int(vi)
            self._held[vi] = False
            if self._sustain:
                self._pending[vi] = True            # 等踏板松开再放
            else:
                self._release(vi)

    def _release(self, vi: int) -> None:
        if self._stage[vi] != STAGE_IDLE:
            self._stage[vi] = STAGE_REL
            self._t[vi] = 0.0

    def _set_sustain(self, on: bool) -> None:
        self._sustain = on
        if not on:
            for vi in np.nonzero(self._pending & (self._stage != STAGE_IDLE))[0]:
                vi = int(vi)
                self._pending[vi] = False
                self._release(vi)

    def _panic(self) -> None:
        self._stage[:] = STAGE_IDLE
        self._level[:] = 0.0
        self._pending[:] = False
        self._held[:] = False
        self._f_stage = STAGE_IDLE
        self._f_level = 0.0
        self._ic1 = self._ic2 = 0.0

    # ---- 内部：滤波 ----

    def _svf(self, x, cut) -> np.ndarray:
        """Cytomic TPT 两极点状态变量低通（低通输出 v2）。逐采样标量递归。"""
        sr = float(self.sample_rate)
        k = 1.0 / max(0.5, float(self.params["reso"]))
        xs = x.tolist()
        cs = cut.tolist()
        ys = [0.0] * len(xs)
        ic1, ic2 = self._ic1, self._ic2
        pi = math.pi
        for i in range(len(xs)):
            g = math.tan(pi * cs[i] / sr)
            a1 = 1.0 / (1.0 + g * (g + k))
            a2 = g * a1
            a3 = g * a2
            v3 = xs[i] - ic2
            v1 = a1 * ic1 + a2 * v3
            v2 = ic2 + a2 * ic1 + a3 * v3
            ic1 = 2.0 * v1 - ic1
            ic2 = 2.0 * v2 - ic2
            ys[i] = v2
        self._ic1, self._ic2 = ic1, ic2
        return np.asarray(ys)

    # ---- 渲染 ----

    def process(self, outdata: np.ndarray) -> None:
        """渲染一块音频到 outdata（shape (N, 2)，float32）。异常一律不抛出。"""
        frames = int(outdata.shape[0])
        n = min(frames, self.block_size)
        if n <= 0:
            return
        if frames > n:
            outdata[n:] = 0.0

        self._drain_events()
        p = self.params
        sr = float(self.sample_rate)
        ramp = self._ramp[:n]
        nyq = min(18000.0, sr * 0.45)

        # ---- 1) LFO（整块向量化）----
        lfo_rate = float(p["lfo_rate"])
        lfo_depth = float(p["lfo_depth"])
        lfo_target = p["lfo_target"]
        ph = (self._lfo_phase + (lfo_rate / sr) * ramp) % 1.0
        self._lfo_phase = float(ph[-1])
        if p["lfo_wave"] == "triangle":
            lfo = 4.0 * np.abs(ph - 0.5) - 1.0
        else:
            lfo = np.sin(2.0 * math.pi * ph)

        # ---- 2) 声部渲染 ----
        poly = max(1, min(int(p["polyphony"]), self.max_voices))
        active = np.nonzero(self._stage[:poly] != STAGE_IDLE)[0]
        mix = np.zeros(n, dtype=np.float64)

        att = max(1.0, float(p["attack"]) * sr)
        dec = max(1.0, float(p["decay"]) * sr)
        rel = max(1.0, float(p["release"]) * sr)
        sus = float(p["sustain"])

        if active.size:
            bend_mul = 2.0 ** (self._bend * float(p["bend_range"]) / 12.0)
            w1_kind, w2_kind = p["osc1_wave"], p["osc2_wave"]
            lvl1, lvl2 = float(p["osc1_level"]), float(p["osc2_level"])
            sc1 = 2.0 ** (int(p["osc1_octave"]) + int(p["osc1_semi"]) / 12.0
                          + float(p["osc1_detune"]) / 1200.0)
            sc2 = 2.0 ** (int(p["osc2_octave"]) + int(p["osc2_semi"]) / 12.0
                          + float(p["osc2_detune"]) / 1200.0)
            if lfo_target == "pitch":
                pitch_mod = 2.0 ** (lfo * lfo_depth * 2.0 / 12.0)   # ±2 半音
            else:
                pitch_mod = None

            env = self._env_buf
            for vi in active:
                vi = int(vi)
                f0 = min(max(440.0 * 2.0 ** ((int(self._note[vi]) - 69) / 12.0) * bend_mul,
                           8.0), nyq)
                d1 = min(max(f0 * sc1, 8.0), nyq) / sr
                d2 = min(max(f0 * sc2, 8.0), nyq) / sr

                if pitch_mod is None:
                    ph1 = (self._ph1[vi] + d1 * ramp) % 1.0
                    ph2 = (self._ph2[vi] + d2 * ramp) % 1.0
                else:
                    ph1 = (self._ph1[vi] + np.cumsum(d1 * pitch_mod)) % 1.0
                    ph2 = (self._ph2[vi] + np.cumsum(d2 * pitch_mod)) % 1.0
                    d1 = d1 * pitch_mod
                    d2 = d2 * pitch_mod

                self._ph1[vi] = float(ph1[-1])
                self._ph2[vi] = float(ph2[-1])

                lvl, t, st = _env_segments(
                    float(self._level[vi]), float(self._t[vi]), int(self._stage[vi]),
                    n, att, dec, sus, rel, env)
                self._level[vi], self._t[vi], self._stage[vi] = lvl, t, st

                vel = float(self._vel[vi])
                if lvl1 > 0.0:
                    mix += _wave(w1_kind, ph1, d1) * (lvl1 * vel * env)
                if lvl2 > 0.0:
                    mix += _wave(w2_kind, ph2, d2) * (lvl2 * vel * env)

            mix *= 0.32          # 声部增益；多声部靠末尾 tanh 软削波兜底

        # ---- 3) 全局滤波包络 + 截止频率 ----
        flvl, ft, fst = _env_segments(
            self._f_level, self._f_t, self._f_stage, n, att, dec, sus, rel,
            self._f_env_buf)
        self._f_level, self._f_t, self._f_stage = flvl, ft, fst

        cut = np.full(n, float(p["cutoff"]), dtype=np.float64)
        kt = float(p["keytrack"])
        if kt > 0.0:
            cut *= 2.0 ** (kt * (self._last_note - 60) / 12.0)
        fe = float(p["filter_env"])
        if abs(fe) > 1e-6:
            cut *= 2.0 ** (fe * self._f_env_buf[:n] * 4.0)
        if lfo_target == "cutoff" and lfo_depth > 0.0:
            cut *= 2.0 ** (lfo * lfo_depth * 3.0)
        np.clip(cut, 20.0, nyq, out=cut)

        y = self._svf(mix, cut)

        # ---- 4) tremolo / 主音量 / 软削波 / 输出 ----
        if lfo_target == "amp" and lfo_depth > 0.0:
            y *= (1.0 - lfo_depth * (0.5 - 0.5 * lfo))
        y *= float(p["volume"])
        if not np.all(np.isfinite(y)):
            self._panic()
            self.dropouts += 1
            outdata[:n] = 0.0
            return
        y = np.tanh(y * 1.2) * 0.92
        self.peak = float(np.max(np.abs(y))) if n else 0.0
        outdata[:n, 0] = y
        outdata[:n, 1] = y

        # ---- 5) 示波器取样（只写环形缓冲，不影响信号链）----
        self._scope_tap(y, n)

    def _scope_tap(self, y: np.ndarray, n: int) -> None:
        """把最新一块输出写进环形缓冲。wrap 时切成两段拷贝，不分配临时数组。"""
        w = self._scope_w
        end = w + n
        if end <= SCOPE_LEN:
            self.scope[w:end] = y[:n]
        else:
            first = SCOPE_LEN - w
            self.scope[w:] = y[:first]
            self.scope[:end - SCOPE_LEN] = y[first:n]
        self._scope_w = end % SCOPE_LEN

    def scope_snapshot(self) -> np.ndarray:
        """返回最近 SCOPE_LEN 个输出采样（旧 → 新），供面板示波器画图。

        音频线程在写、面板在主线程读，最坏情况读到一帧混叠——对示波器显示而言
        无所谓，不值得为此在音频回调里加锁。
        """
        w = self._scope_w
        return np.concatenate((self.scope[w:], self.scope[:w]))


if __name__ == "__main__":
    # 离线自检：渲染约 1 秒 C 大三和弦，检查输出与包络释放
    eng = SynthEngine()
    eng.push_note_on(60, 100)
    eng.push_note_on(64, 90)
    eng.push_note_on(67, 80)
    buf = np.zeros((eng.block_size, 2), dtype=np.float32)
    peak = 0.0
    for blk in range(int(44100 / eng.block_size)):
        if blk == 40:
            eng.push_note_off(64)
        eng.process(buf)
        peak = max(peak, float(np.max(np.abs(buf))))
    print(f"peak={peak:.3f} voices={eng.active_voices} dropouts={eng.dropouts}")
    assert peak > 0.01 and eng.dropouts == 0
    print("OK")