/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwsymbol.h
 */

/**
 * 编译器符号表 + 名称修饰 (CCompiler.md §5 的产出)
 *
 * 设计 (Rust 名称修饰思路的简化版):
 *  - 函数:      cwind.fn.<name>
 *  - 方法:      cwind.method.<owner>.<name>
 *  - 泛型实例:  base.<arg1,arg2,...>.<name> (实参按类型表规范化名拼接)
 *  - 泛型函数声明登记为 template 条目; 具体实例由实例化时登记,
 *    同 name+实参 生成同一修饰名 (单态化 key)。
 */

#ifndef CWIND_CWSYMBOL_H
    #define CWIND_CWSYMBOL_H

    #include <stddef.h>
    #include <stdint.h>

    #include "cwmodule.h"
    #include "cwtype.h"

    typedef enum CwSymKind {
        CW_SYM_FN,        /* 非泛型函数 */
        CW_SYM_METHOD,    /* 非泛型方法 (impl / extra) */
        CW_SYM_TEMPLATE,  /* 泛型函数 / 方法声明 */
        CW_SYM_INSTANCE,  /* 泛型具体实例 */
        CW_SYM_EXTERN     /* extern 块声明的外部函数 (符号 = 原始名) */
    } CwSymKind_t;

    typedef struct CwSymEntry {
        char* mangled;        /* 表所有 */
        const char* name;     /* 指向模块 */
        CwSymKind_t kind;
        const char* owner;    /* 方法: 所属类型名 */
        const char* trait;    /* 方法: 所属 trait, 无则 NULL */
        CwTypeId* inst_args;  /* 实例: 具体实参 (模板为 NULL) */
        size_t inst_count;
        const CwNode_t* decl; /* FnDecl 节点 */
    } CwSymEntry_t;

    typedef struct CwSymTable {
        CwSymEntry_t* items;
        size_t count;
        size_t cap;
    } CwSymTable_t;

    void cwsym_table_init(
        CwSymTable_t* s
    );
    void cwsym_table_destroy(
        CwSymTable_t* s
    );

    /* 名称修饰: 写入 buf, 空间不足返回 false */
    bool cw_mangle_fn(
        char* buf,
        size_t cap,
        const char* name
    );
    bool cw_mangle_method(
        char* buf, size_t cap,
        const char* owner, const char* name
    );
    bool cw_mangle_instance(
        char* buf, size_t cap,
        const char* base,
        const CwTypeTable_t* types,
        const CwTypeId* args, size_t arg_count
    );

    /* 登记 (mangled 重复则返回已有条目, 不重复添加) */
    const CwSymEntry_t* cwsym_add(
        CwSymTable_t* s, const char* mangled,
        const char* name, CwSymKind_t kind,
        const char* owner, const char* trait,
        const CwTypeId* inst_args,
        size_t inst_count,
        const CwNode_t* decl
    );

    /* 从模块构建: fn 符号 / 绑定方法 / 泛型模板 */
    bool cwsym_build_from_module(
        CwSymTable_t* s,
        const CwModule_t* m
    );

    /* 查询 */
    const CwSymEntry_t* cwsym_find_mangled(
        const CwSymTable_t* s,
        const char* mangled
    );
    const CwSymEntry_t* cwsym_find(
        const CwSymTable_t* s,
        const char* owner, const char* name
    );

#endif /* CWIND_CWSYMBOL_H */
