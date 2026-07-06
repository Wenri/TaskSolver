/* gomod.cpp — build + register a synthetic go1.26 runtime.moduledata that covers
 * the cgocall-trampoline page, so the Go GC's stack scanner (findfunc ->
 * findmoduledatap, walking the firstmoduledata linked list) resolves our
 * trampoline PCs instead of throw("unknown pc"). See gomod.h.
 *
 * Split in two phases because our LD_PRELOAD constructor runs BEFORE Go's rt0:
 *   - agy_gomod_prepare(): build the module + all its buffers at constructor time
 *     using the known go1.26/amd64 pcHeader constants. Does NOT read
 *     firstmoduledata (whose pointer fields are still unrelocated / NULL that
 *     early — dereferencing pcHeader there segfaults).
 *   - agy_gomod_ensure(): called from the trampoline on the FIRST hit (goroutine
 *     live, firstmoduledata fully relocated). Once-guarded: self-check the live
 *     layout, then splice our module onto the chain — all before the cgocall that
 *     opens the GC-scan window. Minimal stack + bare write() (runs on a goroutine
 *     stack), so it never overflows it.
 */
#include "gomod.h"
#include "pybridge.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <atomic>

#define GLOG(...) do { fprintf(stderr, "[antigravity/gomod] " __VA_ARGS__); \
                       fputc('\n', stderr); fflush(stderr); } while (0)

/* go1.26: FuncTabBucketSize = 256*MINFUNC, MINFUNC = 16. */
#define GO_FUNCTAB_BUCKET 4096

static_assert(sizeof(go_func) == 44, "go_func header must be 44 bytes");
static_assert(sizeof(go_functab) == 8, "functab must be 8 bytes");
static_assert(sizeof(go_findfuncbucket) == 20, "findfuncbucket must be 20 bytes");
/* Pin the go1.27 moduledata layout (agy). If agy's Go changes these, the build
 * fails loudly instead of corrupting memory (as an off-by-N splice would). */
static_assert(sizeof(go_moduledata) == 568, "moduledata must be 568 bytes (go1.27 amd64)");
static_assert(offsetof(go_moduledata, text) == 176, "text@176");
static_assert(offsetof(go_moduledata, gofunc) == 344, "gofunc@344 (go1.27)");
static_assert(offsetof(go_moduledata, next) == 560, "next@560 (go1.27)");

/* LEB128 varint append (used by the pctab SP-delta encoding). */
static size_t put_uvarint(uint8_t *p, uint32_t v)
{
    size_t n = 0;
    do { uint8_t b = v & 0x7f; v >>= 7; if (v) b |= 0x80; p[n++] = b; } while (v);
    return n;
}
/* zigzag encode for the signed value-delta stream. */
static uint32_t zigzag(int32_t d) { return ((uint32_t)d << 1) ^ (uint32_t)(d >> 31); }

/* Emit one (value-delta, pc-delta) step into the pctab (step() format). */
static size_t put_step(uint8_t *p, int32_t dval, uint32_t dpc)
{
    size_t n = put_uvarint(p, zigzag(dval));
    n += put_uvarint(p + n, dpc);   /* PCQuantum == 1 on amd64 */
    return n;
}

/* Deferred-registration state: prepare() fills this at constructor time; ensure()
 * consumes it (once) at the first trampoline hit. */
static go_moduledata *g_md;
static uint64_t g_firstmd_addr, g_cgocall_rt;
static std::atomic<int> g_claimed{0}, g_done{0};

int agy_gomod_prepare(uint64_t firstmd_addr, uint64_t cgocall_rt,
                      uintptr_t region_base, uintptr_t region_end,
                      uint32_t slot_size, int nslots,
                      uint32_t frame, uint32_t frame_lo, uint32_t frame_hi)
{
    int nfunc = nslots;
    uint32_t region_size = (uint32_t)(region_end - region_base);
    int nbuckets = (int)(region_size / GO_FUNCTAB_BUCKET) + 1;

    /* metadata buffers, off the Go heap (moduledata is NotInHeap; C allocs qualify) */
    go_moduledata     *md   = (go_moduledata *)calloc(1, sizeof *md);
    go_pcHeader       *ph   = (go_pcHeader *)calloc(1, sizeof *ph);
    go_functab        *ftab = (go_functab *)calloc(nfunc + 1, sizeof *ftab);   /* +1 sentinel */
    go_findfuncbucket *fft  = (go_findfuncbucket *)calloc(nbuckets, sizeof *fft); /* all-zero => scan from ftab[0] */
    uint8_t           *pcln = (uint8_t *)calloc(nfunc, AGY_FUNC_STRIDE);       /* the _func records */
    uint8_t           *names = (uint8_t *)calloc(1, 32);                       /* "\0agy_cgo_tramp\0" */
    uint8_t           *pctab = (uint8_t *)calloc(1, 32);                       /* dummy[0] + 3-region pcsp */
    int nbit = (int)((frame - 8) / 8);
    int mapbytes = (nbit + 7) / 8;
    go_stackmap       *smap = (go_stackmap *)calloc(1, sizeof *smap + mapbytes);
    if (!md || !ph || !ftab || !fft || !pcln || !names || !pctab || !smap) {
        GLOG("out of memory building moduledata");
        return -3;
    }

    /* pcHeader: hardcode the go1.26/amd64 constants (NOT read from firstmoduledata,
     * which isn't relocated yet at constructor time). Only moduledataverify1 (a
     * startup-only check that never runs on our dynamically-spliced module) reads
     * these; ensure()'s self-check confirms the live binary matches. */
    ph->magic   = GO_PCLNTAB_MAGIC_GO120;
    ph->minLC   = 1;   /* sys.PCQuantum (amd64) */
    ph->ptrSize = 8;
    ph->nfunc   = nfunc;
    ph->nfiles  = 0;

    /* funcnametab: byte 0 empty, name at offset 1. */
    memcpy(names + 1, "agy_cgo_tramp", 13);
    uint32_t nameOff = 1;

    /* pctab: index 0 is reserved ("no table"); real table starts at 1. */
    size_t po = 1;
    po += put_step(pctab + po, /*dval*/ 0 - (-1),        /*dpc*/ frame_lo);
    po += put_step(pctab + po, /*dval*/ (int32_t)frame,  /*dpc*/ frame_hi - frame_lo);
    po += put_step(pctab + po, /*dval*/ -(int32_t)frame, /*dpc*/ slot_size - frame_hi);
    pctab[po++] = 0;   /* terminator (uvdelta==0) */
    uint32_t pcspOff = 1;

    /* stackmap: n=1 bitmap, nbit words, all pointer bits set (conservative). */
    smap->n = 1;
    smap->nbit = nbit;
    memset(smap->bytedata, 0xff, mapbytes);
    if (nbit % 8) smap->bytedata[mapbytes - 1] = (uint8_t)((1u << (nbit % 8)) - 1);

    /* _func records: identical but for entryOff. funcdata sits at fixed offset 44
     * (Go computes it from &nfuncdata, so it must follow the header):
     *   funcdata[ArgsPointerMaps=0]  = ^0  (nil, args==0 so never read)
     *   funcdata[LocalsPointerMaps=1]= 0   (gofunc+0 == our stackmap) */
    for (int i = 0; i < nfunc; i++) {
        go_func *f = (go_func *)(pcln + (size_t)i * AGY_FUNC_STRIDE);
        f->entryOff  = (uint32_t)i * slot_size;
        f->nameOff   = (int32_t)nameOff;
        f->args      = 0;
        f->pcsp      = pcspOff;
        f->funcID    = 0;   /* normal — NOT asyncPreempt (unwinder injectedCall) */
        f->flag      = 0;   /* NOT SPWrite (sub/add rsp is encoded in pcsp) */
        f->nfuncdata = 2;
        uint32_t *fd = (uint32_t *)(pcln + (size_t)i * AGY_FUNC_STRIDE + 44);
        fd[GO_FUNCDATA_ArgsPointerMaps]   = 0xffffffffu;   /* nil */
        fd[GO_FUNCDATA_LocalsPointerMaps] = 0;             /* gofunc + 0 */
    }

    /* ftab: nfunc entries (sorted by entryoff) + sentinel at [nfunc]. */
    for (int i = 0; i < nfunc; i++) {
        ftab[i].entryoff = (uint32_t)i * slot_size;
        ftab[i].funcoff  = (uint32_t)i * AGY_FUNC_STRIDE;
    }
    ftab[nfunc].entryoff = (uint32_t)nfunc * slot_size;   /* == region_size; textAddr => maxpc */
    ftab[nfunc].funcoff  = 0;

    /* moduledata: only the fields findfunc/GC-unwind consult; the rest stays zero
     * so no phantom data/type ranges are ever scanned (we only join the
     * findmoduledatap linked list, never modulesSlice). */
    md->pcHeader     = ph;
    md->funcnametab  = go_slice{ names, 32, 32 };
    md->pctab        = go_slice{ pctab, (intptr_t)po, (intptr_t)po };
    md->pclntable    = go_slice{ pcln, (intptr_t)nfunc * AGY_FUNC_STRIDE, (intptr_t)nfunc * AGY_FUNC_STRIDE };
    md->ftab         = go_slice{ ftab, nfunc + 1, nfunc + 1 };
    md->findfunctab  = (uintptr_t)fft;
    md->minpc        = region_base;
    md->maxpc        = region_end;
    md->text         = region_base;
    md->etext        = region_end;
    md->gofunc       = (uintptr_t)smap;   /* @344: funcdata[Locals] offset 0 => &stackmap */
    md->next         = nullptr;           /* @560: our module is the chain tail */

    g_md = md;
    g_firstmd_addr = firstmd_addr;
    g_cgocall_rt = cgocall_rt;
    GLOG("prepared synthetic moduledata: text=[%#lx,%#lx) %d funcs, frame=%u "
         "(spdelta in [%u,%u)), nbit=%d — splice deferred to first trampoline hit",
         (unsigned long)region_base, (unsigned long)region_end, nfunc, frame,
         frame_lo, frame_hi, nbit);
    return 0;
}

void agy_gomod_ensure(void)
{
    if (g_done.load(std::memory_order_acquire)) return;             /* fast path */
    int expected = 0;
    while (!g_claimed.compare_exchange_strong(expected, 1,
                                              std::memory_order_acq_rel, std::memory_order_relaxed)) {
        /* Lost the claim to another thread. If it finished the splice, we're done.
         * Otherwise it may have hit the pre-init retry path and dropped g_claimed back
         * to 0 WITHOUT setting g_done — so loop and try to become the splicer ourselves
         * rather than wait forever on a g_done the claimer is not obligated to set. */
        if (g_done.load(std::memory_order_acquire)) return;
        __builtin_ia32_pause();
        expected = 0;
    }

    go_moduledata *md  = g_md;
    go_moduledata *fmd = (go_moduledata *)(uintptr_t)g_firstmd_addr;
    go_pcHeader   *fph = fmd ? fmd->pcHeader : nullptr;
    int magic_ok = fph && !((uintptr_t)fph & 7) && fph->magic == GO_PCLNTAB_MAGIC_GO120;
    int ok = md && fmd && magic_ok
             && fmd->minpc < fmd->maxpc && fmd->text
             && fmd->minpc <= g_cgocall_rt && g_cgocall_rt < fmd->maxpc;
    if (ok) {
        go_moduledata *tail = fmd;
        while (tail->next) tail = tail->next;                       /* @560, correct go1.27 offset */
        /* Publish our module on Go's own `next` field. Its type must stay go_moduledata* (the
         * moduledata layout is pinned by static_asserts), so use atomic_ref for the release store
         * rather than making the field itself std::atomic. */
        std::atomic_ref<go_moduledata *>(tail->next).store(md, std::memory_order_release);
        g_done.store(1, std::memory_order_release);
    } else {
        /* self-check failed (e.g. first hit was pre-runtime-init: firstmoduledata
         * pointers not relocated yet) — release the claim so a later hit retries. */
        g_claimed.store(0, std::memory_order_release);
    }
}
