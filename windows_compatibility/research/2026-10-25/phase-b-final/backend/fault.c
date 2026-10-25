#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int executable(const char *name) {
    char value[4096];
    ssize_t count = readlink("/proc/self/exe", value, sizeof(value) - 1);
    if (count < 0) return 0;
    value[count] = 0;
    char *base = strrchr(value, '/');
    return strcmp(base ? base + 1 : value, name) == 0;
}

static int argument(const char *value) {
    char command[16384];
    int fd = open("/proc/self/cmdline", O_RDONLY);
    ssize_t count = fd < 0 ? 0 : read(fd, command, sizeof(command));
    if (fd >= 0) close(fd);
    return count > 0 && memmem(command, count, value, strlen(value)) != NULL;
}

static void gate(const char *stage) {
    const char *wanted = getenv("PULSARA_TEST_STAGE");
    const char *home = getenv("PULSARA_TEST_HOME");
    if (!wanted || !home || strcmp(wanted, stage) || !argument(home)) return;
    char path[4096], text[256];
    snprintf(path, sizeof(path), "%s/gate-native", home);
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) return;
    int size = snprintf(text, sizeof(text), "%s %d %d\n", stage, getpid(), getppid());
    if (write(fd, text, size) != size) _exit(125);
    close(fd);
    raise(SIGSTOP);
}

__attribute__((constructor)) static void loaded(void) {
    if (executable("postgres")) gate("postgres-exec");
    if (executable("initdb")) gate("initdb-exec");
}

__attribute__((noreturn)) void exit(int status) {
    if (status == 0 && executable("pg_ctl") && argument("stop")) gate("pgctl-stop-tail");
    void (*original)(int) = dlsym(RTLD_NEXT, "exit");
    original(status);
    __builtin_unreachable();
}
