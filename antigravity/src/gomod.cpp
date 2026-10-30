/* gomod.cpp — build + register a synthetic go1.27 runtime.moduledata that covers
 * the `.agy_trampolines` section (`[agy_tramp_region_start, agy_tramp_region_end)`),
 * so the Go GC's stack scanner (findfunc -> findmoduledatap, walking the
 * firstmoduledata linked list) resolves our static trampoline PCs instead of
 * throw("unknown pc"). See gomod.h.
 *
 * Called once from `agy_init()` before `agy_rt0_go()`: `ld.so` has already
 * processed all `R_X86_64_RELATIVE` and `R_X86_64_GLOB_DAT` relocations in
 * `libagy.so` and `vendor/agy`, so `agy_firstmoduledata` is bound directly via `ld`
 * and its `pcHeader` is already relocated. Setting `md->bad = 1` tells Go's startup
 * `runtime.modulesinit()` to skip our synthetic module while `findfunc` still walks
 * `agy_firstmoduledata.next`.
 */
#include "gomod.h"
#include "wirecap.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>
#include <atomic>

#define GLOG(...) do { std::fprintf(stderr, "[antigravity/gomod] " __VA_ARGS__); \
                       std::fputc('\n', stderr); std::fflush(stderr); } while (0)

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

/* The synthetic moduledata + all its tables in ONE static (BSS) block — the process singleton:
 * zero-initialized, fixed address, alive for the whole run (Go owns it forever), and NO heap
 * (nothing to leak, no OOM path, and a second build is structurally impossible). Sized for
 * AGY_GOMOD_MAX_SLOTS; agy_gomod_register() fills only the first `nslots`. The raw byte buffers that back Go
 * structs are aligned for them (go_func / go_stackmap are uint32-aligned; the typed arrays and md/ph
 * self-align). */
static struct {
    go_moduledata     md;
    go_pcHeader       ph;
    go_functab        ftab[AGY_GOMOD_MAX_SLOTS + 1];   /* nfunc entries + 1 sentinel */
    go_findfuncbucket fft[AGY_GOMOD_MAX_SLOTS + 1];    /* nbuckets ≤ nfunc/16+1, always ≤ this */
    alignas(uint32_t) uint8_t pcln[AGY_GOMOD_MAX_SLOTS * AGY_FUNC_STRIDE];   /* the _func records */
    uint8_t           names[32];   /* "\0agy_cgo_tramp\0" */
    uint8_t           pctab[32];   /* dummy[0] + 3-region pcsp */
    alignas(uint32_t) uint8_t smap[sizeof(go_stackmap) + AGY_GOMOD_MAX_MAPBYTES];  /* stackmap hdr+bits */
} g_mod;

int agy_gomod_register(uintptr_t region_base, uintptr_t region_end,
                       uint32_t slot_size, int nslots,
                       uint32_t frame, uint32_t frame_lo, uint32_t frame_hi)
{
    int nfunc = nslots;
    uint32_t region_size = (uint32_t)(region_end - region_base);
    int nbuckets = (int)(region_size / GO_FUNCTAB_BUCKET) + 1;
    int nbit = (int)((frame - 8) / 8);
    int mapbytes = (nbit + 7) / 8;

    if (nfunc > AGY_GOMOD_MAX_SLOTS || nbuckets > AGY_GOMOD_MAX_SLOTS + 1 ||
        mapbytes > AGY_GOMOD_MAX_MAPBYTES) {
        GLOG("moduledata exceeds static caps (nfunc=%d nbuckets=%d mapbytes=%d) — bump AGY_GOMOD_MAX_SLOTS",
             nfunc, nbuckets, mapbytes);
        return -3;
    }
    go_moduledata     *md    = &g_mod.md;
    go_pcHeader       *ph    = &g_mod.ph;
    go_functab        *ftab  = g_mod.ftab;
    go_findfuncbucket *fft   = g_mod.fft;
    uint8_t           *pcln  = g_mod.pcln;
    uint8_t           *names = g_mod.names;
    uint8_t           *pctab = g_mod.pctab;
    go_stackmap       *smap  = (go_stackmap *)g_mod.smap;

    ph->magic   = GO_PCLNTAB_MAGIC_GO120;
    ph->minLC   = 1;   /* sys.PCQuantum (amd64) */
    ph->ptrSize = 8;
    ph->nfunc   = nfunc;
    ph->nfiles  = 0;

    std::memcpy(names + 1, "agy_cgo_tramp", 13);
    uint32_t nameOff = 1;

    size_t po = 1;
    po += put_step(pctab + po, /*dval*/ 0 - (-1),        /*dpc*/ frame_lo);
    po += put_step(pctab + po, /*dval*/ (int32_t)frame,  /*dpc*/ frame_hi - frame_lo);
    po += put_step(pctab + po, /*dval*/ -(int32_t)frame, /*dpc*/ slot_size - frame_hi);
    pctab[po++] = 0;   /* terminator (uvdelta==0) */
    uint32_t pcspOff = 1;

    smap->n = 1;
    smap->nbit = nbit;
    std::memset(smap->bytedata, 0xff, mapbytes);
    if (nbit % 8) smap->bytedata[mapbytes - 1] = (uint8_t)((1u << (nbit % 8)) - 1);

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

    for (int i = 0; i < nfunc; i++) {
        ftab[i].entryoff = (uint32_t)i * slot_size;
        ftab[i].funcoff  = (uint32_t)i * AGY_FUNC_STRIDE;
    }
    ftab[nfunc].entryoff = (uint32_t)nfunc * slot_size;   /* == region_size; textAddr => maxpc */
    ftab[nfunc].funcoff  = 0;

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
    md->bad          = 1;                 /* @513: skip in runtime.modulesinit, still walked by findfunc */
    md->next         = nullptr;           /* @560: our module is the chain tail */

    go_moduledata *fmd = &agy_firstmoduledata;
    go_pcHeader   *fph = fmd->pcHeader;
    if (!fph || ((uintptr_t)fph & 7) || fph->magic != GO_PCLNTAB_MAGIC_GO120 ||
        fmd->minpc >= fmd->maxpc || !fmd->text) {
        GLOG("firstmoduledata layout check failed (fmd=%p pcHeader=%p)", (void *)fmd, (void *)fph);
        return -4;
    }
    go_moduledata *tail = fmd;
    while (tail->next) tail = tail->next;
    std::atomic_ref<go_moduledata *>(tail->next).store(md, std::memory_order_release);

    if (std::getenv("AGY_PROC_ENABLE") && !std::getenv("_AGY_SBOXSERVE")) {
        GLOG("registered synthetic moduledata on agy_firstmoduledata (%p): text=[%#lx,%#lx) %d funcs, "
             "frame=%u (spdelta in [%u,%u)), nbit=%d",
             (void *)fmd, (unsigned long)region_base, (unsigned long)region_end, nfunc, frame,
             frame_lo, frame_hi, nbit);
    }
    return 0;
}
