/* gocall.h — the C side of the stubs: per-row handlers, the handler table, the unwinder. */
#ifndef AGY_GOCALL_H
#define AGY_GOCALL_H

#include <cstdint>
#include "gomod.h"

using agy_book_fn = void (*)(agy_block *);

/* Named by the stub asm; nullptr disables a row. */
extern "C" agy_book_fn g_agy_on[];

void agy_on_generic(agy_block *b);
void agy_on_fh_update(agy_block *b);
void agy_on_stream(agy_block *b);
void agy_on_file_open(agy_block *b);
void agy_on_http_rt(agy_block *b);
void agy_on_exit(agy_block *b);
void agy_on_resp_chunk(agy_block *b);
void agy_on_system_message(agy_block *b);
void agy_on_readlink_filter(agy_block *b);

/* Returns -1 instead of faulting on a bad address. */
long agy_safe_read(uint64_t addr, void *dst, unsigned long n);

/* Return PCs along Go's frame-pointer chain, as link vaddrs (pc - base). */
int  agy_backtrace(uint64_t rbp, uint64_t base, uint64_t *out, int max);

/* A "callstack" event: src_kind, NUL, then the u64 frames. */
void agy_emit_stack(const char *src_kind, uint64_t rbp, uint64_t base);

/* What readlink_filter returns for /proc/self/exe (AGY_PROC_REAL_EXE). */
void agy_set_real_exe(const char *path);

/* Fill g_agy_on once wire_start() succeeds, before Go starts. */
void agy_enable_books(uint64_t base, bool conv_id);

#endif /* AGY_GOCALL_H */
