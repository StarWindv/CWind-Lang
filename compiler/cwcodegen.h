/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwcodegen.h
 */

/**
 * 函数体代码生成 (CCompiler.md §6, v0 子集)
 *
 * v0 支持: 标量/字符串字面量、Vector/Map 字面量、LetStmt、赋值 (=)、
 * Name 读写、Vector/Map 下标读写、算术/比较/位运算、短路 &&/||、
 * if/while/for-in (Vector)、break/continue、复合赋值、函数调用
 * (用户函数 + builtins::print)、Vector/Map/String 内置方法、return、main 包装。
 * 暂不支持: 用户结构体/方法/字段、Set/Tuple 字面量、Map/Set 遍历、
 * String 拼接、泛型函数实例化。
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
        LLVMValueRef current_fn;
        const char* current_ret_type;
        LLVMValueRef ret_global; /* 标量返回值全局缓冲 (跨调用存活) */
        CwLoop_t* loops;
        size_t loop_count;
        size_t loop_cap;
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
