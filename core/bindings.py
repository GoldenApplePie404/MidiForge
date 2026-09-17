import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class BindingSource:
    """绑定源。type=midi 时 channel/event/note/cc/value_min 任一为 None 表示通配；type=keyboard 时用 key。"""

    type: str
    channel: Optional[int] = None
    event: Optional[str] = None
    note: Optional[int] = None
    cc: Optional[int] = None
    value_min: Optional[int] = None  # MIDI 数值下限 (含)，如踏板只在 >=64 时命中
    key: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "BindingSource":
        try:
            src = cls(
                type=d["type"],
                channel=d.get("channel"),
                event=d.get("event"),
                note=d.get("note"),
                cc=d.get("cc"),
                value_min=d.get("value_min"),
                key=d.get("key"),
            )
        except KeyError as exc:
            raise ValueError(f"绑定源缺少字段: {exc}") from exc
        if src.type not in ("midi", "keyboard"):
            raise ValueError(f"未知绑定源类型: {src.type}")
        return src

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Binding:
    signal: str
    sources: list = field(default_factory=list)
    virtual_midi: Optional[dict] = None
    key_out: Optional[dict] = None  # {"key": "f1"} 或 {"key": "ctrl+k"}

    @classmethod
    def from_dict(cls, d: dict) -> "Binding":
        for s in d.get("sources", []):
            if not isinstance(s, dict):
                raise ValueError(f"绑定源必须是对象: {s}")
        return cls(
            signal=d["signal"],
            sources=[BindingSource.from_dict(s) for s in d.get("sources", [])],
            virtual_midi=d.get("virtual_midi"),
            key_out=d.get("key_out"),
        )

    def to_dict(self) -> dict:
        d = {"signal": self.signal, "sources": [s.to_dict() for s in self.sources]}
        if self.virtual_midi is not None:
            d["virtual_midi"] = self.virtual_midi
        if self.key_out is not None:
            d["key_out"] = self.key_out
        return d


@dataclass
class BindingConfig:
    version: int = 1
    bindings: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "BindingConfig":
        for b in d.get("bindings", []):
            if not isinstance(b, dict):
                raise ValueError(f"绑定项必须是对象: {b}")
        return cls(version=d.get("version", 1), bindings=[Binding.from_dict(b) for b in d.get("bindings", [])])

    def to_dict(self) -> dict:
        return {"version": self.version, "bindings": [b.to_dict() for b in self.bindings]}

    @classmethod
    def from_file(cls, path: str) -> "BindingConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
            raise ValueError(f"绑定配置损坏: {exc}") from exc

    def to_file(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")