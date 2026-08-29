/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/gc/cwind_gc.h
 */

/**
 * CWind GC — 阶段 0: 非移动增量三色标记-清扫 (todo-35)
 *
 * 路线基因 (见 .handover/gc-analysis.2026-08-29.md):
 *   调度 = Lua 5.4 (增量步进挂分配路径, 无线程无 STW),
 *   扫描 = TinyGo/D (类型精确 + 保守兜底; 阶段 0 全保守),
 *   终态 = Go 并发 (阶段 2, 不在本档)。
 *
 * 元数据分区 (todo-50): 颜色/年龄/描述符存 memcenter 槽头 reserved,
 * 值载荷 (24B CWValue) 不携带任何 GC 元数据。
 *
 * 状态机: IDLE --分配字节阈值--> MARK(增量) --> FINISH(原子) -->
 *         SWEEP(原子) --> IDLE。
 * cwgc_step() 由 cwmc_alloc 的分配字节驱动; cwgc_collect() 全量轮供
 * 压测/调试; CWGC_DISABLE=1 时全部退化为 no-op (行为与无 GC 一致)。
 *
 * 根集合:
 *   1. 全局根注册表 (静态/常量 blob、arena 段、rt 帧);
 *   2. 保守栈扫描 (init 记主线程栈底, 回收时 setjmp 泼寄存器后扫栈)。
 * 机器栈上的 LLVM alloca (标量/结构体 blob) 由 2 天然覆盖。
 *
 * 阶段 0 红线: 不做写屏障 / 并发 / 分代调度 / 空块释放。
 */

#ifndef CWIND_GC_H
    #define CWIND_GC_H

    #include <stdbool.h>
    #include <stddef.h>
    #include <stdint.h>

    /* 槽头 GC 元数据字 (reserved) 的位布局 */
    #define CWGC_COLOR_MASK       UINT64_C(0x0000000000000003)
    #define CWGC_TOUCHED_BIT      UINT64_C(0x0000000000000004)
    #define CWGC_PINNED_BIT       UINT64_C(0x0000000000000008)
    #define CWGC_AGE_SHIFT       4
    #define CWGC_AGE_MASK         UINT64_C(0x00000000FFFFFFF0)
    #define CWGC_DESC_SHIFT      32
    #define CWGC_DESC_MASK        UINT64_C(0xFFFFFFFF00000000)

    typedef enum CWGCColor {
        CWGC_WHITE = 0,
        CWGC_GREY  = 1,
        CWGC_BLACK = 2,
    } CWGCColor_t;

    typedef enum CWGCState {
        CWGC_IDLE = 0,
        CWGC_MARK,
        CWGC_FINISH,
        CWGC_SWEEP,
    } CWGCState_t;

    typedef struct CWGCStats {
        size_t cycles;            /* 完整回收轮数 */
        size_t steps;             /* cwgc_step 调用次数 */
        size_t nodes_marked;      /* 累计标记槽位 */
        size_t slots_swept;       /* 累计清扫槽位 */
        size_t bytes_reclaimed;   /* 累计回收字节 */
        size_t live_slots;        /* 上一轮清扫后的存活槽位 */
        size_t live_bytes;        /* 上一轮清扫后的存活字节 */
        size_t words_scanned;     /* 累计保守扫描的字数 */
        size_t false_marks;       /* 保守误判次数 (只泄漏不悬垂) */
    } CWGCStats_t;

    /* ---- 横向精确化 (todo-35 B 组) ----
     * 分配点描述符: 容器 data/节点在分配时打上类型 tag (槽头高位),
     * GC 按描述符分派精确 walker 遍历内部引用; 未注册/OPAQUE 退回
     * 保守扫描 (CWGC_CONSERVATIVE=1 可强制全保守做保留率对比)。 */

    typedef enum CWGCDesc {
        CWGC_DESC_OPAQUE       = 0,
        CWGC_DESC_VECTOR_DATA  = 1,
        CWGC_DESC_MAP_DATA     = 2,
        CWGC_DESC_MAP_NODE     = 3,
        CWGC_DESC_SET_DATA     = 4,
        CWGC_DESC_SET_NODE     = 5,
        CWGC_DESC_TUPLE_DATA   = 6,
    } CWGCDesc_t;

    typedef void (*cwgc_walk_fn)(void* base, unsigned size);

    /* 描述符 -> walker 注册 (cwgc_init 时由 rt 注册容器 walker) */
    void cwgc_desc_register(uint32_t desc, cwgc_walk_fn fn);

    /* 分配点打描述符 (容器 rt 分配后调用) */
    void cwgc_set_desc(const void* payload, uint32_t desc);

    /* 对象内部引用: 命中托管槽则 白->灰 入队, 非槽地址 (arena/栈/常量) 忽略 */
    void cwgc_mark_ref(const void* addr);

    /* 从属槽直接染黑 (如 Vector 的 items 数组, 由 data walker 精确代管) */
    void cwgc_mark_obj(const void* addr);

    /* 生命周期: 幂等; cwgc_init 记录主线程栈底 */
    void cwgc_init(void);
    void cwgc_shutdown(void);

    /* 全局根注册表: 扫描 [addr, addr+bytes) 的对齐 word 找堆指针 */
    bool cwgc_global_register(const void* addr, size_t bytes);
    bool cwgc_global_unregister(const void* addr);

    /* 分配路径字节驱动: cwmc_alloc 在超过阈值时调用; 外部亦可显式调用。
     * 返回本轮是否推进了状态机。 */
    bool cwgc_step(void);

    /* 分配字节阈值 (默认 64 KiB; env CWGC_STEP_BYTES 可覆盖) */
    void cwgc_set_step_bytes(size_t bytes);
    size_t cwgc_step_bytes(void);

    /* 写屏障: MARK 期间被写入堆槽的值保活 (容器写点调用; 栈/静态字段
     * 写由 FINISH 根重扫覆盖)。非 MARK 状态为 no-op。 */
    void cwgc_barrier(const void* value_addr);

    /* 分配屏障: MARK 期间的新槽染灰入队 (cwmc_alloc 内部调用) */
    void cwgc_alloc_barrier(void* payload);

    /* 全量回收一轮 (调试/压测); 返回本轮回收字节 */
    size_t cwgc_collect(void);

    /* 状态查询 */
    CWGCState_t cwgc_state(void);
    bool cwgc_enabled(void);
    void cwgc_stats(CWGCStats_t* out);

    /* 运行时启停 (CWGC_DISABLE 的进程内副本; builtins::gc_enable 投影) */
    void cwgc_set_enabled(bool enabled);

    /* 观测 (builtins::gc_* 投影):
     * alloc_bytes = 迄今累计分配字节 (单调, 含已回收);
     * live_bytes  = 上一轮 sweep 后的存活字节 (从未完成过回收轮时为 0);
     * pause_ns    = 最近一轮原子收尾 (FINISH: 根重扫+drain+sweep) 耗时。 */
    size_t cwgc_alloc_bytes(void);
    size_t cwgc_live_bytes(void);
    size_t cwgc_pause_ns(void);

#endif /* CWIND_GC_H */
