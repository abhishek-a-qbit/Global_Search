#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
static const char *fix(const char *p) { return (p && strcmp(p, "/proc/self/io") == 0) ? "/etc/fake-proc-io" : p; }
FILE *fopen(const char *p, const char *m) { static FILE *(*real)(const char *, const char *); if (!real) real = dlsym(RTLD_NEXT, "fopen"); return real(fix(p), m); }
FILE *fopen64(const char *p, const char *m) { static FILE *(*real)(const char *, const char *); if (!real) real = dlsym(RTLD_NEXT, "fopen64"); return real(fix(p), m); }
