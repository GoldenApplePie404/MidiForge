from threading import Lock
from typing import Any, Callable, Dict, List


class EventBus:
    """线程安全的事件总线：任意字符串主题、订阅/退订/发布。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._subs: Dict[str, List[tuple]] = {}
        self._next_id = 0

    def subscribe(self, topic: str, cb: Callable[[Any], None]) -> int:
        with self._lock:
            self._next_id += 1
            sid = self._next_id
            self._subs.setdefault(topic, []).append((sid, cb))
            return sid

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            for topic, lst in list(self._subs.items()):
                lst[:] = [(i, cb) for i, cb in lst if i != sid]
                if not lst:
                    del self._subs[topic]

    def publish(self, topic: str, payload: Any = None) -> None:
        with self._lock:
            subs = list(self._subs.get(topic, []))
        for _, cb in subs:
            cb(payload)

    def clear(self) -> None:
        with self._lock:
            self._subs.clear()

    def topics(self) -> List[str]:
        with self._lock:
            return sorted(self._subs.keys())