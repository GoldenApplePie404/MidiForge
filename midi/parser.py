from dataclasses import dataclass, field
from typing import Optional

from mido import Message

# mido 类型名 → 归一化类型名（用于匹配与显示）
_TYPE_MAP = {
    "note_on": "note_on",
    "note_off": "note_off",
    "polytouch": "poly_aftertouch",
    "control_change": "cc",
    "program_change": "program_change",
    "aftertouch": "aftertouch",
    "pitchwheel": "pitch_bend",
}

_SYSTEM_TYPES = {"clock", "start", "stop", "continue", "active_sensing", "reset", "sysex", "tune_request", "songpos", "song_select"}

# 归一化类型名 → mido 原生类型名（发送时反向转换）
MIDO_TYPE_MAP = {v: k for k, v in _TYPE_MAP.items()}


@dataclass
class ParsedMessage:
    """解析后的 MIDI 消息，供 UI 展示与匹配引擎使用。

    source: "midi_in"（来自外部端口）/ "virtual"（内部转发）。匹配器会跳过 virtual。
    """

    type: str
    channel: Optional[int]
    values: dict = field(default_factory=dict)
    raw_hex: str = ""
    description: str = ""
    source: str = "midi_in"

    def event_key(self) -> str:
        """匹配键：type:channel:主值。省略尾段表示通配匹配。"""
        parts = [self.type]
        if self.type == "system" and self.values.get("raw_type") is not None:
            parts.append(str(self.values["raw_type"]))
        if self.channel is not None:
            parts.append(str(self.channel))
            if self.type in ("note_on", "note_off", "poly_aftertouch") and "note" in self.values:
                parts.append(str(self.values["note"]))
            elif self.type == "cc" and "control" in self.values:
                parts.append(str(self.values["control"]))
            elif self.type == "program_change" and "program" in self.values:
                parts.append(str(self.values["program"]))
        return ":".join(parts)


def _describe(msg: Message) -> str:
    if msg.type == "note_on":
        return f"音符开 ch{msg.channel} note {msg.note} vel {msg.velocity}"
    if msg.type == "note_off":
        return f"音符关 ch{msg.channel} note {msg.note} vel {msg.velocity}"
    if msg.type == "control_change":
        return f"CC ch{msg.channel} CC{msg.control} = {msg.value}"
    if msg.type == "pitchwheel":
        return f"弯音 ch{msg.channel} pitch {msg.pitch}"
    if msg.type == "program_change":
        return f"音色 ch{msg.channel} program {msg.program}"
    if msg.type == "aftertouch":
        return f"通道触后 ch{msg.channel} value {msg.value}"
    if msg.type == "polytouch":
        return f"复音触后 ch{msg.channel} note {msg.note} value {msg.value}"


def parse(msg: Message) -> ParsedMessage:
    """把 mido.Message 解析为 ParsedMessage。"""
    raw_hex = " ".join(f"{b:02X}" for b in msg.bytes())
    if msg.type in _SYSTEM_TYPES:
        return ParsedMessage(type="system", channel=None, values={"raw_type": msg.type}, raw_hex=raw_hex, description=f"系统消息 {msg.type}")
    mtype = _TYPE_MAP[msg.type]
    channel = getattr(msg, "channel", None)
    values = {k: v for k, v in msg.dict().items() if k not in ("channel", "type", "time")}
    return ParsedMessage(type=mtype, channel=channel, values=values, raw_hex=raw_hex, description=_describe(msg))