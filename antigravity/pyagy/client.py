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

Every call installs the full working hook union, so one run captures all surfaces at once:
  * ``.turns``/``.request``/``.usage``/``.model`` — the decoded wire model turn (and the
    ``rewrite=`` SYNC-substitution surface);
  * ``.app_text``/``.source`` — the answer decoded at agy's own consumer boundary;
  * ``.rpc_trace`` — the labeled backend-RPC timeline;
  * ``stack=True`` → ``.stacks``/``.call_graph`` (symbolized Go call stacks);
  * ``arg_probe=True`` → ``.cgt_args`` (trampoline arg-graph reports).
The accessors are lazy: reading one decodes its capture on demand (and returns
empty/None when this run didn't capture that kind). Because the app-boundary answer is
now always captured, ``.text``/``.source`` prefer it over the wire transcript.

Every call is instrumented (shim + capture on the pinned vendor/agy) via
:class:`pyagy.agyprocess.AgyProcess` — the single agy launcher.
"""
import json
import os
from dataclasses import dataclass, field
from functools import cached_property

from . import config as _config
from . import conversations as _conv
from ._term import answer_text as _answer_text
from .agyprocess import AgyProcess
from .session import ensure_git_workspace

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
    app_turns: list = field(default_factory=list)   # app-boundary answers (fh_update)
    funcmap: str = None                              # symbols/funcmap.tsv.gz for .stacks
    conversation_id: str = None                      # agy's native conversation id (resumable)

    def __str__(self):
        return self.text

    @property
    def app_text(self):
        """The assembled answer captured at agy's own consumer boundary
        (``updateWithStep``, a single shallow deref) — the app-boundary RESPONSE. ``""``
        when the capture holds no ``app_response`` events. The text-bearing fires carry
        the full answer, so take the longest."""
        return max(self.app_turns, key=len) if self.app_turns else ""

    @property
    def source(self):
        """Where ``.text`` came from: ``"app"`` (app-boundary decode, preferred),
        ``"wire"`` (http1sse genai_turn), or ``"transcript"`` (PTY fallback)."""
        if self.app_turns:
            return "app"
        return "wire" if self.turns else "transcript"

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

    # --- diagnostic decoders (present only when the matching capture exists) ---
    # These lazily import their agy_process module inside the accessor so a plain
    # `import pyagy` / `ask()` never loads the RPC/stack/graph decoders (or the
    # 132k-row funcmap) unless you actually read the attribute. Each degrades to a
    # falsy/None value when its events (or the funcmap) aren't in this run's capture.
    def _has_capture(self):
        return bool(self.capture_path and self.instrumented
                    and os.path.exists(self.capture_path))

    @cached_property
    def rpc_trace(self):
        """A time-ordered, labeled RPC timeline from the ``CodeAssistClient`` hooks —
        ``StreamGenerateContent`` is the model turn. ``""`` when the capture holds no
        ``rpc_*`` events."""
        if not self._has_capture():
            return ""
        from .agy_process import rpctrace
        return rpctrace.trace(self.capture_path)

    @cached_property
    def cgt_args(self):
        """The trampoline arg-graph reports captured with ``arg_probe=True``
        (``AGY_PROC_CGT_ARGS``) — one rendered string per hook fire. ``[]`` otherwise."""
        if not self._has_capture():
            return []
        reports = []
        with open(self.capture_path) as f:
            for line in f:
                if '"cgt_args"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("kind") == "cgt_args" and o.get("report"):
                    reports.append(o["report"])
        return reports

    @cached_property
    def _symbolizer(self):
        """A funcmap-backed Symbolizer, or None if the funcmap file is absent
        (``make -C antigravity symbols`` produces it; it's gitignored)."""
        from .agy_process import symbolize
        path = self.funcmap or symbolize.DEFAULT_FUNCMAP
        if not os.path.exists(path):
            return None
        return symbolize.Symbolizer(path)

    @cached_property
    def stacks(self):
        """Symbolized, grouped call stacks captured with ``stack=True``
        (``AGY_PROC_STACK``) — a rendered string. ``None`` when there's no capture;
        a short reason string when the funcmap is missing."""
        if not self._has_capture():
            return None
        sym = self._symbolizer
        if sym is None:
            from .agy_process import symbolize
            return (f"(funcmap not found at {self.funcmap or symbolize.DEFAULT_FUNCMAP}; "
                    "run `make -C antigravity symbols`)")
        from .agy_process import symbolize
        return symbolize.render_stacks(self.capture_path, sym)

    @cached_property
    def call_graph(self):
        """Caller→callee edge counts from the ``stack=True`` capture — a
        ``collections.Counter`` keyed by ``(caller, callee)``. ``None`` without a
        capture or funcmap."""
        if not self._has_capture():
            return None
        sym = self._symbolizer
        if sym is None:
            return None
        from .agy_process import symbolize
        return symbolize.call_graph(self.capture_path, sym)


# --- helpers -----------------------------------------------------------------
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


def _load_capture(capture_path, since=0):
    """Single pass over the capture from line ``since`` onward. Returns
    ``(genai_turns, app_texts, new_cursor)`` — ``genai_turns`` are the wire-decoded
    model turns (http1sse), ``app_texts`` are the app-boundary ``app_response`` answer
    strings (updateWithStep). Both come from the same file so one cursor
    covers a multi-turn session."""
    turns, app_texts = [], []
    n = since
    if not capture_path or not os.path.exists(capture_path):
        return turns, app_texts, since
    with open(capture_path) as f:
        for n, line in enumerate(f, 1):
            if n <= since:
                continue
            line = line.strip()
            if not line or ('"genai_turn"' not in line and '"app_response"' not in line):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind = obj.get("kind")
            if kind == "genai_turn":
                turns.append(obj)
            elif kind == "app_response":
                t = obj.get("text", "")
                if t:
                    app_texts.append(t)
    return turns, app_texts, n


def _shim_overlays(rewrite, workspace, stack, arg_probe, extra_env):
    """Shim knobs layered onto AgyProcess's instrumented env (passed as its ``extra_env``):
    the ``stack``/``arg_probe`` diagnostics (call-stack unwind / trampoline arg-graph) and
    the ``rewrite`` spec. Returns ``(overlay_env, rules_path)`` — ``rules_path`` is the live
    rewrite-rules file the in-agy side hot-reloads (or None for func/str/no rewrite)."""
    env = dict(extra_env or {})
    if stack:
        env["AGY_PROC_STACK"] = "1"
    if arg_probe:
        env["AGY_PROC_CGT_ARGS"] = "1"
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
        capture=True, timeout=300, skip_permissions=False, agy_bin=None, extra_env=None,
        stack=False, arg_probe=False, funcmap=None, conversation_id=None,
        continue_latest=False, data_dir=None, trust=True):
    """Run one ``agy --print`` turn and return a decoded :class:`AgyResponse`.

    The shim installs the full working hook union, so one turn populates the wire
    (``.turns``/``.request``/``.usage``/``.model``), app-boundary (``.app_text``), and
    RPC-trace (``.rpc_trace``) surfaces together. The overlays ``stack=True`` /
    ``arg_probe=True`` add the diagnostic call-stack / trampoline arg-graph, surfaced on
    ``.stacks``/``.call_graph`` and ``.cgt_args``. ``funcmap`` overrides the symbol map
    used by ``.stacks``.

    ``conversation_id`` resumes a stored conversation (``--conversation=<id>``, works in
    print mode) and ``continue_latest`` resumes the most recent one (``--continue``); the
    resulting :attr:`AgyResponse.conversation_id` is the id this run created/continued, so
    a later ``ask(..., conversation_id=r.conversation_id)`` continues it.

    ``data_dir`` scopes agy's whole conversation store under that directory (a project repo)
    instead of the global ``~/.gemini`` — login is preserved by seeding credentials (see
    :func:`pyagy.conversations.prepare_scoped_home`). ``trust`` (default on) pre-registers the
    workspace in agy's ``trustedWorkspaces`` so interactive mode never blocks on the
    folder-trust prompt."""
    workspace = ensure_git_workspace(workspace)
    cap_path = None
    if capture:
        cap_path = capture if isinstance(capture, str) else \
            os.path.join(workspace, "pyagy-capture.jsonl")
        open(cap_path, "w").close()   # truncate so we only read this run's turns
    overlays, _ = _shim_overlays(rewrite, workspace, stack, arg_probe, extra_env)
    overlays["AGY_PROC_LOG"] = os.path.join(workspace, "pyagy-shim.log")  # shim logs off the PTY

    cleanup = _inject_config(tools, context)
    try:
        p = AgyProcess(prompt=prompt, model=model, skip_permissions=skip_permissions,
                       agy_bin=agy_bin, workdir=workspace, capture=cap_path,
                       conversation_id=conversation_id, continue_latest=continue_latest,
                       data_dir=data_dir, trust=trust, extra_env=overlays)
        p.start()
        objs = p.collect(timeout=timeout)           # decoded answer streamed home over the Connection
        transcript = p.transcript                   # raw PTY (fallback / diagnostics)
        cid, exit_status = p.conversation_id, p.exit_status
        p.close()
    finally:
        cleanup()

    turns = [o for o in objs if o.get("kind") == "genai_turn"]
    app_texts = [o["text"] for o in objs if o.get("kind") == "app_response" and o.get("text")]
    # Prefer the app-boundary answer (fh_update) when present; else the PTY transcript.
    text = max(app_texts, key=len) if app_texts else _answer_text(transcript)
    return AgyResponse(
        text=text, transcript=transcript, turns=turns, app_turns=app_texts,
        exit_status=exit_status, capture_path=cap_path, workspace=workspace,
        instrumented=True, instrumented_reason="", funcmap=funcmap, conversation_id=cid)


# --- multi-turn --------------------------------------------------------------
class Session:
    """A multi-turn agy session — **the first-class object of pyagy**. Same kwargs as
    :func:`ask`; ``ask(prompt)`` starts it on first call and continues it thereafter.

    In-run turns ride one live ``agy --prompt-interactive`` process. Across a restart,
    agy's *native* conversation store keeps context: pass ``conversation_id=`` (resume a
    specific stored conversation) or ``continue_latest=True`` (resume the most recent),
    or use the module helpers :func:`resume` / :func:`continue_latest`. After the first
    turn, :attr:`conversation_id` holds this session's id — persist it to resume later,
    and read :meth:`history` for the stored transcript.

    ``set_rewrite(spec)`` updates the live rewrite rules (picked up in-agy on mtime). Use
    as a context manager to guarantee cleanup."""

    def __init__(self, *, model=None, workspace=None, tools=None, context=None,
                 rewrite=None, capture=True, timeout=180, idle=25.0, agy_bin=None,
                 extra_env=None, stack=False, arg_probe=False, funcmap=None,
                 conversation_id=None, continue_latest=False, skip_permissions=False,
                 data_dir=None, trust=True):
        self.workspace = ensure_git_workspace(workspace)
        self.agy_bin = agy_bin
        self.model = model
        self.timeout = timeout
        self.idle = idle
        self.capture = capture
        self.stack = stack
        self.arg_probe = arg_probe
        self.funcmap = funcmap
        self.extra_env = extra_env
        self.rewrite = rewrite
        self._tools = tools
        self._context = context
        self.continue_latest = continue_latest      # start with --continue (most recent)
        self.skip_permissions = skip_permissions
        self._data_dir = data_dir                    # scope the conversation store to a repo
        self._trust = trust                          # pre-trust the workspace (no folder prompt)
        self._home = None                            # resolved scoped home (None = global store)
        self._conversation_id = conversation_id      # resume this id; else captured after turn 1
        self.cap_path = None
        self.rules_path = None
        self._cleanup = lambda: None
        self._agy = None                             # the AgyProcess (persistent), set on first ask

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _start(self, prompt):
        if self.capture:
            self.cap_path = os.path.join(self.workspace, "pyagy-session.jsonl")
            open(self.cap_path, "w").close()
        overlays, self.rules_path = _shim_overlays(self.rewrite, self.workspace,
                                                   self.stack, self.arg_probe, self.extra_env)
        overlays["AGY_PROC_LOG"] = os.path.join(self.workspace, "pyagy-session.log")
        self._cleanup = _inject_config(self._tools, self._context)
        self._agy = AgyProcess(persistent=True, prompt=prompt, model=self.model,
                               skip_permissions=self.skip_permissions, agy_bin=self.agy_bin,
                               workdir=self.workspace, capture=self.cap_path,
                               conversation_id=self._conversation_id,
                               continue_latest=self.continue_latest,
                               data_dir=self._data_dir, trust=self._trust, extra_env=overlays)
        self._agy.start()
        self._home = self._agy.home                  # scoped store home (for .history())

    def ask(self, prompt):
        """Send ``prompt`` (starting the session on first call) and return the
        :class:`AgyResponse` for the turn it produced (decoded objects streamed home over the
        Connection; the PTY transcript is the fallback)."""
        if self._agy is None:
            self._start(prompt)
            objs = self._agy.ask(None, idle=self.idle, timeout=self.timeout)   # submit the prefill
        else:
            objs = self._agy.ask(prompt, idle=self.idle, timeout=self.timeout)
        turns = [o for o in objs if o.get("kind") == "genai_turn"]
        app_texts = [o["text"] for o in objs if o.get("kind") == "app_response" and o.get("text")]
        transcript = self._agy.transcript
        if self._conversation_id is None:            # first turn of a fresh session
            self._conversation_id = self._agy.conversation_id
        text = max(app_texts, key=len) if app_texts else _answer_text(transcript)
        return AgyResponse(
            text=text, transcript=transcript, turns=turns, app_turns=app_texts,
            exit_status=None, capture_path=self.cap_path, workspace=self.workspace,
            instrumented=True, instrumented_reason="",
            funcmap=self.funcmap, conversation_id=self._conversation_id)

    @property
    def conversation_id(self):
        """agy's native conversation id for this session — the resumed id, or the one
        captured after the first turn. Persist it and pass to :func:`resume` to continue
        this conversation in a later process."""
        return self._conversation_id

    def history(self):
        """The stored transcript for this conversation, read from agy's own store — a list
        of ``{step_index, role, type, status, created_at, content}`` (see
        :func:`pyagy.conversations.read_transcript`). Empty until an id is known."""
        if not self._conversation_id:
            return []
        return _conv.read_transcript(self._conversation_id, home=self._home)

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

    def close(self):
        try:
            if self._agy is not None:
                self._agy.close(interrupt=True)
        finally:
            self._cleanup()


# --- session entry points (Session is the first-class object of pyagy) -------
def resume(conversation_id, **kwargs):
    """A :class:`Session` that resumes the stored conversation ``conversation_id``
    (``agy --conversation=<id>``). Its first ``.ask()`` continues that conversation with
    full prior context — even in a brand-new process. ``**kwargs`` are :class:`Session`'s."""
    return Session(conversation_id=conversation_id, **kwargs)


def continue_latest(**kwargs):
    """A :class:`Session` resuming agy's most recent conversation (``agy --continue``)."""
    return Session(continue_latest=True, **kwargs)


# Read-only store helpers, re-exported so `pyagy.list_conversations()` / `.latest_...` work.
list_conversations = _conv.list_conversations
latest_conversation_id = _conv.latest_conversation_id
