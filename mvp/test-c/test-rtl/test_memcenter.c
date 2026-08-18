/**
 * 独立测试: 统一内存中心 cwind_memcenter
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_memcenter.exe test_memcenter.c ../../rt-src/rt/cwind_memcenter.c
 */

#include "../../rt-src/include/memory/cwind_memcenter.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

static int all_bytes(const void* p, size_t n, int byte) {
    const unsigned char* c = (const unsigned char*)p;
    for (size_t i = 0; i < n; i++) {
        if (c[i] != (unsigned char)byte) return 0;
    }
    return 1;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWMemCenter tests:\n\n");
    CWMemCenterStats_t s;

    printf(" - init / alloc / free\n");
    cwmc_init();
    T("init -> zero stats",
      cwmc_stats(&s) && s.active_allocs == 0 && s.blocks == 0
      && s.mapped_bytes == 0 && s.used_bytes == 0);
    T("alloc(0) == NULL", cwmc_alloc(0) == NULL);

    void* p = cwmc_alloc(32);
    T("alloc(32) != NULL", p != NULL);
    T("usable_size(32) == 32", cwmc_usable_size(p) == 32);
    T("payload 16-aligned", ((uintptr_t)p & 15) == 0);
    T("stats: 1 active / 32 used",
      cwmc_stats(&s) && s.active_allocs == 1 && s.used_bytes == 32);

    cwmc_free(p);
    T("free -> 0 active / 0 used",
      cwmc_stats(&s) && s.active_allocs == 0 && s.used_bytes == 0);
    cwmc_free(p); /* double free: slab magic 已被清除, 应静默忽略 */
    T("double free ignored + error counted",
      cwmc_stats(&s) && s.errors == 1);

    printf("\n - alignment\n");
    for (size_t a = 1; a <= 16; a <<= 1) {
        void* q = cwmc_alloc_aligned(17, a);
        T("alloc_aligned(17) honors requested align",
          q != NULL && ((uintptr_t)q & (a - 1)) == 0);
        cwmc_free(q);
    }
    T("align(32) rejected", cwmc_alloc_aligned(8, 32) == NULL);
    T("align(3) rejected (non-pow2)", cwmc_alloc_aligned(8, 3) == NULL);

    printf("\n - calloc\n");
    p = cwmc_calloc(64);
    T("calloc zeroed", p != NULL && all_bytes(p, 64, 0));
    cwmc_free(p);

    printf("\n - slab reuse / stress\n");
    enum { N = 10000 };
    void* ptrs[N];
    size_t live = 0;
    for (int i = 0; i < N; i++) {
        size_t sz = (size_t)((i * 37) % 1000) + 1;
        void* q = cwmc_alloc(sz);
        if (!q) { printf("  [FAIL] stress alloc %d\n", i); fail++; break; }
        ptrs[i] = q;
        memset(q, (int)(i & 0xff), sz);
        live++;
    }
    T("stress: all allocated", live == N);
    T("stress: active == N", cwmc_stats(&s) && s.active_allocs == N);
    T("stress: blocks within bound", cwmc_stats(&s) && s.blocks < 256);

    int intact = 1;
    for (int i = 0; i < N && intact; i++) {
        size_t sz = (size_t)((i * 37) % 1000) + 1;
        intact = all_bytes(ptrs[i], sz, (int)(i & 0xff));
    }
    T("stress: all contents intact", intact);

    for (int i = 0; i < N; i += 2) cwmc_free(ptrs[i]);
    T("free half -> active == N/2",
      cwmc_stats(&s) && s.active_allocs == N / 2);
    for (int i = 1; i < N; i += 2) cwmc_free(ptrs[i]);
    T("free rest -> 0 active", cwmc_stats(&s) && s.active_allocs == 0);

    printf("\n - realloc\n");
    p = cwmc_alloc(16);
    memset(p, 0xAB, 16);
    void* q = cwmc_realloc(p, 100);
    T("realloc grow != NULL", q != NULL);
    T("realloc preserves data",
      q != NULL && all_bytes(q, 16, 0xAB));
    T("realloc shrink keeps data",
      q != NULL && cwmc_realloc(q, 8) == q && all_bytes(q, 8, 0xAB));
    T("realloc within class returns same ptr",
      cwmc_usable_size(q) == 8 && cwmc_realloc(q, 12) == q);
    cwmc_free(q);

    p = cwmc_realloc(NULL, 64);
    T("realloc(NULL, n) == alloc", p != NULL && cwmc_usable_size(p) == 64);
    cwmc_free(p);
    p = cwmc_alloc(8);
    T("realloc(p, 0) == NULL", cwmc_realloc(p, 0) == NULL);

    printf("\n - dedicated (large)\n");
    p = cwmc_alloc(8192);
    T("dedicated alloc != NULL", p != NULL);
    T("dedicated 16-aligned", p != NULL && ((uintptr_t)p & 15) == 0);
    memset(p, 0x5C, 8192);
    T("dedicated usable_size", cwmc_usable_size(p) == 8192);

    q = cwmc_realloc(p, 20000);
    T("dedicated grow != NULL", q != NULL);
    T("dedicated grow preserves data",
      q != NULL && all_bytes(q, 8192, 0x5C));
    cwmc_free(q);
    T("dedicated freed -> active == 0",
      cwmc_stats(&s) && s.active_allocs == 0);

    printf("\n - shutdown\n");
    cwmc_shutdown();
    T("shutdown resets stats",
      cwmc_stats(&s) && s.blocks == 0 && s.active_allocs == 0
      && s.mapped_bytes == 0 && s.used_bytes == 0);
    cwmc_shutdown();
    T("double shutdown idempotent",
      cwmc_stats(&s) && s.active_allocs == 0 && s.blocks == 0);

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
