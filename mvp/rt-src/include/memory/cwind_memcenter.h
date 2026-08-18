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

#endif /* CWIND_MEMCENTER_H */
