/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwllvm.c
 */

#include "cwllvm.h"

#include <stdio.h>
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

/* todo (IR 优化管线): dump 前在进程内跑 new-PM opt 管线。
 *
 * cwindc 的 -O 标志此前只透传给 clang 编 obj 步 —— clang 对 .ll
 * 输入只做代码生成 (指令选择/调度), 不跑 IR 优化管线, 所以
 * "开不开 -O3 IR 一样" 且死代码常驻。这里用 LLVM-C 新 pass
 * builder 的 LLVMRunPasses 在 dump 前完成与 `opt -O3 -S` 等价的
 * 管线 (instcombine/sccp/adce/dse/licm/...)。
 *
 * TargetMachine 按 host triple + 可选 target-cpu ("native" 展开
 * 为 LLVMGetHostCPUName) 构建 —— opt 管线与后续 codegen 的向量
 * 能力 (march=native 的等价进程内形态) 都由它决定。opt 级别缺省
 * "2"; "0" 跳过 (保留原始 IR 便于调试)。返回 false 时调用方按
 * 模块加载失败路径报错。
 */
#include <llvm-c/Transforms/PassBuilder.h>
#include <llvm-c/Target.h>

/* --fast-math: 给模块内全部浮点运算指令挂 fast-math 标志
 * (reassoc/contract/afn/nsz/nnan/ninf/arcp)。与 clang -ffast-math
 * 的 C 前端层面标记同构 —— 之后 instcombine/licm/SLP/loopvec 的
 * 浮点重结合与向量化才能合法展开 (前端已做整数结合律, 语言层面
 * 不承诺 IEEE 754 逐位语义, 见 --fast-math CLI 文档)。 */
void cwllvm_apply_fast_math(
    LLVMModuleRef module
) {
    static const unsigned fast = LLVMFastMathAll;
    for (LLVMValueRef fn = LLVMGetFirstFunction(module); fn;
         fn = LLVMGetNextFunction(fn)) {
        for (LLVMBasicBlockRef bb = LLVMGetFirstBasicBlock(fn); bb;
             bb = LLVMGetNextBasicBlock(bb)) {
            for (LLVMValueRef inst = LLVMGetFirstInstruction(bb); inst;
                 inst = LLVMGetNextInstruction(inst)) {
                switch (LLVMGetInstructionOpcode(inst)) {
                    case LLVMFAdd: case LLVMFSub: case LLVMFMul:
                    case LLVMFDiv: case LLVMFRem: case LLVMFCmp:
                        LLVMSetFastMathFlags(inst, fast);
                        break;
                    default:
                        break;
                }
            }
        }
    }
}

bool cwllvm_run_opt_pipeline(
    CwLlvm_t* ll,
    const char* opt_level,
    const char* target_cpu,
    bool* errored
) {
    *errored = false;
    if (!ll || !ll->module) return false;
    if (!opt_level || !opt_level[0] || strcmp(opt_level, "0") == 0) {
        return true;
    }
    /* C API 不自动注册 target; opt.exe 自己注册全量, cwindc 必须
     * 显式注册 native (X86) 后 LLVMGetTargetFromTriple 才能命中。 */
    LLVMInitializeNativeTarget();
    LLVMInitializeNativeAsmParser();
    LLVMInitializeNativeAsmPrinter();
    /* new-PM 级别描述符: 0/1/2/3 -> default<O0..O3>; s/z -> Oz */
    const char* lvl = "O2";
    if (strcmp(opt_level, "0") == 0) lvl = "O0";
    else if (strcmp(opt_level, "1") == 0) lvl = "O1";
    else if (strcmp(opt_level, "2") == 0) lvl = "O2";
    else if (strcmp(opt_level, "3") == 0) lvl = "O3";
    else if (strcmp(opt_level, "s") == 0 || strcmp(opt_level, "z") == 0) {
        lvl = "Oz";
    }
    char desc[32];
    snprintf(desc, sizeof(desc), "default<%s>", lvl);

    /* TargetMachine: host triple + native/cpu 名 (opt 管线的向量
     * 能力由此决定; 与 clang 步的 -march 同源同值)。 */
    LLVMTargetRef target = NULL;
    char* triple = LLVMGetDefaultTargetTriple();
    if (LLVMGetTargetFromTriple(triple, &target, NULL) != 0) {
        LLVMDisposeMessage(triple);
        return false;
    }
    char* cpu = NULL;
    if (target_cpu && strcmp(target_cpu, "native") == 0) {
        cpu = LLVMGetHostCPUName();
    } else if (target_cpu && target_cpu[0]) {
        cpu = (char*)target_cpu;
    }
    LLVMTargetMachineRef tm = LLVMCreateTargetMachine(
        target, triple, cpu ? cpu : "generic", "",
        LLVMCodeGenLevelDefault, LLVMRelocDefault, LLVMCodeModelDefault);
    if (cpu && cpu != target_cpu) LLVMDisposeMessage(cpu);
    LLVMDisposeMessage(triple);
    if (!tm) return false;

    LLVMPassBuilderOptionsRef opts = LLVMCreatePassBuilderOptions();
    LLVMErrorRef err = LLVMRunPasses(ll->module, desc, tm, opts);
    LLVMDisposePassBuilderOptions(opts);
    LLVMDisposeTargetMachine(tm);
    if (err) {
        const char* msg = LLVMGetErrorMessage(err);
        fprintf(stderr, "cwindc: opt pipeline failed: %s\n", msg);
        LLVMDisposeErrorMessage((char*)msg);
        *errored = true;
        return false;
    }    return true;
}
