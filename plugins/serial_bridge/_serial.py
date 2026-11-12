"""pyserial 传输层（无 Qt）。

线程模型（与 MidiEngine"队列单路径"惯例一致）：
  读线程阻塞读 → out_queue 原始字节块 → 主线程 QTimer 消费（plugin.py）
  写侧 write() 任意线程可调，锁保护直写串口
掉线重连：指数退避 1s→8s，直到 stop()；连接状态变化经 on_state 回调
（工作线程上下文，plugin.py 里经 _UiBridge 转回主线程刷 UI）。

pyserial 缺失时模块可安全导入（_HAS_SERIAL=False），worker 创建才报错，
插件加载与面板显示不受影响。
"""

import queue
import threading

try:
    import serial
    from serial.tools import list_ports
    _HAS_SERIAL = True
except ImportError:
    _HAS_SERIAL = False


def list_serial_ports() -> list:
    """枚举串口，返回 [(device, 描述), ...]。pyserial 缺失时返回空列表。"""
    if not _HAS_SERIAL:
        return []
    out = []
    try:
        for p in list_ports.comports():
            out.append((p.device, p.description or ""))
    except Exception:
        pass
    return out


class SerialWorker:
    """一个串口的读写线程。掉线自动重连（指数退避），直到 stop()。"""

    def __init__(self, port: str, baud: int, on_state=None):
        if not _HAS_SERIAL:
            raise RuntimeError("pyserial 未安装")
        self._port = port
        self._baud = int(baud)
        self._on_state = on_state
        self.out_queue: "queue.Queue[bytes]" = queue.Queue()
        self.last_state = "连接中…"
        self._ser = None
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="serial-bridge")

    # ---- 对外 ----

    def start(self) -> None:
        self._thread.start()

    def write(self, data: bytes) -> None:
        """MIDI→串口方向写字节。失败静默：掉线由读线程统一处理重连。"""
        with self._write_lock:
            if self._ser is None:
                return
            try:
                self._ser.write(data)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)

    # ---- 线程体 ----

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                # serial_for_url：既支持普通 COM 口，也支持 loop:// 等虚拟 URL（冒烟测试用）
                self._ser = serial.serial_for_url(
                    self._port, baudrate=self._baud, timeout=0.05)
            except Exception as exc:
                self._notify(f"连接失败：{exc}")
                if self._stop.wait(backoff):       # 退避等待，期间被 stop 则退出
                    break
                backoff = min(backoff * 2, 8.0)
                continue
            backoff = 1.0
            self._notify("在线")
            self._read_loop()
            self._close()
            if self._stop.is_set():
                break
            self._notify("连接断开，重连中…")
            if self._stop.wait(1.0):
                break
        self._close()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except Exception:
                return                              # 掉线 → 外层重连
            if chunk:
                self.out_queue.put(chunk)

    def _close(self) -> None:
        with self._write_lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    def _notify(self, state: str) -> None:
        self.last_state = state
        if self._on_state:
            try:
                self._on_state(state)
            except Exception:
                pass
