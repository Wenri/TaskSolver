/* gomod.h — go1.26 runtime-metadata mirrors for the cgocall trampoline.
 *
 * WHY THIS EXISTS
 * ---------------
 * The robust way to run a C hook from a hooked Go function is the Quarkslab
 * "rabbit hole" technique: redirect the target (past its stack-check prologue)
 * to a trampoline that marshals the Go-ABI arg registers and CALLs
 * runtime.cgocall(fn=our_C_hook, arg=&block). cgocall switches to the g0/system
 * stack, hands off P, and manages async preemption, so the hook runs in a safe
 * context; on return we restore regs, run the overwritten original instructions,
 * and jmp back into the target body.
 *
 * cgocall calls entersyscall() (cgocall is //go:nosplit, so NO morestack), which
 * puts the goroutine in _Gsyscall. In that state the GC scans the goroutine
 * stack WITHOUT preempting it (mgcmark.go scanstack), unwinding every frame above
 * gp.syscallsp via the pclntab unwinder. That unwind walks cgocall -> OUR
 * trampoline -> the target. If findfunc() can't resolve the trampoline PC it does
 * throw("unknown pc") (traceback.go:201-208) — a fatal crash. The Quarkslab blog
 * dodges this only because its FrameConstructor is compiled Go (real pclntab
 * metadata in a c-shared lib's own runtime); a raw C trampoline has none, so the
 * blog's scheme has the same latent crash and simply never stresses GC.
 *
 * FIX: register a synthetic runtime.moduledata that covers the trampoline page so
 * findfunc resolves our PCs. findfunc -> findmoduledatap (symtab.go:866) walks the
 * `firstmoduledata` LINKED LIST, so registration is a single pointer write
 * (walk to the tail, set tail.next = &our_md) — no runtime.modulesinit, no Go
 * allocation, no modulesSlice (that snapshot is only for global data scanning,
 * which our data-less module doesn't need), and moduledataverify1 is startup-only
 * so it never runs on us. build_symbols.py already resolves `moduledata_vaddr`
 * (== &runtime.firstmoduledata); we clone that live struct to inherit the exact
 * go1.26 layout + all gc/type/pcHeader pointers, then patch only the fields below.
 *
 * TRAMPOLINE FRAME GEOMETRY (amd64, framepointer_enabled) — from traceback.go:
 *   fp   = sp + funcspdelta(pc) + 8      (ret-addr slot at fp-8 == [sp+spdelta])
 *   varp = fp - 8 - 8 = sp + spdelta - 8 (the -8..-8 skips ret-addr + saved-BP)
 *   locals scanned = [sp, varp) = [sp, sp+spdelta-8)
 * Entered by JMP at target+skip (post stack-check, PRE frame-alloc), so the
 * trampoline's entry SP == the target's entry SP and [entrySP] still holds the
 * target's own return address. With our frame FRAME bytes and spdelta==FRAME at
 * the `call cgocall` PC:
 *   trampoline.sp = entrySP - FRAME ; ret-addr slot [sp+FRAME] = [entrySP] = the
 *   target's return address -> unwind flows correctly to the target's caller.
 *   locals = [entrySP-FRAME, entrySP-8), nbit = (FRAME-8)/8 words, ALL-ONES map
 *   (conservative: invalid "pointers" are ignored by findObject; keeps the saved
 *   arg-register pointers + the C-hook's read buffer alive across the cgo window).
 *   [entrySP-8, entrySP) is the assumed saved-BP slot (excluded from the map).
 *
 * FuncID must be 0 (normal): NOT FuncID_asyncPreempt/debugCallV2 — the unwinder
 * treats those as injectedCall (traceback.go:485) and misreads our frame. Flag
 * must be 0: NOT FuncFlagSPWrite (would stop the unwind) — our sub/add rsp IS
 * encoded in the pcsp table, so it's a normal frame.
 *
 * All facts verified against /usr/lib64/go1.26.4/go/src/runtime/{symtab.go,
 * runtime2.go,traceback.go,mgcmark.go,stkframe.go,asm_amd64.s,cgocall.go} and
 * internal/abi/symtab.go. agy build: go1.26.4.
 */
#ifndef AGY_GOMOD_H
#define AGY_GOMOD_H

#include <stdint.h>
#include <stddef.h>

/* ---- Go aggregate-type ABI mirrors (amd64) ------------------------------- */
typedef struct { void *ptr; intptr_t len; intptr_t cap; } go_slice;   /* []T   */
typedef struct { const char *ptr; intptr_t len; }         go_string;  /* string*/
typedef struct { int32_t n; uint8_t *bytedata; }          go_bitvector; /* 16 B */

/* ---- runtime.pcHeader (symtab.go:376) ------------------------------------ */
typedef struct {
    uint32_t  magic;                 /* abi.CurrentPCLnTabMagic (go1.20 = 0xFFFFFFF1) */
    uint8_t   pad1, pad2, minLC, ptrSize;
    intptr_t  nfunc;                 /* Go int  (8B) — number of funcs in module */
    uintptr_t nfiles;                /* Go uint (8B) */
    uintptr_t textStart_unused;      /* no longer used; see moduledata.text */
    uintptr_t funcnameOffset, cuOffset, filetabOffset, pctabOffset, pclnOffset;
} go_pcHeader;

/* ---- runtime.functab (symtab.go:580): entryoff relative to moduledata.text */
typedef struct { uint32_t entryoff, funcoff; } go_functab;

/* ---- runtime.findfuncbucket (symtab.go:601) ------------------------------ */
typedef struct { uint32_t idx; uint8_t subbuckets[16]; } go_findfuncbucket;

/* ---- runtime._func (runtime2.go:1072) — 44-byte header, then two trailing
 * uint32 arrays: pcdata[npcdata] and funcdata[nfuncdata]. ------------------- */
typedef struct {
    uint32_t entryOff;     /* start pc as offset from moduledata.text */
    int32_t  nameOff;      /* index into moduledata.funcnametab */
    int32_t  args;         /* in/out arg size (0 for us -> no args map needed) */
    uint32_t deferreturn;
    uint32_t pcsp;         /* offset into moduledata.pctab of the SP-delta table */
    uint32_t pcfile;
    uint32_t pcln;
    uint32_t npcdata;
    uint32_t cuOffset;
    int32_t  startLine;
    uint8_t  funcID;       /* MUST be 0 (normal) */
    uint8_t  flag;         /* MUST be 0 (no SPWrite/TopFrame) */
    uint8_t  pad[1];
    uint8_t  nfuncdata;    /* must be last; uint32-aligned boundary follows */
    /* uint32_t pcdata[npcdata];  uint32_t funcdata[nfuncdata]; */
} go_func;

/* ---- runtime.stackmap (symtab.go:1324): n bitmaps of nbit bits each ------ */
typedef struct { int32_t n, nbit; uint8_t bytedata[]; } go_stackmap;

/* ---- runtime.moduledata — layout MUST match agy's Go EXACTLY. agy is built with
 * go1.27 (go1.27-20260615-RC00), whose moduledata is 568 bytes and DIFFERS from
 * go1.26.4: it adds `typedesclen` (after types) and replaces the `typelinks`/
 * `itablinks` slices with `itaboffset`/`itabsize` uintptrs — which shifts `gofunc`
 * to +344 and `next` to +560 (go1.26.4 had 320/584). Verified against
 * release-branch.go1.27 src/runtime/symtab.go AND the live agy binary (hasmain@512,
 * typedesclen@304, itaboffset@320, gofunc@344, next@560 all match). The _Static_
 * asserts in gomod.cpp pin the critical offsets. Leading sys.NotInHeap is zero-size.
 * We build this fresh (calloc) and set only the fields findfunc/GC-unwind read. */
typedef struct go_moduledata {
    go_pcHeader *pcHeader;
    go_slice  funcnametab;   /* []byte   */
    go_slice  cutab;         /* []uint32 */
    go_slice  filetab;       /* []byte   */
    go_slice  pctab;         /* []byte   */
    go_slice  pclntable;     /* []byte   (holds our go_func) */
    go_slice  ftab;          /* []functab */
    uintptr_t findfunctab;   /* *[]findfuncbucket (raw ptr) */
    uintptr_t minpc, maxpc;
    uintptr_t text, etext;
    uintptr_t noptrdata, enoptrdata;
    uintptr_t data, edata;
    uintptr_t bss, ebss;
    uintptr_t noptrbss, enoptrbss;
    uintptr_t covctrs, ecovctrs;
    uintptr_t end, gcdata, gcbss;
    uintptr_t types, typedesclen, etypes;   /* go1.27: +typedesclen */
    uintptr_t itaboffset, itabsize;          /* go1.27: replaces typelinks/itablinks slices */
    uintptr_t rodata;
    uintptr_t gofunc;        /* @344 — base for _func.funcdata offsets */
    uintptr_t epclntab;
    go_slice  textsectmap;   /* []textsect */
    go_slice  ptab;          /* []ptabEntry */
    go_string pluginpath;
    go_slice  pkghashes;     /* []modulehash */
    go_slice  inittasks;     /* []*initTask  */
    go_string modulename;
    go_slice  modulehashes;  /* []modulehash */
    uint8_t   hasmain;
    uint8_t   bad;           /* bool */
    /* 6 bytes padding to 8-align the bitvectors (matches Go) */
    go_bitvector gcdatamask, gcbssmask;
    uintptr_t typemap;       /* map[*_type]*_type == pointer */
    struct go_moduledata *next;   /* @560 */
} go_moduledata;

/* ---- abi constants (internal/abi/symtab.go, go1.26) ---------------------- */
#define GO_FUNCDATA_ArgsPointerMaps    0
#define GO_FUNCDATA_LocalsPointerMaps  1
#define GO_PCDATA_UnsafePoint          0
#define GO_PCDATA_StackMapIndex        1
#define GO_PCLNTAB_MAGIC_GO120         0xFFFFFFF1u   /* == CurrentPCLnTabMagic; copied from the live pcHeader, not hardcoded */

/* Registers we snapshot into the trampoline frame (= the block the C hook reads).
 * Order is the Go internal register ABI arg order so the hook can name fields.
 * `rbp` is the 11th slot (OFF_RBP = GH_SPILL+88) — the trampoline already spills it;
 * naming it lets the stack unwinder read the caller frame pointer. */
typedef struct {
    uint64_t rax, rbx, rcx, rdi, rsi, r8, r9, r10, r11, rdx, rbp;
} agy_go_regs;   /* 88 bytes; lives in the trampoline locals region (all-ones scanned) */

/* The block the trampoline builds on its stack and passes to the C hook (RDI).
 * `kind` is a borrowed const char* (the procdef.h kind tag) baked into the
 * trampoline as an imm64; regs are the target's snapshotted arg registers.
 * `action` is the filter verdict the hook writes back: 0 = PASS (run the body,
 * the default), nonzero = RETURN (the hook has written the Go-ABI return values
 * into `regs`; the stub restores them and `ret`s to the caller, skipping the
 * body). It occupies the former frame pad (OFF_ACTION), so GH_FRAME is unchanged. */
typedef struct { uint64_t kind; agy_go_regs regs; uint64_t action; } agy_block;

/* _func total stride in pclntable: 44-byte header + funcdata[2] (no pcdata). */
#define AGY_FUNC_STRIDE 52

/* The synthetic moduledata is a process singleton kept in STATIC (BSS) storage — no heap, nothing
 * to leak, no OOM path. These bound its fixed-size tables; agy_gomod_prepare rejects (rc<0) anything
 * larger, and antigravity.cpp static_asserts HK_COUNT <= AGY_GOMOD_MAX_SLOTS so the hook table can
 * never outgrow it (bump the cap if you add that many hooks). MAX_MAPBYTES bounds the locals bitmap:
 * mapbytes = ((frame-8)/8 + 7)/8, which is 2 for the real frame (GH_FRAME = 120). */
#define AGY_GOMOD_MAX_SLOTS    64
#define AGY_GOMOD_MAX_MAPBYTES 8

/* Two-phase registration (our LD_PRELOAD ctor runs before Go rt0, when
 * firstmoduledata's pointer fields are still unrelocated):
 *
 * agy_gomod_prepare() — CONSTRUCTOR time. Build a synthetic moduledata covering
 * [region_base, region_end) as `nslots` identical trampoline funcs (each
 * `slot_size` bytes, entry i at region_base + i*slot_size), using the known
 * go1.26/amd64 pcHeader constants (no firstmoduledata read). Per-slot SP-delta
 * profile: 0 for [0,frame_lo), FRAME for [frame_lo,frame_hi), 0 for
 * [frame_hi,slot_size); saved regs in the locals region are scanned conservatively
 * (all-ones stackmap). Stores state for ensure(). Returns 0, or <0 on OOM.
 *
 * agy_gomod_ensure() — called from the trampoline on the first hit (Go live).
 * Once-guarded: self-check the now-relocated firstmoduledata layout, then splice
 * our module onto the chain tail so findfunc/GC-unwind resolve the trampoline PCs.
 * Minimal stack + bare write() — safe to run on a goroutine stack. */
#ifdef __cplusplus
extern "C" {
#endif
int  agy_gomod_prepare(uint64_t firstmd_addr, uint64_t cgocall_rt,
                       uintptr_t region_base, uintptr_t region_end,
                       uint32_t slot_size, int nslots,
                       uint32_t frame, uint32_t frame_lo, uint32_t frame_hi);
void agy_gomod_ensure(void);
#ifdef __cplusplus
}
#endif

/* The frame-pointer unwinder (agy_safe_read/agy_backtrace/agy_emit_stack) and the
 * cgocall-trampoline installer (class AgyGoHook) are cgotrampoline.cpp's API — see
 * cgotrampoline.h. cgotrampoline.cpp includes this header for agy_gomod_prepare/ensure. */

#endif /* AGY_GOMOD_H */
