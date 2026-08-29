/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwlayout.h
 */

/**
 * 布局缓存 (struct 字段偏移) — C-Like-Layout (todo-50)
 *
 * 设计:
 *  - 实例布局按「实例化后的类型」缓存, 同 name+实参 复用同一布局
 *    (Rust 单态化: 每个具体实例一份布局);
 *  - C-Like-Layout: 字段按 C 规则自然对齐放置 —
 *      标量字段   内联 (宽度 = 自身, 对齐 = 自身);
 *      定长数组   内联 (总量 = 元素宽×N, 对齐 = 元素宽);
 *      指针/函数指针 8 字节内联 (地址即值);
 *      嵌套结构体 内联展开 (递归布局, 值语义与 Rust 同);
 *      其余引用型 (String/Vector/Map/Set/Tuple/枚举) = 24B CWValue cell
 *      (对齐 8);
 *    实例 blob 无头无槽, 整块 memcpy 即深拷贝 (无自指句柄可重定向);
 *  - 泛型字段在实例化时做实参替换 (T -> 具体类型, Vector<T> -> Vector<Int>);
 *  - static 字段不进实例布局。
 */

#ifndef CWIND_CWLAYOUT_H
    #define CWIND_CWLAYOUT_H

    #include <stddef.h>
    #include <stdint.h>

    #include "cwmodule.h"
    #include "cwtype.h"

    #define CWLAYOUT_CELL_SIZE ((size_t)24) /* 引用型字段 cell (CWValue) */
    #define CWLAYOUT_MAX_DEPTH ((size_t)16) /* 嵌套结构体内联深度上限 */

    typedef struct CwFieldLayout {
        const char* name; /* 指向模块 JSON 文档 */
        size_t offset;    /* 实例内字节偏移 */
        size_t size;      /* 字段载荷字节数 (内联字段; cell 恒 24) */
        size_t align;     /* 字段对齐字节数 */
        CwTypeId type;    /* 实参替换后的字段类型 */
    } CwFieldLayout_t;

    typedef struct CwLayout {
        CwTypeId type;    /* 实例化后的 struct 类型 id */
        size_t size;      /* blob 总字节数 (含尾补齐) */
        size_t align;     /* 最大字段对齐 */
        size_t field_count;
        CwFieldLayout_t* fields;
    } CwLayout_t;

    typedef struct CwLayoutCache {
        CwTypeTable_t* types; /* 共享类型表, 不拥有 */
        CwLayout_t** items;   /* 指针数组: 布局地址稳定, 扩容不失效 */
        size_t count;
        size_t cap;
    } CwLayoutCache_t;

    bool cwlayout_cache_init(
        CwLayoutCache_t* c,
        CwTypeTable_t* types
    );
    void cwlayout_cache_destroy(
        CwLayoutCache_t* c
    );

    /* 取 StructDecl 的实例布局; args 为空表示非泛型实例 */
    const CwLayout_t* cwlayout_get(
        CwLayoutCache_t* c,
        const CwModule_t* m,
        const CwNode_t* struct_decl,
        const CwTypeId* args,
        size_t arg_count
    );

#endif /* CWIND_CWLAYOUT_H */
