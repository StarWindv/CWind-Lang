/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwllvm.h
 */

/**
 * LLVM 声明层 (CCompiler.md §5: 声明生成)
 *
 * 值表示统一为 32 字节句柄, 在 LLVM 里建模为:
 *   %cw.handle = type { i64, i64, i64, i64 }
 * 函数签名: 参数 = 句柄 × N (方法含 self), 返回 = 句柄。
 * v0 只做声明, 不生成函数体。
 */

#ifndef CWIND_CWLLVM_H
    #define CWIND_CWLLVM_H

    #include <stdbool.h>
    #include <stddef.h>
    #include <string.h>

    #include <llvm-c/Core.h>

    #include "cwlayout.h"
    #include "cwsymbol.h"
    #include "cwtype.h"

    typedef struct CwLlvm {
        LLVMContextRef ctx;
        LLVMModuleRef module;
        LLVMTypeRef handle_type; /* %cw.handle */
        LLVMTypeRef rec_type;    /* %cw.record = {i32, i8, [3 x i8], handle} */
        CwTypeTable_t* types;    /* 不拥有 */
        CwLayoutCache_t* layouts;/* 不拥有 */
        CwSymTable_t* syms;      /* 不拥有 */
    } CwLlvm_t;

    bool cwllvm_init(CwLlvm_t* ll, const char* module_name,
                     CwTypeTable_t* types,
                     CwLayoutCache_t* layouts,
                     CwSymTable_t* syms);
    void cwllvm_destroy(CwLlvm_t* ll);

    LLVMTypeRef cwllvm_handle_type(const CwLlvm_t* ll);
    LLVMTypeRef cwllvm_rec_type(const CwLlvm_t* ll);

    /* 声明一个函数 (mangled 名, param_count 个句柄参数, 返回句柄) */
    LLVMValueRef cwllvm_declare_function(CwLlvm_t* ll,
                                         const char* mangled,
                                         size_t param_count);

    /* 遍历符号表声明 FN / METHOD / INSTANCE (跳过 TEMPLATE) */
    bool cwllvm_declare_symbols(CwLlvm_t* ll);

    /* 模块文本 (调用方用 LLVMDisposeMessage 释放) */
    char* cwllvm_dump(const CwLlvm_t* ll);

#endif /* CWIND_CWLLVM_H */
