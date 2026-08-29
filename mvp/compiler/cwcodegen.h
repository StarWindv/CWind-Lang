/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwcodegen.h
 */

/**
 * 函数体代码生成 (CCompiler.md §6, v0 子集)
 *
 * v0 支持: 标量/字符串字面量、Vector/Map/Tuple 字面量、LetStmt、赋值 (=)、
 * Name 读写、Vector/Map/Tuple 下标读写、Tuple 元素访问 (t.0/t[0])、
 * 算术/比较/位运算、短路 &&/||、
 * if/while/for-in (Vector/Set/Map)、break/continue、复合赋值、String 拼接 (+/+=)、
 * 函数调用 (用户函数 + builtins::print/type_of/readline/exit)、String::format
 * ({} 占位符按参数顺序替换, \{/\} 字面花括号, 模板扫描在 rt 栈机里做)、
 * Vector/Map/Set/String 内置方法、静态构造 Vector::new/Map::new/Set::new、
 * 用户结构体 (构造/字段读写/方法, 非泛型)、
 * 泛型函数/struct 方法实例化 (按调用点 type_args 单态化)、return、main 包装。
 * 数值类型: Int/Int8/Int16/Int32/Int64/UInt/UInt8/UInt16/UInt32/UInt64
 * /Byte/Float/Float64,
 * 混合宽度/符号的运算与比较自动提升到共同类型 (Rust 风格).
 * 函数指针: `fn(A, B) -> R` 类型; 裸函数名 / 非捕获闭包都可赋给该类型变量,
 * 通过 callee_kind="indirect" 的调用点间接调用.
 * 暂不支持: Set 字面量、泛型 trait/约束方法分派、捕获环境闭包。
 * 变量 = 40 字节对象记录 alloca (%cw.record), 标量值另配存储 alloca。
 */

#ifndef CWIND_CWCODEGEN_H
    #define CWIND_CWCODEGEN_H

    #include <stdbool.h>
    #include <stddef.h>

    #include "cwllvm.h"

    typedef struct CwVar {
        const char* name;
        LLVMValueRef slot;    /* 标量 alloca 或 CWValue alloca (值类型变量) */
        LLVMValueRef blob;    /* 结构体/枚举实例 blob / 定长数组纯载荷, 其它 NULL */
        size_t blob_size;     /* blob 字节数 */
        size_t field_count;   /* 结构体字段数 */
        const CwLayout_t* layout; /* 结构体布局 (C-Like), 非结构体 NULL */
        const char* type_name;
        size_t scope;         /* 声明所在作用域深度 (模式绑定隔离用) */
        bool is_enum;         /* 枚举实例 (blob: tag + 载荷 cell) */
        bool is_array;        /* 定长数组实例 (todo-60: 纯载荷 blob) */
        bool is_value;        /* String/Vector/Map/Set/Tuple: slot 是 CWValue alloca */
        bool is_ref_param;    /* self/&T 引用形参: slot 存调用者传来的值 */
        bool is_ref;          /* todo-145: &T/&mut T 引用绑定: slot 存被借用存储的地址 */
    } CwVar_t;

    typedef struct CwExpr {
        LLVMValueRef handle;  /* %cw.value = {i64, i64, i64} (ABI v2) */
        const char* type_name;
    } CwExpr_t;

    typedef struct CwLoop {
        LLVMBasicBlockRef break_bb;
        LLVMBasicBlockRef continue_bb;
    } CwLoop_t;

    typedef struct CwClosure {
        const char* name;     /* 生成名 ($closure.N), owned_names 所有 */
        const cw_value* decl; /* Closure 节点的 JSON 对象 (非 CwNode) */
        char symbol[256];     /* LLVM 函数符号 cwind.closure.N */
    } CwClosure_t;

    typedef struct CwCodegen {
        CwLlvm_t* ll;
        const CwModule_t* m;
        LLVMBuilderRef builder;
        LLVMBuilderRef alloca_builder; /* 只插 entry block, 避免非入口 alloca */
        LLVMValueRef current_fn;
        const char* current_ret_type;
        LLVMValueRef ret_global; /* 标量返回值全局缓冲 (跨调用存活) */
        LLVMValueRef ret_struct_global; /* 结构体返回值全局缓冲 */
        size_t ret_struct_size;
        size_t ret_struct_fields;
        const CwLayout_t* ret_struct_layout;
        CwLoop_t* loops;
        size_t loop_count;
        size_t loop_cap;
        /* 当前泛型实例上下文: 模板参数名 -> 实参 (emit 实例函数体时设置) */
        const char** tparam_names;
        CwTypeId* targs;
        size_t tcount;
        /* 当前方法所属 struct (Self:: 静态成员解析用) */
        const char* current_owner;
        CwVar_t* vars;
        size_t var_count;
        size_t var_cap;
        size_t scope_depth;
        size_t* scope_marks;   /* 每层作用域开始时的 var_count (弹栈截断) */
        size_t scope_mark_count;
        size_t scope_mark_cap;
        char** owned_names;    /* 生成变量名 (如 $m.N) 的稳定副本 */
        size_t owned_name_count;
        size_t owned_name_cap;
        CwClosure_t* closures; /* 待发射闭包队列 (发射中可能追加嵌套闭包) */
        size_t closure_count;
        size_t closure_cap;
        /* 期望容器 tag 上下文: let/assign 绑定类型已知时, 交给
         * `X::new()` 静态构造写入 data 头 (bug-47 绑定语义的 codegen 侧) */
        int exp_tags[2];
        bool has_exp_tags;
        char error[256];
        bool failed;
    } CwCodegen_t;

    bool cwcodegen_init(
        CwCodegen_t* g, CwLlvm_t* ll,
        const CwModule_t* m
    );
    void cwcodegen_destroy(
        CwCodegen_t* g
    );

    /* 生成全部函数体 + main 包装; 失败可用 cwcodegen_error 查看原因 */
    bool cwcodegen_emit(
        CwCodegen_t* g
    );
    const char* cwcodegen_error(
        const CwCodegen_t* g
    );

#endif /* CWIND_CWCODEGEN_H */
