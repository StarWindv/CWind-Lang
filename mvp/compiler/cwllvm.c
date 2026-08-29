/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwllvm.c
 */

#include "cwllvm.h"

#include <stdlib.h>
#include <llvm-c/TargetMachine.h>

bool cwllvm_init(
    CwLlvm_t* ll, const char* module_name,
    CwTypeTable_t* types,
    CwLayoutCache_t* layouts,
    CwSymTable_t* syms
) {
    if (!ll || !module_name || !types || !layouts || !syms) return false;
    memset(ll, 0, sizeof(*ll));
    ll->ctx = LLVMContextCreate();
    if (!ll->ctx) return false;
    ll->module = LLVMModuleCreateWithNameInContext(module_name, ll->ctx);
    if (!ll->module) {
        LLVMContextDispose(ll->ctx);
        memset(ll, 0, sizeof(*ll));
        return false;
    }

    /* ABI v2: 值 = {address, length, cursor} 24B 纯数据
     * (todo-50: 无类型头、无自指元数据, 元数据分区存放) */
    ll->handle_type = LLVMStructCreateNamed(ll->ctx, "cw.value");
    LLVMTypeRef elems[3] = {
        LLVMInt64TypeInContext(ll->ctx),
        LLVMInt64TypeInContext(ll->ctx),
        LLVMInt64TypeInContext(ll->ctx),
    };
    LLVMStructSetBody(ll->handle_type, elems, 3, false);

    /* 异构边界单元: 4B 类型 tag + 4B pad + 24B 值 = 32B (帧/rt 入口) */
    ll->cell_type = LLVMStructCreateNamed(ll->ctx, "cw.cell");
    LLVMTypeRef cell_elems[5] = {
        LLVMInt32TypeInContext(ll->ctx),
        LLVMInt32TypeInContext(ll->ctx),
        LLVMInt64TypeInContext(ll->ctx),
        LLVMInt64TypeInContext(ll->ctx),
        LLVMInt64TypeInContext(ll->ctx),
    };
    LLVMStructSetBody(ll->cell_type, cell_elems, 5, false);

    /* 设置 target triple + datalayout, 与 clang 编译 .ll 的行为一致 */
    char* triple = LLVMGetDefaultTargetTriple();
    if (triple) {
        LLVMSetTarget(ll->module, triple);
        LLVMDisposeMessage(triple);
    }
    ll->target_data = LLVMCreateTargetData(
        LLVMGetDataLayoutStr(ll->module));

    ll->types = types;
    ll->layouts = layouts;
    ll->syms = syms;
    return true;
}

size_t cwllvm_abisize(const CwLlvm_t* ll, LLVMTypeRef ty) {
    if (!ll || !ll->target_data || !ty) return 0;
    return (size_t)LLVMABISizeOfType(ll->target_data, ty);
}

void cwllvm_destroy(CwLlvm_t* ll) {
    if (!ll) return;
    if (ll->target_data) LLVMDisposeTargetData(ll->target_data);
    if (ll->module) LLVMDisposeModule(ll->module);
    if (ll->ctx) LLVMContextDispose(ll->ctx);
    memset(ll, 0, sizeof(*ll));
}

LLVMTypeRef cwllvm_handle_type(
    const CwLlvm_t* ll
) {
    return ll ? ll->handle_type : NULL;
}

LLVMValueRef cwllvm_declare_function(
    CwLlvm_t* ll,
    const char* mangled,
    size_t param_count
) {
    if (!ll || !mangled || !ll->ctx || !ll->module) return NULL;
    LLVMTypeRef* params = NULL;
    if (param_count > 0) {
        params = (LLVMTypeRef*)malloc(param_count * sizeof(LLVMTypeRef));
        if (!params) return NULL;
        for (size_t i = 0; i < param_count; i++) {
            params[i] = ll->handle_type;
        }
    }
    LLVMTypeRef fn_type = LLVMFunctionType(ll->handle_type,
                                           params, (unsigned)param_count,
                                           false);
    free(params);
    LLVMValueRef existing = LLVMGetNamedFunction(ll->module, mangled);
    if (existing) return existing;
    return LLVMAddFunction(ll->module, mangled, fn_type);
}

bool cwllvm_declare_symbols(
    CwLlvm_t* ll
) {
    if (!ll || !ll->syms) return false;
    for (size_t i = 0; i < ll->syms->count; i++) {
        const CwSymEntry_t* e = &ll->syms->items[i];
        if (e->kind == CW_SYM_TEMPLATE) continue;
        if (e->kind == CW_SYM_EXTERN) continue; /* 按真实 C ABI 在调用点声明 */
        size_t param_count = 0;
        if (e->decl) {
            param_count = cwmodule_fn_param_count(e->decl);
        }
        if (!cwllvm_declare_function(ll, e->mangled, param_count)) {
            return false;
        }
    }
    return true;
}

char* cwllvm_dump(
    const CwLlvm_t* ll
) {
    return ll && ll->module ? LLVMPrintModuleToString(ll->module) : NULL;
}
