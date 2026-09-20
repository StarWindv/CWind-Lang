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
        /* todo-208: 声明层解析后的签名类型名 (长度 = 参数数 + 1,
         * 末位为返回类型名; 字符串由模块 JSON / 类型表持有)。
         * 调用点打包与形参绑定以此为唯一事实源。
         * todo-209: sig_refs[i] = 形参是否为 &T/&mut T 借用
         * (借用句柄 address 是存储/数据地址, 标量不能内联打包)。 */
        const char** sig_names;
        unsigned char* sig_refs;
        size_t sig_count;
    } CwSymEntry_t;

    /*
    [d: title="CwExportEntry"]
    todo-55: 反向 FFI 导出项。`#[export]` / `#[export(name = "...")]`
    的函数在共享库里的 C 符号名 = c_name; `target` 指向同一函数的内部
    CWind 符号 (cwind.fn.<name>, 共享库内部链接)。codegen 为每项生成
    一个 C-ABI 适配器 (LLVM 符号 = c_name), DCE/可见性以适配器为根。

    Fields:
     - c_name: 导出 C 符号名
     - target: 内部 CWind 符号条目 (FN/INSTANCE)
     - decl: FnDecl 节点
    [/d]
    */
    typedef struct CwExportEntry {
        char* c_name;
        /* 内部符号在 items[] 中的下标 (数组会 realloc, 不能存指针) */
        size_t target_index;
        const CwNode_t* decl;
    } CwExportEntry_t;

    typedef struct CwSymTable {
        CwSymEntry_t* items;
        size_t count;
        size_t cap;
        /* todo-55: #[export] 导出面 (c_name 唯一, 前端已挡重名) */
        CwExportEntry_t* exports;
        size_t export_count;
        size_t export_cap;
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
    /* 同一 owner 基名可承载多份同名 impl (如 IterBuiltins<Set<T>> 与
     * IterBuiltins<String> 的 next) —— 方法符号追加 binding 的 decl_id
     * 消歧, 与实例化实参后缀互不冲突 (无 ABI 承诺)。 */
    bool cw_mangle_method_decl(
        char* buf, size_t cap,
        const char* owner, const char* name, int64_t decl_id
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

    /* 从模块构建: fn 符号 / 绑定方法 / 泛型模板 + #[export] 导出面 */
    bool cwsym_build_from_module(
        CwSymTable_t* s,
        const CwModule_t* m
    );

    /* todo-55: #[export] 导出项登记/查询。
     * cwsym_export_add 对重名 (同 c_name) 或无效目标返回 NULL。 */
    const CwExportEntry_t* cwsym_export_add(
        CwSymTable_t* s,
        const char* c_name,
        size_t target_index,
        const CwNode_t* decl
    );
    size_t cwsym_export_count(
        const CwSymTable_t* s
    );
    const CwExportEntry_t* cwsym_export_at(
        const CwSymTable_t* s,
        size_t i
    );
    /* 导出项的内部目标符号 (下标解析, 数组 realloc 后仍有效) */
    const CwSymEntry_t* cwsym_export_target(
        const CwSymTable_t* s,
        const CwExportEntry_t* e
    );
    /* 按导出 C 符号名查找 (无则 NULL) */
    const CwExportEntry_t* cwsym_export_find(
        const CwSymTable_t* s,
        const char* c_name
    );
    /* FnDecl 节点上的导出名 (todo-55 "export_name" 字符串), 无则 NULL */
    const char* cwsym_export_name(
        const CwNode_t* decl
    );

    /* extern 块内 C 符号重命名 (todo-62): 节点 "link_name" 字符串,
     * 无则 NULL (extern "CWind" 方法别名绑定 todo-179 共用) */
    const char* cwsym_extern_link_name(
        cw_value* fn_node
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
