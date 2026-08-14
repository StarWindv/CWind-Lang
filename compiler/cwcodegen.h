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
 * 函数调用 (用户函数 + builtins::print/type_of/readline/exit)、
 * Vector/Map/Set/String 内置方法、静态构造 Vector::new/Map::new/Set::new、
 * 用户结构体 (构造/字段读写/方法, 非泛型)、
 * 泛型函数/struct 方法实例化 (按调用点 type_args 单态化)、return、main 包装。
 * 数值类型: Int/Int8/Int32/Int64/UInt/UInt8/UInt32/UInt64/Byte/Float/Float64,
 * 混合宽度/符号的运算与比较自动提升到共同类型 (Rust 风格).
 * 暂不支持: Set 字面量、泛型 trait/约束方法分派。
 * 变量 = 40 字节对象记录 alloca (%cw.record), 标量值另配存储 alloca。
 */

#ifndef CWIND_CWCODEGEN_H
    #define CWIND_CWCODEGEN_H

    #include <stdbool.h>
    #include <stddef.h>

    #include "cwllvm.h"

    typedef struct CwVar {
        const char* name;
        LLVMValueRef record;  /* %cw.record* */
        LLVMValueRef storage; /* 标量值存储, 非标量 NULL */
        LLVMValueRef blob;    /* 用户结构体实例存储 (头 8B + 句柄槽), 非结构体 NULL */
        size_t blob_size;     /* blob 字节数 */
        size_t field_count;   /* 结构体字段数 (句柄槽数) */
        const CwLayout_t* layout; /* 结构体布局, 非结构体 NULL */
        const char* type_name;
    } CwVar_t;

    typedef struct CwExpr {
        LLVMValueRef handle;  /* %cw.handle */
        const char* type_name;
    } CwExpr_t;

    typedef struct CwLoop {
        LLVMBasicBlockRef break_bb;
        LLVMBasicBlockRef continue_bb;
    } CwLoop_t;

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
        CwVar_t* vars;
        size_t var_count;
        size_t var_cap;
        char error[256];
        bool failed;
    } CwCodegen_t;

    bool cwcodegen_init(CwCodegen_t* g, CwLlvm_t* ll,
                        const CwModule_t* m);
    void cwcodegen_destroy(CwCodegen_t* g);

    /* 生成全部函数体 + main 包装; 失败可用 cwcodegen_error 查看原因 */
    bool cwcodegen_emit(CwCodegen_t* g);
    const char* cwcodegen_error(const CwCodegen_t* g);

#endif /* CWIND_CWCODEGEN_H */
