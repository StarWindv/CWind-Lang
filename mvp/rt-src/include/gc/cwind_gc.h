/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/gc/cwind_gc.h
 */

/**
 * CWind GC — 阶段 1: 非移动分代增量三色标记-清扫 (todo-35/149)
 *
 * 路线基因 (见 .handover/gc-analysis.2026-08-29.md):
 *   调度 = Lua 5.4 (增量步进挂分配路径) + 分代 (minor/major, 149),
 *   扫描 = TinyGo/D (类型精确 + 保守兜底),
 *   终态 = Go 并发 (阶段 2, 不在本档)。
 *
 * 元数据分区 (todo-50): 颜色/年龄/描述符存 memcenter 槽头 reserved,
 * 值载荷 (24B CWValue) 不携带任何 GC 元数据。
 *
 * 状态机: IDLE --分配字节阈值--> MARK(增量) --> FINISH(原子) -->
 *         SWEEP(原子) --> IDLE。
 * 分代 (149): 槽头 age 记存活轮数, 到 CWGC_OLD_AGE 的槽晋升老代;
 *   MINOR 轮只对年轻代根集合做标记 (老代槽直接视为存活, 依保守面
 *   的 remember 集合兜底), MAJOR 轮全堆标记 + 晋升累计。CWGC_MODE
 *   =minor|major 可强制指定本轮形态; 调度默认连续 7 轮 minor 后
 *   插入 1 轮 major (阈值倍率另算)。
 * cwgc_step() 由 cwmc_alloc 的分配字节驱动; cwgc_collect() 全量轮供
 * 压测/调试; CWGC_DISABLE=1 时全部退化为 no-op (行为与无 GC 一致)。
 *
 * 根集合:
 *   1. 全局根注册表 (静态/常量 blob、arena 段、rt 帧);
 *   2. 精确栈图 (todo-155): 生成函数声明引用载体槽, 挂帧内链表,
 *      cwgc_frame_enter/leave 维护活跃帧栈, GC 逐槽精确读;
 *   3. 保守栈扫描兜底 (init 记主线程栈底, setjmp 泼寄存器后扫栈;
 *      CWGC_STACK_SCAN=0 可关闭, 精确路径覆盖后的对照开关):
 *      覆盖 FFI 的 C 栈帧等未登记区间, 设计内保留。
 *
 * OS 归还 (149): sweep 后空 slab 块按「连续 N 轮全空才归还 + 每类
 * 保留 1 块 + 高水位跳过」策略 unmap; 大对象 (dedicated) sweep 未
 * 标记即走留档代码 unmap (解链 + gc_topo++ + mapped_bytes 递减)。
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

    /* 影子帧栈上限 (LIFO 纪律下 = 调用深度; rt 内部使用) */
    #define CWGC_MAX_FRAMES 1024

    /* 阶段 1 分代: 存活轮数 >= CWGC_OLD_AGE 的槽晋升老代 (饱和于
     * 0xFFFFFF, 槽头 age 位 24 bit) */
    #define CWGC_OLD_AGE          3

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

    /* 分代形态 (149): minor 只标年轻代, major 全堆 + 晋升 */
    typedef enum CWGCMode {
        CWGC_MODE_AUTO   = 0, /* 调度决定 (连续 7 minor 后 1 major) */
        CWGC_MODE_MINOR  = 1,
        CWGC_MODE_MAJOR  = 2,
    } CWGCMode_t;

    typedef struct CWGCStats {
        size_t cycles;            /* 完整回收轮数 */
        size_t minor_cycles;      /* 其中 minor 轮数 (149) */
        size_t major_cycles;      /* 其中 major 轮数 (149) */
        size_t steps;             /* cwgc_step 调用次数 */
        size_t nodes_marked;      /* 累计标记槽位 */
        size_t slots_swept;       /* 累计清扫槽位 */
        size_t bytes_reclaimed;   /* 累计回收字节 */
        size_t live_slots;        /* 上一轮清扫后的存活槽位 */
        size_t live_bytes;        /* 上一轮清扫后的存活字节 */
        size_t words_scanned;     /* 累计保守扫描的字数 */
        size_t false_marks;       /* 保守误判次数 (只泄漏不悬垂) */
        size_t blocks_released;   /* 归还 OS 的空 slab 块数 (149) */
        size_t large_released;    /* 归还 OS 的大对象数 (149) */
        size_t os_released_bytes; /* 归还 OS 的累计字节数 (149) */
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

    /* ---- 精确栈图 (todo-155) ----
     * codegen 为每个发射的函数体登记一个「帧头槽」: 函数入口 alloca 一个
     * head 指针槽并置 NULL, 调 cwgc_frame_enter(&head) 入 rt 帧栈; 之后
     * 每个引用载体槽 (CWValue 变量槽 / blob 内 cell / 引用绑定槽 / 临时
     * cell) 在声明点把 {next, addr} 节点挂到 head 链上; 每个返回点调
     * cwgc_frame_leave(&head) 出栈。GC 标记根时先精确走帧链 (逐槽读
     * word -> mark_maybe), 保守栈扫描可用 CWGC_STACK_SCAN=0 关闭
     * (精确路径覆盖后的对照开关; C/FFI 栈帧依赖生成代码已把跨调用
     * 存活的句柄物化进登记槽的纪律)。
     * 纪律: codegen 新增引用形态的值必须同步登记 —— 漏标 = 悬垂,
     * 比误保留严重。单线程全局帧栈; todo-150 并发时改 per-thread。 */

    /* 生成代码按 {ptr, ptr} 字面结构体构造, 布局必须一致 (16B) */
    typedef struct CWGCNode {
        struct CWGCNode* next; /* 链表下一节点 (声明序倒挂) */
        void* addr;            /* 引用载体槽地址 (GC 逐槽读 word) */
    } CWGCNode_t;

    /* 帧登记 (LIFO): head_slot = 函数内 head 指针槽的地址 */
    void cwgc_frame_enter(void** head_slot);
    void cwgc_frame_leave(void** head_slot);

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

    /* 全量回收一轮 (调试/压测); 返回本轮回收字节。
     * mode: CWGC_MODE_AUTO 由调度决定; MINOR/MAJOR 强制指定形态
     * (测试/诊断用)。minor 轮只标年轻代 (老代视为存活), major 全堆。 */
    size_t cwgc_collect(void);
    size_t cwgc_collect_mode(CWGCMode_t mode);

    /* 状态查询 */
    CWGCState_t cwgc_state(void);
    bool cwgc_enabled(void);
    void cwgc_stats(CWGCStats_t* out);
    CWGCMode_t cwgc_last_mode(void);

    /* 运行时启停 (CWGC_DISABLE 的进程内副本; builtins::gc_enable 投影) */
    void cwgc_set_enabled(bool enabled);

    /* 观测 (builtins::gc_* 投影):
     * alloc_bytes = 迄今累计分配字节 (单调, 含已回收);
     * live_bytes  = 上一轮 sweep 后的存活字节 (从未完成过回收轮时为 0);
     * pause_ns    = 最近一轮原子收尾 (FINISH: 根重扫+drain+sweep) 耗时。 */
    size_t cwgc_alloc_bytes(void);
    size_t cwgc_live_bytes(void);
    size_t cwgc_pause_ns(void);

    /* OS 归还 (149, builtins/诊断投影):
     * mapped_bytes  = memcenter 当前映射总字节 (RSS 观测口径);
     * os_released   = 累计归还 OS 的字节 (空块 + 大对象);
     * 空块归还水位: CWGC_RELEASE_VACANT=0 (默认) 关闭, >=2 开启并
     * 取「连续 N 轮全空才归还」; CWGC_RELEASE_KEEP 每类保留块数。 */
    size_t cwgc_mapped_bytes(void);
    size_t cwgc_os_released_bytes(void);
    void cwgc_set_release_vacant(size_t rounds);
    size_t cwgc_release_vacant(void);

    /* 精确栈图观测 (todo-155): 最近一次 mark_roots 走帧链读过的槽数 */
    size_t cwgc_frame_slots_seen(void);

#endif /* CWIND_GC_H */
