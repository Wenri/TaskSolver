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
import time
from dataclasses import dataclass, field
from functools import cached_property
from multiprocessing import get_context as _get_context

from . import config as _config
from . import conversations as _conv
from ._term import answer_text as _answer_text
from ._pty import service_many as _service_many
from .agyprocess import AgyProcess
from .conversations import ensure_git_workspace

_UNIQ = [0]
_ANSWER_KINDS = ("genai_turn", "app_response")   # decoded objects the default target streams home
_SPAWN = _get_context("spawn")                    # context for the caller-owned result SimpleQueue


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

    @classmethod
    def from_objs(cls, objs, transcript, **meta):
        """Build a response from the decoded objects the worker streamed home + the PTY
        transcript. One answer policy: the app-boundary text (longest ``app_response``), else
        the wire turn (longest ``genai_turn`` text), else the filtered transcript — so ``.text``
        and ``.source`` always agree. ``**meta`` supplies exit_status/capture_path/workspace/
        funcmap/conversation_id (``instrumented`` defaults True)."""
        turns = [o for o in objs if o.get("kind") == "genai_turn"]
        app_texts = [o["text"] for o in objs if o.get("kind") == "app_response" and o.get("text")]
        wire_texts = [o["text"] for o in turns if o.get("text")]
        text = (max(app_texts, key=len) if app_texts else
                max(wire_texts, key=len) if wire_texts else _answer_text(transcript))
        meta.setdefault("instrumented", True)
        return cls(text=text, transcript=transcript, turns=turns, app_turns=app_texts, **meta)

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
        ``"wire"`` (http1sse genai_turn), or ``"transcript"`` (PTY fallback). Matches the
        answer policy in :meth:`from_objs`."""
        if self.app_turns:
            return "app"
        if any(t.get("text") for t in self.turns):
            return "wire"
        return "transcript"

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
        (``pixi run shim-symbols`` produces it; it's gitignored)."""
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
                    "run `pixi run shim-symbols`)")
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


# --- caller-owned result channel (the queue + drain helpers) -----------------
# Stock-mp layering: we create the SimpleQueue, hand it to AgyProcess via ``args=(q,)`` (so it rides
# the process pickle), and drain it here — AgyProcess is just the Process. ``proc.service_pty`` keeps
# agy's PTY drained in the same wait we use to read the queue, so no background thread is needed.
def _new_channel():
    """A spawn-context result SimpleQueue for one agy run. The caller keeps the reader; the child
    (agy) inherits both ends across execve and drops the reader. Close the parent's writer right
    after ``proc.start()`` so the reader EOFs when agy dies (crash detection)."""
    return _SPAWN.SimpleQueue()


def _close_channel(q):
    """Tear down the queue's pipe ends (idempotent). Its named SemLocks unlink via their own
    resource_tracker Finalize when ``q`` is GC'd; this just makes the fd close prompt."""
    for c in (q._reader, q._writer):
        try:
            c.close()
        except Exception:
            pass


def _collect(proc, q, timeout=300.0, kinds=_ANSWER_KINDS):
    """One-shot: drain the PTY and collect the decoded objects (of ``kinds``) the target streams home
    over ``q`` until agy exits / the target signals done (``_agy_done``/``_agy_exc``) / ``timeout``.
    Returns the dicts in arrival order (possibly empty; use ``proc.transcript`` as the fallback)."""
    reader = q._reader
    got, start = [], time.time()
    while time.time() - start < timeout:
        if proc.service_pty(1.0, [reader]):        # drains the PTY; True when the queue is readable
            try:
                while reader.poll(0):
                    o = q.get()
                    if isinstance(o, tuple) and o and o[0] in ("_agy_done", "_agy_exc"):
                        proc.reap()                 # reap agy so exit_status is set
                        return got
                    if isinstance(o, dict) and o.get("kind") in kinds:
                        got.append(o)
            except EOFError:
                proc.reap()                         # agy exited — the normal one-shot completion
                return got
    return got


def _ask_turn(proc, q, prompt=None, idle=6.0, pty_idle=15.0, timeout=180.0, ready=2.5,
              kinds=_ANSWER_KINDS):
    """Persistent multi-turn: submit ``prompt`` (or the ``--prompt-interactive`` prefill if None),
    then collect the decoded objects (of ``kinds``) for that turn from ``q`` until it settles (no new
    object for ``idle`` s, or agy stays quiet ``pty_idle`` s with none), or ``timeout``. Drains the
    PTY meanwhile."""
    reader = q._reader
    rstart = time.time()                 # wait until agy is ready (TUI drawn / prior turn done)
    while time.time() - rstart < 30 and time.time() - proc.last_output < ready:
        proc.service_pty(0.2, [reader])  # drain the PTY while waiting for agy to settle
    if prompt is None:
        proc.write(b"\r")                # submit the prefilled initial prompt
    else:
        proc.send_line(prompt)           # type + submit a follow-up
    proc.last_output = time.time()       # measure idle from the submit, not the prior turn
    got, last, start = [], None, time.time()
    while time.time() - start < timeout:
        if proc.service_pty(0.2, [reader]):        # drains the PTY; True once a result is ready
            while reader.poll(0):
                try:
                    o = q.get()
                except EOFError:
                    return got
                if isinstance(o, dict) and o.get("kind") in kinds:
                    got.append(o)
                    last = time.time()
        now = time.time()
        if last is not None and now - last >= idle:
            break                        # turn(s) settled
        if last is None and now - proc.last_output >= pty_idle:
            break                        # agy went idle without producing a turn
    return got


def _collect_many(procs, queues, timeout=300.0, kinds=_ANSWER_KINDS):
    """One-shot collect from several already-``start()``ed AgyProcesses concurrently, in one event
    loop. ``procs`` and ``queues`` are parallel. A single ``_pty.service_many`` watches every live
    proc's PTY + queue reader together, so all PTYs are drained while each proc's ``kinds`` objects
    are gathered until it signals done or its queue EOFs. Returns a list parallel to ``procs`` — each
    entry that proc's dicts in arrival order."""
    got = [[] for _ in procs]
    done = [False] * len(procs)
    readers = [q._reader for q in queues]
    reader_idx = {readers[i]: i for i in range(len(procs))}
    end = time.time() + timeout
    while not all(done) and time.time() < end:
        live = [i for i in range(len(procs)) if not done[i]]
        if not live:
            break
        pops = [procs[i]._popen for i in live]         # PTY-multiplex is a _pty-layer primitive
        rds = [readers[i] for i in live]
        for r in _service_many(pops, rds, max(0.0, end - time.time())):
            i = reader_idx[r]
            try:
                while r.poll(0):
                    o = queues[i].get()
                    if isinstance(o, tuple) and o and o[0] in ("_agy_done", "_agy_exc"):
                        procs[i].reap()               # reap agy so exit_status is set
                        done[i] = True
                        break
                    if isinstance(o, dict) and o.get("kind") in kinds:
                        got[i].append(o)
            except EOFError:
                procs[i].reap()                       # agy exited — the normal completion signal
                done[i] = True
    return got


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
    q = _new_channel()                              # caller owns the result queue (stock-mp style)
    try:
        p = AgyProcess(prompt=prompt, model=model, skip_permissions=skip_permissions,
                       agy_bin=agy_bin, workdir=workspace, capture=cap_path, args=(q,),
                       conversation_id=conversation_id, continue_latest=continue_latest,
                       data_dir=data_dir, trust=trust, extra_env=overlays)
        p.start()
        q._writer.close()                           # parent reads only; reader now EOFs on agy death
        objs = _collect(p, q, timeout=timeout)      # decoded answer streamed home over the queue
        transcript = p.transcript                   # raw PTY (fallback / diagnostics)
        cid, exit_status = p.conversation_id, p.exit_status
        p.close()
    finally:
        _close_channel(q)
        cleanup()

    return AgyResponse.from_objs(
        objs, transcript, exit_status=exit_status, capture_path=cap_path,
        workspace=workspace, funcmap=funcmap, conversation_id=cid)


def ask_many(prompt, n, *, model=None, workspace=None, tools=None, context=None, rewrite=None,
             capture=True, timeout=300, skip_permissions=False, agy_bin=None, extra_env=None,
             stack=False, arg_probe=False, funcmap=None, conversation_id=None,
             continue_latest=False, data_dir=None, trust=True):
    """Run ``n`` independent ``agy --print`` turns of the same ``prompt`` concurrently and
    return a list of ``n`` decoded :class:`AgyResponse` (parallel sampling — e.g. ``AgyModel``
    with ``n_choices>1``). Same kwargs as :func:`ask`; all share one workspace + tools/context,
    each gets its own capture file. No threads: every process is ``start()``ed (a fast, serial,
    non-blocking fork), then the caller-side ``_collect_many`` services them all in one event loop
    (draining every PTY + result queue together). ``n <= 1`` delegates to :func:`ask`."""
    if n <= 1:
        return [ask(prompt, model=model, workspace=workspace, tools=tools, context=context,
                    rewrite=rewrite, capture=capture, timeout=timeout,
                    skip_permissions=skip_permissions, agy_bin=agy_bin, extra_env=extra_env,
                    stack=stack, arg_probe=arg_probe, funcmap=funcmap,
                    conversation_id=conversation_id, continue_latest=continue_latest,
                    data_dir=data_dir, trust=trust)]
    workspace = ensure_git_workspace(workspace)
    overlays, _ = _shim_overlays(rewrite, workspace, stack, arg_probe, extra_env)
    overlays["AGY_PROC_LOG"] = os.path.join(workspace, "pyagy-shim.log")
    cap_paths = [os.path.join(workspace, f"pyagy-capture-{i}.jsonl") if capture else None
                 for i in range(n)]
    for c in cap_paths:
        if c:
            open(c, "w").close()                 # truncate so each reads only its own run
    cleanup = _inject_config(tools, context)     # one shared MCP config for all n
    queues = [_new_channel() for _ in range(n)]   # one caller-owned result queue per proc
    try:
        procs = [AgyProcess(prompt=prompt, model=model, skip_permissions=skip_permissions,
                            agy_bin=agy_bin, workdir=workspace, capture=cap_paths[i], args=(queues[i],),
                            conversation_id=conversation_id, continue_latest=continue_latest,
                            data_dir=data_dir, trust=trust, extra_env=overlays)
                 for i in range(n)]
        for p, q in zip(procs, queues):
            p.start()                            # non-blocking fork; serial → no fork-in-thread
            q._writer.close()                    # parent reads only; reader EOFs on that agy's death
        objs_list = _collect_many(procs, queues, timeout=timeout)   # one event loop services all n
        responses = [AgyResponse.from_objs(
                         objs, p.transcript, exit_status=p.exit_status, capture_path=cap,
                         workspace=workspace, funcmap=funcmap, conversation_id=p.conversation_id)
                     for p, objs, cap in zip(procs, objs_list, cap_paths)]
        for p in procs:
            p.close()
        return responses
    finally:
        for q in queues:
            _close_channel(q)
        cleanup()


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
        self._q = None                               # caller-owned result queue, beside self._agy

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
        self._q = _new_channel()                     # caller-owned result queue for this session
        self._agy = AgyProcess(persistent=True, prompt=prompt, model=self.model,
                               skip_permissions=self.skip_permissions, agy_bin=self.agy_bin,
                               workdir=self.workspace, capture=self.cap_path, args=(self._q,),
                               conversation_id=self._conversation_id,
                               continue_latest=self.continue_latest,
                               data_dir=self._data_dir, trust=self._trust, extra_env=overlays)
        self._agy.start()
        self._q._writer.close()                      # parent reads only; reader EOFs on agy death
        self._home = self._agy.home                  # scoped store home (for .history())

    def ask(self, prompt):
        """Send ``prompt`` (starting the session on first call) and return the
        :class:`AgyResponse` for the turn it produced (decoded objects streamed home over the
        caller-owned result queue; the PTY transcript is the fallback)."""
        if self._agy is None:
            self._start(prompt)
            objs = _ask_turn(self._agy, self._q, None, idle=self.idle, timeout=self.timeout)  # prefill
        else:
            objs = _ask_turn(self._agy, self._q, prompt, idle=self.idle, timeout=self.timeout)
        transcript = self._agy.transcript
        if self._conversation_id is None:            # first turn of a fresh session
            self._conversation_id = self._agy.conversation_id
        return AgyResponse.from_objs(
            objs, transcript, exit_status=None, capture_path=self.cap_path,
            workspace=self.workspace, funcmap=self.funcmap,
            conversation_id=self._conversation_id)

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
            if self._q is not None:
                _close_channel(self._q)
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
