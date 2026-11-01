/* Exercise the real metadata builder without starting Go or the capture bridge. */
#include "gomod.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#define CHECK(expr) do { if (!(expr)) { \
    std::fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #expr); \
    std::exit(1); \
} } while (0)

extern "C" { go_moduledata agy_firstmoduledata{}; }
static go_pcHeader first_header{};

static uintptr_t entry(const agy_stub_info &s) { return (uintptr_t)s.entry; }
static agy_stub_info stub(uintptr_t pc, uintptr_t size, uintptr_t lo, uintptr_t hi)
{
    return {(void (*)(void))pc, (const unsigned char *)(pc + lo),
            (const unsigned char *)(pc + hi), (const unsigned char *)(pc + size)};
}

/* Independent translations of runtime.moduledata.textOff/textAddr and findfunc.
 * In particular, findfunc itself has no invalid-functab-entry check: textOff must
 * reject holes before the bucket lookup. */
static bool text_off(const go_moduledata *md, uintptr_t pc, uint32_t &off)
{
    uintptr_t value = pc - md->text;
    if (md->textsectmap.len > 1) {
        auto sections = (const go_textsect *)md->textsectmap.ptr;
        bool found = false;
        for (intptr_t i = 0; i < md->textsectmap.len; i++) {
            const auto &s = sections[i];
            uintptr_t end = s.baseaddr + s.end - s.vaddr;
            if (s.baseaddr <= pc && (pc < end || (i + 1 == md->textsectmap.len && pc == end))) {
                value = pc - s.baseaddr + s.vaddr;
                found = true;
                break;
            }
        }
        if (!found) return false;
    }
    CHECK(value <= UINT32_MAX);
    off = (uint32_t)value;
    return true;
}

static uintptr_t text_addr(const go_moduledata *md, uint32_t off)
{
    if (md->textsectmap.len > 1) {
        auto sections = (const go_textsect *)md->textsectmap.ptr;
        for (intptr_t i = 0; i < md->textsectmap.len; i++) {
            const auto &s = sections[i];
            if (s.vaddr <= off && (off < s.end || (i + 1 == md->textsectmap.len && off == s.end)))
                return s.baseaddr + off - s.vaddr;
        }
        CHECK(false);
    }
    return md->text + off;
}

static const go_func *findfunc(uintptr_t pc)
{
    auto md = &agy_firstmoduledata;
    while (md && !(md->minpc <= pc && pc < md->maxpc)) md = md->next;
    if (!md) return nullptr;
    uint32_t off;
    if (!text_off(md, pc, off)) return nullptr;
    uintptr_t x = off + md->text - md->minpc;
    auto buckets = (const go_findfuncbucket *)md->findfunctab;
    const auto &bucket = buckets[x / 4096];
    uint32_t idx = bucket.idx + bucket.subbuckets[x % 4096 / 256];
    auto table = (const go_functab *)md->ftab.ptr;
    CHECK(idx + 1 < (uintptr_t)md->ftab.len);
    while (table[idx + 1].entryoff <= off) {
        idx++;
        CHECK(idx + 1 < (uintptr_t)md->ftab.len);
    }
    CHECK(table[idx].entryoff <= off);
    CHECK(table[idx].funcoff + sizeof(go_func) <= (uintptr_t)md->pclntable.len);
    return (const go_func *)((const unsigned char *)md->pclntable.ptr + table[idx].funcoff);
}

static uint32_t uvarint(const uint8_t *&p, const uint8_t *end)
{
    uint32_t value = 0;
    for (unsigned shift = 0; shift < 35; shift += 7) {
        CHECK(p < end);
        uint8_t byte = *p++;
        value |= (uint32_t)(byte & 127) << shift;
        if (!(byte & 128)) return value;
    }
    CHECK(false);
    return 0;
}

static int32_t spdelta(const go_moduledata *md, const go_func *fn, uint32_t pc)
{
    auto bytes = (const uint8_t *)md->pctab.ptr;
    const uint8_t *p = bytes + fn->pcsp, *end = bytes + md->pctab.len;
    int32_t value = -1;
    uint32_t limit = 0;
    for (bool first = true;; first = false) {
        uint32_t encoded = uvarint(p, end);
        CHECK(first || encoded != 0);  // every in-function PC must be described
        value += (encoded & 1) ? ~(int32_t)(encoded >> 1) : (int32_t)(encoded >> 1);
        limit += uvarint(p, end);  // PCQuantum == 1 on amd64
        if (pc < limit) return value;
    }
}

static void check_registered(std::vector<agy_stub_info> stubs)
{
    CHECK(agy_gomod_register(stubs) == 0);
    std::sort(stubs.begin(), stubs.end(), [](auto &a, auto &b) { return entry(a) < entry(b); });
    auto md = agy_firstmoduledata.next;
    CHECK(md && !md->next && md->bad);
    CHECK(md->pcHeader->nfunc == (intptr_t)stubs.size());
    CHECK(md->ftab.len == (intptr_t)stubs.size() + 1);
    CHECK(md->textsectmap.len == (intptr_t)stubs.size());
    CHECK(md->text == entry(stubs.front()) && md->minpc == md->text);
    CHECK(md->maxpc == (uintptr_t)stubs.back().end && md->etext == md->maxpc);
    CHECK(!findfunc(md->minpc - 1) && !findfunc(md->maxpc));
    auto table = (const go_functab *)md->ftab.ptr;
    auto sections = (const go_textsect *)md->textsectmap.ptr;
    auto map = (const go_stackmap *)md->gofunc;
    CHECK(map->n == 1 && map->nbit == (AGY_FRAME - 8) / 8);
    CHECK(map->bytedata[0] == 0xff && map->bytedata[1] == 0x1f);
    uint32_t logical = 0;
    for (size_t i = 0; i < stubs.size(); i++) {
        const auto &s = stubs[i];
        uintptr_t begin = entry(s), end = (uintptr_t)s.end;
        CHECK(sections[i].baseaddr == begin && sections[i].vaddr == logical);
        CHECK(sections[i].end == logical + end - begin);
        CHECK(table[i].entryoff == logical);
        const auto *fn = findfunc(begin);
        CHECK(fn && fn->entryOff == logical && text_addr(md, fn->entryOff) == begin);
        CHECK(fn->funcID == 0 && fn->flag == GO_FUNCFLAG_ASM);
        CHECK(fn->npcdata == 0 && fn->nfuncdata == 2);
        const auto *funcdata = (const uint32_t *)((const uint8_t *)fn + sizeof(go_func));
        CHECK(funcdata[GO_FUNCDATA_ArgsPointerMaps] == UINT32_MAX);
        CHECK(funcdata[GO_FUNCDATA_LocalsPointerMaps] == 0);
        for (uintptr_t pc = begin; pc < end; pc++) {
            CHECK(findfunc(pc) == fn);
            int expected = (uintptr_t)s.frame_start <= pc && pc < (uintptr_t)s.frame_end ? AGY_FRAME : 0;
            CHECK(spdelta(md, fn, (uint32_t)(pc - begin)) == expected);
        }
        if (i + 1 < stubs.size() && end < entry(stubs[i + 1])) {
            CHECK(!findfunc(end));
            CHECK(!findfunc(entry(stubs[i + 1]) - 1));
            CHECK(!findfunc(end + (entry(stubs[i + 1]) - end) / 2));
        }
        logical += (uint32_t)(end - begin);
    }
    CHECK(table[stubs.size()].entryoff == logical);
    CHECK(text_addr(md, logical) == md->maxpc);
}

int main(int argc, char **argv)
{
    CHECK(argc == 2 || argc == 3);
    first_header.magic = GO_PCLNTAB_MAGIC_GO120;
    agy_firstmoduledata.pcHeader = &first_header;
    agy_firstmoduledata.text = agy_firstmoduledata.minpc = 0x1000;
    agy_firstmoduledata.etext = agy_firstmoduledata.maxpc = 0x2000;
    std::vector<agy_stub_info> stubs;
    if (!std::strcmp(argv[1], "linked")) {
        CHECK(argc == 3);
        FILE *file = std::fopen(argv[2], "r");
        CHECK(file);
        unsigned long long pc, lo, hi, end;
        while (std::fscanf(file, "%llx %llx %llx %llx", &pc, &lo, &hi, &end) == 4)
            stubs.push_back(stub(pc, end - pc, lo - pc, hi - pc));
        CHECK(std::feof(file) && !stubs.empty());
        std::fclose(file);
        check_registered(stubs);
        return 0;
    }
    if (!std::strcmp(argv[1], "sparse")) {
        uintptr_t pc = 0x100000;
        for (unsigned i = 0; i < 28; i++) {
            // Vary frame boundaries and lengths; cross several bucket/subbucket boundaries.
            uintptr_t size = i == 11 ? 5003 : 37 + i * 13;
            stubs.push_back(stub(pc, size, 1 + i, size - 3));
            pc += size + (i % 3 ? 0 : 0x100003);
        }
        std::reverse(stubs.begin(), stubs.end());
        std::rotate(stubs.begin(), stubs.begin() + 9, stubs.end());
        check_registered(stubs);
        return 0;
    }
    stubs.push_back(stub(0x100000, 201, 20, 173));
    if (!std::strcmp(argv[1], "single")) {
        check_registered(stubs);
        return 0;
    }
    if (!std::strcmp(argv[1], "frame_start")) stubs[0].frame_start = (const unsigned char *)entry(stubs[0]);
    else if (!std::strcmp(argv[1], "frame_end")) stubs[0].frame_end = stubs[0].frame_start;
    else if (!std::strcmp(argv[1], "end")) stubs[0].end = stubs[0].frame_end;
    else if (!std::strcmp(argv[1], "overlap")) stubs.push_back(stub(entry(stubs[0]) + 200, 201, 20, 173));
    else if (!std::strcmp(argv[1], "module_overlap")) stubs[0] = stub(0x1ff0, 201, 20, 173);
    else if (!std::strcmp(argv[1], "count")) stubs.resize(AGY_GOMOD_MAX_FUNCS + 1, stubs[0]);
    else if (!std::strcmp(argv[1], "size")) stubs[0] = stub(0x100000, AGY_GOMOD_MAX_TEXT_SIZE + 1, 20, 173);
    else CHECK(false);
    CHECK(agy_gomod_register(stubs) < 0);
    CHECK(!agy_firstmoduledata.next);  // invalid metadata must never reach the runtime
    return 0;
}
