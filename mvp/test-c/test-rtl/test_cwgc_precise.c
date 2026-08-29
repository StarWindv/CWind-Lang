/**
 * 独立测试: GC B 组横向精确化 — 保留率对比 (精确 vs 保守)
 *
 * 构造: 一个不可达 "victim" 容器, 其 data 地址值被伪装成 Int64 元素
 * 残留在存活 Vector 的 items 数组 stale 区 (count 之外)。
 *  - 精确模式: items 槽由 data walker 染黑代管, 只遍历 count 内 cell,
 *    stale 区的假指针不可见 -> victim 被回收。
 *  - 保守模式 (CWGC_CONSERVATIVE=1): items 全量字节扫描, 假指针
 *    命中 -> victim 被误保留 (只泄漏不悬垂)。
 * 两模式回收量的差 = 精确化收益。
 *
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwgc_precise.exe test_cwgc_precise.c
 *       ../../rt-src/rt/cwind_gc.c
 *       ../../rt-src/rt/cwind_container.c
 *       ../../rt-src/rt/cwind_object.c
 *       ../../rt-src/rt/cwind_memcenter.c
 *       ../../rt-src/rt/cwind_builtin.c
 */

#include "../../rt-src/include/rt/cwind_builtin.h"
#include "../../rt-src/include/gc/cwind_gc.h"
#include "../../rt-src/include/object/cwind_container.h"
#include "../../rt-src/include/memory/cwind_memcenter.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

static CWValue_t holder; /* 经全局根注册的存活 Vector */
static uint64_t g_trap;  /* trap 地址存 static (.data 不被栈扫描覆盖) */

/* victim 分配 -> 记录地址 -> 伪造 cell 直写 items 残留区 -> 切断引用。
 * 陷阱值不进 arena (arena 段是根, 会被保守根扫描覆盖), 只存在于
 * items 数组的 stale 槽位: 精确模式 (items 染黑代管, 只遍历 count 内)
 * 不可见; 保守模式 (items 全量字节扫描) 可见。 */
static void make_trap(void) {
    CWValue_t victim;
    memset(&victim, 0, sizeof(victim));
    cwmap_init(&victim, CWInt, CWInt);
    g_trap = victim.address;

    /* 伪造 cell: address 字段 = victim 槽地址; push 后 pop 留在 stale 区 */
    CWValue_t fake;
    fake.address = victim.address;
    fake.length = 8;
    fake.cursor = 0;
    cwvec_push(&holder, &fake);
    cwvec_pop(&holder, NULL); /* count-- ; items[0] 仍持 fake */

    /* 栈上残留也要清: collect 的栈扫描会看到本帧的死字节 */
    memset(&fake, 0, sizeof(fake));
    memset(&victim, 0, sizeof(victim)); /* 切断栈引用 */
}

/* 清擦死栈: 保守扫描会看到深层调用留下的死字节, 产生假保留
 * (Boehm 式保守 GC 的固有代价)。本测试用大栈帧覆盖写零后再 collect,
 * 使两模式的差异只来自 items 数组的遍历策略。 */
static void scrub_stack(void) {
    volatile char pad[128 * 1024];
    memset((void*)pad, 0, sizeof(pad));
}

static size_t run_collect(void) {
    const CWMemCenterStats_t before;
    CWMemCenterStats_t b;
    cwmc_stats(&b);
    const size_t before_allocs = b.active_allocs;
    cwgc_collect();
    CWMemCenterStats_t a;
    cwmc_stats(&a);
    (void)before;
    return before_allocs - a.active_allocs;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWGC precise-vs-conservative retention tests:\n\n");

    printf(" - 精确模式 (默认)\n");
    cwgc_init();
    memset(&holder, 0, sizeof(holder));
    cwvec_init(&holder, CWInt64, 2);
    cwgc_global_register(&holder, sizeof(holder));

    make_trap();
    static uint64_t s_trap1; /* static: .data 不进栈扫描范围 */
    s_trap1 = g_trap;
    scrub_stack();
    /* 多轮收敛: 首轮可能被寄存器/死栈残留假引用延迟 (保守扫描固有代价,
     * 只泄漏不悬垂), 收敛后精确模式必须收回 victim */
    size_t freed_precise = 0;
    for (int i = 0; i < 3; i++) freed_precise += run_collect();

    /* victim 槽必须已被回收: magic 已清, meta 探测不中 */
    CWGCStats_t st;
    cwgc_stats(&st);
    T("precise: victim reclaimed", freed_precise > 0);
    T("precise: trap addr not live",
      cwmc_gc_meta_of((void*)(uintptr_t)s_trap1) == NULL);

    /* holder 仍然完好 */
    T("precise: holder intact", cwvec_size(&holder) == 0);

    printf("\n - 保守模式 (CWGC_CONSERVATIVE=1)\n");
    cwgc_global_unregister(&holder);
    cwvec_destroy(&holder);
    cwgc_shutdown();

#if defined(_WIN32)
    _putenv("CWGC_CONSERVATIVE=1");
#else
    setenv("CWGC_CONSERVATIVE", "1", 1);
#endif
    cwgc_init();
    memset(&holder, 0, sizeof(holder));
    cwvec_init(&holder, CWInt64, 2);
    cwgc_global_register(&holder, sizeof(holder));

    make_trap();
    static uint64_t s_trap2;
    s_trap2 = g_trap;
    scrub_stack();
    size_t freed_conservative = 0;
    for (int i = 0; i < 3; i++) freed_conservative += run_collect();

    T("conservative: fake ptr retains victim",
      freed_conservative == 0);
    T("conservative: victim still live",
      cwmc_gc_meta_of((void*)(uintptr_t)s_trap2) != NULL);
    T("conservative: holder intact", cwvec_size(&holder) == 0);

    printf("\n - 结论\n");
    T("precise reclaims more than conservative",
      freed_precise > 0 && freed_conservative == 0);

    cwgc_shutdown();
    cwmc_shutdown();
    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
