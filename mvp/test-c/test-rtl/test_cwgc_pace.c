/**
 * 独立测试: GC 触发阈值 pacing (bug-82 回归)
 *
 * 验收标准: 极大量存活堆分配下, 触发阈值必须随存活面增长, 否则
 * 「固定 64KiB 触发」会以 O(存活面) 的单轮成本反复触发, 总成本随
 * 分配量退化为 O(n²) (bug-82: 500 万次堆分配出现异常长卡顿)。
 *
 * 覆盖:
 *   1. 默认基线     — 无存活时阈值 = step 基线 (64 KiB);
 *   2. 存活增长     — 16 MiB arena 段分配后阈值上调, 轮数保持对数级;
 *   3. 显式阈值语义 — cwgc_set_step_bytes / CWGC_STEP_BYTES 固定阈值
 *                     (测试与调参依赖, 不被 pacing 覆盖)。
 *
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwgc_pace.exe test_cwgc_pace.c
 *       ../../rt-src/rt/cwind_gc.c
 *       ../../rt-src/rt/cwind_container.c
 *       ../../rt-src/rt/cwind_object.c
 *       ../../rt-src/rt/cwind_memcenter.c
 *       ../../rt-src/rt/cwind_builtin.c
 */

#include "../../rt-src/include/rt/cwind_builtin.h"
#include "../../rt-src/include/gc/cwind_gc.h"
#include "../../rt-src/include/memory/cwind_memcenter.h"

#include <stdint.h>
#include <stdio.h>

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

/* 每段一块: 60 KiB 负载 > 64 KiB 段的剩余空间, 保证一调用一段 */
#define PACE_CELL_BYTES (60 * 1024)
#define PACE_TARGET_BYTES ((size_t)16 * 1024 * 1024)

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWGC pacing tests (bug-82):\n");

    cwgc_init();
    T("base step default", cwgc_step_bytes() == 64 * 1024);
    T("adaptive trigger starts at base",
      cwgc_trigger_bytes() == cwgc_step_bytes());

    printf("\n - 存活堆增长 (16 MiB arena 段)\n");
    CWGCStats_t before;
    cwgc_stats(&before);
    size_t allocated = 0;
    while (allocated < PACE_TARGET_BYTES) {
        void* cell = cwrt_arena_alloc(PACE_CELL_BYTES);
        if (!cell) break;
        *(volatile char*)cell = (char)(allocated >> 10);
        allocated += PACE_CELL_BYTES;
    }
    CWGCStats_t after;
    cwgc_stats(&after);
    const size_t cycles = after.cycles - before.cycles;

    T("arena target allocated", allocated >= PACE_TARGET_BYTES);
    T("trigger grew with live heap",
      cwgc_trigger_bytes() > cwgc_step_bytes());
    T("live accounting tracks arena", after.live_bytes > 0);
    /* 固定阈值下 16 MiB / 64 KiB = 256 轮; pacing 后轮数对数级。
     * 上界放宽仅防平台计数噪声, 与 256 仍有量级差。 */
    printf("  [dbg] cycles=%zu trigger=%zu live=%zu\n",
           cycles, cwgc_trigger_bytes(), after.live_bytes);
    T("cycles bounded (pacing amortizes)", cycles > 0 && cycles < 64);

    printf("\n - 显式阈值维持固定语义\n");
    cwgc_set_step_bytes(8 * 1024);
    T("explicit step pins trigger", cwgc_trigger_bytes() == 8 * 1024);
    T("step query returns explicit value", cwgc_step_bytes() == 8 * 1024);

    cwgc_shutdown();
    cwmc_shutdown();

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
