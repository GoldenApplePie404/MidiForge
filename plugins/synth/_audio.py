"""SynthLab 音频输出层 —— sounddevice 的软封装。

设计要点：
    1) 软依赖：sounddevice / PortAudio 缺失时**不抛异常**，只让 ok=False 并记下原因，
       插件面板据此显示"音频后端不可用"，宿主与其它插件不受影响。
    2) 回调内兜底：audio callback 里任何异常都会毒死 PortAudio 流，
       所以 process() 外面再包一层 try/except，出错就静音并累加 errors。
    3) 采样率探测：优先 44100（与 DSP 自检一致），不被设备接受就退到 48000；
       两者都不行则报错，不擅自重采样（避免音高错误）。
    4) 块大小：固定 256。若驱动实际给的 frames 与 engine.block_size 不一致，
       engine.process() 自身会只渲染前 block_size 个采样、其余置零（见 _dsp.py），
       这里不额外处理，保持单一职责。
"""

from __future__ import annotations

# 探测顺序：44100 优先（DSP 默认），其次 48000
CANDIDATE_RATES = (44100, 48000)
BLOCK_SIZE = 256

try:
    import sounddevice as _sd
except Exception as exc:            # pragma: no cover - 取决于环境
    _sd = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    _IMPORT_ERROR = ""


def list_output_devices() -> list[tuple[int, str]]:
    """列出可用输出设备 [(index, name)]，name 里带上主机 API。

    为什么带主机 API：同一块声卡在 PortAudio 里每个主机 API 各占一个条目，
    能不能开流差别极大（实测本机 MME 的"声音映射器"条目就开不了流）。用户要
    靠这个名字才挑得中能用的那一条，所以名字必须自带来源。
    后端不可用时返回空列表。
    """
    if _sd is None:
        return []
    try:
        devices = _sd.query_devices()
    except Exception:
        return []
    apis = _hostapi_names()
    out = []
    for idx, dev in enumerate(devices):
        if int(dev.get("max_output_channels", 0)) > 0:
            out.append((idx, _label(dev, apis)))
    return out


def default_output_device():
    """默认输出设备索引；查不到返回 None。"""
    if _sd is None:
        return None
    try:
        return _sd.default.device[1]
    except Exception:
        return None


# 主机 API 的开流优先级（越小越先试）。
# ASIO / WDM-KS 是独占路径，会抢走其它应用的声音，所以排在共享 API 之后。
_HOSTAPI_RANK = {
    "MME": 0,
    "Windows DirectSound": 1,
    "Windows WASAPI": 2,
    "ASIO": 5,
    "Windows WDM-KS": 6,
}


def _hostapi_names() -> list[str]:
    try:
        return [str(ha.get("name", "")) for ha in _sd.query_hostapis()]
    except Exception:
        return []


def _api_name(dev, apis: list[str]) -> str:
    try:
        i = int(dev.get("hostapi", -1))
    except (TypeError, ValueError):
        return ""
    return apis[i] if 0 <= i < len(apis) else ""


def _label(dev, apis: list[str]) -> str:
    name = str(dev.get("name", "?"))
    api = _api_name(dev, apis)
    return f"{name} [{api}]" if api else name


def fallback_devices() -> list[int]:
    """默认设备开不了流时的兜底顺序（共享主机 API 优先，独占的最后）。"""
    if _sd is None:
        return []
    try:
        devices = _sd.query_devices()
    except Exception:
        return []
    apis = _hostapi_names()
    ranked = []
    for idx, dev in enumerate(devices):
        if int(dev.get("max_output_channels", 0)) <= 0:
            continue
        ranked.append((_HOSTAPI_RANK.get(_api_name(dev, apis), 4), idx))
    ranked.sort()
    return [idx for _, idx in ranked]


class AudioOut:
    """把 SynthEngine 接到系统音频输出。

    线程模型：
        start() 里 sd.OutputStream 起独立回调线程 → _callback(outdata, ...)
        → engine.process(outdata)。
        stop() 关闭流（PortAudio 会 join 回调线程）后才可释放 engine 引用。
    """

    def __init__(self, engine, device=None, block_size: int = BLOCK_SIZE):
        self.engine = engine
        self.block_size = int(block_size)
        self.device = device

        self._stream = None
        self._sample_rate = 0
        self._device_name = ""
        self._active_device = None      # 实际开流用的设备索引（可能是自动挑的）
        self.errors = 0
        self._last_error = ""

    # ---- 状态 ----

    @property
    def ok(self) -> bool:
        """流是否正在运行。"""
        return self._stream is not None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def info(self) -> dict:
        """面板状态条用的一篮子信息。"""
        return {
            "ok": self.ok,
            "device": self._device_name,
            "sample_rate": self._sample_rate,
            "block_size": self.block_size,
            "latency_ms": round(self.block_size / self._sample_rate * 1000.0, 1)
            if self._sample_rate else 0.0,
            "errors": self.errors,
            "last_error": self._last_error,
        }

    # ---- 生命周期 ----

    def start(self, device=None, sample_rate: int | None = None) -> bool:
        """启动音频流。返回是否成功；失败原因见 last_error（走 info）。

        设备候选：
            - 用户/配置明确指定的 → 只试它（尊重选择，不偷偷换设备）
            - 没指定 → 先试系统默认，再按 fallback_devices() 扫一遍其它输出设备

        为什么"没指定"也要扫：PortAudio 的默认输出在本机是 MME 的"声音映射器"
        条目，它连 2ch/float32 流都开不起来（[MME error 32]），只试默认的话
        synth 永远哑着。扫描按主机 API 优先级来，独占设备排在最后。
        """
        self.stop()
        if _sd is None:
            self._last_error = _IMPORT_ERROR or "sounddevice 未安装"
            return False
        explicit = device is not None
        if device is not None:
            self.device = device

        rates = (sample_rate,) if sample_rate else CANDIDATE_RATES
        if explicit or self.device is not None:
            candidates = [self.device]
        else:
            candidates = [None] + fallback_devices()
            if self._active_device is not None:
                # 上次成功的设备优先，省掉每次重开都先撞一次已知失败的默认条目
                candidates = [self._active_device] + [
                    c for c in candidates if c != self._active_device]

        err = "找不到可用输出设备"
        for cand in candidates:
            for rate in rates:
                try:
                    _sd.check_output_settings(
                        device=cand, samplerate=rate, channels=2,
                        dtype="float32")
                    self.engine.set_sample_rate(rate)
                    stream = _sd.OutputStream(
                        samplerate=rate,
                        blocksize=self.block_size,
                        device=cand,
                        channels=2,
                        dtype="float32",
                        callback=self._callback,
                    )
                    stream.start()
                except Exception as exc:
                    err = f"{type(exc).__name__}: {exc}"
                    continue

                self._stream = stream
                self._active_device = cand
                self._sample_rate = int(rate)
                self._device_name = self._resolve_device_name()
                self._last_error = ""
                return True

        self._last_error = err
        return False

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
        self._sample_rate = 0

    # ---- 内部 ----

    def _resolve_device_name(self) -> str:
        try:
            dev = self._active_device
            if dev is None:
                dev = self.device if self.device is not None else default_output_device()
            if dev is None:
                return "系统默认"
            info = _sd.query_devices(dev)
            return _label(info, _hostapi_names())
        except Exception:
            return str(self.device) if self.device is not None else "系统默认"

    def _callback(self, outdata, frames, time_info, status) -> None:
        """PortAudio 回调：任何异常都不能逃出去。"""
        try:
            if status:
                # 只记数，不打日志（回调线程里写文件/IO 会加剧 xrun）
                self.errors += 1
            self.engine.process(outdata)
        except Exception as exc:
            self.errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            try:
                outdata.fill(0.0)
            except Exception:
                pass