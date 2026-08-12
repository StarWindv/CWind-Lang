/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwlayout.h
 */

/**
 * 布局缓存 (struct 字段偏移)
 *
 * 设计:
 *  - 实例布局按「实例化后的类型」缓存, 同 name+实参 复用同一布局
 *    (Rust 单态化: 每个具体实例一份布局);
 *  - v0 布局策略: 每个字段占一个 32 字节句柄槽, 偏移 = 序号 * 32
 *    (与 CWIND_ABI_HANDLE_SIZE 一致, 等 ABI 定稿后可切换策略);
 *  - 泛型字段在实例化时做实参替换 (T -> 具体类型, Vector<T> -> Vector<Int>);
 *  - static 字段不进实例布局。
 */

#ifndef CWIND_CWLAYOUT_H
    #define CWIND_CWLAYOUT_H

    #include <stddef.h>
    #include <stdint.h>

    #include "cwmodule.h"
    #include "cwtype.h"

    #define CWLAYOUT_SLOT_SIZE ((size_t)32) /* 对应 ABI 句柄大小 */

    typedef struct CwFieldLayout {
        const char* name; /* 指向模块 JSON 文档 */
        size_t offset;    /* 实例内字节偏移 */
        CwTypeId type;    /* 实参替换后的字段类型 */
    } CwFieldLayout_t;

    typedef struct CwLayout {
        CwTypeId type;    /* 实例化后的 struct 类型 id */
        size_t field_count;
        CwFieldLayout_t* fields;
    } CwLayout_t;

    typedef struct CwLayoutCache {
        CwTypeTable_t* types; /* 共享类型表, 不拥有 */
        CwLayout_t** items;   /* 指针数组: 布局地址稳定, 扩容不失效 */
        size_t count;
        size_t cap;
    } CwLayoutCache_t;

    bool cwlayout_cache_init(CwLayoutCache_t* c, CwTypeTable_t* types);
    void cwlayout_cache_destroy(CwLayoutCache_t* c);

    /* 取 StructDecl 的实例布局; args 为空表示非泛型实例 */
    const CwLayout_t* cwlayout_get(CwLayoutCache_t* c,
                                  const CwModule_t* m,
                                  const CwNode_t* struct_decl,
                                  const CwTypeId* args,
                                  size_t arg_count);

#endif /* CWIND_CWLAYOUT_H */
