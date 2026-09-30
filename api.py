"""SDK 统一入口：AppContext 聚合引擎/事件总线/绑定配置/匹配器/动作执行。

=== 插件可用的 EventBus Topic ===
    "midi.all"            ← 所有消息（相当于旧的 "midi.message"，向后兼容）
    "midi.note_on"        ← note_on only
    "midi.note_off"       ← note_off only
    "midi.cc"             ← control change only
    "midi.pitch_bend"     ← pitchwheel only
    "midi.aftertouch"     ← channel/poly aftertouch
    "midi.sysex"          ← system exclusive
    "midi.signal"         ← 信号命中
    "midi.sent"           ← 宿主发送的消息
    "key.pressed"         ← 键盘钩子事件
    "binding.changed"     ← 绑定增删改
    "binding.action"      ← 动作执行

=== AppContext 子 API ===
    app.state           → _MidiState      当前按下的 note + 历史缓冲
    app.log             → _Logger          插件日志（自动带插件名前缀）
    app.bindings        → _BindingRuntime  绑定 CRUD + trigger_signal
    app.audio           → (v0.3 加入)     统一音频服务
    app.clock           → (v0.3 加入)     时钟 + Tap Tempo
    app.data_dir(name)  → Path            插件私有数据目录
"""

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.bindings import Binding, BindingConfig
from core.audio import _AudioService
from core.clock import _Clock
from core.events import EventBus
from core.matcher import Matcher
from core.midi_file import _MidiFileService
from midi.engine import MidiEngine
from midi.parser import ParsedMessage
from midi.sysex import (
    classify, blacklist_hit, DEFAULT_BLACKLIST_ADDRS,
    RISK_OK, RISK_WARN, RISK_HIGH, RANK_TEXT,
)

_DEFAULT_APP: Optional["AppContext"] = None

# ---- Topic 常量 ----
TOPIC_ALL = "midi.all"          # 全量消息（向后兼容原 midi.message）
TOPIC_NOTE_ON = "midi.note_on"
TOPIC_NOTE_OFF = "midi.note_off"
TOPIC_CC = "midi.cc"
TOPIC_PITCH_BEND = "midi.pitch_bend"
TOPIC_AFTERTOUCH = "midi.aftertouch"
TOPIC_SYSEX = "midi.sysex"
TOPIC_SIGNAL = "midi.signal"
TOPIC_SENT = "midi.sent"
TOPIC_KEY = "key.pressed"
TOPIC_BINDING = "binding.changed"
TOPIC_ACTION = "binding.action"

# 向后兼容旧 topic 名
TOPIC_MESSAGE = TOPIC_ALL

# type → topic 映射（_on_parsed 里按 type 分发）
_TYPE_TOPIC = {
    "note_on": TOPIC_NOTE_ON,
    "note_off": TOPIC_NOTE_OFF,
    "cc": TOPIC_CC,
    "control_change": TOPIC_CC,
    "pitchwheel": TOPIC_PITCH_BEND,
    "pitch_bend": TOPIC_PITCH_BEND,
    "polytouch": TOPIC_AFTERTOUCH,
    "poly_aftertouch": TOPIC_AFTERTOUCH,
    "aftertouch": TOPIC_AFTERTOUCH,
    "channel_aftertouch": TOPIC_AFTERTOUCH,
    "sysex": TOPIC_SYSEX,
}

# 动作类型
ACTION_VIRTUAL_MIDI = "virtual_midi"
ACTION_KEY_OUT = "key_out"


# ============================================================
# 子 API: 状态快照
# ============================================================

class _MidiState:
    """宿主维护的实时 MIDI 状态快照，插件可以主动查询。

    维护的东西：
      - pressed_notes: 每个 channel 当前按住的 note（note_off 或新 note_on 同 note 时移除）
      - recent_messages: 环形历史缓冲（默认最后 1000 条，按时间戳）
      - last_message: 每个 channel 最近一条消息
    """

    def __init__(self, max_history: int = 1000):
        self._pressed: Dict[int, Dict[int, float]] = {}  # channel → {note: timestamp}
        self._last: Dict[int, ParsedMessage] = {}        # channel → last message
        self._history: collections.deque = collections.deque(maxlen=max_history)
        self._lock = threading.Lock()

    # ---- 宿主内部更新（_on_parsed 调用）----

    def feed(self, parsed: ParsedMessage) -> None:
        with self._lock:
            self._history.append((time.monotonic(), parsed))
            if parsed.channel is not None:
                self._last[parsed.channel] = parsed

            t = parsed.type
            if t in ("note_on", "note_off"):
                ch = parsed.channel or 0
                note = parsed.values.get("note")
                vel = parsed.values.get("velocity", 0)
                if ch not in self._pressed:
                    self._pressed[ch] = {}
                if t == "note_on" and vel > 0:
                    self._pressed[ch][note] = time.monotonic()
                elif t == "note_off" or (t == "note_on" and vel == 0):
                    self._pressed[ch].pop(note, None)

    # ---- 插件查询接口 ----

    def pressed_notes(self, channel: Optional[int] = None) -> Dict[int, List[int]]:
        """返回当前按住的 note。{channel: [note, note, ...]}。channel=None 时返回全 channel。"""
        with self._lock:
            if channel is not None:
                return {channel: list(self._pressed.get(channel, {}))}
            return {ch: list(notes.keys()) for ch, notes in self._pressed.items()}

    def is_pressed(self, note: int, channel: int = 0) -> bool:
        with self._lock:
            return note in self._pressed.get(channel, {})

    def latest_message(self, channel: int = 0) -> Optional[ParsedMessage]:
        with self._lock:
            return self._last.get(channel)

    def recent_messages(self, n: int = 100, type_filter: Optional[str] = None) -> List[ParsedMessage]:
        """返回最近 n 条消息。type_filter 可选过滤 note_on / cc / ..."""
        with self._lock:
            msgs = [p for _, p in self._history]
        if type_filter:
            msgs = [m for m in msgs if m.type == type_filter]
        return msgs[-n:]


# ============================================================
# 子 API: 统一日志
# ============================================================

class _Logger:
    """给插件用的 logger wrapper，自动加插件名前缀。"""

    def __init__(self, name: str):
        self._name = name
        self._log = logging.getLogger(f"midi.plugin.{name}")

    def debug(self, msg: str, *args):
        self._log.debug(f"[{self._name}] {msg}", *args)

    def info(self, msg: str, *args):
        self._log.info(f"[{self._name}] {msg}", *args)

    def warning(self, msg: str, *args):
        self._log.warning(f"[{self._name}] {msg}", *args)

    def error(self, msg: str, *args):
        self._log.error(f"[{self._name}] {msg}", *args)

    def critical(self, msg: str, *args):
        self._log.critical(f"[{self._name}] {msg}", *args)


# ============================================================
# 子 API: Binding 运行时
# ============================================================

class _BindingRuntime:
    """让插件能在运行时读写绑定配置。"""

    def __init__(self, app: "AppContext"):
        self._app = app

    # ---- 查询 ----

    def get_all(self) -> List[Binding]:
        return list(self._app.config.bindings)

    def find_by_signal(self, signal: str) -> Optional[Binding]:
        for b in self._app.config.bindings:
            if b.signal == signal:
                return b
        return None

    # ---- 增删 ----

    def add(self, binding: Binding) -> None:
        if self.find_by_signal(binding.signal):
            raise ValueError(f"binding signal 已存在: {binding.signal}")
        self._app.config.bindings.append(binding)
        self._app.bus.publish(TOPIC_BINDING, {"action": "add", "signal": binding.signal})
        self._app.save_bindings()

    def remove(self, signal: str) -> bool:
        for i, b in enumerate(self._app.config.bindings):
            if b.signal == signal:
                self._app.config.bindings.pop(i)
                self._app.bus.publish(TOPIC_BINDING, {"action": "remove", "signal": signal})
                self._app.save_bindings()
                return True
        return False

    # ---- 触发 ----

    def trigger_signal(self, signal: str, midi_data: Optional[dict] = None) -> list:
        """主动触发一个信号（不需要真实 MIDI/键盘输入）。"""
        midi_parsed = None
        if midi_data:
            from midi.parser import ParsedMessage
            midi_parsed = ParsedMessage(**midi_data)
        self._app.bus.publish(TOPIC_SIGNAL, {"signals": {signal}, "parsed": midi_parsed, "data": midi_data})
        return self._app.execute_actions({signal}, source_parsed=midi_parsed)


# ============================================================
# AppContext
# ============================================================

@dataclass
class AppContext:
    engine: MidiEngine
    bus: EventBus
    config: BindingConfig
    matcher: Matcher
    bindings_path: Optional[str] = field(default=None, repr=False)
    default_virtual_out_port: Optional[str] = None

    # ---- 子 API（初始化时创建）----
    state: _MidiState = field(default=None, repr=False)       # type: ignore[assignment]
    log: _Logger = field(default=None, repr=False)            # type: ignore[assignment]
    bindings: _BindingRuntime = field(default=None, repr=False)  # type: ignore[assignment]
    audio: _AudioService = field(default=None, repr=False)    # type: ignore[assignment]
    clock: _Clock = field(default=None, repr=False)           # type: ignore[assignment]
    midi_file: _MidiFileService = field(default=None, repr=False)  # type: ignore[assignment]

    # ---- 运行时内部 ----
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)
    _plugin_dir: Optional[Path] = field(default=None, repr=False)  # data_dir 根

    def __post_init__(self):
        # dataclass field default=None → 这里真初始化
        self.state = _MidiState()
        self.log = _Logger("host")
        self.bindings = _BindingRuntime(self)
        self.audio = _AudioService()
        self.clock = _Clock(bus=self.bus)
        self.midi_file = _MidiFileService()

    # ---- 事件订阅 ----

    def subscribe(self, topic: str, cb: Callable) -> int:
        return self.bus.subscribe(topic, cb)

    # ---- 插件数据目录 ----

    def data_dir(self, plugin_name: str = "host") -> Path:
        """返回插件私有数据目录（自动创建）。host 用全局 config/。"""
        if plugin_name == "host":
            # host 用 config 目录
            if self.bindings_path:
                return Path(self.bindings_path).parent
            return Path("config")
        # 插件: plugins/<name>/data/
        d = self._plugin_dir / plugin_name / "data" if self._plugin_dir else Path("plugins") / plugin_name / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---- 保存绑定 ----

    def save_bindings(self) -> None:
        if self.bindings_path:
            self.config.to_file(self.bindings_path)

    # ---- 输入分发 ----

    def _on_parsed(self, parsed: ParsedMessage) -> None:
        # 更新状态快照
        self.state.feed(parsed)

        # 全量 topic（向后兼容）
        self.bus.publish(TOPIC_ALL, parsed)
        # 按 type 细分 topic
        type_topic = _TYPE_TOPIC.get(parsed.type)
        if type_topic:
            self.bus.publish(type_topic, parsed)

        # 信号匹配 + 动作
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
        self.engine.open_input(name)

    def close_inputs(self) -> None:
        self.engine.close_inputs()

    # ---- 发送 ----

    def send_midi(self, type_: str, channel: int = 0, **kwargs) -> None:
        self.engine.send_message(type_, channel=channel, **kwargs)
        self.bus.publish(TOPIC_SENT, {"type": type_, "channel": channel, "values": kwargs})

    def send_sysex(self, data, *, safe: bool = True,
                   blacklist: Optional[dict] = None) -> dict:
        """发送 SysEx 消息，默认启用安全网关。

        三道安全机制：
          1. 黑名单 block=True 的条目 → 直接 raise RuntimeError（硬拦截）
          2. RISK_HIGH 消息 → 记录 warning 日志 + TOPIC_SENT 里带风险信息，
             但不拦截（因为二次确认是 UI 层职责，API 不知道运行时上下文）
          3. safe=False → 完全绕过（开发者显式声明知道自己在做什么）

        Args:
            data: 不含 F0/F7 的 payload 字节列表（0-127）
            safe: 默认 True。False = 绕过所有检查（危险，仅限调试/固件刷写）
            blacklist: 可选，覆盖默认黑名单。None 时用内置 DEFAULT_BLACKLIST_ADDRS。

        Returns:
            dict: {"level": int, "reason": str, "blacklist_hit": dict|None, "sent": bool}

        Raises:
            RuntimeError: 命中黑名单 block=True 条目（safe=True 时）
        """
        level, reason, vendor_known = RISK_OK, "safe=False 绕过", False
        hi = None

        if safe:
            level, reason, vendor_known = classify(list(data))
            bl_cfg = blacklist if blacklist is not None else {
                "enabled": True, "named_addresses": DEFAULT_BLACKLIST_ADDRS, "custom": []
            }
            hi = blacklist_hit(list(data), bl_cfg)
            if hi and hi.get("block"):
                raise RuntimeError(
                    f"SysEx 命中非参数区黑名单，已禁发:\n{hi.get('reason', '')}\n"
                    f"payload 前 8 字节: {' '.join(f'{b:02X}' for b in list(data)[:8])}..."
                )
            if level == RISK_HIGH:
                self.log.warning(
                    "发送高危 SysEx [%s]: %s — 请确认用户已知风险并授权",
                    RANK_TEXT[level], reason)

        self.engine.send_sysex(data)

        info = {
            "type": "sysex",
            "data": list(data),
            "level": level,
            "reason": reason,
            "risk": RANK_TEXT.get(level, "?"),
            "blacklist_hit": hi,
            "safe": safe,
        }
        self.bus.publish(TOPIC_SENT, info)
        return info

    # ---- 动作执行 ----

    def execute_actions(self, signals: set, source_parsed: Optional[ParsedMessage] = None,
                        source_key: Optional[str] = None) -> list:
        """对命中的信号集执行所有绑定的动作，返回实际执行的动作描述列表。"""
        from midi.keyboard_input import send_key

        executed = []
        for b in self.config.bindings:
            if not b.enabled:
                continue  # 停用的绑定任何路径都不执行（含 trigger_signal）
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
        type_ = vm.get("type")
        channel = vm.get("channel", 0)
        note = vm.get("note")
        velocity = vm.get("velocity", 100)
        control = vm.get("control")
        value = vm.get("value", 127)

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
            type_ = "note_on"
            if note is None:
                return None

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

    # ---- 运行线程 ----

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


# ============================================================
# 工厂 + 装饰器
# ============================================================

def create_app(bindings_path: Optional[str] = None,
               plugin_dir: Optional[Path] = None) -> AppContext:
    global _DEFAULT_APP
    config = BindingConfig.from_file(bindings_path) if bindings_path else BindingConfig()
    app = AppContext(engine=MidiEngine(), bus=EventBus(), config=config,
                     matcher=Matcher(config), bindings_path=bindings_path)
    app._plugin_dir = plugin_dir or Path("plugins")
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


# 重导出常用符号
from core.bindings import BindingSource  # noqa: E402
from core.events import EventBus  # noqa: E402
from midi.parser import ParsedMessage  # noqa: E402
