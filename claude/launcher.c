/* Keep Claude's native entry points while its private Bun and graph stay separate. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int fail(const char *operation) {
    fprintf(stderr, "tasksolver claude: %s: %s\n", operation, strerror(errno));
    return 126;
}

int main(int argc, char **argv) {
    char self[PATH_MAX], bun[PATH_MAX], entry[PATH_MAX];
    ssize_t length = readlink("/proc/self/exe", self, sizeof(self) - 1);
    if (length < 0) return fail("read executable path");
    if ((size_t)length >= sizeof(self) - 1) {
        errno = ENAMETOOLONG;
        return fail("executable path");
    }
    self[length] = '\0';
    char *slash = strrchr(self, '/');
    if (!slash) { errno = EINVAL; return fail("executable directory"); }
    *slash = '\0';
    if (snprintf(bun, sizeof(bun), "%s/bun", self) >= (int)sizeof(bun)
        || snprintf(entry, sizeof(entry), "%s/source/tasksolver-entry.mjs", self) >= (int)sizeof(entry)) {
        errno = ENAMETOOLONG;
        return fail("runtime path");
    }
    const char *name = strrchr(argv[0], '/');
    name = name ? name + 1 : argv[0];
    const char *applets[] = {"rg", "ug", "ugrep", "bfs", "apply-seccomp"};
    for (size_t i = 0; i < sizeof(applets) / sizeof(applets[0]); ++i) {
        if (strcmp(name, applets[i]) == 0) {
            execv(bun, argv);
            return fail("start native helper");
        }
    }
    /* Match the original standalone graph's disabled configuration autoloading. */
    char **command = calloc((size_t)argc + 7, sizeof(*command));
    if (!command) return fail("allocate arguments");
    command[0] = bun;
    command[1] = "--no-env-file";
    command[2] = "--config=/dev/null";
    command[3] = "--tsconfig-override=/dev/null";
    command[4] = "--no-install";
    command[5] = "--zero-fill-buffers";
    command[6] = entry;
    for (int i = 1; i < argc; ++i) command[i + 6] = argv[i];
    execv(bun, command);
    int status = fail("start private Bun");
    free(command);
    return status;
}
