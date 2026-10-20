"""统一音频服务 — 封装 pygame.mixer，解决多插件冲突。

=== 为什么要统一音频 ===
    1. pygame.mixer.init() 只能调一次——多个插件各自 init 会崩。
    2. 主音量控制应该全局生效，不该每个插件自己管。
    3. 以后换后端（sounddevice / miniaudio），插件代码不用改。

=== 插件使用 ===
    class MyPlugin(Plugin):
        def on_activate(self, app):
            audio = app.audio               # _AudioService 实例
            audio.init_if_needed()          # 确保已初始化
            sfx = audio.load("kick.wav")    # 预加载
            audio.play(sfx, channel_pool=4) # round-robin 4 条 channel
            audio.set_master_volume(0.8)    # 全局音量
"""

import os
import threading
from pathlib import Path
from typing import Optional

try:
    import pygame
    _PYGAME_AVAILABLE = True
except ImportError:
    _PYGAME_AVAILABLE = False


class _SfxHandle:
    """预加载的音效句柄。"""

    def __init__(self, path: str, pygame_sound):
        self.path = path
        self._sound = pygame_sound
        self.sound = pygame_sound   # 公有引用：池复用后验证通道归属用
        self.duration = pygame_sound.get_length() if pygame_sound else 0.0

    def play(self, channel=None, volume: float = 1.0) -> Optional[int]:
        """播放。channel=None 时从全局池自动分配；返回 channel 索引或 None。"""
        if not self._sound:
            return None
        self._sound.set_volume(volume)
        if channel is not None:
            ch = pygame.mixer.Channel(channel)
            ch.play(self._sound)
            return channel
        ch = self._sound.play()
        return ch.get_id() if ch else None

    def stop(self):
        if self._sound:
            self._sound.stop()


class _AudioService:
    """宿主统一音频服务。单例，宿主在 AppContext 里创建一次。"""

    def __init__(self, sample_rate: int = 44100, channels: int = 32, buffer: int = 1024):
        self._sample_rate = sample_rate
        self._channels = channels
        self._buffer = buffer
        self._inited = False
        self._lock = threading.Lock()
        self._master_volume = 1.0
        # 初始化失败的真实原因（空串=正常）。UI/日志用它替代"静默失败"。
        self.last_error = ""
        # 按插件名分组的 channel 池状态
        self._pools: dict = {}  # plugin_name → {"base": int, "count": int, "idx": int}
        # 下一个可用的 base channel（避免池重叠）
        self._next_base = 0
        # 无池全局播放的 round-robin 索引
        self._global_idx = 0

    # ---- 初始化 ----

    def init_if_needed(self) -> bool:
        """延迟初始化——pygame 不在也不会崩。

        依次尝试三档配置，失败原因写进 last_error：
            1. 默认驱动（WASAPI，音质最好）
            2. WinMM（waveOut）
            3. DirectSound
        为什么需要后面两档：实测本机 SDL 的 WASAPI 后端稳定失败
        （"WASAPI can't initialize audio client"），不降级的话 pad_sentry 这类
        插件会全军覆没，且只会看到一堆含糊的"采样加载失败"。
        WinMM 排在 DirectSound 之前是因为实测：WinMM 初始化后 PortAudio
        （synth 插件用的）仍能正常开 MME 流，而 DirectSound 会把输出设备占住，
        导致 PortAudio 的 MME/WASAPI 全部开不了流——两败俱伤。
        """
        if self._inited:
            return True
        if not _PYGAME_AVAILABLE:
            self.last_error = "pygame 不可用"
            return False
        with self._lock:
            if self._inited:
                return True
            errors = []
            for driver in ("", "winmm", "directsound"):
                try:
                    pygame.mixer.quit()
                except Exception:
                    pass
                if driver:
                    os.environ["SDL_AUDIODRIVER"] = driver
                else:
                    os.environ.pop("SDL_AUDIODRIVER", None)
                try:
                    pygame.mixer.init(self._sample_rate, -16, 2, self._buffer)
                    pygame.mixer.set_num_channels(self._channels)
                except Exception as exc:
                    errors.append(f"{driver or 'default'}: {exc}")
                    continue
                self._inited = True
                self.last_error = ""
                return True
            self.last_error = " | ".join(errors)
            return False

    @property
    def inited(self) -> bool:
        return self._inited

    # ---- 主音量 ----

    @property
    def master_volume(self) -> float:
        return self._master_volume

    def set_master_volume(self, vol: float) -> None:
        """全局主音量 0.0 ~ 1.0。"""
        self._master_volume = max(0.0, min(1.0, vol))

    # ---- Channel 池 ----

    def allocate_pool(self, plugin_name: str, channel_count: int = 4) -> dict:
        """为一个插件分配独占的 channel 池，返回 {"base": start_idx, "count": N}。

        空间不够时自动扩容 mixer（只增不减），不再回绕——
        旧行为回绕会踩到前面插件池的通道，两池互相抢音（表现为
        打鼓打断钢琴延音、钢琴打断鼓声，密集时像随机丢音）。
        """
        with self._lock:
            if plugin_name in self._pools:
                return self._pools[plugin_name]
            base = self._next_base
            end = base + channel_count
            if self._inited:
                cur = pygame.mixer.get_num_channels()
                if end > cur:
                    pygame.mixer.set_num_channels(end)
            if end > self._channels:
                self._channels = end
            self._next_base = end
            self._pools[plugin_name] = {"base": base, "count": channel_count, "idx": 0}
            return self._pools[plugin_name]

    def fadeout_if(self, channel: int, sound, ms: int) -> None:
        """仅当通道上还是当初那个声音时才淡出。

        通道池是轮转复用的：句柄里的通道号随后可能被别的发声占用，
        直接 fadeout 会把别人的声音误杀（表现为曲子声音被断续掐断）。
        """
        if not self._inited:
            return
        try:
            ch = pygame.mixer.Channel(channel)
            if ch.get_sound() is sound:
                ch.fadeout(max(30, ms))
        except Exception:
            pass

    def release_pool(self, plugin_name: str) -> None:
        self._pools.pop(plugin_name, None)

    def ensure_channels(self, n: int) -> int:
        """确保 mixer 至少有 n 条 channel，返回当前总数。

        宿主默认只开 32 条，多插件各占一个池后，钢琴类插件（88 键复音）
        会不够用。只增不减；须在 allocate_pool 之前调用。
        """
        if not self._inited:
            return 0
        with self._lock:
            cur = pygame.mixer.get_num_channels()
            if n > cur:
                pygame.mixer.set_num_channels(n)
                self._channels = n
            return pygame.mixer.get_num_channels()

    # ---- 加载 / 播放 ----

    def load(self, path: str) -> Optional[_SfxHandle]:
        """预加载一个 wav 文件，返回可重复 play 的句柄。"""
        if not self.init_if_needed():
            return None
        try:
            sound = pygame.mixer.Sound(path)
            return _SfxHandle(path, sound)
        except Exception:
            return None

    def _poly_attenuation(self) -> float:
        """复音自适应衰减：按当前忙碌通道数压低新出声音量。

        pygame 混音是 int16 饱和加法，多音波形叠加超过 1.0 会削波成噪声。
        单音不打折；活跃越多压得越狠（active=4 → ×0.68，active=8 → ×0.51），
        相当于给全局输出加了一个粗糙的软限幅器。
        """
        if not self._inited:
            return 1.0
        try:
            n = pygame.mixer.get_num_channels()
            active = sum(1 for i in range(n)
                         if pygame.mixer.Channel(i).get_busy())
        except Exception:
            return 1.0
        return 1.0 / (1.0 + 0.12 * active)

    def play(self, sfx: _SfxHandle, plugin_name: Optional[str] = None,
             channel_pool: Optional[int] = None, volume: float = 1.0) -> Optional[int]:
        """播放音效。

        - 有 plugin_name → 用该插件的独占池 round-robin
        - 有 channel_pool → 用前 N 条全局 channel round-robin
        - 都没有 → 全局自动分配
        """
        eff_vol = volume * self._master_volume * self._poly_attenuation()

        if plugin_name and plugin_name in self._pools:
            pool = self._pools[plugin_name]
            idx = pool["base"] + pool["idx"]
            pool["idx"] = (pool["idx"] + 1) % pool["count"]
            return sfx.play(channel=idx, volume=eff_vol)

        if channel_pool:
            # 用全局前 channel_pool 条 round-robin（需要外部维护 idx）
            # 简化：随机挑一条空闲的，或者直接用 Channel(idx).play 打断最老
            idx = self._pools.get("_global", {}).get("idx", 0) % channel_pool
            self._pools.setdefault("_global", {"idx": 0})["idx"] = idx + 1
            return sfx.play(channel=idx, volume=eff_vol)

        # 都没有 → 全局 round-robin 显式分配。
        # 不能用 Sound.play() 的返回值：pygame 2.6 的 Channel 对象
        # 不暴露通道号（无 get_index/get_id），插件拿不到句柄就没法止音。
        n = pygame.mixer.get_num_channels() or self._channels
        idx = self._global_idx
        self._global_idx = (self._global_idx + 1) % n
        return sfx.play(channel=idx, volume=eff_vol)

    # ---- 清理 ----

    def stop_all(self) -> None:
        if self._inited:
            pygame.mixer.stop()

    def shutdown(self) -> None:
        if self._inited:
            try:
                pygame.mixer.quit()
            except Exception:
                pass
            self._inited = False
