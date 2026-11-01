/* gomod.h — go1.27 runtime-metadata mirrors for the cgocall trampolines.
 *
 * cgocall's entersyscall() parks the goroutine in _Gsyscall, where the GC scans its stack
 * without preempting it, unwinding every frame above gp.syscallsp through the pclntab. That
 * walk crosses our stub, and a PC findfunc cannot resolve is a fatal throw("unknown pc"), so
 * each stub needs Go metadata. findmoduledatap walks the firstmoduledata LINKED LIST, so
 * publishing a module is one pointer write, with no Go allocation and no modulesinit.
 *
 * FRAME GEOMETRY (amd64, framepointer_enabled) — from traceback.go:
 *   fp   = sp + funcspdelta(pc) + 8     (ret-addr slot at fp-8 == [sp+spdelta])
 *   locals scanned = [sp, fp - 16)      (the -16 skips the ret-addr and saved-BP slots)
 * A stub is entered past the target's stack check but before its frame allocation, so the entry
 * SP is still the target's and [entrySP] holds the target's return address: with spdelta ==
 * AGY_FRAME at the call, the unwind flows on to the target's caller. The all-ones locals map is
 * safe because findObject ignores non-pointers, and it keeps the saved argument registers and
 * the handler's read buffer alive across the cgo window.
 *
 * FuncID must stay 0: the asyncPreempt/debugCallV2 ids make the unwinder read the frame as an
 * injectedCall. FuncFlagAsm bars async preemption during the register saves and the displaced
 * prologue; FuncFlagSPWrite would stop the unwind.
 *
 * Verified against release-branch.go1.27 src/runtime and the live agy binary.
 */
#ifndef AGY_GOMOD_H
#define AGY_GOMOD_H

#include <stdint.h>
#include <stddef.h>
#include <span>
#include "goframe.h"

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

/* ---- runtime.functab: logical text entryoff and pclntable funcoff. */
typedef struct { uint32_t entryoff, funcoff; } go_functab;

/* Logical text offsets mapped onto independent linked function ranges. */
typedef struct { uintptr_t vaddr, end, baseaddr; } go_textsect;

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
    uint8_t  flag;         /* FuncFlagAsm only (no SPWrite/TopFrame) */
    uint8_t  pad[1];
    uint8_t  nfuncdata;    /* must be last; uint32-aligned boundary follows */
    /* uint32_t pcdata[npcdata];  uint32_t funcdata[nfuncdata]; */
} go_func;

/* ---- runtime.stackmap (symtab.go:1324): n bitmaps of nbit bits each ------ */
typedef struct { int32_t n, nbit; uint8_t bytedata[]; } go_stackmap;

/* ---- runtime.moduledata — must match agy's Go exactly; gomod.cpp pins the offsets.
 * go1.27 adds `typedesclen` and replaces the typelinks/itablinks slices with itaboffset/itabsize. */
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
#define GO_FUNCFLAG_ASM               4
#define GO_PCLNTAB_MAGIC_GO120         0xFFFFFFF1u   /* CurrentPCLnTabMagic since go1.20 */

/* Go's register-ABI argument order; `rbp` lands on the frame's saved-BP slot. */
typedef struct {
    uint64_t rax, rbx, rcx, rdi, rsi, r8, r9, r10, r11, rdx, rbp;
} agy_go_regs;

/* `action` is the handler's verdict: 0 runs the body; nonzero returns what it wrote into `regs`. */
typedef struct { uint32_t book, action; agy_go_regs regs; } agy_block;

/* _func total stride in pclntable: 44-byte header + funcdata[2] (no pcdata). */
#define AGY_FUNC_STRIDE 52

/* Static capacities: TEXT_SIZE bounds the summed stub sizes, not their spacing. */
#define AGY_GOMOD_MAX_FUNCS    64
#define AGY_GOMOD_MAX_TEXT_SIZE (AGY_GOMOD_MAX_FUNCS * 4096u)
#define AGY_GOMOD_MAX_MAPBYTES 8

extern "C" go_moduledata agy_firstmoduledata;

/* Publish the stubs as one module on agy_firstmoduledata.next, before Go starts. md->bad = 1
 * makes modulesinit skip it while findfunc still walks it; invalid input publishes nothing. */
int agy_gomod_register(std::span<const agy_stub_info> stubs);

#endif /* AGY_GOMOD_H */
