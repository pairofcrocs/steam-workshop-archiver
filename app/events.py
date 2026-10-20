"""In-process event bus feeding the SSE stream. Every event gets a global sequence
number and lives in a bounded ring, so any number of clients can read it and
reconnecting clients can catch up via Last-Event-ID."""

import threading
from collections import deque


class EventBus:
    def __init__(self, maxlen: int = 5000):
        self._events: deque[tuple[int, dict]] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    @property
    def seq(self) -> int:
        return self._seq

    def publish(self, type_: str, **data) -> int:
        with self._lock:
            self._seq += 1
            self._events.append((self._seq, {"type": type_, **data}))
            return self._seq

    def since(self, seq: int) -> tuple[list[tuple[int, dict]], bool]:
        """Events after *seq*. The bool is False when *seq* has fallen out of the
        ring (the caller should resync with a snapshot)."""
        with self._lock:
            if seq > self._seq:
                return [], False
            if not self._events or seq >= self._seq:
                return [], True
            oldest = self._events[0][0]
            if seq < oldest - 1:
                return [], False
            return [e for e in self._events if e[0] > seq], True


bus = EventBus()
