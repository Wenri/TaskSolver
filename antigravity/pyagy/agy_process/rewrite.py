"""Standalone request-byte transformation utility, retained for rule compatibility.

The current native TLS trampoline does not apply replacements. Public agy clients reject
rewrite requests; the capture decoder never uses this registry to fabricate sent traffic.
Rules operate on UTF-8 bytes and require equal-length output unless allow_shrink is explicit.
"""
import importlib
import os
import re
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from wirecap.types import JSONObject, JSONValue, json_array, json_load, json_object
from ..types import RewriteFunction, RewriteSpec


class EventRecorder(Protocol):
    def event(self, obj: Mapping[str, object]) -> None: ...

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
    def __init__(self, rules: Sequence[RewriteSpec] | None = None,
                 func: RewriteFunction | None = None, match: str | Sequence[str] | None = None,
                 allow_shrink: bool = False, recorder: EventRecorder | None = None,
                 rules_path: str | None = None) -> None:
        self.rules = list(rules or [])
        self.func = func
        self.match = self._markers(match)
        self.allow_shrink = allow_shrink
        self.rec = recorder
        self._path = rules_path
        self._mtime = self._stat(rules_path)
        self._model_streams: set[int] = set()

    @staticmethod
    def _markers(match: str | Sequence[str] | None) -> tuple[bytes, ...]:
        m = match or os.environ.get("AGY_PROC_REWRITE_MATCH")
        markers = (m,) if isinstance(m, str) else (tuple(m) if m else _DEFAULT_MATCH)
        return tuple(s.encode() for s in markers)

    # --- construction ---------------------------------------------------------
    @classmethod
    def from_env(cls, recorder: EventRecorder | None = None) -> "RewriteRegistry":
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
    def _stat(path: str | None) -> float | None:
        try:
            return os.stat(path).st_mtime if path else None
        except OSError:
            return None

    def _maybe_reload(self) -> None:
        if not self._path:
            return
        mtime = self._stat(self._path)
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self.rules, match, _shrink = _load_rules(self._path)
                if match:
                    self.match = self._markers(match)
            except Exception as e:      # keep serving the old rules on a bad reload
                self._event({"kind": "rewrite_error", "phase": "reload", "error": str(e)})

    # --- the hot path ---------------------------------------------------------
    def rewrite(self, stream_id: int, data: bytes) -> bytes | None:
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

    def _apply(self, data: bytes) -> bytes | None:
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

    def _event(self, obj: JSONObject) -> None:
        if self.rec is not None:
            self.rec.event(obj)


def _rule(value: JSONValue) -> RewriteSpec:
    rule = json_object(value)
    find, replacement, count = rule.get("find", ""), rule.get("replace", ""), rule.get("count", 0)
    if not isinstance(find, str) or not isinstance(replacement, str):
        raise TypeError("rewrite find/replace values must be strings")
    if not isinstance(count, (str, int, float)):
        raise TypeError("rewrite count must be numeric")
    return {"find": find, "replace": replacement, "count": int(count),
            "regex": bool(rule.get("regex", False))}


def _load_rules(path: str | None) -> tuple[list[RewriteSpec], str | list[str] | None, bool]:
    """Return (rules_list, match_or_None, allow_shrink_bool) from a rules JSON file."""
    if not path:
        return [], None, False
    with open(path) as f:
        doc = json_load(f)
    if isinstance(doc, list):
        return [_rule(value) for value in doc], None, False
    options = json_object(doc)
    match = options.get("match")
    if isinstance(match, list):
        if any(not isinstance(marker, str) for marker in match):
            raise TypeError("rewrite match markers must be strings")
        match = [marker for marker in match if isinstance(marker, str)]
    elif match is not None and not isinstance(match, str):
        raise TypeError("rewrite match must be a string or list of strings")
    return ([_rule(value) for value in json_array(options.get("rules") or [])], match,
            bool(options.get("allow_shrink", False)))


def _load_func(spec: str) -> RewriteFunction:
    """Resolve a ``module:func`` (or ``module.func``) dotted spec to a callable."""
    mod, _, name = spec.partition(":")
    if not name:
        mod, _, name = spec.rpartition(".")
    fn = cast(object, getattr(importlib.import_module(mod), name))
    if not callable(fn):
        raise TypeError(f"AGY_PROC_REWRITE target {spec!r} is not callable")
    return cast(RewriteFunction, fn)
