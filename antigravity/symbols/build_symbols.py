#!/usr/bin/env python3
"""Resolve Go function addresses in the stripped, Propeller/BOLT-reordered `agy`
(Antigravity) binary and emit symbols.json for the native shim.

Why not debug/gosym or GoReSym?
  - `agy` has no ELF symtab and no standard Go buildinfo header.
  - Its pclntab records `pcHeader.textStart == 0`; the *real* text base lives in
    `moduledata.text`, which is a PIE-relocated pointer (0x4eb5080 = 0x80 past the
    ELF .text addr). gosym uses textStart and GoReSym can't even locate the
    pclntab, so both mis-resolve. We read moduledata.text from the relocation
    table and use `real_vaddr = moduledata.text + func.entryoff`.
  - `moduledata.textsectmap` has a single entry (Go treats its code as one
    contiguous section), so the mapping is linear. We *verify* it: every emitted
    hook address is disassembly-checked for a Go function prologue and the tool
    errors out if any check fails (which would signal a layout change).

Output offsets are file virtual addresses (PIE preferred base 0); at runtime the
shim adds the load bias: `runtime_addr = main_module_base + offset`.

Usage:  python3 build_symbols.py <agy-binary> <out.json>
"""
import json
import struct
import sys

# Functions the native shim hooks by default. Keep in sync with src/procdef.h.
PROC_TARGETS = [
    "crypto/tls.(*Conn).Write",          # plaintext egress to the LLM backend
    "crypto/tls.(*Conn).Read",           # plaintext ingress from the backend
    "net/http.(*Transport).RoundTrip",   # HTTP-level request/response
    # ingress RESPONSE capture: http2 pipe.Write gets each de-framed response body
    # chunk as []byte, CPU-only (no park) — the safe analog of tls_write.
    "net/http/internal/http2.(*pipe).Write",
    # BETTER response hook: TLS record decrypt runs AFTER the socket read parked,
    # on already-received ciphertext — CPU-only, doesn't park. on_leave []byte =
    # decrypted inbound record (HTTP/2 frames of the response).
    "crypto/tls.(*halfConn).decrypt",
    "runtime.main",                      # smoke-test anchor (fires every launch)
    "os.Getenv",                         # smoke-test anchor (no auth/network)
    # conversation-id capture (AGY_PROC_CONV_ID overlay): agy opens its conversation
    # store at .../conversations/<uuid>.db and .../brain/<uuid>/.../transcript.jsonl —
    # the uuid is IN the path, so an enter-only probe reading OpenFile's name arg
    # (RAX=ptr,RBX=len, same shape as os.Getenv) yields the exact id in-process.
    "os.OpenFile",

    # --- app-layer capture R&D (CPU-only funcs returning []byte = readable) ---
    "google3/third_party/jetski/cli/model/model.(*RootModel).Serialize",
    "google3/third_party/jetski/cli/model/model.(*PromptModel).MarshalJSON",
    # generic protobuf marshal: CPU-only, returns []byte — captures the marshaled
    # StreamGenerateChat request (and every other proto) before the network send.
    "google3/third_party/golang/gogo/protobuf/proto/proto.Marshal",
    # --- agy-native application boundary (cgocall-trampoline app-boundary hooks) ---
    # ServerBackend is agy's own client to the model backend; these run in-process on
    # fully-decoded Go data (no HTTP/2/HPACK/gzip). Parking funcs → cgocall trampoline.
    "google3/third_party/jetski/cli/backend/backend.(*ServerBackend).SendUserMessage",
    "google3/third_party/jetski/cli/backend/backend.(*callbackStreamer).Send",
    # --- cgocall gateway: the trampoline's runtime entry points (resolved, not hooked) ---
    "runtime.cgocall",
    # asmcgocall: the g0-stack-switch INNER half of cgocall, WITHOUT entersyscall/
    # exitsyscall (no _Gsyscall, no P handoff, no reschedule). Called (not hooked)
    # by AGY_ASMCGO trampoline hooks (the lighter per-hook variant, for hot / syscall-
    # at-entry-sensitive funcs). Two same-named entries exist (BOLT hot/cold); build picks
    # the lower entryoff (0x4f52780), which is the one cgocall actually CALLs.
    "runtime.asmcgocall",

    # --- CodeAssistClient RPC trace (app-semantic backend boundary; trampoline) ---
    # (*CodeAssistClient).* is agy's single client to the CloudCode backend; each
    # method = one named RPC with typed proto args. Trampoline (they park on the HTTP
    # round-trip). Entry-arg walk (AGY_PROC_CGT_ARGS) captures the request proto; the
    # RPC name IS the trace label. StreamGenerateContent is the model turn itself.
    "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).StreamGenerateContent",
    "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).GenerateContent",
    "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).FetchLoadCodeAssistResponse",
    "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).FetchUserInfo",
    "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).FetchAvailableModels",
    "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).ListExperiments",
    "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).RetrieveUserQuotaSummary",
    "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).RecordConversationOffered",
    "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).RecordTrajectorySegmentAnalytics",
    "google3/third_party/jetski/language_server/code_assist_client/codeassistclient.(*CodeAssistClient).WriteTrajectoryACLs",

    # --- model-text pipeline (gemini_coder framework; cgocall-trampoline) ---
    # The CLEAN assistant text flows through framework/{generator,core}, NOT the
    # jetski/cli/backend tail (callbackStreamer.Send, which only sees the wrapped
    # AgentStateUpdate proto). These framed funcs are on the parking stream-consumer
    # path → hooked via the cgocall trampoline; AGY_PROC_CGT_ARGS walks their args.
    "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).finalizePlannerResponse",
    "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).updateWithStep",
    "google3/third_party/gemini_coder/framework/generator/generator.(*streamResponseHandler).processStream",
    "google3/third_party/gemini_coder/framework/core/core.createPlannerResponseStep",
    # consumer-entry hook for the RESPONSE: these take the completed *Step (with the
    # assembled assistant text) as an entry ARG — sidesteps the return-value problem.
    # (protoTrajectory.AppendStep did NOT fire in --print/interactive; the step commit
    # goes through the framework/cortex trajectory + the step-changed callback instead.)
    "google3/third_party/gemini_coder/framework/core/integration/integration.(*ToolContextTrajectory).AppendStep",
    "google3/third_party/jetski/cortex/traj/traj.(*Trajectory).AddStep",
    "google3/third_party/jetski/cortex/agent_state_component/agent_state_component.(*AgentState).OnStepsChanged",
    # THE better consumer (Plan 7): one frame above OnStepsChanged in the CONSUMER
    # goroutine, runExecution calls this with the completed *Step as a SHALLOW entry
    # arg — the assembled assistant text is 3 proto-stable derefs away (Step+0x70 →
    # deref → +0x8 ptr / +0x10 len), not behind AgentState internals + a mutex.
    "google3/third_party/gemini_coder/framework/executor/executor.(*ExecutionTrajectory).AppendStep",
]

# Nosplit/frameless leaf getters that RETURN the streamed model text as a Go string
# (RAX=ptr, RBX=len on return) — the cleanest possible signal, zero struct-offset
# fragility. They legitimately lack a push-rbp/stack-split prologue, so they're
# resolved with skip=0 and exempt from the is_prologue assert. Hooked via gum-attach
# on_leave (CPU-only leaf → re-entry-safe).
NOSPLIT_TARGETS = [
    "google3/third_party/jetski/api_server_pb/api_server_go_proto.(*GetChatMessageResponse).GetDeltaText",
    "google3/third_party/jetski/codeium_common_pb/codeium_common_go_proto.(*CompletionDelta).GetDeltaText",
    # RESPONSE getters (return the assembled assistant text as a plain Go string →
    # gum on_leave, zero struct-offset fragility). The cortex step's response/thinking.
    "google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetResponse",
    "google3/third_party/jetski/cortex_pb/cortex_go_proto.(*CortexStepPlannerResponse).GetThinking",
    "google3/third_party/jetski/cortex/trajectory/trajectory.(*PlannerResponseStepView).Response",
]

# Reference groups emitted for picking further hook points (not hooked by default).
CATALOG = {
    "jetski_mcp":     lambda n: "jetski/cli" in n and "mcp" in n.lower(),
    "jetski_tool":    lambda n: "jetski/cli" in n and "Tool" in n,
    "jetski_backend": lambda n: "jetski/cli/backend/backend." in n,
    "jetski_prompt":  lambda n: "jetski/cli" in n and "prompt" in n.lower(),
    "tls_conn":       lambda n: n.startswith("crypto/tls.(*Conn)."),
    "http_transport": lambda n: n.startswith("net/http.(*Transport)."),
}
CATALOG_CAP = 500

R_X86_64_RELATIVE = 8


class ELF:
    def __init__(self, data: bytes):
        self.d = data
        self.u16 = lambda o: struct.unpack_from("<H", data, o)[0]
        self.u32 = lambda o: struct.unpack_from("<I", data, o)[0]
        self.i32 = lambda o: struct.unpack_from("<i", data, o)[0]
        self.u64 = lambda o: struct.unpack_from("<Q", data, o)[0]
        assert data[:4] == b"\x7fELF", "not an ELF"
        self.e_entry = self.u64(0x18)
        shoff, shent = self.u64(0x28), self.u16(0x3a)
        shnum, shstrndx = self.u16(0x3c), self.u16(0x3e)
        self.secs = []
        for i in range(shnum):
            b = shoff + i * shent
            self.secs.append(dict(name=self.u32(b), type=self.u32(b + 4),
                                  addr=self.u64(b + 16), off=self.u64(b + 24),
                                  size=self.u64(b + 32)))
        so = self.secs[shstrndx]["off"]
        for s in self.secs:
            o = so + s["name"]
            s["n"] = data[o:data.index(b"\0", o)].decode()
        # PT_LOAD segments for vaddr<->fileoff mapping
        phoff, phent = self.u64(0x20), self.u16(0x36)
        phnum = self.u16(0x38)
        self.loads = []
        for i in range(phnum):
            b = phoff + i * phent
            if self.u32(b) == 1:  # PT_LOAD
                self.loads.append(dict(off=self.u64(b + 8), vaddr=self.u64(b + 16),
                                       filesz=self.u64(b + 32)))

    def sec(self, name):
        for s in self.secs:
            if s["n"] == name:
                return s
        return None

    def v2o(self, vaddr):
        for L in self.loads:
            if L["vaddr"] <= vaddr < L["vaddr"] + L["filesz"]:
                return L["off"] + (vaddr - L["vaddr"])
        raise ValueError(f"vaddr {vaddr:#x} not in any PT_LOAD")

    def rv64(self, vaddr):  # read u64 at a virtual address
        return self.u64(self.v2o(vaddr))

    def ri32(self, vaddr):
        return self.i32(self.v2o(vaddr))

    def build_id(self):
        s = self.sec(".note.gnu.build-id")
        if not s:
            return ""
        o = s["off"]
        namesz, descsz = self.u32(o), self.u32(o + 4)
        nend = o + 12 + ((namesz + 3) & ~3)
        return self.d[nend:nend + descsz].hex()


def find_pclntab(d: bytes):
    for m0 in (0xF1, 0xF0):
        i = 0
        magic = bytes([m0, 0xFF, 0xFF, 0xFF])
        while True:
            i = d.find(magic, i)
            if i < 0:
                break
            if d[i + 4] == 0 and d[i + 5] == 0 and d[i + 6] in (1, 2, 4) and d[i + 7] in (4, 8):
                return i  # fileoff; in seg0 fileoff==vaddr
            i += 4
    raise SystemExit("pclntab magic not found")


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: build_symbols.py <agy-binary> <out.json>")
    binpath, outpath = sys.argv[1], sys.argv[2]
    d = open(binpath, "rb").read()
    elf = ELF(d)

    B = find_pclntab(d)                      # pclntab vaddr (== fileoff in seg0)
    u32, i32, u64 = elf.u32, elf.i32, elf.u64
    ptr_size = d[B + 7]
    nfunc = u64(B + 8)
    funcnametab = u64(B + 32)
    pcln_off = u64(B + 64)
    FT = B + pcln_off                        # functab base
    FUNCBASE = B + pcln_off                  # _func = FUNCBASE + funcoff

    # --- locate moduledata: RELATIVE reloc whose addend == pclntab vaddr ---
    rela = elf.sec(".rela.dyn")
    if not rela:
        raise SystemExit("no .rela.dyn (need relocations to find moduledata.text)")
    md_vaddr = None
    rbase, rcnt = rela["off"], rela["size"] // 24
    for i in range(rcnt):
        o = rbase + i * 24
        if u64(o + 16) == B and (u64(o + 8) & 0xFFFFFFFF) == R_X86_64_RELATIVE:
            md_vaddr = u64(o)
            break
    if md_vaddr is None:
        raise SystemExit("could not find moduledata (no reloc addend == pclntab)")

    # moduledata.text is a relocated pointer; read its addend (field at md+0xb0
    # for this Go version; verified by prologue checks below).
    relwin = {}
    for i in range(rcnt):
        o = rbase + i * 24
        roff = u64(o)
        if md_vaddr <= roff < md_vaddr + 0x400 and (u64(o + 8) & 0xFFFFFFFF) == R_X86_64_RELATIVE:
            relwin[roff] = u64(o + 16)
    text_base = relwin.get(md_vaddr + 0xB0)   # moduledata.text
    minpc = relwin.get(md_vaddr + 0xA0)       # moduledata.minpc (cross-check)

    # Fallback: derive text_base as an addend within the first page of .text.
    text_sec = elf.sec(".text")
    if text_base is None or not (text_sec["addr"] <= text_base < text_sec["addr"] + 0x1000):
        cands = sorted({v for v in relwin.values()
                        if text_sec["addr"] <= v < text_sec["addr"] + 0x1000})
        text_base = cands[0] if cands else text_sec["addr"]

    # --- resolve every function: real = text_base + entryoff ---
    def name_at(noff):
        o = B + funcnametab + noff
        return d[o:d.index(b"\0", o)].decode("utf-8", "replace")

    name2addr = {}
    catalog = {k: [] for k in CATALOG}
    funcmap = []                         # (addr, name) for EVERY func → stack symbolization
    for i in range(nfunc):
        eo = u32(FT + i * 8)
        fo = u32(FT + i * 8 + 4)
        nm = name_at(i32(FUNCBASE + fo + 4))
        addr = text_base + eo
        if nm not in name2addr:
            name2addr[nm] = addr
        funcmap.append((addr, nm))
        for fam, pred in CATALOG.items():
            if pred(nm) and len(catalog[fam]) < CATALOG_CAP:
                catalog[fam].append({"name": nm, "addr": addr})

    # --- self-verify: each hook address must look like a Go entry ---
    def is_prologue(addr):
        try:
            b = d[elf.v2o(addr):elf.v2o(addr) + 24]
        except ValueError:
            return False
        # Stack-split check `cmp 0x10(%r14),<reg>`: REX.WB (+opt R) 3B modrm 10,
        # where modrm has mod=01, rm=110(r14): (modrm & 0xC7) == 0x46. Covers the
        # small-frame form (reg=rsp, REX 0x49) and large-frame form after
        # `lea -N(%rsp),%r12` (reg=r12, REX 0x4d).
        for k in range(18):  # large-frame prologues put the cmp ~offset 12
            if (b[k] & 0xFB) == 0x49 and b[k + 1] == 0x3B and (b[k + 2] & 0xC7) == 0x46 and b[k + 3] == 0x10:
                return True
        # nosplit frames: push rbp (0x55) or sub $x,%rsp
        if b[:1] == b"\x55" or b[:3] in (b"\x48\x83\xec", b"\x48\x81\xec"):
            return True
        return False

    def prologue_skip(addr):
        """Bytes to skip so the hook lands PAST the stack-check prologue — else Go's
        morestack re-runs the entry and re-enters our trampoline, stalling agy.
        Finds `cmp 0x10(%r14),reg` then the following Jcc (to the morestack stub);
        the real body begins right after. 0 for nosplit funcs (no check)."""
        try:
            b = d[elf.v2o(addr):elf.v2o(addr) + 48]
        except ValueError:
            return 0
        for k in range(40):
            if (b[k] & 0xFB) == 0x49 and b[k + 1] == 0x3B and (b[k + 2] & 0xC7) == 0x46 and b[k + 3] == 0x10:
                j = k + 4  # past the cmp
                if b[j] == 0x0F and b[j + 1] in (0x82, 0x83, 0x86, 0x87):  # jb/jae/jbe/ja rel32
                    return j + 6
                if b[j] in (0x72, 0x73, 0x76, 0x77):                       # rel8
                    return j + 2
                return j
        return 0

    hooks, skips, missing, unverified = {}, {}, [], []
    for nm in PROC_TARGETS:
        if nm not in name2addr:
            missing.append(nm)
            continue
        a = name2addr[nm]
        hooks[nm] = a
        skips[nm] = prologue_skip(a)
        if not is_prologue(a):
            unverified.append(nm)
    # nosplit leaf getters: resolve with skip=0, exempt from the prologue assert.
    for nm in NOSPLIT_TARGETS:
        if nm not in name2addr:
            missing.append(nm)
            continue
        hooks[nm] = name2addr[nm]
        skips[nm] = 0

    for fam in catalog:
        catalog[fam].sort(key=lambda x: x["name"])

    out = {
        "agy_path": binpath,
        "build_id": elf.build_id(),
        "ptr_size": ptr_size,
        "moduledata_vaddr": md_vaddr,
        "text_base": text_base,
        "minpc": minpc,
        "total_funcs": nfunc,
        "hooks": {k: hooks[k] for k in PROC_TARGETS + NOSPLIT_TARGETS if k in hooks},
        "skips": {k: skips[k] for k in PROC_TARGETS + NOSPLIT_TARGETS if k in hooks},
        "missing": missing,
        "catalog": catalog,
    }
    with open(outpath, "w") as f:
        json.dump(out, f, indent=2)

    # Full sorted funcmap (addr → name for EVERY function) for offline stack
    # symbolization (pyagy.agy_process.symbolize). Gzipped, gitignored, regenerated
    # by `make symbols`. Keyed by absolute link vaddr so it's ASLR-independent:
    # a captured runtime PC maps via link_vaddr = pc - module_base, then bisect.
    import gzip
    import os as _os
    funcmap.sort()
    fmpath = _os.path.join(_os.path.dirname(_os.path.abspath(outpath)), "funcmap.tsv.gz")
    with gzip.open(fmpath, "wt", encoding="utf-8") as f:
        for addr, nm in funcmap:
            f.write(f"{addr:x}\t{nm}\n")

    print(f"wrote {outpath}")
    print(f"  funcmap:    {fmpath} ({len(funcmap)} funcs)")
    print(f"  build-id:   {out['build_id']}")
    print(f"  moduledata: {md_vaddr:#x}   text_base: {text_base:#x}   minpc: {minpc:#x}" if minpc else
          f"  moduledata: {md_vaddr:#x}   text_base: {text_base:#x}")
    print(f"  funcs:      {nfunc}")
    print(f"  hooks:      {len(hooks)}/{len(PROC_TARGETS) + len(NOSPLIT_TARGETS)}"
          + (f"  MISSING={missing}" if missing else ""))
    for nm in PROC_TARGETS + NOSPLIT_TARGETS:
        if nm in hooks:
            flag = "  !! NOT A PROLOGUE" if nm in unverified else \
                   "  (nosplit)" if nm in NOSPLIT_TARGETS else ""
            print(f"    {hooks[nm]:#012x} (+{skips[nm]:2d})  {nm}{flag}")
    for fam, lst in catalog.items():
        print(f"  catalog[{fam}]: {len(lst)}")
    if unverified or missing:
        raise SystemExit(f"FAILED verification: missing={missing} unverified={unverified}")
    print("  all hook addresses verified as Go prologues ✓")


if __name__ == "__main__":
    main()
