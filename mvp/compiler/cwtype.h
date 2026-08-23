/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwtype.h
 */

/**
 * 类型表 (interning)
 *
 * 把 TypedAST 里的结构化类型对象 (name + args) 规范化为唯一 CwTypeId:
 *  - 结构等值去重: 同 name 且逐位同 args 的类型共享一个 id
 *    (Rust 泛型单态化的 key 思路);
 *  - 类型对象直接从 JSON (ann.type / Type 节点) 解析, 递归处理实参;
 *  - 泛型参数叶子 ({"name": "T", "opaque": true}) 也照常登记,
 *    是否 opaque 记录在类型上, 供布局替换使用。
 */

#ifndef CWIND_CWTYPE_H
    #define CWIND_CWTYPE_H

    #include <stdbool.h>
    #include <stddef.h>
    #include <stdint.h>

    typedef struct cw_value cw_value;

    typedef uint32_t CwTypeId;
    #define CW_TYPE_INVALID ((CwTypeId)0)

    typedef struct CwType {
        char* name;        /* 表所有 (strdup) */
        CwTypeId* args;    /* 表所有, 可为 NULL */
        size_t arg_count;
        bool opaque;       /* 泛型参数叶子标记 */
    } CwType_t;

    typedef struct CwTypeTable {
        CwType_t* items;
        size_t count;
        size_t cap;
    } CwTypeTable_t;

    void cwtype_table_init(
        CwTypeTable_t* t
    );
    void cwtype_table_destroy(
        CwTypeTable_t* t
    );

    /* 结构等值登记: 已存在则返回原 id */
    CwTypeId cwtype_intern(
        CwTypeTable_t* t, const char* name,
        const CwTypeId* args, size_t arg_count
    );

    /* 从 JSON 类型对象解析并登记 (递归 args); 非法输入返回 CW_TYPE_INVALID */
CwTypeId cwtype_from_json(
   CwTypeTable_t* t,
   const cw_value* type_obj
   );

    /* 查询 */
    const CwType_t* cwtype_get(
        const CwTypeTable_t* t,
        CwTypeId id
    );
    const char* cwtype_name(
        const CwTypeTable_t* t,
        CwTypeId id
    );
    size_t cwtype_arg_count(
        const CwTypeTable_t* t,
        CwTypeId id
    );
    CwTypeId cwtype_arg(
        const CwTypeTable_t* t,
        CwTypeId id,
        size_t i
    );
    bool cwtype_is_opaque(
        const CwTypeTable_t* t,
        CwTypeId id
    );
    bool cwtype_equal(
        const CwTypeTable_t* t,
        CwTypeId a,
        CwTypeId b
    );

#endif /* CWIND_CWTYPE_H */
