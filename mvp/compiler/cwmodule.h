/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwmodule.h
 */

/**
 * CwModule: TypedAST 装载产物
 *
 * 消费 `cwindf --typed-ast` 输出的 JSON (cwind-typed-ast v1), 产出:
 *  - node 池: 按 id 索引的全部 AST 节点 (指向模块持有的 JSON 文档);
 *  - symbols: 顶层符号表;
 *  - bindings: impl / extra 方法绑定表。
 *
 * 校验范围: format / version / 引用完整性 (symbol.ref, binding.decl_id,
 * binding.fn_id, binding.id 唯一递增)。不做语义检查。
 *
 * 生命周期: cwmodule_load_* 成功返回的模块用 cwmodule_free 释放;
 * 失败返回 NULL, 详情见 cwmodule_error()。
 */

#ifndef CWIND_CWMODULE_H
    #define CWIND_CWMODULE_H

    #include <stdbool.h>
    #include <stddef.h>
    #include <stdint.h>

    typedef struct cw_value cw_value; /* stl/json 的 DOM 值 */

    #define CWMODULE_FORMAT  "cwind-typed-ast"
    #define CWMODULE_VERSION 1

    typedef struct CwSymbol {
        const char* name;
        const char* kind; /* const / type / struct / enum / trait / fn / group */
        int64_t ref;      /* 指向声明节点的 AST id */
    } CwSymbol_t;

    typedef struct CwBinding {
        int64_t id;       /* bindings 表独立编号, 严格递增 */
        int64_t decl_id;  /* ImplDecl / ExtraDecl 节点 id */
        const char* owner; /* 被扩展的 struct 名, extra 方法为 NULL */
        const char* trait; /* 所属 trait 名, extra 方法为 NULL */
        int64_t fn_id;    /* 方法声明 FnDecl 节点 id */
    } CwBinding_t;

    /* extern 块上的 #[link(...)] 属性 (todo-49); 字段均可为 NULL */
    typedef struct CwLinkInfo {
        const char* name; /* 库名 (gcc -l<name>) */
        const char* kind; /* static / dylib */
        const char* path; /* 具体库文件路径 (直接作为链接输入) */
    } CwLinkInfo_t;

    typedef struct CwNode {
        int64_t id;
        const char* kind;
        cw_value* value; /* 指向模块持有 JSON 文档中的节点对象 */
    } CwNode_t;

    typedef struct CwModule CwModule_t;

    /* 装载 */
    CwModule_t* cwmodule_load_file(
        const char* path
    );
    CwModule_t* cwmodule_load_string(
        const char* json,
        size_t len
    );
    void cwmodule_free(
        CwModule_t* m
    );

    /* 失败时返回最近一次错误信息 (静态缓冲, 线程不安全) */
    const char* cwmodule_error(
        void
    );

    /* 头部信息 */
    const char* cwmodule_format(
        const CwModule_t* m
    );
    int64_t cwmodule_version(
        const CwModule_t* m
    );

    /* 符号表 */
    size_t cwmodule_symbol_count(
        const CwModule_t* m
    );
    const CwSymbol_t* cwmodule_symbol(
        const CwModule_t* m,
        size_t i
    );
    const CwSymbol_t* cwmodule_find_symbol(
        const CwModule_t* m,
        const char* name
    );

    /* 绑定表 */
    size_t cwmodule_binding_count(
        const CwModule_t* m
    );
    const CwBinding_t* cwmodule_binding(
        const CwModule_t* m,
        size_t i
    );

    /* extern 块 #[link(...)] 属性表 (已按 name+kind+path 去重) */
    size_t cwmodule_link_count(
        const CwModule_t* m
    );
    const CwLinkInfo_t* cwmodule_link(
        const CwModule_t* m,
        size_t i
    );

    /* 节点池 (按 id 排序, 二分查找) */
    size_t cwmodule_node_count(
        const CwModule_t* m
    );
    const CwNode_t* cwmodule_node_at(
        const CwModule_t* m,
        size_t i
    );
    const CwNode_t* cwmodule_node(
        const CwModule_t* m,
        int64_t id
    );

    /* 顶层 ast 对象 (Program 节点) */
    cw_value* cwmodule_ast_root(
        const CwModule_t* m
    );

    /* ---- 类型化节点访问 (v0: 声明层) ---- */

    /* 通用字段查询: 返回节点 JSON 中 key 对应的值 (无则 NULL) */
    cw_value* cwmodule_node_field(
        const CwNode_t* n,
        const char* key
    );

    /* Type 对象 (kind == "Type", 带 name/args) */
    bool cwmodule_type_is(
        cw_value* v
    );
    const char* cwmodule_type_name(
        cw_value* type
    );   /* 非 Type 返回 NULL */
    size_t cwmodule_type_arg_count(
        cw_value* type
    );
    cw_value* cwmodule_type_arg(
        cw_value* type,
        size_t i
    );

    /* FnDecl: 名称 / 参数 / 返回类型 / 函数体 */
    const char* cwmodule_fn_name(
        const CwNode_t* n
    );
    size_t cwmodule_fn_param_count(
        const CwNode_t* n
    );
    cw_value* cwmodule_fn_param(
        const CwNode_t* n,
        size_t i
    );
    cw_value* cwmodule_fn_return_type(
        const CwNode_t* n
    );
    cw_value* cwmodule_fn_body(
        const CwNode_t* n
    );

    /* Param */
    const char* cwmodule_param_name(
        const CwNode_t* n
    );
    bool cwmodule_param_is_self(
        const CwNode_t* n
    );
    cw_value* cwmodule_param_type(
        const CwNode_t* n
    );

#endif /* CWIND_CWMODULE_H */
