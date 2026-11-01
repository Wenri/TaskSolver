/* gomod.cpp — build and register the synthetic moduledata describing the linked stubs
 * from their entry, frame and end labels. See gomod.h.
 */
#include "goframe.h"
#include "gomod.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#define GLOG(...) do { std::fprintf(stderr, "[antigravity/gomod] " __VA_ARGS__); \
                       std::fputc('\n', stderr); std::fflush(stderr); } while (0)

/* go1.27: FuncTabBucketSize = 256*MINFUNC, MINFUNC = 16. */
#define GO_FUNCTAB_BUCKET 4096

/* goframe.h's offsets are literals the tests read; each must match agy_block's layout. */
#define AGY_CHK(f, off) static_assert(AGY_OFF_BLOCK + offsetof(agy_block, f) == (off), \
                                      "trampoline frame offset drifted from agy_block::" #f)
AGY_CHK(book,     AGY_OFF_BOOK);    AGY_CHK(regs.rax, AGY_OFF_RAX);
AGY_CHK(regs.rbx, AGY_OFF_RBX);     AGY_CHK(regs.rcx, AGY_OFF_RCX);
AGY_CHK(regs.rdi, AGY_OFF_RDI);     AGY_CHK(regs.rsi, AGY_OFF_RSI);
AGY_CHK(regs.r8,  AGY_OFF_R8);      AGY_CHK(regs.r9,  AGY_OFF_R9);
AGY_CHK(regs.r10, AGY_OFF_R10);     AGY_CHK(regs.r11, AGY_OFF_R11);
AGY_CHK(regs.rdx, AGY_OFF_RDX);     AGY_CHK(regs.rbp, AGY_OFF_RBP);
AGY_CHK(action,   AGY_OFF_ACTION);
#undef AGY_CHK
static_assert(AGY_OFF_BLOCK + sizeof(agy_block) == AGY_FRAME,
              "the stub frame must be exactly the cgocall arg pair plus agy_block");
static_assert(AGY_OFF_RBP + sizeof(uint64_t) == AGY_FRAME,
              "Go's unwinder expects the saved frame pointer at the top of the frame");
static_assert(sizeof(go_func) == 44, "go_func header must be 44 bytes");
static_assert(sizeof(go_functab) == 8, "functab must be 8 bytes");
static_assert(sizeof(go_findfuncbucket) == 20, "findfuncbucket must be 20 bytes");
static_assert(sizeof(go_textsect) == 24, "textsect must be 24 bytes");
static_assert(AGY_GOMOD_MAX_FUNCS <= 256, "findfunc subbucket deltas must fit in a byte");
static_assert((AGY_FRAME - 8 + 63) / 64 <= AGY_GOMOD_MAX_MAPBYTES,
              "locals bitmap exceeds static capacity");
/* A layout change in agy's Go must fail the build, not corrupt the splice. */
static_assert(sizeof(go_moduledata) == 568, "moduledata must be 568 bytes (go1.27 amd64)");
static_assert(offsetof(go_moduledata, text) == 176, "text@176");
static_assert(offsetof(go_moduledata, gofunc) == 344, "gofunc@344 (go1.27)");
static_assert(offsetof(go_moduledata, next) == 560, "next@560 (go1.27)");

/* LEB128 */
static size_t put_uvarint(uint8_t *p, uint32_t v)
{
    size_t n = 0;
    do { uint8_t b = v & 0x7f; v >>= 7; if (v) b |= 0x80; p[n++] = b; } while (v);
    return n;
}
static uint32_t zigzag(int32_t d) { return ((uint32_t)d << 1) ^ (uint32_t)(d >> 31); }

/* One pctab step, as runtime.step decodes it. */
static size_t put_step(uint8_t *p, int32_t dval, uint32_t dpc)
{
    size_t n = put_uvarint(p, zigzag(dval));
    n += put_uvarint(p + n, dpc);   /* PCQuantum == 1 on amd64 */
    return n;
}

/* Logical offsets bound the bucket array by code size, however far apart the stubs link. */
static struct {
    go_moduledata md;
    go_pcHeader ph;
    go_functab ftab[AGY_GOMOD_MAX_FUNCS + 1];
    go_textsect sections[AGY_GOMOD_MAX_FUNCS];
    go_findfuncbucket fft[(AGY_GOMOD_MAX_TEXT_SIZE + GO_FUNCTAB_BUCKET - 1) / GO_FUNCTAB_BUCKET];
    alignas(uint32_t) uint8_t pcln[AGY_GOMOD_MAX_FUNCS * AGY_FUNC_STRIDE];
    uint8_t names[32];   /* "\0agy_cgo_tramp\0" */
    uint8_t pctab[1 + AGY_GOMOD_MAX_FUNCS * 32];  /* three steps: at most six u32 varints + terminator */
    alignas(uint32_t) uint8_t smap[sizeof(go_stackmap) + AGY_GOMOD_MAX_MAPBYTES];
} g_mod;

int agy_gomod_register(std::span<const agy_stub_info> stubs)
{
    const size_t nstubs = stubs.size();
    if (stubs.empty() || nstubs > AGY_GOMOD_MAX_FUNCS || g_mod.md.pcHeader) {
        GLOG("invalid stub count or repeated module registration (nfunc=%zu)", nstubs);
        return -1;
    }
    struct range { uintptr_t entry, frame_start, frame_end, end; };
    std::array<range, AGY_GOMOD_MAX_FUNCS> ranges;
    uint32_t text_size = 0;
    for (size_t i = 0; i < nstubs; ++i) {
        const auto &s = stubs[i];
        auto &r = ranges[i];
        r = {(uintptr_t)s.entry, (uintptr_t)s.frame_start, (uintptr_t)s.frame_end, (uintptr_t)s.end};
        if (!r.entry || r.entry >= r.frame_start || r.frame_start >= r.frame_end || r.frame_end >= r.end) {
            GLOG("invalid linked boundaries for stub %zu", i);
            return -2;
        }
        if (r.end - r.entry > AGY_GOMOD_MAX_TEXT_SIZE - text_size) {
            GLOG("stub code exceeds static metadata capacity (%u bytes)", AGY_GOMOD_MAX_TEXT_SIZE);
            return -3;
        }
        text_size += (uint32_t)(r.end - r.entry);
    }
    std::sort(ranges.begin(), ranges.begin() + nstubs,
              [](const range &a, const range &b) { return a.entry < b.entry; });
    for (size_t i = 1; i < nstubs; ++i) {
        if (ranges[i].entry < ranges[i - 1].end) {
            GLOG("overlapping linked stub ranges");
            return -2;
        }
    }
    uintptr_t region_base = ranges[0].entry, region_end = ranges[nstubs - 1].end;
    go_moduledata *fmd = &agy_firstmoduledata;
    go_pcHeader *fph = fmd->pcHeader;
    if (!fph || ((uintptr_t)fph & 7) || fph->magic != GO_PCLNTAB_MAGIC_GO120 ||
        fmd->minpc >= fmd->maxpc || !fmd->text) {
        GLOG("firstmoduledata layout check failed (fmd=%p pcHeader=%p)", (void *)fmd, (void *)fph);
        return -4;
    }
    go_moduledata *tail = fmd;
    for (go_moduledata *m = fmd; m; m = m->next) {
        // Go selects the first covering module before checking its textsectmap.
        if (region_base < m->maxpc && m->minpc < region_end) {
            GLOG("stub address span overlaps an existing Go module");
            return -2;
        }
        tail = m;
    }

    auto *md = &g_mod.md;
    auto *ph = &g_mod.ph;
    auto *ftab = g_mod.ftab;
    auto *smap = (go_stackmap *)g_mod.smap;
    ph->magic = GO_PCLNTAB_MAGIC_GO120;
    ph->minLC = 1;
    ph->ptrSize = 8;
    ph->nfunc = (intptr_t)nstubs;
    std::memcpy(g_mod.names + 1, "agy_cgo_tramp", 13);

    const int nbit = (AGY_FRAME - 8) / 8;
    const int mapbytes = (nbit + 7) / 8;
    smap->n = 1;
    smap->nbit = nbit;
    std::memset(smap->bytedata, 0xff, mapbytes);
    if (nbit % 8) smap->bytedata[mapbytes - 1] = (uint8_t)((1u << (nbit % 8)) - 1);

    size_t po = 1;
    uint32_t logical = 0;
    for (size_t i = 0; i < nstubs; ++i) {
        const auto &r = ranges[i];
        uint32_t size = (uint32_t)(r.end - r.entry);
        g_mod.sections[i] = {logical, logical + size, r.entry};
        ftab[i] = {logical, (uint32_t)i * AGY_FUNC_STRIDE};
        auto *f = (go_func *)(g_mod.pcln + i * AGY_FUNC_STRIDE);
        f->entryOff = logical;
        f->nameOff = 1;
        f->pcsp = (uint32_t)po;
        f->funcID = 0;
        f->flag = GO_FUNCFLAG_ASM;
        f->nfuncdata = 2;
        auto *fd = (uint32_t *)(g_mod.pcln + i * AGY_FUNC_STRIDE + sizeof(go_func));
        fd[GO_FUNCDATA_ArgsPointerMaps] = UINT32_MAX;
        fd[GO_FUNCDATA_LocalsPointerMaps] = 0;

        // Go's pcsp value changes after each stack adjustment instruction ends.
        po += put_step(g_mod.pctab + po, 1, (uint32_t)(r.frame_start - r.entry));
        po += put_step(g_mod.pctab + po, AGY_FRAME, (uint32_t)(r.frame_end - r.frame_start));
        po += put_step(g_mod.pctab + po, -AGY_FRAME, (uint32_t)(r.end - r.frame_end));
        g_mod.pctab[po++] = 0;
        logical += size;
    }
    ftab[nstubs] = {text_size, 0};

    // Each hint starts at the function covering the subbucket's first logical PC.
    const size_t nbuckets = (text_size + GO_FUNCTAB_BUCKET - 1) / GO_FUNCTAB_BUCKET;
    size_t index = 0;
    for (size_t b = 0; b < nbuckets; ++b) {
        uint32_t pc = (uint32_t)b * GO_FUNCTAB_BUCKET;
        while (index + 1 < nstubs && ftab[index + 1].entryoff <= pc) ++index;
        auto &bucket = g_mod.fft[b];
        bucket.idx = (uint32_t)index;
        for (size_t sub = 0; sub < std::size(bucket.subbuckets); ++sub) {
            uint32_t subpc = pc + (uint32_t)sub * (GO_FUNCTAB_BUCKET / 16);
            if (subpc >= text_size) break;
            while (index + 1 < nstubs && ftab[index + 1].entryoff <= subpc) ++index;
            bucket.subbuckets[sub] = (uint8_t)(index - bucket.idx);
        }
    }

    md->pcHeader = ph;
    md->funcnametab = go_slice{g_mod.names, sizeof(g_mod.names), sizeof(g_mod.names)};
    md->pctab = go_slice{g_mod.pctab, (intptr_t)po, (intptr_t)po};
    md->pclntable = go_slice{g_mod.pcln, (intptr_t)nstubs * AGY_FUNC_STRIDE, (intptr_t)nstubs * AGY_FUNC_STRIDE};
    md->ftab = go_slice{ftab, (intptr_t)nstubs + 1, (intptr_t)nstubs + 1};
    md->textsectmap = go_slice{g_mod.sections, (intptr_t)nstubs, (intptr_t)nstubs};
    md->findfunctab = (uintptr_t)g_mod.fft;
    md->minpc = md->text = region_base;
    md->maxpc = md->etext = region_end;
    md->gofunc = (uintptr_t)smap;
    md->bad = 1;   // modulesinit skips it; findfunc still walks it.
    std::atomic_ref<go_moduledata *>(tail->next).store(md, std::memory_order_release);

    if (std::getenv("AGY_PROC_ENABLE") && !std::getenv("_AGY_SBOXSERVE")) {
        GLOG("registered synthetic moduledata on agy_firstmoduledata (%p): %zu funcs, "
             "%u code bytes, text=[%#lx,%#lx), frame=%u, nbit=%d",
             (void *)fmd, nstubs, text_size, (unsigned long)region_base, (unsigned long)region_end,
             AGY_FRAME, nbit);
    }
    return 0;
}
