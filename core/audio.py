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


# 驱动降级链：实测 SDL 的 WASAPI 后端在本机可能失败，WinMM/DirectSound 兜底。
# WinMM 排在 DirectSound 之前：DirectSound 会占住输出设备，
# 导致 PortAudio（synth 插件）开不了流——两败俱伤。
_DRIVER_FALLBACK = ("", "winmm", "directsound")


class _AudioService:
    """宿主统一音频服务。单例，宿主在 AppContext 里创建一次。"""

    def __init__(self, sample_rate: int = 44100, channels: int = 32, buffer: int = 1024,
                 desired: Optional[dict] = None):
        self._channels = channels
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
        # 用户期望配置（None 值 = 用默认）。sample_rate/buffer 缺省回退到参数值。
        self._desired = {"device": None, "driver": None,
                         "sample_rate": None, "buffer": None}
        if desired:
            self._desired.update({k: v for k, v in desired.items() if k in self._desired})
        self._sample_rate = self._desired["sample_rate"] or sample_rate
        self._buffer = self._desired["buffer"] or buffer
        self._driver = ""       # 当前实际生效的驱动（""=SDL 默认）
        # mixer 重建成功后的回调（AppContext 注入 → 广播 audio.restarted）。
        # core 层不知事件总线，以此解耦。
        self.on_restarted = None

    # ---- 初始化 ----

    def _try_init(self, drivers, sample_rate: int, buffer: int,
                  device: Optional[str]) -> bool:
        """按驱动序列逐个尝试 init，成功返回 True，失败原因写入 last_error。"""
        errors = []
        for driver in drivers:
            try:
                pygame.mixer.quit()
            except Exception:
                pass
            if driver:
                os.environ["SDL_AUDIODRIVER"] = driver
            else:
                os.environ.pop("SDL_AUDIODRIVER", None)
            try:
                pygame.mixer.init(sample_rate, -16, 2, buffer,
                                  devicename=device or None)
                pygame.mixer.set_num_channels(self._channels)
                self._driver = driver or ""
                self._inited = True
                self.last_error = ""
                return True
            except Exception as exc:
                errors.append(f"{driver or 'default'}: {exc}")
        self.last_error = " | ".join(errors)
        return False

    def init_if_needed(self) -> bool:
        """延迟初始化——pygame 不在也不会崩。

        用户指定了驱动时先试指定值，失败回退默认降级链（原因记入 last_error）。
        """
        if self._inited:
            return True
        if not _PYGAME_AVAILABLE:
            self.last_error = "pygame 不可用"
            return False
        with self._lock:
            if self._inited:
                return True
            sr, buf, dev = self._sample_rate, self._buffer, self._desired["device"]
            if self._desired["driver"]:
                if self._try_init([self._desired["driver"]], sr, buf, dev):
                    return True
            if self._try_init(_DRIVER_FALLBACK, sr, buf, dev):
                return True
            # 指定设备可能就是失败根源：最后再试一次全默认
            if dev or self._desired["driver"]:
                if self._try_init(_DRIVER_FALLBACK, sr, buf, None):
                    return True
            return False

    @property
    def inited(self) -> bool:
        return self._inited

    # ---- 设备枚举 / 配置 ----

    def list_output_devices(self) -> list:
        """枚举输出设备名。pygame._sdl2 缺失或失败时返回空列表（UI 显示"仅系统默认"）。"""
        if not _PYGAME_AVAILABLE:
            return []
        try:
            from pygame._sdl2.audio import get_audio_device_names
            return list(get_audio_device_names(False))   # False=输出（非录音）
        except Exception:
            return []

    def current_config(self) -> dict:
        """当前生效配置（desired 语义：None=系统默认）。UI 打开对话框时回填用。"""
        return dict(self._desired)

    def restart(self, **overrides) -> tuple:
        """应用新音频参数并重建 mixer。返回 (成功?, 错误信息)。

        新参数失败时按旧参数回退重试；都失败返回原因（mixer 可能已停，
        调用方应提示用户）。成功后经 on_restarted 回调通知宿主广播
        audio.restarted——mixer 重建使所有已加载 Sound 失效，插件须清缓存。
        """
        if not _PYGAME_AVAILABLE:
            return False, "pygame 不可用"
        with self._lock:
            old = dict(self._desired)
            new = dict(old)
            # 显式传入的 key 全部覆盖（含 None=恢复默认）；未传入的不动
            for k in new:
                if k in overrides:
                    new[k] = overrides[k]
            sr = new["sample_rate"] or self._sample_rate
            buf = new["buffer"] or self._buffer
            dev = new["device"]

            self._inited = False
            # 新参数：指定驱动优先，未指定走降级链
            drivers = [new["driver"]] if new["driver"] else _DRIVER_FALLBACK
            if self._try_init(drivers, sr, buf, dev):
                self._desired = new
                self._sample_rate, self._buffer = sr, buf
                self._notify_restarted()
                return True, ""
            # 新参数失败 → 旧参数回退（全默认兜底），保证音频尽量活着。
            # 回退成功会清空 last_error，先存下切换失败的原因供 UI 提示。
            first_err = self.last_error
            old_drivers = [old["driver"]] if old["driver"] else _DRIVER_FALLBACK
            old_sr = old["sample_rate"] or 44100
            old_buf = old["buffer"] or 1024
            if self._try_init(old_drivers, old_sr, old_buf, old["device"]) \
                    or self._try_init(_DRIVER_FALLBACK, old_sr, old_buf, None):
                self._desired = old
                self._sample_rate, self._buffer = old_sr, old_buf
                self.last_error = first_err
                self._notify_restarted()
                return False, first_err
            return False, self.last_error

    def _notify_restarted(self) -> None:
        if self.on_restarted:
            try:
                self.on_restarted()
            except Exception:
                pass

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
