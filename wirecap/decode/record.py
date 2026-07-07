"""JSONL recorder for plotting/analysis — provider-neutral.

One JSON object per line: raw hook events (tls_write/…/resp_chunk) plus higher-level
decoded turns and reassembled HTTP/2 messages (kind="h2msg"). Timestamps are epoch
seconds (float) so bytes-over-time, request/response sizes, and inter-event latencies
plot directly.

The output path + preview length are passed in by the caller (the provider package reads
them from its own env knobs) so this module stays env-agnostic and stdlib-pure.
"""
import hashlib
import json
import threading
import time


class Recorder:
    def __init__(self, path, preview=64):
        self.path = path
        self.preview = preview
        self._lock = threading.Lock()
        self._f = open(self.path, "a", buffering=1)  # line-buffered
        self._subs = []                              # in-process event subscribers (WireProcess)

    def subscribe(self, fn):
        """Register fn(obj) to receive every recorded event (raw records + decoded turns).
        Called on the dispatch worker thread — keep it quick and non-throwing (e.g. queue.put)."""
        self._subs.append(fn)

    def record(self, kind, stream_id, data):
        rec = {"t": round(time.time(), 6), "kind": kind,
               "stream": stream_id, "len": len(data)}
        if data:
            rec["sha8"] = hashlib.sha256(data).hexdigest()[:16]
            if self.preview:
                rec["head"] = data[:self.preview].hex()
        self._write(rec)

    def event(self, obj):
        obj.setdefault("t", round(time.time(), 6))
        self._write(obj)

    def _write(self, obj):
        line = json.dumps(obj, ensure_ascii=False)
        with self._lock:
            self._f.write(line + "\n")
        for fn in self._subs:                        # notify in-process subscribers (outside the lock)
            try:
                fn(obj)
            except Exception:
                pass
