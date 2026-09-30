from typing import Set

from core.bindings import BindingConfig, BindingSource
from midi.parser import ParsedMessage


class Matcher:
    """把 MIDI 事件或按键映射为命中的信号集合。停用（enabled=False）的绑定不参与匹配。"""

    def __init__(self, config: BindingConfig):
        self._config = config

    def signals_for_parsed(self, parsed: ParsedMessage) -> Set[str]:
        if parsed.source == "virtual":
            return set()  # 转发的消息不再触发绑定，防止无限循环
        hits = set()
        for b in self._config.bindings:
            if not b.enabled:
                continue
            if any(_source_matches_parsed(s, parsed) for s in b.sources):
                hits.add(b.signal)
        return hits

    def signals_for_key(self, key: str) -> Set[str]:
        key = key.lower()
        hits = set()
        for b in self._config.bindings:
            if not b.enabled:
                continue
            if any(_source_matches_key(s, key) for s in b.sources):
                hits.add(b.signal)
        return hits


def _source_matches_parsed(src: BindingSource, parsed: ParsedMessage) -> bool:
    if src.type != "midi":
        return False
    if src.event and src.event != parsed.type:
        return False
    if src.channel is not None and src.channel != parsed.channel:
        return False
    if src.note is not None and parsed.values.get("note") != src.note:
        return False
    if src.cc is not None and parsed.values.get("control") != src.cc:
        return False
    if src.value_min is not None:
        # 从 values 里取合适的数值字段
        val = parsed.values.get("value")
        if val is None:
            val = parsed.values.get("velocity")
        if val is None:
            val = parsed.values.get("pitch")
        if val is None or val < src.value_min:
            return False
    return True


def _source_matches_key(src: BindingSource, key: str) -> bool:
    return src.type == "keyboard" and src.key is not None and src.key.lower() == key