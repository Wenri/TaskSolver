"""JSONL recorder for plotting/analysis.

One JSON object per line. Raw hook events (tls_write/tls_read/dns/...) plus
higher-level reassembled HTTP/2 messages (kind="h2msg"). Timestamps are epoch
seconds (float) so you can plot bytes-over-time, request/response sizes, and
inter-event latencies directly.

Output path: $AGY_HOOK_CAPTURE (default ./agy-capture.jsonl).
Hex preview length per raw event: $AGY_HOOK_PREVIEW bytes (default 64; 0 = off).
"""
import hashlib
import json
import os
import threading
import time


class Recorder:
    def __init__(self, path=None):
        self.path = path or os.environ.get("AGY_HOOK_CAPTURE", "agy-capture.jsonl")
        self.preview = int(os.environ.get("AGY_HOOK_PREVIEW", "64"))
        self._lock = threading.Lock()
        self._f = open(self.path, "a", buffering=1)  # line-buffered

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
