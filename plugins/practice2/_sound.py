"""音色合成 — 为每个琴键生成一段自然衰减的 wav 采样。

仿 Practice2 的"多种音色"思路，但不用采样库（体积太大），
而是首次用到某个音时用 numpy 现场合成，存成 wav 缓存：

    sounds/grand/n60.wav      三角钢琴
    sounds/electric/n60.wav   电钢琴
    sounds/musicbox/n60.wav   八音盒

每个文件只在第一次用到时生成一次（幂等），之后直接加载。
力度（velocity）不做进采样——播放时按 (vel/127)^1.35 调音量，
公式与 Practice2 的 gain 曲线一致。

注意：写 wav 必须用 wave 模块（项目教训——手写 struct 头会错位）。
"""

import math
import wave
from pathlib import Path

import numpy as np

SR = 44100
NOTE_MIN = 21    # A0
NOTE_MAX = 108   # C8

TIMBRES = ("grand", "electric", "musicbox")
TIMBRE_LABELS = {"grand": "三角钢琴", "electric": "电钢琴", "musicbox": "八音盒"}
# 播放时的音色基础增益（合成时已归一化，这里是播放链路上的平衡）。
# 合成峰值 0.55 是为多音叠加留 headroom（pygame 混音超 1.0 会削波成噪声），
# 增益相应调高补偿单音响度。
TIMBRE_GAIN = {"grand": 1.0, "electric": 0.95, "musicbox": 0.9}

# 各音色的时长范围（低音长、高音短），单位秒
_DUR = {"grand": (4.5, 1.4), "electric": (3.5, 1.2), "musicbox": (2.2, 1.1)}


def pitch_freq(p: int) -> float:
    """MIDI 音号 → 频率（Hz），69 = A4 = 440。"""
    return 440.0 * 2 ** ((p - 69) / 12.0)


def _dur_for(timbre: str, pitch: int) -> float:
    lo, hi = _DUR[timbre]
    t = (pitch - NOTE_MIN) / (NOTE_MAX - NOTE_MIN)
    return lo + (hi - lo) * t


def _attack(out: np.ndarray, sec: float) -> None:
    """起音淡入，防止"啪"的爆音。"""
    n = min(len(out), max(1, int(sec * SR)))
    out[:n] *= np.linspace(0.0, 1.0, n)


def _finish(out: np.ndarray) -> np.ndarray:
    """尾部淡出 + 峰值归一化，输出 int16。

    峰值压到 0.55 给多音叠加留 headroom：pygame 混音是 int16 饱和加法，
    采样满幅的话三四个音同响就削波成噪声。
    """
    n = min(len(out), int(0.12 * SR))
    if n > 0:
        out[-n:] *= np.linspace(1.0, 0.0, n)
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * 0.55 * 32767).astype(np.int16)


def _synth_grand(f: float, dur: float) -> np.ndarray:
    """三角钢琴：加法合成，分音带轻微失谐（弦的真实非谐性），高泛音衰减更快。"""
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    inharm = 0.00015
    for k in range(1, 11):
        fk = f * k * math.sqrt(1 + inharm * k * k)
        if fk > 16000:
            break
        amp = k ** -1.8
        tau = dur / (1 + 0.45 * (k - 1))
        out += amp * np.sin(2 * np.pi * fk * t + 0.3 * k) * np.exp(-t / tau)
    # 锤击噪声：3ms 一闪而过的宽带脉冲，让起音更"实"
    n = int(0.003 * SR)
    rng = np.random.default_rng(int(f) + 21)
    out[:n] += rng.uniform(-1, 1, n) * 0.12 * np.linspace(1, 0, n)
    _attack(out, 0.008)
    return _finish(out)


def _synth_electric(f: float, dur: float) -> np.ndarray:
    """电钢琴：正弦琴体 + 快衰减的"叮"泛音（tine），DX 电钢的味道。"""
    t = np.arange(int(dur * SR)) / SR
    out = np.sin(2 * np.pi * f * t) * np.exp(-t / (dur * 0.8))
    out += 0.4 * np.sin(2 * np.pi * f * 2.003 * t) * np.exp(-t / (dur * 0.45))
    out += 0.3 * np.sin(2 * np.pi * f * 3.97 * t) * np.exp(-t / (dur * 0.10))
    if f * 13.3 < 16000:
        out += 0.12 * np.sin(2 * np.pi * f * 13.3 * t) * np.exp(-t / (dur * 0.05))
    _attack(out, 0.003)
    return _finish(out)


def _synth_musicbox(f: float, dur: float) -> np.ndarray:
    """八音盒：非谐波分音（钟/梳齿发声物理），亮而短促，加一点失谐闪烁。"""
    t = np.arange(int(dur * SR)) / SR
    partials = [(1.0, 1.0, 1.0), (1.0035, 0.5, 0.9), (3.36, 0.35, 0.30),
                (5.9, 0.18, 0.15), (8.7, 0.08, 0.08)]
    out = np.zeros_like(t)
    for ratio, amp, tau_r in partials:
        fk = f * ratio
        if fk > 16000:
            continue
        out += amp * np.sin(2 * np.pi * fk * t) * np.exp(-t / (dur * tau_r))
    _attack(out, 0.002)
    return _finish(out)


_SYNTH = {"grand": _synth_grand, "electric": _synth_electric, "musicbox": _synth_musicbox}


def ensure_click(sounds_dir: Path) -> tuple:
    """预备拍 click 音（hi=第一拍重音, lo=普通拍）。返回 (hi_path, lo_path)。"""
    out = []
    for name, freq in (("click_hi.wav", 1320.0), ("click_lo.wav", 880.0)):
        path = Path(sounds_dir) / name
        if not (path.exists() and path.stat().st_size > 44):
            path.parent.mkdir(parents=True, exist_ok=True)
            t = np.arange(int(0.09 * SR)) / SR
            data = np.sin(2 * np.pi * freq * t) * np.exp(-t / 0.03)
            pcm = (data / max(float(np.max(np.abs(data))), 1.0) * 0.6 * 32767).astype(np.int16)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(pcm.tobytes())
        out.append(path)
    return tuple(out)


def wav_path(sounds_dir: Path, timbre: str, pitch: int) -> Path:
    # _s2 = 合成参数 v2（峰值 0.85→0.55 留多音 headroom），自动废弃旧缓存
    return Path(sounds_dir) / timbre / f"n{pitch}_s2.wav"


def ensure_sample(sounds_dir: Path, timbre: str, pitch: int) -> Path:
    """确保某个键的采样存在，不存在就现场合成一个。返回 wav 路径。"""
    path = wav_path(sounds_dir, timbre, pitch)
    if path.exists() and path.stat().st_size > 44:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    dur = _dur_for(timbre, pitch)
    data = _SYNTH[timbre](pitch_freq(pitch), dur)
    with wave.open(str(path), "wb") as w:   # wave 模块保证头正确（项目教训）
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())
    return path
