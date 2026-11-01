"""Record raw book events, decoded turns and HTTP/2 messages as JSONL.

Timestamps are epoch seconds. Callers supply the path and preview length, keeping
this module independent of provider configuration and third-party imports.
"""
import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from os import PathLike

from ..types import Event, StreamId


class Recorder:
    def __init__(self, path: str | PathLike[str], preview: int = 64) -> None:
        self.path = path
        self.preview = preview
        self._lock = threading.Lock()
        self._f = open(self.path, "a", buffering=1)  # line-buffered
        self._subs: list[Callable[[Event], object]] = []

    def subscribe(self, fn: Callable[[Event], object]) -> None:
        """Register fn(obj) to receive every recorded event (raw records + decoded turns).
        Called on the dispatch worker thread — keep it quick and non-throwing (e.g. queue.put).
        Copy-on-write under the lock, so `_write`'s iteration always sees a stable list."""
        with self._lock:
            self._subs = self._subs + [fn]

    def unsubscribe(self, fn: Callable[[Event], object]) -> None:
        """Remove a subscriber, if present. Targets must unsubscribe when they stop
        streaming, or the host keeps filling their abandoned queues."""
        with self._lock:
            self._subs = [f for f in self._subs if f is not fn]

    def record(self, kind: str, stream_id: StreamId, data: bytes) -> None:
        rec: Event = {"t": round(time.time(), 6), "kind": kind,
               "stream": stream_id, "len": len(data)}
        if data:
            rec["sha8"] = hashlib.sha256(data).hexdigest()[:16]
            if self.preview:
                rec["head"] = data[:self.preview].hex()
        self._write(rec)

    def event(self, obj: Mapping[str, object]) -> None:
        event = obj if isinstance(obj, dict) else dict(obj)
        event.setdefault("t", round(time.time(), 6))
        self._write(event)

    def _write(self, obj: Event) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with self._lock:
            self._f.write(line + "\n")
        for fn in self._subs:                        # notify in-process subscribers (outside the lock)
            try:
                fn(obj)
            except Exception:
                pass
