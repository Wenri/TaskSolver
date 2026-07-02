"""pyagy public API — talk to agy, modify the request, get a decoded response.

    from pyagy import ask, Session, ToolSpec, ContextResource, RewriteRule

    r = ask("Summarize this repo.")                      # one-shot, decoded
    print(r.text, r.usage.total_tokens, r.model)

    with Session(tools=[ToolSpec("weather", handler="mytools:weather")]) as s:
        print(s.ask("What's the weather in Paris?").text)
        s.set_rewrite([RewriteRule("Paris", "Tokyo")])   # live, equal-length
        print(s.ask("And there?").text)

Two modify mechanisms compose here (the package accepts *both*, per the design):
  * ``rewrite=`` — live equal-length SYNC substitution on the outbound request
    (framing-safe; additive changes are impossible — use tools/context for those).
  * ``tools=`` / ``context=`` — additive, via an injected MCP server (config.py).

When the shim isn't built (or agy's build-id doesn't match the pin), the call
**degrades gracefully** to a clean PTY run: you still get ``.text``/``.transcript``,
just no decoded ``.turns``/``.request``/rewrite — with the reason on
``.instrumented_reason``. Detection is post-hoc from the shim log, so an agy
auto-update never turns a call into a hard failure.
"""
import json
import os
import tempfile
from dataclasses import dataclass, field

from . import _env
from . import config as _config
from ._pty import PtyProcess
from ._term import strip_ansi
from .session import AGY_BIN, ensure_git_workspace

_UNIQ = [0]


# --- declarative inputs ------------------------------------------------------
@dataclass
class ToolSpec:
    """A custom tool exposed to agy via the injected MCP server. Back it with a
    ``handler`` (a top-level callable or ``"module:func"`` string the server imports)
    or a static ``response`` string."""
    name: str
    description: str = ""
    input_schema: dict = None
    handler: object = None
    response: str = ""


@dataclass
class ContextResource:
    """An MCP context resource (inline text) exposed to agy."""
    uri: str
    text: str = ""
    mime_type: str = "text/plain"
    name: str = None


@dataclass
class RewriteRule:
    """One equal-length egress substitution. ``pattern``/``replacement`` are UTF-8
    (byte-for-byte); ``count`` 0 = all; ``regex`` uses ``re`` on bytes. The net length
    must stay equal — a length-changing rule is skipped in-agy and recorded."""
    pattern: str
    replacement: str
    count: int = 0
    regex: bool = False

    def as_dict(self):
        return {"find": self.pattern, "replace": self.replacement,
                "count": self.count, "regex": self.regex}


@dataclass
class Usage:
    prompt_tokens: int = 0
    candidates_tokens: int = 0
    total_tokens: int = 0
    raw: dict = field(default_factory=dict)


# --- response ----------------------------------------------------------------
@dataclass
class AgyResponse:
    text: str
    transcript: str
    turns: list
    exit_status: object
    capture_path: str
    workspace: str
    instrumented: bool
    instrumented_reason: str = ""

    def __str__(self):
        return self.text

    @property
    def primary(self):
        """The substantive model turn (most tokens) — agent turns dwarf the
        title-generation calls. None if nothing was decoded."""
        if not self.turns:
            return None
        return max(self.turns,
                   key=lambda t: (t.get("usage") or {}).get("totalTokenCount", 0))

    @property
    def request(self):
        p = self.primary
        return p.get("request") if p else None

    @property
    def events(self):
        p = self.primary
        return p.get("events", []) if p else []

    @property
    def json(self):
        """The full decoded primary turn (request summary + response text/usage)."""
        return self.primary

    @property
    def model(self):
        r = self.request
        return r.get("model") if r else None

    @property
    def usage(self):
        u = Usage()
        for t in self.turns:
            m = t.get("usage") or {}
            u.prompt_tokens += m.get("promptTokenCount", 0)
            u.candidates_tokens += m.get("candidatesTokenCount", 0)
            u.total_tokens += m.get("totalTokenCount", 0)
        p = self.primary
        if p:
            u.raw = p.get("usage") or {}
        return u


# --- helpers -----------------------------------------------------------------
def _resolve_instrumented(instrumented):
    """(use_instrumented, reason). None auto-detects from the shim presence; the
    build-id verdict is confirmed post-run from the shim log."""
    if instrumented is False:
        return False, "instrumented=False"
    if not os.path.exists(_env.SHIM):
        if instrumented is True:
            return True, "shim missing (run `make -C antigravity`) — will fall back"
        return False, "shim not built"
    return True, ""


def _prepare_rewrite(spec, workdir):
    """Return (env_updates, rules_path). rules_path is None for func/callable specs."""
    env = {"AGY_PROC_TLS_WRITE_SYNC": "1"}
    if isinstance(spec, str):                       # "module:func"
        env["AGY_PROC_REWRITE"] = spec
        return env, None
    if callable(spec):
        mod = getattr(spec, "__module__", None)
        qual = getattr(spec, "__qualname__", getattr(spec, "__name__", None))
        if not mod or mod == "__main__" or "<locals>" in (qual or ""):
            raise ValueError(
                "a callable rewrite must be a top-level importable function "
                f"(got {mod}:{qual}); pass a 'module:func' string or RewriteRule list")
        env["AGY_PROC_REWRITE"] = f"{mod}:{qual}"
        return env, None
    rules = [r.as_dict() if isinstance(r, RewriteRule) else r for r in spec]
    path = os.path.join(workdir, "pyagy-rewrite.json")
    with open(path, "w") as f:
        json.dump({"rules": rules}, f)
    env["AGY_PROC_REWRITE_RULES"] = path
    return env, path


def _load_turns(capture_path, since=0):
    """Return (genai_turn dicts from line ``since`` onward, new line cursor)."""
    turns = []
    n = 0
    if not capture_path or not os.path.exists(capture_path):
        return turns, since
    with open(capture_path) as f:
        for n, line in enumerate(f, 1):
            if n <= since:
                continue
            line = line.strip()
            if not line or '"genai_turn"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("kind") == "genai_turn":
                turns.append(obj)
    return turns, n


def _answer_text(transcript):
    """The clean final answer from the PTY transcript (drop our own log lines)."""
    lines = [ln for ln in transcript.splitlines()
             if ln.strip() and "[antigravity" not in ln
             and "[agy_process]" not in ln and "gohook" not in ln
             and "gomod" not in ln]
    return "\n".join(lines).strip()


def _argv(agy, flag, prompt, model, skip_permissions, extra_flags):
    argv = [agy, flag, prompt]
    if model:
        argv += ["--model", model]
    if skip_permissions:
        argv += ["--dangerously-skip-permissions"]
    if extra_flags:
        argv += list(extra_flags)
    return argv


def _build_env(*, instrumented, stage, capture_path, rewrite, workspace, extra_env):
    """Assemble the child env + (rules_path). Non-instrumented → a clean env."""
    if not instrumented:
        return _env.clean_env(), None
    env = _env.instrumented_env(stage=stage, capture=capture_path, extra_env=extra_env)
    rules_path = None
    if rewrite is not None:
        upd, rules_path = _prepare_rewrite(rewrite, workspace)
        env.update(upd)
    return env, rules_path


def _server_name():
    _UNIQ[0] += 1
    return f"pyagy-{os.getpid()}-{_UNIQ[0]}"


def _inject_config(tools, context):
    """Write an MCP server entry for tools/context. Returns a cleanup callable."""
    if not (tools or context):
        return lambda: None
    name = _server_name()
    _config.write_mcp_config(tools=tools, context=context, server_name=name)
    return lambda: _config.remove_mcp_config(name)


# --- one-shot ----------------------------------------------------------------
def ask(prompt, *, model=None, workspace=None, tools=None, context=None, rewrite=None,
        capture=True, instrumented=None, timeout=300, skip_permissions=False,
        agy_bin=None, extra_env=None, stage=3):
    """Run one ``agy --print`` turn and return a decoded :class:`AgyResponse`."""
    workspace = ensure_git_workspace(workspace)
    agy = agy_bin or AGY_BIN
    use_instr, reason = _resolve_instrumented(instrumented)

    cap_path = None
    if use_instr and capture:
        cap_path = capture if isinstance(capture, str) else \
            os.path.join(workspace, "pyagy-capture.jsonl")
        open(cap_path, "w").close()   # truncate so we only read this run's turns

    log_path = os.path.join(workspace, "pyagy-shim.log") if use_instr else None
    env, _ = _build_env(instrumented=use_instr, stage=stage, capture_path=cap_path,
                        rewrite=rewrite, workspace=workspace, extra_env=extra_env)
    if log_path:
        env["AGY_PROC_LOG"] = log_path

    cleanup = _inject_config(tools, context) if use_instr else lambda: None
    try:
        proc = PtyProcess().spawn(_argv(agy, "--print", prompt, model,
                                        skip_permissions, extra_flags=None),
                                  workspace, env)
        transcript = proc.read_until_exit(timeout=timeout)
        proc.close(interrupt=False)
    finally:
        cleanup()

    # Confirm the shim actually hooked (build-id match) for the instrumented flag.
    if use_instr and log_path and os.path.exists(log_path):
        logtxt = open(log_path, errors="replace").read()
        if "build-id ok" not in logtxt:
            use_instr = False
            reason = "shim build-id != running agy (run `make -C antigravity symbols`)"

    turns, _ = _load_turns(cap_path) if (cap_path and use_instr) else ([], 0)
    return AgyResponse(
        text=_answer_text(transcript), transcript=transcript, turns=turns,
        exit_status=proc.status, capture_path=cap_path, workspace=workspace,
        instrumented=use_instr, instrumented_reason=reason)


# --- multi-turn --------------------------------------------------------------
class Session:
    """A multi-turn agy session. Same kwargs as :func:`ask`; ``ask(prompt)`` starts
    it on first call. ``set_rewrite(spec)`` updates the live rewrite rules (picked up
    in-agy on mtime). Use as a context manager to guarantee cleanup."""

    def __init__(self, *, model=None, workspace=None, tools=None, context=None,
                 rewrite=None, capture=True, instrumented=None, timeout=180,
                 idle=25.0, agy_bin=None, extra_env=None, stage=3):
        self.workspace = ensure_git_workspace(workspace)
        self.agy = agy_bin or AGY_BIN
        self.model = model
        self.timeout = timeout
        self.idle = idle
        self.capture = capture
        self.stage = stage
        self.extra_env = extra_env
        self.rewrite = rewrite
        self._tools = tools
        self._context = context
        self.instrumented, self.instrumented_reason = _resolve_instrumented(instrumented)
        self.cap_path = None
        self.log_path = None
        self.rules_path = None
        self._cursor = 0
        self._cleanup = lambda: None
        self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _start(self, prompt):
        if self.instrumented and self.capture:
            self.cap_path = os.path.join(self.workspace, "pyagy-session.jsonl")
            open(self.cap_path, "w").close()
        self.log_path = (os.path.join(self.workspace, "pyagy-session.log")
                         if self.instrumented else None)
        env, self.rules_path = _build_env(
            instrumented=self.instrumented, stage=self.stage, capture_path=self.cap_path,
            rewrite=self.rewrite, workspace=self.workspace, extra_env=self.extra_env)
        if self.log_path:
            env["AGY_PROC_LOG"] = self.log_path
        self._cleanup = _inject_config(self._tools, self._context) if self.instrumented else (lambda: None)
        argv = [self.agy, "--prompt-interactive", prompt]
        if self.model:
            argv += ["--model", self.model]
        self.proc = PtyProcess()
        self.proc.spawn(argv, self.workspace, env)

    def ask(self, prompt):
        """Send ``prompt`` (starting the session on first call) and return the
        :class:`AgyResponse` for the turn(s) it produced."""
        if self.proc is None:
            self._start(prompt)
            transcript = self.proc.read_until_idle(idle=self.idle, timeout=self.timeout)
        else:
            self.proc.send_line(prompt)
            transcript = self.proc.read_until_idle(idle=self.idle, timeout=self.timeout)
        if self.instrumented and self.log_path and os.path.exists(self.log_path):
            if "build-id ok" not in open(self.log_path, errors="replace").read():
                self.instrumented = False
                self.instrumented_reason = "shim build-id != running agy"
        turns, self._cursor = (_load_turns(self.cap_path, self._cursor)
                               if (self.cap_path and self.instrumented) else ([], self._cursor))
        return AgyResponse(
            text=_answer_text(transcript), transcript=transcript, turns=turns,
            exit_status=None, capture_path=self.cap_path, workspace=self.workspace,
            instrumented=self.instrumented, instrumented_reason=self.instrumented_reason)

    def set_rewrite(self, spec):
        """Replace the live rewrite spec. For a RewriteRule list this rewrites the
        rules file the in-agy side hot-reloads; for func/str specs it takes effect on
        the next session start."""
        self.rewrite = spec
        if self.rules_path is not None and isinstance(spec, (list, tuple)):
            rules = [r.as_dict() if isinstance(r, RewriteRule) else r for r in spec]
            with open(self.rules_path, "w") as f:
                json.dump({"rules": rules}, f)
        return self

    def send(self, data):
        self.proc.write(data if isinstance(data, (bytes, bytearray)) else str(data).encode())

    def read(self, idle=None, timeout=None):
        return self.proc.read_until_idle(idle=idle or self.idle,
                                         timeout=timeout or self.timeout)

    def close(self):
        try:
            if self.proc is not None:
                self.proc.close(interrupt=True)
        finally:
            self._cleanup()
