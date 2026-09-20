/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwllvm.c
 */

#include "cwllvm.h"

#include <stdio.h>
#include <stdlib.h>
#include <llvm-c/TargetMachine.h>

#include "../rt-src/include/object/cwind_type.h"
#include "../rt-src/include/stl/json/cwind_json.h"

/* ---- todo-208: 标量类型表与签名映射 ----
 * 类型名 → 基础 id 与原生 LLVM 类型 (与 codegen / rt 三方同口径)。 */
int cwllvm_type_id(const char* name) {
    if (!name) return -1;
    if (strcmp(name, "Int") == 0) return CWInt;
    if (strcmp(name, "UInt") == 0) return CWUInt;
    if (strcmp(name, "Int8") == 0) return CWInt8;
    if (strcmp(name, "UInt8") == 0) return CWUInt8;
    if (strcmp(name, "Int16") == 0) return CWInt16;
    if (strcmp(name, "UInt16") == 0) return CWUInt16;
    if (strcmp(name, "Int32") == 0) return CWInt32;
    if (strcmp(name, "UInt32") == 0) return CWUInt32;
    if (strcmp(name, "Int64") == 0) return CWInt64;
    if (strcmp(name, "UInt64") == 0) return CWUInt64;
    if (strcmp(name, "Byte") == 0) return CWByte;
    if (strcmp(name, "Float") == 0) return CWFloat;
    if (strcmp(name, "Float64") == 0) return CWFloat64;
    if (strcmp(name, "Bool") == 0) return CWBool;
    if (strcmp(name, "String") == 0) return CWString;
    if (strcmp(name, "None") == 0) return CWNone;
    if (strcmp(name, "Vector") == 0) return CWVector;
    if (strcmp(name, "Map") == 0) return CWMap;
    if (strcmp(name, "Set") == 0) return CWSet;
    if (strcmp(name, "Tuple") == 0) return CWTuple;
    return -1;
}

LLVMTypeRef cwllvm_scalar_type(const CwLlvm_t* ll, const char* name,
                               size_t* size) {
    switch (cwllvm_type_id(name)) {
    case CWInt:
    case CWUInt:
        if (size) *size = 2;
        return LLVMInt16TypeInContext(ll->ctx);
    case CWInt8:
    case CWUInt8:
    case CWByte:
    case CWBool:
        if (size) *size = 1;
        return LLVMInt8TypeInContext(ll->ctx);
    case CWInt16:
    case CWUInt16:
        if (size) *size = 2;
        return LLVMInt16TypeInContext(ll->ctx);
    case CWInt32:
    case CWUInt32:
        if (size) *size = 4;
        return LLVMInt32TypeInContext(ll->ctx);
    case CWInt64:
    case CWUInt64:
        if (size) *size = 8;
        return LLVMInt64TypeInContext(ll->ctx);
    case CWFloat:
        if (size) *size = 4;
        return LLVMFloatTypeInContext(ll->ctx);
    case CWFloat64:
        if (size) *size = 8;
        return LLVMDoubleTypeInContext(ll->ctx);
    default:
        return NULL;
    }
}

LLVMTypeRef cwllvm_fn_mapped_arg(const CwLlvm_t* ll, const char* name) {
    LLVMTypeRef vt = name ? cwllvm_scalar_type(ll, name, NULL) : NULL;
    return vt ? vt : ll->handle_type;
}

/* 借用位 (ref) 的类型对象: &T/&mut T 形参与返回一律按 24B 句柄
 * 承载 (address 直指被借存储), 与 codegen cg_type_is_ref 同判据 */
static bool cwllvm_obj_is_ref(cw_value* type_obj) {
    if (!type_obj || cw_typeof(type_obj) != CW_OBJECT) return false;
    cw_value* rv = cw_object_get(type_obj, "ref");
    bool ref = false;
    if (rv && cw_typeof(rv) == CW_BOOL) cw_as_bool(rv, &ref);
    if (!ref) {
        cw_value* ann = cw_object_get(type_obj, "ann");
        cw_value* t = ann ? cw_object_get(ann, "type") : NULL;
        if (t && cw_typeof(t) == CW_OBJECT) {
            cw_value* rv2 = cw_object_get(t, "ref");
            if (rv2 && cw_typeof(rv2) == CW_BOOL) cw_as_bool(rv2, &ref);
        }
    }
    return ref;
}

/* todo-208: 类型对象 → 规范类型名 (codegen 的 cg_type_name_of 同纪律:
 * 优先 SA 解析后的 ann.type, Self 绑 owner, 泛型叶按 targs 替换) */
static const char* cwllvm_json_str(cw_value* obj, const char* key) {
    if (!obj || cw_typeof(obj) != CW_OBJECT) return NULL;
    cw_value* v = cw_object_get(obj, key);
    return (v && cw_typeof(v) == CW_STRING) ? cw_string_cstr(v) : NULL;
}

static cw_value* cwllvm_ann_type(cw_value* node) {
    if (!node || cw_typeof(node) != CW_OBJECT) return NULL;
    cw_value* ann = cw_object_get(node, "ann");
    if (!ann || cw_typeof(ann) != CW_OBJECT) return NULL;
    return cw_object_get(ann, "type");
}

static const char* cwllvm_type_name(
    const CwLlvm_t* ll,
    cw_value* type_obj,
    const char* owner,
    const char* const* tparams,
    const CwTypeId* targs,
    size_t nt
) {
    if (!type_obj || cw_typeof(type_obj) != CW_OBJECT) return NULL;
    const char* raw = cwllvm_json_str(type_obj, "name");
    const char* n = NULL;
    if (!raw || strcmp(raw, "Self") != 0) {
        cw_value* resolved = cwllvm_ann_type(type_obj);
        n = cwllvm_json_str(resolved, "name");
    }
    if (!n) n = raw;
    if (!n) return NULL;
    if (strcmp(n, "Self") == 0 && owner) n = owner;
    for (size_t i = 0; nt && i < nt; i++) {
        if (tparams[i] && strcmp(n, tparams[i]) == 0) {
            return cwtype_name(ll->types, targs[i]);
        }
    }
    return n;
}

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

LLVMValueRef cwllvm_declare_function_ex(
    CwLlvm_t* ll,
    const char* mangled,
    cw_value* fn_obj,
    const char* owner,
    const char* const* tparams,
    const CwTypeId* targs,
    size_t nt,
    CwSymEntry_t* store
) {
    if (!ll || !mangled || !ll->ctx || !ll->module) return NULL;
    LLVMValueRef existing = LLVMGetNamedFunction(ll->module, mangled);
    if (existing) return existing;
    cw_value* params = fn_obj ? cw_object_get(fn_obj, "params") : NULL;
    const size_t np = (params && cw_typeof(params) == CW_ARRAY)
        ? cw_array_size(params) : 0;
    /* todo-208: 解析后的签名类型名缓存到符号条目 (调用点打包的事实源) */
    const char** names = NULL;
    /* todo-209: 借用位随签名缓存 (标量实参打包须区分内联值/存储地址) */
    unsigned char* refs = NULL;
    if (store && !store->sig_names) {
        names = (const char**)calloc(np + 1, sizeof(char*));
        if (names) refs = (unsigned char*)calloc(np + 1, 1);
    }
    LLVMTypeRef* pt = NULL;
    if (np > 0) {
        pt = (LLVMTypeRef*)malloc(np * sizeof(LLVMTypeRef));
        if (!pt) { free(names); free(refs); return NULL; }
        for (size_t i = 0; i < np; i++) {
            cw_value* p = cw_array_get(params, i);
            cw_value* t = p ? cw_object_get(p, "type") : NULL;
            const char* tn = cwllvm_type_name(ll, t, owner,
                                              tparams, targs, nt);
            if (names) names[i] = tn;
            const bool is_ref = cwllvm_obj_is_ref(t);
            if (refs) refs[i] = is_ref ? 1 : 0;
            /* todo-208: 借用形参 (&T/&mut T/self 借用位) 恒为句柄承载 */
            pt[i] = is_ref
                ? ll->handle_type
                : cwllvm_fn_mapped_arg(ll, tn);
        }
    }
    cw_value* rt = fn_obj ? cw_object_get(fn_obj, "return_type") : NULL;
    const char* rn = cwllvm_type_name(ll, rt, owner, tparams, targs, nt);
    if (names) names[np] = rn;
    const bool ret_ref = cwllvm_obj_is_ref(rt);
    if (refs) refs[np] = ret_ref ? 1 : 0;
    LLVMTypeRef ret = ret_ref
        ? ll->handle_type
        : cwllvm_fn_mapped_arg(ll, rn);
    LLVMTypeRef fty = LLVMFunctionType(ret, pt, (unsigned)np, false);
    free(pt);
    LLVMValueRef fn = LLVMAddFunction(ll->module, mangled, fty);
    if (fn && names && store) {
        store->sig_names = names;
        store->sig_refs = refs;
        store->sig_count = np + 1;
    } else {
        free(names);
        free(refs);
    }
    return fn;
}

/* 实例条目的泛型形参名序列: owner 声明 params 在前、方法
 * type_params 在后 (与 cg_emit_function 的单态化上下文同序)。 */
static char** cwllvm_instance_tparams(
    const CwModule_t* m,
    const CwSymEntry_t* e,
    size_t* out_n
) {
    *out_n = 0;
    if (!m || !e->decl || e->inst_count == 0) return NULL;
    const CwNode_t* fdecl = e->decl;
    cw_value* ftp = cw_object_get(fdecl->value, "type_params");
    const size_t n_fn = (ftp && cw_typeof(ftp) == CW_ARRAY)
        ? cw_array_size(ftp) : 0;
    const CwNode_t* owner_decl = NULL;
    if (e->owner) {
        for (size_t i = 0; i < cwmodule_binding_count(m); i++) {
            const CwBinding_t* bx = cwmodule_binding(m, i);
            if (bx->owner && strcmp(bx->owner, e->owner) == 0
                && bx->fn_id == fdecl->id) {
                owner_decl = cwmodule_node(m, bx->decl_id);
                break;
            }
        }
    }
    cw_value* otp = owner_decl
        ? cw_object_get(owner_decl->value, "params") : NULL;
    const size_t n_owner = (otp && cw_typeof(otp) == CW_ARRAY)
        ? cw_array_size(otp) : 0;
    /* todo-147: 具体类型特化 (extra Cell<Int>): owner 声明无泛型形参,
     * 形参名取被特化 struct 声明的 params, 实参即 struct args
     * (与 cg_emit_function 的 147 分支同纪律) */
    if (owner_decl && n_owner == 0) {
        cw_value* st = cw_object_get(owner_decl->value, "struct");
        cw_value* sargs = st ? cw_object_get(st, "args") : NULL;
        const size_t na = (sargs && cw_typeof(sargs) == CW_ARRAY)
            ? cw_array_size(sargs) : 0;
        const char* sname = cwllvm_json_str(st, "name");
        if (na > 0 && sname) {
            const CwSymbol_t* os = cwmodule_find_symbol(m, sname);
            const CwNode_t* sdecl = os ? cwmodule_node(m, os->ref) : NULL;
            cw_value* sp = sdecl
                ? cw_object_get(sdecl->value, "params") : NULL;
            if (sp && cw_typeof(sp) == CW_ARRAY
                && cw_array_size(sp) == na) {
                otp = sp;
            }
        }
    }
    const size_t n_owner2 = (otp && cw_typeof(otp) == CW_ARRAY)
        ? cw_array_size(otp) : 0;
    if (n_owner2 + n_fn == 0) return NULL;
    char** names = (char**)malloc((n_owner2 + n_fn) * sizeof(char*));
    if (!names) return NULL;
    size_t k = 0;
    cw_value* lists[2] = { otp, ftp };
    size_t counts[2] = { n_owner2, n_fn };
    for (size_t pass = 0; pass < 2; pass++) {
        for (size_t i = 0; i < counts[pass]; i++) {
            cw_value* tp = cw_array_get(lists[pass], i);
            const char* nm = cwllvm_json_str(tp, "name");
            names[k++] = (char*)(nm ? nm : "");
        }
    }
    *out_n = n_owner2 + n_fn;
    return names;
}

LLVMValueRef cwllvm_declare_sym(
    CwLlvm_t* ll,
    const CwModule_t* m,
    CwSymEntry_t* e
) {
    if (!ll || !e) return NULL;
    if (e->kind == CW_SYM_EXTERN || e->kind == CW_SYM_TEMPLATE) return NULL;
    cw_value* fn_obj = e->decl ? e->decl->value : NULL;
    if (e->kind == CW_SYM_INSTANCE) {
        size_t ntp = 0;
        char** tps = cwllvm_instance_tparams(m, e, &ntp);
        if (tps && ntp != e->inst_count) {
            /* 收集与实例登记不一致: 保守不做替换 (全句柄签名) */
            free(tps);
            tps = NULL;
            ntp = 0;
        }
        LLVMValueRef fn = cwllvm_declare_function_ex(
            ll, e->mangled, fn_obj, e->owner,
            (const char* const*)tps, e->inst_args, tps ? ntp : 0, e);
        free(tps);
        return fn;
    }
    return cwllvm_declare_function_ex(ll, e->mangled, fn_obj,
                                      e->owner, NULL, NULL, 0, e);
}

bool cwllvm_declare_symbols(
    CwLlvm_t* ll,
    const CwModule_t* m
) {
    if (!ll || !ll->syms) return false;
    for (size_t i = 0; i < ll->syms->count; i++) {
        CwSymEntry_t* e = &ll->syms->items[i];
        if (e->kind == CW_SYM_TEMPLATE) continue;
        if (e->kind == CW_SYM_EXTERN) continue; /* 按真实 C ABI 在调用点声明 */
        if (e->mangled && LLVMGetNamedFunction(ll->module, e->mangled)) {
            continue; /* 已在更早处境声明 (如调用点先建实例) */
        }
        if (!cwllvm_declare_sym(ll, m, e)) return false;
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
