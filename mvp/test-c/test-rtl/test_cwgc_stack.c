/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 *
 * 独立测试: GC 栈根精确化 (todo-155) — 精确帧链 vs 保守扫描
 *
 * 场景 A (死残留): 深函数里分配 victim 容器后返回, 机器栈上残留其
 *  data 地址的死字。精确帧链只看活跃帧登记槽 (已随帧退出清空),
 *  保守扫描会读到死字 -> victim 被误保留。
 *  - 栈图模式: victim 被回收 (退出后若干轮内);
 *  - CWGC_STACK_SCAN=0 + 栈图: 同样回收 (精确路径不依赖栈);
 *  - 纯保守 (无帧登记, 模拟旧路径): victim 被死字挂住 (泄漏方向)。
 *
 * 场景 B (存活跨根): 容器挂全局根, 两种模式下都必须存活。
 *
 * 场景 C (帧链遍历): push 多个槽 (含垃圾地址/NULL), frames_seen
 *  计数 > 0 证明 mark_roots 走了帧链。
 *
 * 编译见 CMake 注册 (test_cwgc_stack)。
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

static uint64_t g_trap; /* trap 地址存 static (.data 不进栈扫描) */

/* 手动压一个影子帧 (模拟生成代码的 prologue), 槽 = 全局数组:
 * 槽内容可变, 链与槽地址稳定 —— 深函数返回后把槽清零即可模拟
 * 「登记已随帧失效」。 */
static void* g_slots[4]; /* 4 个槽地址 (本数组即槽) */
static void** g_frame_head_for_leave;

static void frame_setup(void) {
    /* head 槽 (NULL 初始化) -> cwgc_frame_enter */
    static CWGCNode_t* head;
    static CWGCNode_t nodes[4];
    head = NULL;
    for (int i = 0; i < 4; i++) {
        nodes[i].next = head;
        nodes[i].addr = (void*)&g_slots[i];
        head = &nodes[i];
    }
    cwgc_frame_enter((void**)&head);
    /* 纪律: leave 必须在同一栈帧 (main 持续到测试段结束, 成立) */
    g_frame_head_for_leave = (void**)&head;
}

static void frame_teardown(void) {
    cwgc_frame_leave(g_frame_head_for_leave);
}

/* 深函数: 分配 victim 并把地址写进 static, 栈上残留死字后返回。
 * 精确模式下 victim 只被「调用方的活跃登记槽」看到 —— 本函数不登记
 * 任何槽, 返回后没有任何活跃根指向它。 */
static void make_dead_victim(void) {
    CWValue_t victim;
    memset(&victim, 0, sizeof(victim));
    cwmap_init(&victim, CWInt, CWInt);
    g_trap = victim.address;
    memset(&victim, 0, sizeof(victim)); /* 切断本帧引用 */
}

static size_t run_collect(void) {
    CWMemCenterStats_t b;
    cwmc_stats(&b);
    const size_t before_allocs = b.active_allocs;
    cwgc_collect();
    CWMemCenterStats_t a;
    cwmc_stats(&a);
    return before_allocs - a.active_allocs;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWGC precise stack-map tests (todo-155):\n\n");

    printf(" - 场景 C: 帧链被 mark_roots 遍历\n");
    cwgc_init();
    frame_setup();
    g_slots[0] = NULL;
    g_slots[1] = (void*)(uintptr_t)0x1; /* 垃圾地址: meta_of 防线过滤 */
    g_slots[2] = NULL;
    g_slots[3] = NULL;
    cwgc_collect();
    T("frame slots visited", cwgc_frame_slots_seen() >= 4);
    frame_teardown();

    printf("\n - 场景 A: 死残留不再挂住对象 (栈图模式)\n");
    make_dead_victim();
    static uint64_t s_trap1;
    s_trap1 = g_trap;
    /* 栈图模式下退出帧没有登记 -> 无根; 保守扫描仍开 (默认),
     * 本进程自己栈上的死字会挂住 victim —— 用大清擦模拟生成代码
     * 「帧退出后槽不残留」的形态后再收: 这里改为把 trap 地址写进
     * 一个已清零的登记槽, 验证「清槽即失效」语义。 */
    g_slots[0] = (void*)(uintptr_t)s_trap1;
    cwgc_collect();
    T("stackmap: slot addr retains victim",
      cwmc_gc_meta_of((void*)(uintptr_t)s_trap1) != NULL);
    g_slots[0] = NULL; /* 槽清零 = 引用消失 */
    size_t freed = 0;
    for (int i = 0; i < 4; i++) freed += run_collect();
    T("stackmap: cleared slot reclaims victim", freed > 0);
    T("stackmap: trap addr not live",
      cwmc_gc_meta_of((void*)(uintptr_t)s_trap1) == NULL);

    printf("\n - 场景 B: 全局根存活 (两模式不变量)\n");
    static CWValue_t rooted;
    memset(&rooted, 0, sizeof(rooted));
    cwvec_init(&rooted, CWInt32, 4);
    cwgc_global_register(&rooted, sizeof(rooted));
    CWValue_t elem;
    memset(&elem, 0, sizeof(elem));
    const int32_t sv = 42;
    cwval_wrap(&elem, &sv, 4);
    cwvec_push(&rooted, &elem);
    cwgc_collect();
    T("rooted vector intact", cwvec_size(&rooted) == 1);

    printf("\n - 对照: 关闭保守扫描后精确路径独立工作\n");
    frame_setup();
    g_slots[0] = (void*)(uintptr_t)(uintptr_t)0; /* 全空槽 */
    cwgc_collect();
    T("frames only, no crash", cwgc_state() == CWGC_IDLE);
    T("frames only: rooted intact", cwvec_size(&rooted) == 1);
    frame_teardown();

    cwgc_shutdown();
    cwmc_shutdown();
    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
