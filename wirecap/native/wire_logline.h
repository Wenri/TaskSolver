/* wire_logline.h — native diagnostics, one write(2) per line.
 *
 * The bridge's worker, the embedded Python it runs and the front-end (agy's host and its Go
 * runtime, Codex, the Kimi addon) all write to fd 2. glibc stages printf output to an
 * unbuffered stream in 128-byte pieces, and a separate fputc('\n') is a write of its own, so a
 * line written that way could come out split around another thread's. A pipe write of at most
 * PIPE_BUF bytes is atomic, so formatting the whole line, newline included, keeps lines whole.
 */
#ifndef WIRE_LOGLINE_H
#define WIRE_LOGLINE_H

#include <cerrno>
#include <cstdarg>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <unistd.h>

__attribute__((format(printf, 2, 3)))
static inline void wire_logline(int fd, const char *fmt, ...)
{
    char small[512];
    va_list ap;
    va_start(ap, fmt);
    int n = std::vsnprintf(small, sizeof small, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    char *line = small;
    size_t len = (size_t)n;
    if (len + 1 >= sizeof small) {   /* no room for the newline */
        line = static_cast<char *>(std::malloc(len + 2));
        if (line) {
            va_start(ap, fmt);
            std::vsnprintf(line, len + 1, fmt, ap);
            va_end(ap);
        } else {
            line = small;
            len = sizeof small - 2;   /* truncated */
        }
    }
    line[len++] = '\n';
    for (size_t off = 0; off < len;) {
        ssize_t w = write(fd, line + off, len - off);
        if (w > 0) off += (size_t)w;
        else if (w < 0 && errno == EINTR) continue;
        else break;
    }
    if (line != small) std::free(line);
}

#endif
