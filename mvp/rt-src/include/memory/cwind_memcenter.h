/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/memory/cwind_memcenter.h
 */

/**
 * CWind 统一内存中心 (Memory Center)
 *
 * 功能:
 *  - 为 rt 内部的底层簿记内存提供统一的分配 / 回收:
 *    栈帧节点、帧变量表、链表 / 数组 / 容器节点等。
 *  - 不负责高层 CWindObject 变量值的分配:
 *    那属于帧值栈 / GC 页 (cwind_mempage) 的职责, 与内存中心分开。
 *  - v0 例外: 进程期值 arena (String 拼接 / 枚举载荷单元 / 容器标量元素)
 *    的段也由这里分配 (大对象映射), 段进程期存活、不单独归还;
 *    GC 页落地后由 GC 取代。
 *
 * 设计:
 *  - 小对象 (<= CWMC_SLAB_MAX) 走 slab 池: 每类大小对应若干 64 KiB
 *    OS 块 (VirtualAlloc / mmap), 块内按定长槽位分配, 空闲槽串成链表。
 *  - 大对象 (> CWMC_SLAB_MAX) 单独映射一整块, 头带 32 字节元数据。
 *  - 每个分配前都有 32 字节头 (magic + 请求大小 + 所属块), free 时校验
 *    magic, 能识别失效指针 (double-free 的 slab 指针会被静默忽略)。
 *  - 首次分配时惰性初始化; cwmc_shutdown() 归还全部内存。
 *  - 非线程安全, 与现有容器一致。
 *
 * 编译开关:
 *  - CWMC_BLOCK_SIZE: slab 块大小, 默认 64 KiB。
 *  - CWMC_SLAB_MAX  : slab 最大请求大小, 默认 4096。
 *  - CWMC_MAX_ALIGN : slab 载荷对齐, 默认 16。
 *  内存来源只走平台 API (Windows VirtualAlloc / POSIX mmap), 不退回 malloc。
 */

#ifndef CWIND_MEMCENTER_H
    #define CWIND_MEMCENTER_H

    #include <stdbool.h>
    #include <stddef.h>
    #include <stdint.h>

    #ifndef CWMC_BLOCK_SIZE
        #define CWMC_BLOCK_SIZE ((size_t)64 * 1024)
    #endif

    #ifndef CWMC_SLAB_MAX
        #define CWMC_SLAB_MAX ((size_t)4096)
    #endif

    #ifndef CWMC_MAX_ALIGN
        #define CWMC_MAX_ALIGN ((size_t)16)
    #endif

    typedef struct CWMemCenterStats {
        size_t blocks;         /* 当前从 OS 持有的块数 (slab 块 + 大对象块) */
        size_t active_allocs;  /* 存活分配数 */
        size_t mapped_bytes;   /* 已从 OS 映射的总字节数 */
        size_t used_bytes;     /* 用户实际请求的存活字节数 */
        size_t errors;         /* 非法操作 (无效指针 / 非法对齐等) 计数 */
    } CWMemCenterStats_t;

    /* 生命周期: 均幂等; alloc 首次调用会自动 init */
    void cwmc_init(void);
    void cwmc_shutdown(void);

    /* 分配 / 回收 */
    void* cwmc_alloc(size_t size);
    void* cwmc_alloc_aligned(size_t size, size_t align);
    void* cwmc_calloc(size_t size);
    void* cwmc_realloc(void* ptr, size_t new_size);
    void  cwmc_free(void* ptr);

    /* 查询 */
    bool  cwmc_stats(CWMemCenterStats_t* out);
    size_t cwmc_usable_size(const void* ptr);

    /* ---- GC 协作接口 (todo-35 阶段 0, 元数据分区) ----
     * 槽头 reserved 字段拆分为 GC 元数据区 (与值载荷分离):
     *   bit 0-1  color (cwgc color: 0=white 1=grey 2=black)
     *   bit 2    touched (阶段 1 分代: 老槽写入新指针)
     *   bit 3    pinned (C 边界根化, todo-55 预留)
     *   bit 4-31 age (存活轮数, 阶段 1)
     *   bit 32-63 descriptor id (分配点类型描述, 0=OPAQUE 保守兜底)
     * GC 位由 cwind_gc 独占读写, memcenter 只负责提供访问与遍历。 */

    typedef bool (*cwmc_gc_used_cb)(void* payload, size_t size,
                                     uint64_t* meta, void* ud);

    /* 地址 -> 槽头 GC 元数据字指针; 不在托管堆返回 NULL */
    uint64_t* cwmc_gc_meta_of(const void* addr);

    /* 遍历全部在用槽 (slab 槽 + 大对象), sweep 用; 回调返回 false 中止 */
    void cwmc_gc_iter_used(cwmc_gc_used_cb cb, void* ud);

    /* 归还单槽 (等价 cwmc_free, 但用于 sweep: 空块一律保留, 不还给 OS);
     * 返回 false 表示指针非法 */
    bool cwmc_gc_release(void* payload);

    /* 分配字节计数器 (cwgc step 挂分配路径的字节驱动) */
    size_t cwmc_gc_alloc_bytes(void);
void cwmc_gc_take_alloc_bytes(size_t bytes);

    /* 迄今累计分配字节 (单调递增, 含已回收) */
    size_t cwmc_alloc_total_bytes(void);

#endif /* CWIND_MEMCENTER_H */
