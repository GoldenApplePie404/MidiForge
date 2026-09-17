"""合成 GM 打击乐 wav 采样。纯 Python struct，零依赖。

生成 8 个 16-bit / 44100Hz / 单声道 wav 到指定目录。
"""

import math
import random
import struct
import sys
import wave
from pathlib import Path

SR = 44100  # sample rate


def _write_wav(path: Path, samples: list[float]) -> None:
    """写 16-bit PCM 单声道 wav。samples 值范围 -1.0 ~ 1.0。"""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SR)
        frames = b"".join(
            struct.pack("<h", max(-32768, min(32767, int(s * 32767)))) for s in samples
        )
        wf.writeframes(frames)


def _env(n_samples: int, attack: float = 0.001, decay: float = 0.25,
         sustain: float = 0.0, release: float = 0.05, peak: float = 1.0) -> list[float]:
    """ADSR 包络，返回长度为 n_samples 的系数数组。"""
    a = int(attack * SR)
    d = int(decay * SR)
    s = int(sustain * SR)
    r = int(release * SR)
    total = a + d + s + r
    env = [0.0] * n_samples
    for i in range(n_samples):
        if i < a:
            env[i] = (i / max(1, a)) * peak
        elif i < a + d:
            env[i] = peak - (peak - sustain) * ((i - a) / max(1, d))
        elif i < a + d + s:
            env[i] = sustain
        elif i < a + d + s + r:
            env[i] = sustain * (1 - (i - a - d - s) / max(1, r))
        else:
            env[i] = 0.0
    return env


def kick() -> list[float]:
    """底鼓：低频正弦 80Hz→40Hz 快速下滑。"""
    dur = 0.35
    n = int(dur * SR)
    env = _env(n, attack=0.001, decay=0.25, sustain=0.0, release=0.05, peak=1.0)
    out = []
    for i in range(n):
        t = i / SR
        f = 80 * math.exp(-t * 18) + 40  # 80Hz 滑到 40Hz
        out.append(math.sin(2 * math.pi * f * t) * env[i])
    return out


def snare() -> list[float]:
    """军鼓：白噪声 + 中频 body。"""
    dur = 0.35
    n = int(dur * SR)
    random.seed(42)
    noise = [random.uniform(-1, 1) for _ in range(n)]
    env_noise = _env(n, attack=0.001, decay=0.15, sustain=0.0, release=0.05, peak=0.7)
    # 中频 body（200Hz 正弦）
    body_env = _env(n, attack=0.001, decay=0.2, sustain=0.0, release=0.05, peak=0.4)
    body = [math.sin(2 * math.pi * 200 * i / SR) * body_env[i] for i in range(n)]
    return [noise[i] * env_noise[i] + body[i] for i in range(n)]


def hihat_closed() -> list[float]:
    """闭镲：高频白噪声快速衰减。"""
    dur = 0.08
    n = int(dur * SR)
    random.seed(7)
    noise = [random.uniform(-1, 1) for _ in range(n)]
    env = _env(n, attack=0.0005, decay=0.05, sustain=0.0, release=0.02, peak=0.6)
    # 高通：简单差分近似
    hp = [noise[0]] + [noise[i] - noise[i - 1] for i in range(1, n)]
    return [hp[i] * env[i] for i in range(n)]


def hihat_open() -> list[float]:
    """开镲：高频噪声 + 长衰减。"""
    dur = 0.35
    n = int(dur * SR)
    random.seed(13)
    noise = [random.uniform(-1, 1) for _ in range(n)]
    env = _env(n, attack=0.0005, decay=0.25, sustain=0.0, release=0.1, peak=0.6)
    hp = [noise[0]] + [noise[i] - noise[i - 1] for i in range(1, n)]
    return [hp[i] * env[i] for i in range(n)]


def crash() -> list[float]:
    """吊镲：白噪声 + 超长自然衰减。"""
    dur = 1.2
    n = int(dur * SR)
    random.seed(99)
    noise = [random.uniform(-1, 1) for _ in range(n)]
    env = _env(n, attack=0.001, decay=0.6, sustain=0.0, release=0.4, peak=0.5)
    hp = [noise[0]] + [noise[i] - noise[i - 1] for i in range(1, n)]
    return [hp[i] * env[i] for i in range(n)]


def ride() -> list[float]:
    """节奏镲：白噪声 + 中等衰减 + bell 感。"""
    dur = 0.9
    n = int(dur * SR)
    random.seed(55)
    noise = [random.uniform(-1, 1) for _ in range(n)]
    env = _env(n, attack=0.001, decay=0.4, sustain=0.0, release=0.3, peak=0.45)
    hp = [noise[0]] + [noise[i] - noise[i - 1] for i in range(1, n)]
    # 加一点 bell 泛音（高频正弦）
    bell_env = _env(n, attack=0.0005, decay=0.3, sustain=0.0, release=0.2, peak=0.25)
    bell = [math.sin(2 * math.pi * 4000 * i / SR) * bell_env[i] for i in range(n)]
    return [hp[i] * env[i] + bell[i] for i in range(n)]


def tom(freq_hz: float) -> list[float]:
    """通鼓：指定频率的正弦 body。"""
    dur = 0.4
    n = int(dur * SR)
    env = _env(n, attack=0.002, decay=0.3, sustain=0.0, release=0.05, peak=0.9)
    out = []
    for i in range(n):
        t = i / SR
        f = freq_hz * math.exp(-t * 6) + freq_hz * 0.7
        out.append(math.sin(2 * math.pi * f * t) * env[i])
    return out


def generate_all(out_dir: Path, skip_existing: bool = True) -> None:
    """生成 GM 鼓组采样。skip_existing=True 时已存在的文件不覆盖（保护用户音色）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "pad1_crash.wav": crash(),
        "pad2_tom_left.wav": tom(180),    # 左通鼓 ~180Hz
        "pad3_tom_right.wav": tom(260),   # 右通鼓 ~260Hz
        "pad4_ride.wav": ride(),
        "pad5_hihat_closed.wav": hihat_closed(),
        "pad6_hihat_open.wav": hihat_open(),
        "pad7_snare.wav": snare(),
        "pad8_kick.wav": kick(),
    }
    written = 0
    skipped = 0
    for name, samples in files.items():
        path = out_dir / name
        if skip_existing and path.exists():
            skipped += 1
            continue
        _write_wav(path, samples)
        written += 1
        print(f"  ✓ {name:28s} {len(samples)/SR:.2f}s")
    if skipped:
        print(f"  · 跳过 {skipped} 个已存在文件（保护用户音色）")
    print(f"完成：生成 {written} / 跳过 {skipped}")


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
    print(f"生成 GM 鼓组采样 → {d}")
    generate_all(d)
    print("完成")
