/* cgotrampoline.h — public API of the cgocall-trampoline installer + the frame-pointer
 * stack unwinder (cgotrampoline.cpp). The GC-safety machinery it depends on — the synthetic
 * runtime.moduledata and the Go runtime struct mirrors — lives in gomod.h. */
#ifndef AGY_CGOTRAMPOLINE_H
#define AGY_CGOTRAMPOLINE_H

#include <cstdint>
#include <memory>

/* ---- frame-pointer stack unwinder --------------------------------------------
 * C linkage kept: these are called across TUs (and agy_safe_read is resolved by name),
 * so unmangled symbols keep the cross-TU wiring byte-identical regardless of compiler. */
#ifdef __cplusplus
extern "C" {
#endif

/* Fault-safe read of `n` bytes from a possibly-bogus address in our own process
 * (process_vm_readv → EFAULT instead of SIGSEGV). Returns bytes read or -1. */
long agy_safe_read(uint64_t addr, void *dst, unsigned long n);

/* Walk the Go frame-pointer chain from `rbp` (Go keeps rbp: [rbp]=saved caller rbp,
 * [rbp+8]=return address). Stores up to `max` return PCs, each reduced to a link
 * vaddr (pc - base), into `out`. Terminates on rbp==0 / non-monotonic / unreadable.
 * Returns the number of frames captured. */
int  agy_backtrace(uint64_t rbp, uint64_t base, uint64_t *out, int max);

/* Emit a "callstack" event: src_kind (NUL-terminated) followed by the packed u64
 * frame vaddrs from agy_backtrace(rbp,base). No-op-safe if rbp is bogus. */
void agy_emit_stack(const char *src_kind, uint64_t rbp, uint64_t base);

#ifdef __cplusplus
}
#endif

/* ---- cgocall-trampoline installer (uses frida-gum) ---------------------------
 * A streaming builder so the caller need not stage the targets in an intermediate
 * array — it resolves + filters its own hook table and calls add() per target:
 *
 *   auto h = AgyGoHook::begin(base, cgocall_va, asmcgocall_va, max_targets);
 *   if (h) { for each trampoline hook: h->add(entry, skip, kind, asmcgo);
 *            int made = h->finalize(md_vaddr); }
 *
 * add() redirects (base+entry+skip) to a generated trampoline that marshals the
 * Go-ABI arg registers and CALLs runtime.cgocall(agy_cgo_hook, &block) — or
 * runtime.asmcgocall when asmcgo is set (g0 switch, NO syscall transition) — then
 * resumes the original body. finalize() builds the covering synthetic moduledata
 * (agy_gomod_prepare) so the trampolines are GC-unwind-safe, and returns the count
 * installed. Taking resolved (entry, skip) keeps this module symbol-map- and
 * hook-table-agnostic. begin() hard-fails (nullptr) if cgocall/asmcgocall is
 * unresolved; asmcgocall is REQUIRED (assumed resolved — build_symbols.py always
 * resolves it), no silent cgocall fallback.
 *
 * Lifetime: the returned unique_ptr owns the builder — its dtor releases the gum
 * code writer, while the generated RWX trampoline region and the synthetic
 * moduledata are permanent (they hold live code Go keeps executing) and are
 * intentionally never freed. */
struct _GumX86Writer;   /* frida-gum's opaque x86 writer — fwd-declared to keep this header gum-free */

class AgyGoHook {
public:
    static std::unique_ptr<AgyGoHook> begin(uint64_t base, uint64_t cgocall_va,
                                            uint64_t asmcgocall_va, int max_targets);
    void add(uint64_t entry, uint32_t skip, const char *kind, int asmcgo);
    int  finalize(uint64_t md_vaddr);
    ~AgyGoHook();

    AgyGoHook(const AgyGoHook &) = delete;
    AgyGoHook &operator=(const AgyGoHook &) = delete;

private:
    AgyGoHook() = default;

    uint64_t base_ = 0, cgocall_abs_ = 0, asmcgocall_abs_ = 0;
    uint8_t *region_ = nullptr;          /* gum RWX pages holding the trampolines (permanent) */
    struct _GumX86Writer *w_ = nullptr;  /* gum code writer (released in the dtor) */
    int made_ = 0, max_ = 0;
    uint32_t frame_lo_ = 0, frame_hi_ = 0;
    int frame_is_fullcgo_ = 0;   /* the shared pcsp must match a full-cgo slot (only those get GC-unwound) */
};

#endif /* AGY_CGOTRAMPOLINE_H */
