"""SDK 统一入口：AppContext 聚合引擎/事件总线/绑定配置/匹配器/动作执行。"""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.bindings import BindingConfig
from core.events import EventBus
from core.matcher import Matcher
from midi.engine import MidiEngine
from midi.parser import ParsedMessage

_DEFAULT_APP: Optional["AppContext"] = None

TOPIC_MESSAGE = "midi.message"
TOPIC_SIGNAL = "midi.signal"
TOPIC_SENT = "midi.sent"
TOPIC_KEY = "key.pressed"
TOPIC_BINDING = "binding.changed"
TOPIC_ACTION = "binding.action"   # 动作执行时发，便于 UI 指示

# 动作类型
ACTION_VIRTUAL_MIDI = "virtual_midi"
ACTION_KEY_OUT = "key_out"


@dataclass
class AppContext:
    engine: MidiEngine
    bus: EventBus
    config: BindingConfig
    matcher: Matcher
    bindings_path: Optional[str] = field(default=None, repr=False)
    # 转发目标端口：binding.virtual_midi.output_port 优先，否则用这个
    default_virtual_out_port: Optional[str] = None
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)

    # ---- 事件订阅便捷方法 ----
    def subscribe(self, topic: str, cb: Callable) -> int:
        return self.bus.subscribe(topic, cb)

    def save_bindings(self) -> None:
        if self.bindings_path:
            self.config.to_file(self.bindings_path)

    # ---- 输入分发 ----
    def _on_parsed(self, parsed: ParsedMessage) -> None:
        self.bus.publish(TOPIC_MESSAGE, parsed)
        signals = self.matcher.signals_for_parsed(parsed)
        if signals:
            self.bus.publish(TOPIC_SIGNAL, {"signals": signals, "parsed": parsed})
            self.execute_actions(signals, source_parsed=parsed)

    def on_key(self, key: str) -> None:
        self.bus.publish(TOPIC_KEY, key)
        signals = self.matcher.signals_for_key(key)
        if signals:
            self.bus.publish(TOPIC_SIGNAL, {"signals": signals, "key": key})
            self.execute_actions(signals, source_key=key)

    def open_input(self, name: str) -> None:
        self.engine.open_input(name)  # 消息统一经 queue → drain → _on_parsed 一条路径分发

    def close_inputs(self) -> None:
        self.engine.close_inputs()

    # ---- 发送 ----
    def send_midi(self, type_: str, channel: int = 0, **kwargs) -> None:
        self.engine.send_message(type_, channel=channel, **kwargs)
        self.bus.publish(TOPIC_SENT, {"type": type_, "channel": channel, "values": kwargs})

    # ---- 动作执行（信号命中后的统一出口）----
    def execute_actions(self, signals: set, source_parsed: Optional[ParsedMessage] = None,
                        source_key: Optional[str] = None) -> list:
        """对命中的信号集执行所有绑定的动作，返回实际执行的动作描述列表。"""
        from midi.keyboard_input import send_key

        executed = []
        for b in self.config.bindings:
            if b.signal not in signals:
                continue
            if b.virtual_midi:
                try:
                    info = self._do_virtual_midi(b.virtual_midi, source_parsed, source_key)
                    if info:
                        executed.append(info)
                except RuntimeError as exc:
                    executed.append({"error": str(exc), "binding": b.signal, "action": ACTION_VIRTUAL_MIDI})
            if b.key_out:
                key = b.key_out.get("key")
                if key:
                    ok = send_key(key)
                    if ok:
                        executed.append({"action": ACTION_KEY_OUT, "key": key})
                    else:
                        executed.append({"error": f"send_key 失败: {key}",
                                         "binding": b.signal, "action": ACTION_KEY_OUT})
        if executed:
            self.bus.publish(TOPIC_ACTION, {"signals": list(signals), "actions": executed})
        return executed

    def _do_virtual_midi(self, vm: dict, source_parsed: Optional[ParsedMessage],
                         source_key: Optional[str]) -> Optional[dict]:
        """执行一条 virtual_midi 转发。"""
        # 如果没指定 type/channel/note，尽量从 source 推断
        type_ = vm.get("type")
        channel = vm.get("channel", 0)
        note = vm.get("note")
        velocity = vm.get("velocity", 100)
        control = vm.get("control")
        value = vm.get("value", 127)

        # 推断：用 source 的消息类型和值作为默认
        if type_ is None and source_parsed is not None:
            type_ = source_parsed.type
            channel = vm.get("channel", source_parsed.channel if source_parsed.channel is not None else 0)
            if type_ in ("note_on", "note_off"):
                note = vm.get("note", source_parsed.values.get("note"))
                velocity = vm.get("velocity", source_parsed.values.get("velocity", 100))
            elif type_ == "cc":
                control = vm.get("control", source_parsed.values.get("control"))
                value = vm.get("value", source_parsed.values.get("value", 127))

        if type_ is None:
            # keyboard source 且没指定 type → 默认发 note_on
            type_ = "note_on"
            if note is None:
                return None  # 缺关键参数，跳过

        kwargs = {}
        if type_ in ("note_on", "note_off"):
            if note is None:
                return None
            kwargs["note"] = note
            kwargs["velocity"] = velocity
        elif type_ == "cc":
            if control is None:
                return None
            kwargs["control"] = control
            kwargs["value"] = value
        elif type_ in ("pitchwheel", "pitch_bend"):
            kwargs["pitch"] = vm.get("pitch", 0)
        elif type_ == "program_change":
            kwargs["program"] = vm.get("program", 0)
        elif type_ in ("poly_aftertouch", "polytouch"):
            kwargs["note"] = vm.get("note", 0)
            kwargs["value"] = vm.get("value", 0)
        elif type_ in ("channel_aftertouch", "aftertouch"):
            kwargs["value"] = vm.get("value", 0)

        self.engine.send_message(type_, channel=channel, **kwargs)
        return {"action": ACTION_VIRTUAL_MIDI, "type": type_, "channel": channel, "kwargs": kwargs}

    # ---- 无 GUI 调度线程 ----
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _loop(self) -> None:
        while self._running:
            for parsed in self.engine.drain():
                self._on_parsed(parsed)
            time.sleep(0.02)


def create_app(bindings_path: Optional[str] = None) -> AppContext:
    global _DEFAULT_APP
    config = BindingConfig.from_file(bindings_path) if bindings_path else BindingConfig()
    app = AppContext(engine=MidiEngine(), bus=EventBus(), config=config,
                     matcher=Matcher(config), bindings_path=bindings_path)
    _DEFAULT_APP = app
    return app


def on(topic: str):
    """装饰器：订阅默认实例（需先调用 create_app()）。"""

    def deco(cb: Callable) -> Callable:
        if _DEFAULT_APP is None:
            raise RuntimeError("请先调用 api.create_app()")
        _DEFAULT_APP.bus.subscribe(topic, cb)
        return cb

    return deco


# 重导出常用符号，供第三方直接使用
from core.bindings import Binding, BindingSource  # noqa: E402
from core.events import EventBus  # noqa: E402
from midi.parser import ParsedMessage  # noqa: E402