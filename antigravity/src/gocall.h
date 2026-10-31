/* gocall.h — static cgocall-trampoline hooks, per-hook C handlers (`agy_on_*`),
 * and the frame-pointer stack unwinder (`gocall.cpp`).
 *
 * All trampoline stubs (`agy_stub__<id>`) are emitted at build time into
 * `agy_trampolines` (section) (bounded by `agy_tramp_region_start`
 * and `agy_tramp_region_end`). `ld` and `ld-linux-x86-64.so.2` bind both directions
 * at link/load time via `R_X86_64_GLOB_DAT` relocations:
 *   - `libagy.so` statically jumps from each hooked Go function's `entry + skip`
 *     through `agy_tramp_slots[slot]` to `agy_stub__<id>` in `vendor/agy`.
 *   - `agy_stub__<id>` calls its dedicated C handler (`agy_on_*`) via `ld`-bound
 *     `runtime.cgocall` / `runtime.asmcgocall` and jumps back to `agy_go__* + skip + ow`.
 */
#ifndef AGY_GOCALL_H
#define AGY_GOCALL_H

#include <cstdint>
#include "gomod.h"

using agy_book_fn = void (*)(agy_block *);

/* For the C definitions the stub asm (goframe.cpp) names: LTO can't see those references, so
 * `used` keeps them and `externally_visible` keeps their names and ABI. */
#define AGY_ASM_REFERENCED __attribute__((used, externally_visible))

#ifdef __cplusplus
extern "C" {
#endif

extern uint8_t g_agy_enabled;
extern uint8_t g_agy_conv_id;

void agy_on_generic(agy_block *b);
void agy_on_fh_update(agy_block *b);
void agy_on_resp(agy_block *b);
void agy_on_tls_write(agy_block *b);
void agy_on_file_open(agy_block *b);
void agy_on_http_rt(agy_block *b);
void agy_on_exit(agy_block *b);
void agy_on_resp_chunk(agy_block *b);
void agy_on_readlink_filter(agy_block *b);

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

/* Set the real agy binary path (from AGY_PROC_REAL_EXE) the readlink_filter hook returns for
 * os.readlink("/proc/self/exe"). Called once at agy_init (before Go starts). */
void agy_set_real_exe(const char *path);

#ifdef __cplusplus
}
#endif

/* Activate the statically `ld`-bound trampoline stubs once `wire_start()` is ready. */
void agy_enable_hooks(uint64_t base, bool conv_id);

#endif /* AGY_GOCALL_H */
