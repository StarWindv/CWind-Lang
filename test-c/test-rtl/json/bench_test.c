/**
 * 最好成绩:
 * input size     : 1.53 MiB
 * parse          : 14.1 ms, 108 MiB/s
 * lookup         : 2.3 ms (18 M lookups/s)
 * dump compact   : 12.3 ms, 123 MiB/s (1585721 bytes)
 * dump pretty(4) : 15.1 ms, 221 MiB/s (3485722 bytes)
 * sax validate   : 9.2 ms, 167 MiB/s (rc=0, values=1)
 */

#define CW_JSON_IMPLEMENTATION
#if defined(__clang__) || defined(__GNUC__) || defined(_MSC_VER)
    #include "../../../rt-src/include/stl/json/cwind_json.h"
#else
    #include "cwind_json.h"
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32)
#include <windows.h>
static double now_sec(void) {
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)f.QuadPart;
}
#else
#include <time.h>
static double now_sec(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}
#endif

int main(void) {
    enum { N = 20000 };
    size_t cap = (size_t)N * 96 + 16;
    char *buf = (char *)malloc(cap);
    if (!buf) return 1;
    size_t pos = 0;
#define BAPPEND(fmt, ...)                                                     \
    do {                                                                      \
        int n = snprintf(buf + pos, cap - pos, fmt, __VA_ARGS__);             \
        pos += (size_t)n;                                                     \
    } while (0)
#define BAPPENDS(s)                                                           \
    do {                                                                      \
        size_t n = strlen(s);                                                 \
        memcpy(buf + pos, s, n);                                              \
        pos += n;                                                             \
    } while (0)
    BAPPENDS("[");
    for (int i = 0; i < N; ++i) {
        if (i) BAPPENDS(",");
        BAPPEND("{\"id\":%d,\"name\":\"user%d\",\"active\":%s,"
                "\"score\":%.4f,\"tags\":[\"a\",\"b\"]}",
                i, i, (i & 1) ? "true" : "false", (double)i / 7.0);
    }
    BAPPENDS("]");
#undef BAPPEND
#undef BAPPENDS

    double t0 = now_sec();
    cw_doc *d = cw_parse(buf, pos);
    double t1 = now_sec();
    if (!d) {
        printf("parse failed\n");
        free(buf);
        return 1;
    }
    double mb = (double)pos / (1024.0 * 1024.0);
    printf("input size     : %.2f MiB\n", mb);
    printf("parse          : %.1f ms, %.0f MiB/s\n", (t1 - t0) * 1e3,
           mb / (t1 - t0));

    /* 2M hash lookups */
    cw_value *arr = cw_doc_root(d);
    volatile int64_t sink = 0;
    t0 = now_sec();
    for (int i = 0; i < 2 * N; ++i) {
        cw_value *o = cw_array_get(arr, (size_t)(i % N));
        cw_value *id = cw_object_get(o, "id");
        int64_t iv = 0;
        if (cw_as_int(id, &iv) == CW_OK) sink += iv;
        (void)cw_object_get(o, "name");
    }
    t1 = now_sec();
    printf("lookup         : %.1f ms (%.0f M lookups/s)\n", (t1 - t0) * 1e3,
           (2.0 * N) / (t1 - t0) / 1e6);

    /* compact dump */
    size_t outlen = 0;
    t0 = now_sec();
    char *out = cw_dump_malloc(arr, 0, &outlen);
    t1 = now_sec();
    if (!out) {
        printf("dump failed\n");
        return 1;
    }
    double omb = (double)outlen / (1024.0 * 1024.0);
    printf("dump compact   : %.1f ms, %.0f MiB/s (%zu bytes)\n",
           (t1 - t0) * 1e3, omb / (t1 - t0), outlen);

    /* pretty dump with 4-space indent */
    t0 = now_sec();
    size_t plen = 0;
    char *pretty = cw_dump_malloc(arr, 4, &plen);
    t1 = now_sec();
    if (!pretty) return 1;
    double pmb = (double)plen / (1024.0 * 1024.0);
    printf("dump pretty(4) : %.1f ms, %.0f MiB/s (%zu bytes)\n",
           (t1 - t0) * 1e3, pmb / (t1 - t0), plen);

    /* SAX validation throughput (64 KiB chunks, no DOM built) */
    t0 = now_sec();
    cw_sax *v = cw_sax_new(NULL, NULL);
    size_t i = 0;
    int rc = CW_OK;
    while (i < pos) {
        size_t n = pos - i > 65536 ? 65536 : pos - i;
        rc = cw_sax_feed(v, buf + i, n);
        if (rc < 0) break;
        i += n;
    }
    if (rc >= 0) rc = cw_sax_finish(v);
    t1 = now_sec();
    printf("sax validate    : %.1f ms, %.0f MiB/s (rc=%d, values=%d)\n",
           (t1 - t0) * 1e3, mb / (t1 - t0), rc, cw_sax_values(v));

    cw_sax_free(v);
    free(pretty);
    free(out);
    cw_doc_free(d);
    free(buf);
    return 0;
}
