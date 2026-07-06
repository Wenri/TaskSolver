export const meta = {
  name: 'agy-mp-process-design',
  description: 'Design an agy version of multiprocessing.Process (spawn model) + PTY handling in the LD_PRELOAD shim: fan-out research, adversarial red-team, synthesize an implementation plan',
  phases: [
    { title: 'Understand' },
    { title: 'Design' },
    { title: 'RedTeam' },
    { title: 'Synthesize' },
  ],
}

const REPO = '/home/wenri/Git/TaskSolver/antigravity'
const CTX = `PROJECT CONTEXT (read-only investigation — do NOT edit anything):
Goal: make Google's \`agy\` CLI pluggable into Python's multiprocessing under the SPAWN
start method — an \`AgyProcess\` that behaves like multiprocessing.Process (.start/.join/
.exitcode) where the parent gets a Queue/Pipe/Connection to receive data the shim captures
from agy. Motivation: "easier data passing" than the current capture-JSONL file.

Key facts:
- \`agy\` is a stripped Go ELF (x86-64), LD_PRELOADed with our shim \`${REPO}/vendor/antigravity.so\`.
- The shim (src/pybridge.c) EMBEDS CPython (Py_InitializeEx(0)) on a worker thread INSIDE agy,
  importing pyagy.agy_process; hooks push events via a C job queue to that worker (today it
  writes a capture JSONL). src/gohook.c hooks fire on agy goroutines (cgocall trampoline).
- agy is a TUI needing a controlling PTY. multiprocessing spawn children get NO PTY. The user
  explicitly wants us to consider changing the shim's code/threading to set up the PTY
  (openpty/setsid/TIOCSCTTY), and/or a custom Popen that allocates the PTY.
- CRITICAL known hazard to evaluate: the PARENT driver runs under the pixi env's Python 3.13;
  the shim embeds the SYSTEM libpython (3.12). pickle/spawn across mismatched CPython versions.
- Repo files: ${REPO}/src/{pybridge.c,pybridge.h,antigravity.c,gohook.c,proc.def},
  ${REPO}/pyagy/{_pty.py,_term.py,_env.py,session.py,client.py,model.py},
  ${REPO}/pyagy/agy_process/{__init__.py,capture.py,http1sse.py,hooks.py}, ${REPO}/Makefile.
- CPython multiprocessing source: find via \`python3 -c "import multiprocessing,os;print(os.path.dirname(multiprocessing.__file__))"\`
  (check BOTH the pixi 3.13 and system 3.12: \`/usr/lib/python3.12/multiprocessing\`).
Be concrete, cite file:line and exact symbols. This is WSL1 (kernel 4.4 "-Microsoft"); flag
any POSIX feature that WSL1 breaks.`

const FINDING = {
  type: 'object', additionalProperties: false,
  properties: {
    facet: { type: 'string' },
    summary: { type: 'string' },
    key_findings: { type: 'array', items: { type: 'string' } },
    mechanisms: { type: 'array', items: { type: 'string' } },
    gotchas: { type: 'array', items: { type: 'string' } },
    sources: { type: 'array', items: { type: 'string' } },
  },
  required: ['facet', 'summary', 'key_findings'],
}
const DESIGN_OUT = {
  type: 'object', additionalProperties: false,
  properties: {
    subproblem: { type: 'string' },
    approach: { type: 'string' },
    mechanism: { type: 'string' },
    files_touched: { type: 'array', items: { type: 'string' } },
    pros: { type: 'array', items: { type: 'string' } },
    cons: { type: 'array', items: { type: 'string' } },
    depends_on: { type: 'array', items: { type: 'string' } },
    open_questions: { type: 'array', items: { type: 'string' } },
  },
  required: ['subproblem', 'approach', 'mechanism'],
}
const HAZARD_OUT = {
  type: 'object', additionalProperties: false,
  properties: {
    hazard: { type: 'string' },
    verdict: { type: 'string', enum: ['FATAL', 'SERIOUS', 'MANAGEABLE', 'NON_ISSUE'] },
    reasoning: { type: 'string' },
    evidence: { type: 'array', items: { type: 'string' } },
    mitigation: { type: 'string' },
  },
  required: ['hazard', 'verdict', 'reasoning'],
}
const SYNTH_OUT = {
  type: 'object', additionalProperties: false,
  properties: {
    verdict_feasibility: { type: 'string' },
    recommended_design: { type: 'string' },
    api_surface: { type: 'string' },
    spawn_integration: { type: 'string' },
    pty_approach: { type: 'string' },
    shim_changes: { type: 'string' },
    data_channel: { type: 'string' },
    fd_inheritance_plan: { type: 'string' },
    version_mismatch_resolution: { type: 'string' },
    implementation_steps: { type: 'array', items: { type: 'string' } },
    files_to_change: { type: 'array', items: { type: 'string' } },
    top_risks: { type: 'array', items: { type: 'string' } },
    testing_plan: { type: 'array', items: { type: 'string' } },
    open_decisions: { type: 'array', items: { type: 'string' } },
    simpler_alternative: { type: 'string' },
  },
  required: ['verdict_feasibility', 'recommended_design', 'implementation_steps', 'top_risks'],
}

// ---- Round 1: UNDERSTAND (multiprocessing internals + our codebase) ----
const MP = [
  'spawn start method end-to-end: get_context/DefaultContext/set_start_method, how the Popen class is selected (popen_spawn_posix), and the overall parent→child sequence.',
  'popen_spawn_posix.Popen._launch: step by step — get_preparation_data, ForkingPickler.dumps(process_obj), fork_exec, fds_to_keep, the sentinel, returuning. Quote it.',
  'spawn.get_command_line + spawn.get_preparation_data for CPython 3.12 AND 3.13: the EXACT child argv (spawn_main(...) kwargs: pipe_handle/parent_pid/tracker_fd) and prep_data dict keys.',
  'spawn.spawn_main + spawn._main: how the child reads the pipe_handle fd, prepare(prep_data), unpickles the process object, and calls _bootstrap. Quote it.',
  'BaseProcess._bootstrap + run(): what runs the target, how exitcode is set, how the sentinel is written, exception handling. Quote it.',
  'multiprocessing.reduction: ForkingPickler, DupFd, sendfds/recvfds (SCM_RIGHTS), how Connection/Queue/Lock/Semaphore reduce for spawn (fd passing vs inheritance). Quote reducers.',
  'multiprocessing.connection internals: Client/Listener, Connection.send/recv, _ConnectionBase, the underlying fd, authkey handshake (deliver/answer_challenge) — is it used under spawn?',
  'multiprocessing.queues.Queue: the feeder thread, _buffer, the underlying Pipe/Connection, _sem/_rlock/_wlock, and exactly which OS resources get inherited by a spawn child.',
  'SimpleQueue and Pipe(): their fds/locks and how they reduce into a spawn child. When is SimpleQueue preferable (no feeder thread)?',
  'resource_tracker: what it tracks (semaphores, shared_memory), the tracker subprocess, how children connect to it (inherited fd / env), and leak warnings.',
  'POSIX fd inheritance for spawn: _posixsubprocess.fork_exec, fds_to_keep, close-on-exec/set_inheritable, and how the child keeps exactly the needed fds.',
  'How stdio (fd 0/1/2) is handled for a spawn child: inherited, redirected, or closed? Where in popen_spawn_posix/util.',
  'join()/exitcode/sentinel mechanics: Popen.poll/wait, the sentinel fd the parent selects on, os.waitpid/WNOHANG.',
  'Pickling a Process SUBCLASS and its attributes across spawn: what must be picklable, __reduce__/__getstate__, how attributes (e.g. a Queue) travel to the child.',
  'spawn prepare(): main-module handling (main_path/init_main_from_path, __mp_main__), _check_not_importing_main, sys.path/sys.argv[0] replication.',
  'multiprocessing.util: Finalize, _exit_function, at_fork hooks, get_temp_dir, and any atexit registration relevant to a foreign child.',
  'multiprocessing.context: registering a CUSTOM start method / context and a custom Popen (reduction.register, _default_context, Process._Popen). How to subclass cleanly.',
  'Signal handling in a spawn child: default disposition set by _bootstrap, SIGCHLD for waitpid, SIGWINCH for PTY resize, SIGTERM/terminate().',
  'shared_memory.SharedMemory: create/attach by name across unrelated interpreters, resource_tracker interaction, lifecycle/unlink — for large payload passing.',
  'Cross-version pickle/spawn: does spawn require the SAME CPython version parent↔child? Which pickle protocol default in 3.12 vs 3.13, and what breaks (bytecode, class layout, C-level reducers) if versions differ.',
]
const CODE = [
  'src/pybridge.c: embedded interpreter lifecycle (Py_InitializeEx(0)), the worker pthread, PyGILState usage, the job queue + agy_py_emit + run_dispatch signature, and where capture is written. Quote key parts.',
  'src/antigravity.c: constructor timing (relative to Go rt0/agy main), AGY_PROC_* env gating, install_hooks, build-id guard, getaddrinfo interposer, thread creation. When does the Python worker become ready?',
  'pyagy/_pty.py PtyProcess: exact PTY setup (pty.fork vs openpty), setsid, TIOCSCTTY, winsize/TIOCSWINSZ, the read/pump loop, and terminal-query auto-responses. Quote spawn().',
  'pyagy/_env.py: clean_env/instrumented_env, LD_PRELOAD assembly, AGY_PROC_* knobs, GODEBUG=netdns=cgo, AGY_CLI_DISABLE_AUTO_UPDATE. How env reaches the child.',
  'pyagy/session.py: run_print + InteractiveSession — how agy is launched today (PtyProcess.spawn/execve), lifecycle, close/teardown, and the AGY_BIN resolution.',
  'pyagy/client.py: ask()/Session/AgyResponse, _build_env, how the capture JSONL is read and decoded (lazy accessors), the stage/overlay model, degrade-on-mismatch.',
  'pyagy/agy_process/__init__.py + capture.py: dispatch(kind,stream_id,data) → what a capture record is and how it is emitted/serialized today (the thing we would replace/augment with a live channel).',
  'pyagy/agy_process/http1sse.py + hooks.py: the decoded objects (genai turns) and event kinds — what a live channel would actually stream, and where decode happens (in-process vs parent).',
  'How agy spawns its OWN child processes (language server, MCP servers, shells): the mechanism (Go os/exec), and env/fd inheritance implications for any fds we inject into agy.',
  'The shim threading + Go-runtime coexistence: does the worker thread interact with Go scheduler; how do cgocall-trampoline hooks (gohook.c) call into Python; GIL contention; signal masks on the worker thread.',
  'Makefile + build: how the shim embeds libpython (system python3.12 via python3-config), and how AGY_BIN/vendor/agy/the pin/WSL1 patch relate to launching a run.',
  'pyagy/_term.py: terminal query responder + winsize handling — relevant if PTY setup moves into the shim (what the responder does and whether the shim would need it).',
]

// ---- Round 2: DESIGN subproblems ----
const DESIGN = [
  'Custom start method + context + Popen to exec agy as the spawn child (VARIANT A: subclass BaseContext + Popen(popen_spawn_posix) overriding get_command_line/_launch to exec LD_PRELOADed agy).',
  'Same as above (VARIANT B: keep stock spawn but make the child a tiny python bootstrap that then exec()s agy after completing the multiprocessing handshake — python-first, agy-second).',
  'How the shim embedded Python becomes the multiprocessing child endpoint (VARIANT A: run real multiprocessing.spawn.spawn_main inside the shim worker, reading the inherited pipe_handle).',
  'Same (VARIANT B: a MINIMAL custom handshake — the shim reads a Connection fd we pass by env/inheritance and speaks connection.Connection directly, bypassing spawn_main/Process bootstrap).',
  'AgyProcess public API design (VARIANT A: subclass multiprocessing.Process, target=None, override run/_bootstrap; expose .start/.join/.exitcode + a .channel Queue).',
  'AgyProcess public API (VARIANT B: a Process-LOOKALIKE that internally uses PtyProcess + a Connection, NOT a real multiprocessing.Process subclass — duck-typed .start/.join/.exitcode).',
  'PTY approach A: PARENT allocates openpty; slave dup2 to child fd 0/1/2 via fork_exec; child (shim) does setsid+TIOCSCTTY; parent keeps master for transcript + SIGWINCH.',
  'PTY approach B: the SHIM allocates the PTY internally (constructor/thread), setsid+TIOCSCTTY before agy main; parent gets the master fd via an inherited/sent fd. Detail the shim threading changes.',
  'PTY approach C: reuse the existing pyagy/_pty.PtyProcess to allocate the PTY and thread it through the custom Popen (minimize new PTY code). Feasibility + wiring.',
  'Data channel A: parent Queue reduced+pickled into the child; shim rehydrates it and .put()s events. Detail the fd/semaphore inheritance and which stdlib pieces the shim reuses.',
  'Data channel B: parent connection.Listener on a Unix socket (path via env, like AGY_PROC_CAPTURE); shim does Client(addr).send(events). Simplest; contrast with A.',
  'Data channel C: shared_memory ring/segment for large response bodies + a small Queue/Connection for control events. When is this worth it?',
  'Map agy process exit → Process.exitcode + sentinel: who writes the sentinel (shim atexit? a parent-side waitpid on the agy pid?), and clean vs crash vs signal.',
  'fd inheritance plan: enumerate every fd that must survive exec into agy (pipe_handle, sentinel, PTY slave, Queue read/write + sem fds, tracker fd); fds_to_keep + set_inheritable wiring.',
  'resource_tracker/semaphore lifecycle with a foreign Go child + embedded Python: who owns cleanup, how to avoid leaked-semaphore warnings, whether to disable the tracker for this path.',
  'Shim threading design: where the multiprocessing endpoint runs (reuse the pybridge worker vs a new dedicated thread), GIL discipline, and coexistence with cgocall-trampoline hooks + Go runtime.',
  'prep_data handling for the embedded interpreter: does the shim need spawn.prepare() at all (it is not a normal python main)? What minimal subset (sys.path for pyagy) is required.',
  'Signal plan: PTY SIGWINCH forwarding, SIGCHLD/waitpid for join, SIGTERM for terminate(); reconcile with Py_InitializeEx(0) (no python signal handlers) and Go signal handling.',
  'Env + handshake plumbing: pass pipe_handle/PTY/channel via inherited fds vs env vars; integrate cleanly with _env.py instrumented_env; keep AGY_CLI_DISABLE_AUTO_UPDATE + pin.',
  'Compose with the EXISTING capture JSONL: keep both (file + live channel), make the channel optional, or replace? Backward-compat with client.py lazy decode accessors.',
  'Backpressure/blocking policy for the live channel from hook context: non-blocking send + bounded buffer + drop/coalesce policy so a slow parent never stalls agy.',
  'Parent-side ergonomics: e.g. with AgyProcess(prompt=..., stage=..., channel=q) as p: for ev in p.events(): ... — design .events()/.recv()/context-manager semantics + the returned object shape.',
  'Exception/error propagation agy/shim → parent (mirror how Process reports a target exception), including agy startup failure, build-id/PTY failure, and shim import failure (graceful degrade).',
  'Teardown/terminate/kill: killing the Go process mid-turn, draining+closing the PTY master, reaping the pid, releasing channel/tracker resources without warnings.',
  'The version-mismatch resolution (parent 3.13 vs shim system-3.12): options — (a) force the shim to embed the SAME python as the parent, (b) run the parent driver under system 3.12, (c) use a version-neutral wire (json/pickle-proto-4) instead of the spawn/Process object graph. Recommend.',
  'Minimal viable slice: the smallest thing that demonstrates the concept end-to-end on WSL1 (e.g. AgyProcess that starts agy on a PTY and delivers ONE Queue of raw events), to de-risk before full API.',
]

// ---- Round 3: RED-TEAM hazards ----
const HAZARDS = [
  'Parent/shim CPython VERSION MISMATCH (pixi 3.13 vs system 3.12): can a 3.13 parent pickle a Process/Queue and have a 3.12 embedded interpreter unpickle+rehydrate it? Test/verify pickle proto + class layout compatibility.',
  'The embedded Python is a GUEST THREAD inside a Go process — but spawn_main/_bootstrap assume they OWN the process (they set sys.argv, main module, exit the process). Does running spawn_main on a guest thread corrupt agy/Go?',
  'multiprocessing forks in the PARENT which is multi-threaded (has threads); fork+exec vs fork-only safety; does popen_spawn_posix avoid the fork-in-threaded-parent deadlock?',
  'agy forks its OWN children (LSP/MCP/shells) which INHERIT our injected fds (pipe/sentinel/Queue/PTY/semaphores) → double-holders, resource_tracker confusion, deadlock on close. Verify inheritance + CLOEXEC.',
  'PTY controlling-terminal vs multiprocessing: setting a PTY as ctty needs setsid (new session, loses parent session); does that break agy or the multiprocessing sentinel/waitpid path?',
  'GIL/deadlock: the endpoint thread holds the GIL to service the Connection while cgocall-trampoline hooks (on agy goroutines) call agy_py_emit needing the GIL → contention or deadlock under load.',
  'Py_InitializeEx(0) installs NO python signal handlers; multiprocessing/resource_tracker/Connection may rely on SIGCHLD/SIGPIPE handling. What breaks?',
  'Who writes the multiprocessing SENTINEL at agy exit? Go exit does not run C atexit reliably; if the sentinel never fires, parent .join() hangs forever. Verify agy exit path + shim exit hook.',
  'Startup ORDERING deadlock: parent writes the pickled process_obj into the pipe before the shim worker is ready to read; pipe buffer (64KB) fills → parent blocks; shim not yet reading. Verify readiness handshake.',
  'resource_tracker: a semaphore created in the parent and inherited by agy — who unlinks it? Leaked-semaphore warnings / stale /dev/shm entries after agy exits. Verify.',
  'WSL1 (kernel 4.4 "-Microsoft"): do openpty/grantpt/TIOCSCTTY/setsid, AF_UNIX SCM_RIGHTS fd passing, POSIX semaphores (sem_open), and shared_memory (shm_open) all actually WORK on WSL1? Any that are broken (like /proc/*/mem was for gdb)?',
  'Does multiprocessing insist on a callable target? target=None + overridden run — does Process.__init__/_bootstrap tolerate it, or assert/AttributeError?',
  'get_command_line assumes the child is python (sys.executable). Overriding it to exec agy — does any OTHER spawn code path re-derive python or re-import main and break?',
  'The Queue FEEDER thread runs in the sender process; if the shim uses a Queue it must run a feeder thread inside agy — extra thread + GIL + Go coexistence. Is SimpleQueue/raw Connection safer?',
  'Backpressure: if the parent stops reading, does the shim/agy BLOCK (stalling agy mid-turn) or drop? Verify the failure mode of a full pipe/Queue from the hook path.',
  'terminate()/kill() semantics: SIGTERM to a Go process mid-cgo — does it leave the PTY/semaphores/tracker in a bad state? Does the parent reap correctly?',
  'authkey handshake: does connection.Client/Listener require an authkey exchange the shim must replicate? Where does the key come from under spawn, and can the shim get it?',
  'The parent-side custom Popen must pass EXTRA fds (PTY slave/master, channel) through _posixsubprocess.fork_exec; verify fds_to_keep supports arbitrary fds and they are not closed by close_fds.',
  'Double interpreter / import: the shim embedded interp importing multiprocessing + pyagy while agy also uses cgo/threads — any C-global or locale/env clash; and pickle needs pyagy classes importable identically in both interps.',
  'Is this actually SIMPLER than the current PTY+JSONL? Attack the premise: enumerate the added moving parts vs the "easier data passing" benefit; when does it NOT pay off.',
  'The cgocall-trampoline hooks already run Python (agy_py_emit) on agy goroutines via g0; adding a multiprocessing endpoint thread — reentrancy/GIL ordering between hook-driven emits and the endpoint drain.',
  'agy auto-relaunch / re-exec: does agy ever re-exec itself (e.g. after the shim, or the loader shim /lib64/ld-linux launch trick)? A re-exec would drop the shim/fds. Verify the launch path (the ld.so wrapper noted in patch_agy_wsl1).',
]

const SYNTH = [
  'PRIMARY SYNTHESIS: from all findings/designs/hazards, produce the recommended concrete design + implementation plan for an agy AgyProcess (spawn model) with PTY handled in/around the shim. Be decisive; pick variants; give ordered implementation steps, files, the version-mismatch resolution, risks, and a WSL1 testing plan.',
  'ALTERNATIVE SYNTHESIS (independent): assume the FATAL/SERIOUS red-team hazards make a literal multiprocessing.Process subclass infeasible; design the closest PRACTICAL thing (duck-typed Process-lookalike over PtyProcess + a connection channel) that still gives the ergonomics. Ordered steps, files, risks, testing.',
  'COMPLETENESS CRITIC: given the two syntheses + all hazards, list what is unresolved, any hazard not mitigated, any step that is hand-wavy, and the crux decisions the human must make. Then state which synthesis to prefer and why.',
]

phase('Understand')
const r1 = await parallel([...MP, ...CODE].map((ask, i) => () =>
  agent(`${CTX}\n\nINVESTIGATE THIS FACET and report structured findings:\n${ask}`,
    { agentType: 'Explore', phase: 'Understand', schema: FINDING,
      label: `u:${i < MP.length ? 'mp' : 'code'}:${i}` })
))
const facts = r1.filter(Boolean)
const primer = facts.map(f => `### ${f.facet}\n${f.summary}\n- ${(f.key_findings || []).join('\n- ')}${(f.gotchas && f.gotchas.length) ? '\nGOTCHAS: ' + f.gotchas.join('; ') : ''}`).join('\n\n')
log(`Understand: ${facts.length} findings gathered`)

phase('Design')
const r2 = await parallel(DESIGN.map((sp, i) => () =>
  agent(`${CTX}\n\nSHARED RESEARCH PRIMER (from the understand phase):\n${primer}\n\n` +
        `DESIGN THIS SUB-PROBLEM concretely (cite the primer + re-read source as needed):\n${sp}`,
    { agentType: 'Explore', phase: 'Design', schema: DESIGN_OUT, label: `d:${i}` })
))
const designs = r2.filter(Boolean)
const designDigest = designs.map(d => `### ${d.subproblem}\nAPPROACH: ${d.approach}\nMECH: ${d.mechanism}\nCONS: ${(d.cons || []).join('; ')}`).join('\n\n')
log(`Design: ${designs.length} design notes produced`)

phase('RedTeam')
const r3 = await parallel(HAZARDS.map((h, i) => () =>
  agent(`${CTX}\n\nSHARED PRIMER:\n${primer}\n\nPROPOSED DESIGNS (digest):\n${designDigest}\n\n` +
        `ADVERSARIALLY ATTACK THIS HAZARD — try to prove it breaks the design; verify empirically where possible (read source, check WSL1 behavior). Give a verdict + concrete mitigation:\n${h}`,
    { agentType: 'Explore', phase: 'RedTeam', schema: HAZARD_OUT, label: `r:${i}` })
))
const hz = r3.filter(Boolean)
const hazardDigest = hz.map(h => `[${h.verdict}] ${h.hazard}: ${h.reasoning}${h.mitigation ? ' | MITIGATION: ' + h.mitigation : ''}`).join('\n')
const fatal = hz.filter(h => h.verdict === 'FATAL' || h.verdict === 'SERIOUS')
log(`RedTeam: ${hz.length} hazards assessed; ${fatal.length} FATAL/SERIOUS`)

phase('Synthesize')
const synth = await parallel(SYNTH.map((s, i) => () =>
  agent(`${CTX}\n\nPRIMER:\n${primer}\n\nDESIGNS:\n${designDigest}\n\nRED-TEAM VERDICTS:\n${hazardDigest}\n\nTASK:\n${s}`,
    { agentType: 'Explore', phase: 'Synthesize', schema: SYNTH_OUT, effort: 'high', label: `s:${i}` })
))
const syntheses = synth.filter(Boolean)

return {
  counts: { understand: facts.length, design: designs.length, hazards: hz.length, fatal_serious: fatal.length },
  fatal_serious_hazards: fatal.map(h => `[${h.verdict}] ${h.hazard}: ${h.reasoning} | MIT: ${h.mitigation || 'none'}`),
  syntheses,
}
