/* gocall.h — the C side of the trampoline stubs (src/goframe.cpp): the per-row handlers
 * (`agy_on_*`), the per-row gate, and the frame-pointer stack unwinder (`gocall.cpp`).
 */
#ifndef AGY_GOCALL_H
#define AGY_GOCALL_H

#include <cstdint>
#include "gomod.h"

using agy_book_fn = void (*)(agy_block *);

#ifdef __cplusplus
extern "C" {
#endif

extern agy_book_fn g_agy_on[];         /* per BOOKS row: handler, or nullptr when disabled */

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
