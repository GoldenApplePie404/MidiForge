import queue
import time
from collections import deque
from typing import Callable, List, Optional

import mido

from midi.parser import MIDO_TYPE_MAP, ParsedMessage, parse


class MidiEngine:
    """枚举/打开/收发 MIDI 端口。输入回调运行在 rtmidi 线程，消息经队列转交 UI。"""

    def __init__(self):
        self._queue: "queue.Queue[ParsedMessage]" = queue.Queue()
        self._in_ports: List[mido.ports.BaseInput] = []
        self._out_port: Optional[mido.ports.BaseOutput] = None
        # 发送回声指纹：去重 loopMIDI 回环中自己发出的消息，标记 source="virtual"
        self._echoes: deque[tuple] = deque(maxlen=32)

    # ---- 枚举 ----
    def list_inputs(self) -> List[str]:
        return mido.get_input_names()

    def list_outputs(self) -> List[str]:
        return mido.get_output_names()

    # ---- 打开/关闭 ----
    def open_input(
        self,
        name: str,
        callback: Optional[Callable[[ParsedMessage], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        def _on_msg(msg: mido.Message) -> None:
            try:
                parsed = parse(msg)
            except Exception as exc:  # 防止回调线程崩溃导致后续接收中断
                if on_error:
                    on_error(str(exc))
                return
            # 标记回环回声
            fp = self._fingerprint(parsed)
            if fp in self._echoes:
                parsed.source = "virtual"
                self._echoes.discard(fp)
                # 回声也入队，让上层决定要不要过滤；不再额外调 callback 避免重复
                self._queue.put(parsed)
                return
            self._queue.put(parsed)
            # 注意：不再在这里直接调 callback，而是让上层轮询统一分发
            # 否则同一条消息会被 callback 和 drain 两条路径各处理一次（双重输入）

        port = mido.open_input(name, callback=_on_msg)
        self._in_ports.append(port)

    def open_output(self, name: str) -> None:
        self.close_output()
        self._out_port = mido.open_output(name)

    def close_inputs(self) -> None:
        for p in self._in_ports:
            p.close()
        self._in_ports.clear()

    def close_output(self) -> None:
        if self._out_port is not None:
            self._out_port.close()
            self._out_port = None

    def close_all(self) -> None:
        self.close_inputs()
        self.close_output()

    # ---- 消费 ----
    def drain(self) -> List[ParsedMessage]:
        out = []
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        return out

    # ---- 发送 ----
    def send_message(self, type: str, channel: int = 0, **kwargs) -> None:
        if self._out_port is None:
            raise RuntimeError("未打开输出端口")
        mido_type = MIDO_TYPE_MAP.get(type, type)
        mid = mido.Message(mido_type, channel=channel, **kwargs)
        # 先入指纹再发，这样回调线程收到时能匹配
        # 构造一个临时 ParsedMessage 来取指纹
        parsed = parse(mid)
        self._echoes.append(self._fingerprint(parsed))
        self._out_port.send(mid)

    @staticmethod
    def _fingerprint(parsed: ParsedMessage) -> tuple:
        return (parsed.type, parsed.channel, tuple(sorted(parsed.values.items())))