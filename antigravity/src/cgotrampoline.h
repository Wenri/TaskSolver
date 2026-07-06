/* cgotrampoline.h — public API of the cgocall-trampoline installer + the frame-pointer
 * stack unwinder (cgotrampoline.cpp). The GC-safety machinery it depends on — the synthetic
 * runtime.moduledata and the Go runtime struct mirrors — lives in gomod.h. */
#ifndef AGY_CGOTRAMPOLINE_H
#define AGY_CGOTRAMPOLINE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- frame-pointer stack unwinder --------------------------------------------
 * Fault-safe read of `n` bytes from a possibly-bogus address in our own process
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

/* ---- cgocall-trampoline installer (uses frida-gum) ---------------------------
 * A streaming builder so the caller need not stage the targets in an intermediate
 * array — it resolves + filters its own hook table and calls add() per target:
 *
 *   agy_gohook *h = agy_gohook_begin(base, cgocall_va, asmcgocall_va, max_targets);
 *   for each trampoline hook: agy_gohook_add(h, entry, skip, kind, asmcgo);
 *   int made = agy_gohook_finalize(h, md_vaddr);
 *
 * add() redirects (base+entry+skip) to a generated trampoline that marshals the
 * Go-ABI arg registers and CALLs runtime.cgocall(agy_cgo_hook, &block) — or
 * runtime.asmcgocall when asmcgo is set (g0 switch, NO syscall transition) — then
 * resumes the original body. finalize() builds the covering synthetic moduledata
 * (agy_gomod_prepare) so the trampolines are GC-unwind-safe, and returns the count
 * installed. Taking resolved (entry, skip) keeps this module symbol-map- and
 * hook-table-agnostic. begin() hard-fails (NULL) if cgocall/asmcgocall is unresolved;
 * asmcgocall is REQUIRED (assumed resolved — build_symbols.py always resolves it),
 * no silent cgocall fallback. add()/finalize() tolerate a NULL handle. */
typedef struct agy_gohook agy_gohook;
agy_gohook *agy_gohook_begin(uint64_t base, uint64_t cgocall_va, uint64_t asmcgocall_va,
                             int max_targets);
void        agy_gohook_add(agy_gohook *h, uint64_t entry, uint32_t skip,
                           const char *kind, int asmcgo);
int         agy_gohook_finalize(agy_gohook *h, uint64_t md_vaddr);

#ifdef __cplusplus
}
#endif

#endif /* AGY_CGOTRAMPOLINE_H */
