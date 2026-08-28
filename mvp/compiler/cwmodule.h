/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwmodule.h
 */

/*
[d: title="CwModule"]
TypedAST 装载产物。

消费 `cwindf --typed-ast` 输出的 JSON (cwind-typed-ast v1), 产出:

 - node 池: 按 id 索引的全部 AST 节点 (指向模块持有的 JSON 文档)
 - symbols: 顶层符号表
 - bindings: impl / extra 方法绑定表

Raises:
 - cwmodule_error()
   失败时返回最近一次错误信息 (静态缓冲, 线程不安全)
[/d]
*/

#ifndef CWIND_CWMODULE_H
    #define CWIND_CWMODULE_H

    #include <stdbool.h>
    #include <stddef.h>
    #include <stdint.h>

    /* stl/json 的 DOM 值 */
    typedef struct cw_value cw_value;

    #define CWMODULE_FORMAT  "cwind-typed-ast"
    #define CWMODULE_VERSION 1

    /*
    [d: title="CwSymbol"]
    符号表条目。`kind` 取值: const / type / struct / enum / trait / fn / group。
    `ref` 指向声明节点的 AST id。

    Fields:
     - name: 符号名称
     - kind: 符号种类
     - ref: 声明节点 id
    [/d]
    */
    typedef struct CwSymbol {
        const char* name;
        const char* kind;
        int64_t ref;
    } CwSymbol_t;

    /*
    [d: title="CwBinding"]
    impl / extra 方法绑定表。`owner` 为 NULL 表示 extra 方法。
    `trait` 为 NULL 表示 extra 方法。

    Fields:
     - id: 绑定表独立编号, 严格递增
     - decl_id: ImplDecl / ExtraDecl 节点 id
     - owner: 被扩展的 struct 名, extra 方法为 NULL
     - trait: 所属 trait 名, extra 方法为 NULL
     - fn_id: 方法声明 FnDecl 节点 id
    [/d]
    */
    typedef struct CwBinding {
        int64_t id;
        int64_t decl_id;
        const char* owner;
        const char* trait;
        int64_t fn_id;
    } CwBinding_t;

    /*
    [d: title="CwLinkInfo"]
    extern 块上的 `#[link(...)]` 属性 (todo-49); 字段均可为 NULL。

    Fields:
     - name: 库名 (gcc -l<name>)
     - kind: static / dylib
     - path: 具体库文件路径 (直接作为链接输入)
     - relative: todo-63: path 锚点 "cwd"/"source", NULL 视为 "cwd"
    [/d]
    */
    typedef struct CwLinkInfo {
        const char* name;
        const char* kind;
        const char* path;
        const char* relative;
    } CwLinkInfo_t;

    typedef struct CwNode {
        int64_t id;
        const char* kind;
        /* 指向模块持有 JSON 文档中的节点对象 */
        cw_value* value;
    } CwNode_t;

    typedef struct CwModule CwModule_t;

    /* ---- 装载与释放 ---- */
    CwModule_t* cwmodule_load_file(
        const char* path
    );
    CwModule_t* cwmodule_load_string(
        const char* json,
        size_t len
    );
    /* 释放由 cwmodule_load_* 成功返回的模块 */
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
    /* 按名称查找符号 (无则 NULL) */
    const CwSymbol_t* cwmodule_find_symbol(
        const CwModule_t* m,
        const char* name
    );

    /* 绑定表 (impl / extra 方法) */
    size_t cwmodule_binding_count(
        const CwModule_t* m
    );
    const CwBinding_t* cwmodule_binding(
        const CwModule_t* m,
        size_t i
    );

    /* extern 块 #[link(...)] 属性表 (已按 name+kind+path+relative 去重) */
    size_t cwmodule_link_count(
        const CwModule_t* m
    );
    const CwLinkInfo_t* cwmodule_link(
        const CwModule_t* m,
        size_t i
    );

    /*
    [d: title="cwmodule_source"]
    源文件路径 (todo-63): 信封顶层 "source" 字段, 无则为 NULL。
    relative = "source" 的 link path 以其目录为锚点解析。

    Args:
     - m: 模块句柄
    [/d]
    */
    const char* cwmodule_source(
        const CwModule_t* m
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
    /* 非 Type 返回 NULL */
    const char* cwmodule_type_name(
        cw_value* type
    );
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
