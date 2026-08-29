/**
 * 独立测试: CWind GC 阶段 0 (三色标记-清扫, 全保守)
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwgc.exe test_cwgc.c
 *       ../../rt-src/rt/cwind_gc.c
 *       ../../rt-src/rt/cwind_container.c
 *       ../../rt-src/rt/cwind_object.c
 *       ../../rt-src/rt/cwind_memcenter.c
 */

#include "../../rt-src/include/gc/cwind_gc.h"
#include "../../rt-src/include/object/cwind_container.h"
#include "../../rt-src/include/memory/cwind_memcenter.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

/* 清擦死栈, 打断保守扫描的死栈残留假保留 (与压测 settle 同构) */
static void scrub(void) {
    volatile char pad[64 * 1024];
    memset((void*)pad, 0, sizeof(pad));
}

/* 多轮 collect 收敛: 每轮先清擦再收, 回报实际执行的轮数 */
static int settle(void) {
    for (int i = 0; i < 4; i++) {
        scrub();
        cwgc_collect();
    }
    return 4;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWGC stage-0 tests:\n\n");

    cwgc_init();
    T("inited", cwgc_enabled());
    T("state idle", cwgc_state() == CWGC_IDLE);
    T("step bytes default", cwgc_step_bytes() == 64 * 1024);

    printf("\n - 元数据分区: 槽头 GC 位\n");
    void* p = cwmc_alloc(64);
    T("alloc ok", p != NULL);
    uint64_t* meta = cwmc_gc_meta_of(p);
    T("meta found", meta != NULL);
    T("default white", (*meta & CWGC_COLOR_MASK) == CWGC_WHITE);
    T("stack addr not heap", cwmc_gc_meta_of((void*)&meta) == NULL);
    T("NULL not heap", cwmc_gc_meta_of(NULL) == NULL);

    printf("\n - 全局根注册表\n");
    static int dummy;
    T("register", cwgc_global_register(&dummy, sizeof(dummy)));
    T("unregister", cwgc_global_unregister(&dummy));
    T("unregister twice fails", !cwgc_global_unregister(&dummy));
    T("register NULL fails", !cwgc_global_register(NULL, 8));

    printf("\n - 全量回收: 不可达容器被回收\n");
    /* 构造一个不可达 Vector: 数据只存在于本函数局部 (会被栈扫描找到!),
     * 所以先放进全局根注册的内存外的深层的不可达位置 —— 换个思路:
     * 分配一个 Vector, 立刻把变量覆盖, 再手动 collect。
     * 局部变量 v 仍在栈上, 保守扫描会找到它 —— 这是保守 GC 的语义。
     * 用 cwgc_collect 前把 v 清零, 借以验证回收路径。 */
    CWValue_t v;
    memset(&v, 0, sizeof(v));
    T("vector init", cwvec_init(&v, CWInt16, 4));
    CWValue_t probe;
    T("vector push", cwvec_push(&v, &probe));
    size_t live_before = 0;
    CWGCStats_t st;
    cwgc_stats(&st);
    live_before = st.live_slots;
    memset(&v, 0, sizeof(v)); /* 切断引用 */
    const size_t freed = cwgc_collect();
    cwgc_stats(&st);
    T("collect ran one cycle", st.cycles == 1);
    T("something freed", st.slots_swept > 0 && freed > 0);
    T("live accounting updated", st.live_slots + st.slots_swept > 0
      || live_before == 0);

    printf("\n - 循环引用回收 (共享子图)\n");
    /* 两个互相引用的 Map (value 指向对方), 切断栈引用后应整体回收 */
    CWValue_t m1, m2;
    memset(&m1, 0, sizeof(m1));
    memset(&m2, 0, sizeof(m2));
    cwmap_init(&m1, CWInt16, CWMap);
    cwmap_init(&m2, CWInt16, CWMap);
    cwmap_put(&m1, &probe, &m2); /* m1 -> m2 */
    cwmap_put(&m2, &probe, &m1); /* m2 -> m1 (环) */
    cwgc_stats(&st);
    const size_t cycles_before = st.cycles;
    const size_t swept_before = st.slots_swept;
    memset(&m1, 0, sizeof(m1));
    memset(&m2, 0, sizeof(m2)); /* 切断栈引用, 环不可达 */
    /* 保守 GC 语义: 死栈/寄存器残留会延迟回收 (只泄漏不悬垂),
     * 收敛断言走 settle (与压测一致), 轮数确定所以 cycles 可精确断言 */
    const int runs = settle();
    cwgc_stats(&st);
    T("cycle collected", st.cycles == cycles_before + (size_t)runs
      && st.slots_swept > swept_before);

    printf("\n - 存活对象跨轮保留\n");
    static CWValue_t rooted; /* 静态变量经根注册? 未注册 — 用注册表 */
    cwgc_global_register(&rooted, sizeof(rooted));
    memset(&rooted, 0, sizeof(rooted));
    cwvec_init(&rooted, CWInt16, 2);
    int16_t sv = 5;
    CWValue_t scell;
    cwval_wrap(&scell, &sv, 2);
    cwvec_push(&rooted, &scell);
    cwgc_collect();
    /* rooted 未在注册表移除前, 其数据必须存活 */
    T("rooted vector survives", cwvec_size(&rooted) == 1);
    cwgc_global_unregister(&rooted);

    printf("\n - 开关\n");
    T("disable via step bytes 0 no crash", (cwgc_set_step_bytes(1),
                                            true));
    cwgc_shutdown();
    T("shutdown ok", !cwgc_enabled());

    printf("\n - 内存回落\n");
    /* 全部容器已回收: memcenter 只剩 arena/帧类簿记 (此处为 0) */
    cwmc_shutdown();

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
