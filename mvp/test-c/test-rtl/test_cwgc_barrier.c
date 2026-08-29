/**
 * 独立测试: GC 增量 MARK 期间的屏障边界 (todo-35 阶段 0-C)
 *
 * 声音性关键路径 (见 handover.gc-impl-status §5/§7):
 *   1. 写屏障   — MARK 期间被写入存活容器的值必须保活 (F6 场景的
 *                 "指向已建立容器"一侧);
 *   2. 分配屏障 — MARK 期间 cwmc_alloc 的新槽立即染灰, 未经 FINISH
 *                 根重扫也不得被本轮 sweep 误回收;
 *   3. 增量交错 — MARK 状态挂起时持续分配/写容器, 状态机按预算推进;
 *   4. 屏障保活的对象不是永生的: 引用切断后下一轮照常回收。
 *
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwgc_barrier.exe test_cwgc_barrier.c
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

static size_t live_allocs(void) {
    CWMemCenterStats_t ms;
    cwmc_stats(&ms);
    return ms.active_allocs;
}

static void scrub(void) {
    volatile char pad[64 * 1024];
    memset((void*)pad, 0, sizeof(pad));
}

/* 标量 cell: 值拷进 arena 单元再 wrap (与 codegen cg_value_cell 同构) */
static void wrap_i16(CWValue_t* out, int v) {
    int16_t* unit = (int16_t*)cwrt_arena_alloc(sizeof(int16_t));
    *unit = (int16_t)v;
    cwval_wrap(out, unit, sizeof(int16_t));
}

/* 驱动状态机进入 MARK: 阈值调到 1, 分配累积 pending, step 触发 begin。
 * MARK 期间 budget = step_bytes/8 = 0, 灰节点不推进, 状态保持 MARK。 */
static void enter_mark(const char* label) {
    cwgc_set_step_bytes(1);
    void* p = cwmc_alloc(64);
    (void)p;
    cwgc_step();
    T(label, cwgc_state() == CWGC_MARK);
}

static void finish_cycle(void) {
    cwgc_set_step_bytes(64 * 1024);
    cwgc_collect();
    T("cycle finished -> IDLE", cwgc_state() == CWGC_IDLE);
}

/* MARK 期间向已存活容器写入的值 */
static CWValue_t holder;    /* 全局根: Vector<Vector<Int16>> */
static CWValue_t late_map;  /* MARK 期间新建的 Map */

static void test_write_barrier(void) {
    printf("\n - MARK 期间写屏障 (已存活容器收到新元素)\n");
    memset(&holder, 0, sizeof(holder));
    cwvec_init(&holder, CWVector, 4);
    cwgc_global_register(&holder, sizeof(holder));

    /* MARK 前的基线元素 */
    CWValue_t base;
    memset(&base, 0, sizeof(base));
    cwvec_init(&base, CWInt16, 2);
    CWValue_t cell;
    wrap_i16(&cell, 7);
    cwvec_push(&base, &cell);
    cwvec_push(&holder, &base);
    memset(&base, 0, sizeof(base));

    enter_mark("state MARK (write barrier)");

    /* MARK 期间: 内层 Vector 新分配 (分配屏障) + push 进 holder (写屏障) */
    CWValue_t during;
    memset(&during, 0, sizeof(during));
    cwvec_init(&during, CWInt16, 2);
    wrap_i16(&cell, 42);
    cwvec_push(&during, &cell);
    cwvec_push(&holder, &during);
    memset(&during, 0, sizeof(during));

    finish_cycle();

    /* 屏障保活: 两个内层容器都完好 */
    CWValue_t out0, out1;
    T("holder size 2", cwvec_size(&holder) == 2);
    T("baseline element intact",
      cwvec_at(&holder, 0, &out0) && cwvec_size(&out0) == 1);
    T("mid-mark element survives",
      cwvec_at(&holder, 1, &out1) && cwvec_size(&out1) == 1);
    if (cwvec_at(&holder, 1, &out1)) {
        CWValue_t v;
        T("mid-mark scalar readable",
          cwvec_at(&out1, 0, &v) && v.length == 2
          && *(const int16_t*)(uintptr_t)v.address == 42);
    }

    /* 切断后下一轮照常回收 (屏障保活 != 永生) */
    cwgc_global_unregister(&holder);
    memset(&holder, 0, sizeof(holder));
    scrub();
    const size_t b = live_allocs();
    for (int i = 0; i < 4; i++) {
        scrub();
        cwgc_collect();
    }
    T("barrier-kept garbage reclaimed", live_allocs() < b);
}

static void test_alloc_barrier(void) {
    printf("\n - MARK 期间分配屏障 (新容器未挂根前不得误回收)\n");
    enter_mark("state MARK (alloc barrier)");

    /* MARK 期间新建 Map 并写入: 分配屏障染灰 data/节点。
     * value 字节存 arena 单元 (与 stress 混合负载同构, 栈缓冲复用会
     * 让所有值指向同一失效地址)。 */
    memset(&late_map, 0, sizeof(late_map));
    cwmap_init(&late_map, CWInt, CWString);
    for (int i = 0; i < 8; i++) {
        CWValue_t k, v;
        wrap_i16(&k, i + 1);
        char* s = (char*)cwrt_arena_alloc(16);
        snprintf(s, 16, "late%d", i);
        cwval_wrap(&v, s, strlen(s));
        T("late put ok", cwmap_put(&late_map, &k, &v));
    }
    cwgc_global_register(&late_map, sizeof(late_map));

    finish_cycle();

    /* FINISH 根重扫 + sweep 后, MARK 期间的新容器完好 */
    T("late map size 8", cwmap_size(&late_map) == 8);
    CWValue_t probe, out;
    wrap_i16(&probe, 5);
    memset(&out, 0, sizeof(out));
    if (!cwmap_get(&late_map, &probe, &out)) {
        printf("    [dbg] get(5) returned FALSE\n");
    } else if (out.length != 5
               || memcmp((const void*)(uintptr_t)out.address, "late4", 5)
                      != 0) {
        printf("    [dbg] get(5) len=%llu bytes=%.5s addr=%#llx\n",
               (unsigned long long)out.length,
               out.address ? (const char*)(uintptr_t)out.address : "?",
               (unsigned long long)out.address);
    }
    T("late map intact",
      cwmap_get(&late_map, &probe, &out) && out.length == 5
      && memcmp((const void*)(uintptr_t)out.address, "late4", 5) == 0);

    cwgc_global_unregister(&late_map);
    memset(&late_map, 0, sizeof(late_map));
    scrub();
    const size_t b = live_allocs();
    for (int i = 0; i < 4; i++) {
        scrub();
        cwgc_collect();
    }
    T("late map reclaimed after cut", live_allocs() < b);
}

/* MARK 状态挂起时持续分配: 状态机不推进 (budget=0), 分配路径畅通 */
static void test_mark_interleave(void) {
    printf("\n - MARK 挂起时持续分配 (增量交错)\n");
    enter_mark("state MARK (interleave)");
    for (int r = 0; r < 64; r++) {
        CWValue_t v;
        memset(&v, 0, sizeof(v));
        cwvec_init(&v, CWInt16, 2);
        CWValue_t c;
        wrap_i16(&c, r);
        cwvec_push(&v, &c);
        memset(&v, 0, sizeof(v));
    }
    T("allocations proceed in MARK", cwgc_state() == CWGC_MARK);
    finish_cycle();
    scrub();
    const size_t b = live_allocs();
    for (int i = 0; i < 3; i++) {
        scrub();
        cwgc_collect();
    }
    T("interleave garbage reclaimed", live_allocs() <= b);
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWGC barrier boundary tests:\n");

    cwgc_init();
    test_write_barrier();
    test_alloc_barrier();
    test_mark_interleave();

    CWGCStats_t st;
    cwgc_stats(&st);
    printf("\n - 统计汇总\n");
    printf("  cycles=%zu steps=%zu marked=%zu swept=%zu reclaimed=%zu\n",
           st.cycles, st.steps, st.nodes_marked, st.slots_swept,
           st.bytes_reclaimed);
    T("aggregate cycles ran", st.cycles > 0);
    T("aggregate reclaimed", st.bytes_reclaimed > 0);

    cwgc_shutdown();
    cwmc_shutdown();

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
