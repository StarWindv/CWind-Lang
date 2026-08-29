/**
 * 独立测试: GC 压测验收 (todo-35 阶段 0)
 *
 * 验收标准 (gc-analysis §压测基准): 随便怎么分配、怎么丢,
 * 内存曲线都回落, 程序不崩。覆盖:
 *   1. 循环引用   — 大环 Map/节点互相引用, 切断后回收;
 *   2. 共享子图   — 单个容器被多处引用, 只回收一次, 存活期正确;
 *   3. 批量丢弃重建 — churn: 反复建/丢, 内存回落不增长;
 *   4. 长存活+短存活混合 — 服务端负载形态, 长存活索引保持正确;
 *   5. 单步延迟   — 增量 step 单次节点预算受控。
 *
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwgc_stress.exe test_cwgc_stress.c
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

/* 清擦死栈, 降低假保留噪声 */
static void scrub(void) {
    volatile char pad[64 * 1024];
    memset((void*)pad, 0, sizeof(pad));
}

/* 多轮 collect 收敛 (容忍寄存器/死栈残留的假引用延迟)。
 * 每轮之间清擦本帧以下的死栈: 上一轮 GC 自身遍历帧 (walker 持有的
 * 节点指针) 会被下一轮栈扫描看到而自持假保留 —— 先擦后收, 打断该链。 */
static void settle(void) {
    for (int i = 0; i < 4; i++) {
        scrub();
        cwgc_collect();
    }
    scrub();
}

/* 标量 cell: 值拷进 arena 单元再 wrap (与 codegen cg_value_cell 同构)。
 * 假地址键 (cwval_wrap(out, (void*)(uintptr_t)i, 0)) 会被
 * cwobj_value_equal 的 memcmp 解引用 —— 压测必须走真实字节。 */
static void wrap_i16(CWValue_t* out, int v) {
    int16_t* unit = (int16_t*)cwrt_arena_alloc(sizeof(int16_t));
    *unit = (int16_t)v;
    cwval_wrap(out, unit, sizeof(int16_t));
}

/* ---- 1. 循环引用: N 节点环 ---- */

static CWCell_t ring_nodes[64];

static void build_ring(size_t n) {
    for (size_t i = 0; i < n; i++) {
        memset(&ring_nodes[i], 0, sizeof(ring_nodes[i]));
        cwmap_init(&ring_nodes[i].value, CWInt, CWMap);
        cwgc_set_desc((const void*)(uintptr_t)ring_nodes[i].value.address,
                      CWGC_DESC_MAP_DATA);
    }
    /* 环: node[i].value 指向 node[(i+1)%n] */
    for (size_t i = 0; i < n; i++) {
        CWValue_t key;
        wrap_i16(&key, (int)i + 1);
        cwmap_put(&ring_nodes[i].value, &key, &ring_nodes[(i + 1) % n].value);
    }
}

static void test_ring(void) {
    printf("\n - 循环引用 (64 节点环)\n");
    /* 静态数组不在栈上, 必须 显式注册为 GC 根 */
    cwgc_global_register(ring_nodes, sizeof(ring_nodes));
    scrub();
    settle();
    const size_t base = live_allocs();
    const size_t base_arena = cwrt_arena_blocks();
    build_ring(64);
    const size_t with_ring = live_allocs();
    T("ring allocated", with_ring >= base + 64);

    /* 环经静态数组存活: 收敛后不得回收 */
    scrub();
    settle();
    T("ring alive while referenced", live_allocs() == with_ring);

    /* 切断: 注销根 + 清空数组槽 -> 整环不可达 -> 收敛后回收
     * (真实键值存 arena 单元, arena 段进程期存活: 断言扣除其增量) */
    cwgc_global_unregister(ring_nodes);
    memset(ring_nodes, 0, sizeof(ring_nodes));
    scrub();
    settle();
    const size_t after = live_allocs();
    const size_t arena_delta = cwrt_arena_blocks() - base_arena;
    T("ring collected after cut", after <= base + arena_delta);
}

/* ---- 2. 共享子图 ---- */

static CWValue_t shared_obj;
static CWValue_t refs[16];

static void test_shared(void) {
    printf("\n - 共享子图 (16 处引用同一容器)\n");
    memset(&shared_obj, 0, sizeof(shared_obj));
    cwvec_init(&shared_obj, CWInt16, 1);
    int16_t v = 42;
    CWValue_t cell;
    cwval_wrap(&cell, &v, 2);
    cwvec_push(&shared_obj, &cell);
    for (size_t i = 0; i < 16; i++) refs[i] = shared_obj;

    /* 回收后 shared_obj 仍完好 (多引用不引发提前回收/重复回收) */
    settle();
    T("shared survives", cwvec_size(&shared_obj) == 1);

    /* 切断全部 16 处引用 */
    memset(refs, 0, sizeof(refs));
    memset(&shared_obj, 0, sizeof(shared_obj));
    scrub();
    const size_t b = live_allocs();
    settle();
    T("shared collected once", live_allocs() < b);
}

/* ---- 3. 批量丢弃重建 (churn) ---- */

static void test_churn(void) {
    printf("\n - 批量丢弃重建 (20 轮 x 每轮 50 容器)\n");
    cwgc_set_step_bytes(8 * 1024); /* 高频触发 */
    size_t peak = 0;
    for (int round = 0; round < 20; round++) {
        CWValue_t keep[50];
        for (int i = 0; i < 50; i++) {
            memset(&keep[i], 0, sizeof(keep[i]));
            cwvec_init(&keep[i], CWInt16, 2);
            CWValue_t c;
            wrap_i16(&c, i + 1);
            cwvec_push(&keep[i], &c);
        }
        /* 本轮全部丢弃 (keep 在栈上, 下一轮覆盖) */
        memset(keep, 0, sizeof(keep));
        if (round % 4 == 3) {
            scrub();
            settle();
        }
        CWMemCenterStats_t ms;
        cwmc_stats(&ms);
        if (ms.active_allocs > peak) peak = ms.active_allocs;
    }
    scrub();
    settle();
    CWMemCenterStats_t ms;
    cwmc_stats(&ms);
    T("memory settles after churn", ms.active_allocs < peak);
    T("no crash across churn", 1);
    cwgc_set_step_bytes(64 * 1024);
}

/* ---- 4. 长存活索引 + 短存活临时 ---- */

static CWValue_t index_map;

static void test_mixed(void) {
    printf("\n - 长存活索引 + 短存活临时 (混合负载)\n");
    memset(&index_map, 0, sizeof(index_map));
    cwmap_init(&index_map, CWInt, CWString);
    cwgc_global_register(&index_map, sizeof(index_map));

    /* 长存活: 索引 100 键值 */
    for (int i = 0; i < 100; i++) {
        CWValue_t k, v;
        wrap_i16(&k, i + 1);
        char* s = (char*)cwrt_arena_alloc(16);
        snprintf(s, 16, "key%d", i);
        cwval_wrap(&v, s, strlen(s));
        if (!cwmap_put(&index_map, &k, &v)) {
            printf("  [dbg] put %d FAILED (allocs=%zu)\n",
                   i, live_allocs());
            break;
        }
        if (i % 10 == 0) printf("  [dbg] put %d ok\n", i);
    }

    /* 短存活: 大量临时容器 (churn) */
    for (int r = 0; r < 30; r++) {
        CWValue_t tmp;
        memset(&tmp, 0, sizeof(tmp));
        cwvec_init(&tmp, CWInt, 4);
        for (int i = 0; i < 10; i++) {
            CWValue_t c;
            wrap_i16(&c, i + 1);
            cwvec_push(&tmp, &c);
        }
        memset(&tmp, 0, sizeof(tmp));
        if (r % 5 == 4) {
            scrub();
            settle();
        }
    }

    /* 长存活索引内容完好 */
    CWValue_t probe, out;
    wrap_i16(&probe, 50);
    T("index intact after mixed load",
      cwmap_get(&index_map, &probe, &out) && out.length == 5
      && memcmp((const void*)(uintptr_t)out.address, "key49", 5) == 0);
    T("index size 100", cwmap_size(&index_map) == 100);

    cwgc_global_unregister(&index_map);
    memset(&index_map, 0, sizeof(index_map));
    scrub();
    const size_t b = live_allocs();
    settle();
    T("index collected after unregister", live_allocs() < b);
}

/* ---- 5. 单步延迟 (预算受控) ---- */

static void test_step_latency(void) {
    printf("\n - 增量 step 预算\n");
    /* 高分配速率下, 单步处理的节点数受 budget=step_bytes/8 约束 */
    cwgc_set_step_bytes(4 * 1024);
    CWGCStats_t before;
    cwgc_stats(&before);
    for (int r = 0; r < 200; r++) {
        CWValue_t t;
        memset(&t, 0, sizeof(t));
        cwvec_init(&t, CWInt16, 8);
        CWValue_t c;
        wrap_i16(&c, 1);
        cwvec_push(&t, &c);
        memset(&t, 0, sizeof(t));
    }
    CWGCStats_t after;
    cwgc_stats(&after);
    T("steps happened", after.steps > before.steps);
    T("cycles happened", after.cycles > before.cycles);
    T("reclaim happened", after.bytes_reclaimed > before.bytes_reclaimed);
    cwgc_set_step_bytes(64 * 1024);
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWGC stress tests:\n");

    cwgc_init();
    test_ring();
    test_shared();
    test_churn();
    test_mixed();
    test_step_latency();

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
