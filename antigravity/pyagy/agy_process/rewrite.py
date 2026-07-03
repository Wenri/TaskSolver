"""SYNC egress rewrite registry — the only place agy's outbound request is mutated.

Consumed by ``agy_process.on_tls_write`` when a rewrite source is configured
(``AGY_PROC_REWRITE_RULES`` = a JSON rules file, or ``AGY_PROC_REWRITE`` =
``module:func``). Runs *inside* agy's embedded interpreter, so it is stdlib-only, and
it runs on the goroutine that is blocked in ``crypto/tls.(*Conn).Write`` — keep it
fast and CPU-bound.

**Length is the hard constraint.** The C shim rewrites the buffer in place and only
accepts ``out_len <= len`` (antigravity.c). But *shrinking* the body desyncs the
HTTP ``Content-Length`` / chunk framing, and *growing* is impossible — so the safe,
enforced default is **equal-length substitution**. A rule set whose net effect
changes the buffer length is skipped and recorded as ``rewrite_skip`` (opt out of the
equal-length guard, at your own framing risk, with ``AGY_PROC_REWRITE_ALLOW_SHRINK=1``,
which permits ``<=``-length results). Additive changes (extra tools/context) must go
through config-injection, not here.

Rules JSON (a bare list of rules, or ``{"match":..., "allow_shrink":..., "rules":[...]}``):
    {"find": "sk-secret-000", "replace": "sk-REDACTED-00", "count": 0, "regex": false}
``find``/``replace`` are UTF-8; ``count`` 0 = all occurrences; ``regex`` uses ``re`` on
bytes (``\\g<0>`` etc. in ``replace``). Only buffers that sniff as the model request
are touched (override the sniff with ``match`` / ``AGY_PROC_REWRITE_MATCH``).
"""
import importlib
import os
import re

# The agy model request path is `.../v1internal:streamGenerateContent` (note the
# capital G — "generateContent" lowercase is NOT a substring). We also accept the
# direct `:generateContent` endpoint. A buffer matches if it contains any marker,
# and — because a large request is split across 32 KiB writes whose continuations
# no longer carry the request line — a stream stays eligible once its request line
# has been seen (keep-alive means unrelated later requests on the same conn are then
# also eligible, but a rule only fires where its `find` pattern occurs).
_DEFAULT_MATCH = ("streamGenerateContent", "generateContent")
_SIZE_CAP = 8 << 20        # never attempt a rewrite on an absurdly large buffer


class RewriteRegistry:
    def __init__(self, rules=None, func=None, match=None, allow_shrink=False,
                 recorder=None, rules_path=None):
        self.rules = rules or []
        self.func = func
        self.match = self._markers(match)
        self.allow_shrink = allow_shrink
        self.rec = recorder
        self._path = rules_path
        self._mtime = self._stat(rules_path)
        self._model_streams = set()

    @staticmethod
    def _markers(match):
        m = match or os.environ.get("AGY_PROC_REWRITE_MATCH")
        markers = (m,) if isinstance(m, str) else (tuple(m) if m else _DEFAULT_MATCH)
        return tuple(s.encode() for s in markers)

    # --- construction ---------------------------------------------------------
    @classmethod
    def from_env(cls, recorder=None):
        allow_shrink = os.environ.get("AGY_PROC_REWRITE_ALLOW_SHRINK") not in (None, "", "0")
        func_spec = os.environ.get("AGY_PROC_REWRITE")
        if func_spec:
            return cls(func=_load_func(func_spec), allow_shrink=allow_shrink,
                       recorder=recorder)
        path = os.environ.get("AGY_PROC_REWRITE_RULES")
        rules, match, shrink = _load_rules(path)
        return cls(rules=rules, match=match, allow_shrink=allow_shrink or shrink,
                   recorder=recorder, rules_path=path)

    @staticmethod
    def _stat(path):
        try:
            return os.stat(path).st_mtime if path else None
        except OSError:
            return None

    def _maybe_reload(self):
        if not self._path:
            return
        mtime = self._stat(self._path)
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self.rules, match, shrink = _load_rules(self._path)
                if match:
                    self.match = self._markers(match)
            except Exception as e:      # keep serving the old rules on a bad reload
                self._event({"kind": "rewrite_error", "phase": "reload", "error": str(e)})

    # --- the hot path ---------------------------------------------------------
    def rewrite(self, stream_id, data):
        """Return replacement bytes (equal length by default) for a model-request
        buffer, or None to leave it unchanged."""
        if not data or len(data) > _SIZE_CAP:
            return None
        self._maybe_reload()
        if self.match:
            if any(m in data for m in self.match):
                self._model_streams.add(stream_id)     # sticky: header write marks it
            elif stream_id not in self._model_streams:
                return None             # not the model request — never touch it
        try:
            out = self._apply(bytes(data))
        except Exception as e:
            self._event({"kind": "rewrite_error", "phase": "apply", "error": str(e),
                         "stream": stream_id})
            return None
        if out is None or out == data:
            return None
        if len(out) > len(data) or (len(out) != len(data) and not self.allow_shrink):
            self._event({"kind": "rewrite_skip", "stream": stream_id,
                         "reason": "grow" if len(out) > len(data) else "shrink",
                         "orig_len": len(data), "new_len": len(out)})
            return None
        self._event({"kind": "rewrite_applied", "stream": stream_id,
                     "orig_len": len(data), "new_len": len(out)})
        return out

    def _apply(self, data):
        if self.func is not None:
            return self.func(data)
        out = data
        for rule in self.rules:
            find = rule.get("find", "")
            repl = rule.get("replace", "")
            count = int(rule.get("count", 0))
            if rule.get("regex"):
                out = re.sub(find.encode(), repl.encode(), out,
                             count=count if count else 0)
            else:
                fb, rb = find.encode(), repl.encode()
                if fb:
                    out = out.replace(fb, rb, count if count else -1)
        return out

    def _event(self, obj):
        if self.rec is not None:
            self.rec.event(obj)


def _load_rules(path):
    """Return (rules_list, match_or_None, allow_shrink_bool) from a rules JSON file."""
    import json
    if not path:
        return [], None, False
    with open(path) as f:
        doc = json.load(f)
    if isinstance(doc, list):
        return doc, None, False
    return (doc.get("rules", []) or [], doc.get("match"),
            bool(doc.get("allow_shrink", False)))


def _load_func(spec):
    """Resolve a ``module:func`` (or ``module.func``) dotted spec to a callable."""
    mod, _, name = spec.partition(":")
    if not name:
        mod, _, name = spec.rpartition(".")
    fn = getattr(importlib.import_module(mod), name)
    if not callable(fn):
        raise TypeError(f"AGY_PROC_REWRITE target {spec!r} is not callable")
    return fn
