/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwcodegen.c
 */

#include "cwcodegen.h"

#include "../rt-src/include/object/cwind_type.h"
#include "../rt-src/include/stl/json/cwind_json.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* 结构 tag 与 typedef 名的兼容别名 (cwcodegen.h 定义的是 CwVar_t/CwExpr_t) */
typedef CwVar_t CwVar;
typedef CwExpr_t CwExpr;

/* ---- 基础工具 ---- */

static void cg_error(CwCodegen_t* g, const char* fmt, ...) {
    if (g->failed) return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g->error, sizeof(g->error), fmt, ap);
    va_end(ap);
    g->failed = true;
}

static LLVMContextRef cg_ctx(CwCodegen_t* g) {
    return g->ll->ctx;
}

static LLVMBuilderRef cg_b(CwCodegen_t* g) {
    return g->builder;
}

static LLVMValueRef cg_i64(CwCodegen_t* g, uint64_t v) {
    return LLVMConstInt(LLVMInt64TypeInContext(cg_ctx(g)), v, 0);
}

static LLVMValueRef cg_i32(CwCodegen_t* g, uint32_t v) {
    return LLVMConstInt(LLVMInt32TypeInContext(cg_ctx(g)), v, 0);
}

static LLVMValueRef cg_i8(CwCodegen_t* g, uint8_t v) {
    return LLVMConstInt(LLVMInt8TypeInContext(cg_ctx(g)), v, 0);
}

static LLVMValueRef cg_i16(CwCodegen_t* g, int64_t v) {
    return LLVMConstInt(LLVMInt16TypeInContext(cg_ctx(g)),
                        (uint64_t)(int16_t)v, 1);
}

static LLVMValueRef cg_f32(CwCodegen_t* g, double v) {
    return LLVMConstReal(LLVMFloatTypeInContext(cg_ctx(g)), v);
}

static cw_value* cg_node_ann_type(cw_value* node) {
    if (!node || cw_typeof(node) != CW_OBJECT) return NULL;
    cw_value* ann = cw_object_get(node, "ann");
    if (!ann || cw_typeof(ann) != CW_OBJECT) return NULL;
    return cw_object_get(ann, "type");
}

static const char* cg_node_type_name(cw_value* node) {
    cw_value* t = cg_node_ann_type(node);
    if (!t || cw_typeof(t) != CW_OBJECT) return NULL;
    cw_value* name = cw_object_get(t, "name");
    return (name && cw_typeof(name) == CW_STRING)
        ? cw_string_cstr(name) : NULL;
}

static const char* cg_json_name(cw_value* obj) {
    if (!obj || cw_typeof(obj) != CW_OBJECT) return NULL;
    cw_value* name = cw_object_get(obj, "name");
    return (name && cw_typeof(name) == CW_STRING)
        ? cw_string_cstr(name) : NULL;
}

static const char* cg_node_kind(cw_value* node) {
    if (!node || cw_typeof(node) != CW_OBJECT) return NULL;
    cw_value* k = cw_object_get(node, "kind");
    return (k && cw_typeof(k) == CW_STRING) ? cw_string_cstr(k) : NULL;
}

/* ---- 类型映射 ---- */

static int cg_type_id(const char* name) {
    if (!name) return -1;
    if (strcmp(name, "Int") == 0) return CWInt;
    if (strcmp(name, "UInt") == 0) return CWUInt;
    if (strcmp(name, "Int8") == 0) return CWInt8;
    if (strcmp(name, "UInt8") == 0) return CWUInt8;
    if (strcmp(name, "Byte") == 0) return CWByte;
    if (strcmp(name, "Float") == 0) return CWFloat;
    if (strcmp(name, "Bool") == 0) return CWBool;
    if (strcmp(name, "String") == 0) return CWString;
    if (strcmp(name, "None") == 0) return CWNone;
    return -1;
}

static bool cg_is_scalar(const char* name) {
    const int id = cg_type_id(name);
    return id == CWInt || id == CWUInt || id == CWInt8 || id == CWUInt8
        || id == CWByte || id == CWFloat || id == CWBool;
}

static bool cg_is_int(const char* name) {
    const int id = cg_type_id(name);
    return id == CWInt || id == CWUInt || id == CWInt8 || id == CWUInt8
        || id == CWByte;
}

static bool cg_is_unsigned(const char* name) {
    const int id = cg_type_id(name);
    return id == CWUInt || id == CWUInt8 || id == CWByte;
}

static LLVMTypeRef cg_scalar_type(CwCodegen_t* g, const char* name,
                                  size_t* size) {
    const int id = cg_type_id(name);
    switch (id) {
    case CWInt:
    case CWUInt:
        if (size) *size = 2;
        return LLVMInt16TypeInContext(cg_ctx(g));
    case CWInt8:
    case CWUInt8:
    case CWByte:
    case CWBool:
        if (size) *size = 1;
        return LLVMInt8TypeInContext(cg_ctx(g));
    case CWFloat:
        if (size) *size = 4;
        return LLVMFloatTypeInContext(cg_ctx(g));
    default:
        return NULL;
    }
}

/* ---- handle 构造 ---- */

static LLVMValueRef cg_build_handle(CwCodegen_t* g,
                                    LLVMValueRef object,
                                    LLVMValueRef address,
                                    LLVMValueRef length,
                                    LLVMValueRef cursor) {
    LLVMValueRef h = LLVMGetUndef(g->ll->handle_type);
    h = LLVMBuildInsertValue(cg_b(g), h, object, 0, "h.obj");
    h = LLVMBuildInsertValue(cg_b(g), h, address, 1, "h.addr");
    h = LLVMBuildInsertValue(cg_b(g), h, length, 2, "h.len");
    h = LLVMBuildInsertValue(cg_b(g), h, cursor, 3, "h.cur");
    return h;
}

static LLVMValueRef cg_null_handle(CwCodegen_t* g) {
    return LLVMConstNull(g->ll->handle_type);
}

static CwExpr cg_make_scalar(CwCodegen_t* g, LLVMValueRef value,
                             LLVMTypeRef value_type,
                             const char* type_name, size_t size) {
    LLVMValueRef storage = LLVMBuildAlloca(cg_b(g), value_type, "tmp");
    LLVMBuildStore(cg_b(g), value, storage);
    LLVMValueRef addr = LLVMBuildPtrToInt(cg_b(g), storage,
                                          LLVMInt64TypeInContext(cg_ctx(g)),
                                          "tmp.addr");
    CwExpr e = {
        cg_build_handle(g, cg_i64(g, 0), addr, cg_i64(g, size), cg_i64(g, 0)),
        type_name,
    };
    return e;
}

static LLVMValueRef cg_handle_addr(CwCodegen_t* g, CwExpr e) {
    return LLVMBuildExtractValue(cg_b(g), e.handle, 1, "addr");
}

static LLVMValueRef cg_load_value(CwCodegen_t* g, CwExpr e,
                                  LLVMTypeRef value_type) {
    LLVMValueRef ptr = LLVMBuildIntToPtr(
        cg_b(g), cg_handle_addr(g, e),
        LLVMPointerType(LLVMVoidTypeInContext(cg_ctx(g)), 0), "p");
    return LLVMBuildLoad2(cg_b(g), value_type, ptr, "v");
}

static LLVMValueRef cg_bool_cond(CwCodegen_t* g, CwExpr e) {
    LLVMValueRef v = cg_load_value(g, e, LLVMInt8TypeInContext(cg_ctx(g)));
    return LLVMBuildICmp(cg_b(g), LLVMIntNE, v, cg_i8(g, 0), "cond");
}

/* ---- 变量 ---- */

static CwVar_t* cg_var_find(CwCodegen_t* g, const char* name) {
    for (size_t i = 0; i < g->var_count; i++) {
        if (strcmp(g->vars[i].name, name) == 0) return &g->vars[i];
    }
    return NULL;
}

static bool cg_var_declare(CwCodegen_t* g, const char* name,
                           const char* type_name) {
    if (cg_var_find(g, name)) {
        cg_error(g, "变量重复声明: %s", name);
        return false;
    }
    if (g->var_count == g->var_cap) {
        const size_t nc = g->var_cap ? g->var_cap * 2 : 16;
        CwVar_t* nv = (CwVar_t*)realloc(g->vars, nc * sizeof(CwVar_t));
        if (!nv) {
            cg_error(g, "变量表扩容失败");
            return false;
        }
        g->vars = nv;
        g->var_cap = nc;
    }
    CwVar_t* v = &g->vars[g->var_count++];
    v->name = name;
    v->type_name = type_name;
    v->record = LLVMBuildAlloca(cg_b(g), g->ll->rec_type, name);
    size_t size = 0;
    v->storage = cg_is_scalar(type_name)
        ? LLVMBuildAlloca(cg_b(g), cg_scalar_type(g, type_name, &size),
                          "v.storage")
        : NULL;
    return true;
}

/* 把表达式写进变量记录: 标量拷值, 引用类型拷 handle */
static bool cg_rec_store(CwCodegen_t* g, CwVar_t* v, CwExpr e) {
    LLVMValueRef tid = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                           v->record, 0, "tid");
    LLVMBuildStore(cg_b(g), cg_i32(g, (uint32_t)cg_type_id(v->type_name)),
                   tid);

    LLVMValueRef handle;
    if (v->storage) {
        size_t size = 0;
        LLVMTypeRef vt = cg_scalar_type(g, v->type_name, &size);
        if (!vt) {
            cg_error(g, "未知标量类型: %s", v->type_name);
            return false;
        }
        LLVMValueRef val = cg_load_value(g, e, vt);
        LLVMBuildStore(cg_b(g), val, v->storage);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), v->storage, LLVMInt64TypeInContext(cg_ctx(g)), "addr");
        LLVMValueRef obj = LLVMBuildPtrToInt(
            cg_b(g), v->record, LLVMInt64TypeInContext(cg_ctx(g)), "obj");
        handle = cg_build_handle(g, obj, addr, cg_i64(g, size), cg_i64(g, 0));
    } else {
        handle = e.handle; /* String / None: 引用语义 */
    }
    LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                            v->record, 3, "h");
    LLVMBuildStore(cg_b(g), handle, hptr);
    return true;
}

static CwExpr cg_var_read(CwCodegen_t* g, CwVar_t* v) {
    LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                            v->record, 3, "h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hptr, "vh");
    CwExpr e = { h, v->type_name };
    return e;
}

/* ---- rt 声明 ---- */

static LLVMValueRef cg_rt_declare(CwCodegen_t* g, const char* name,
                                  LLVMTypeRef ret,
                                  LLVMTypeRef* params, size_t n) {
    LLVMValueRef existing = LLVMGetNamedFunction(g->ll->module, name);
    if (existing) return existing;
    LLVMTypeRef ft = LLVMFunctionType(ret, params, (unsigned)n, false);
    return LLVMAddFunction(g->ll->module, name, ft);
}

static LLVMTypeRef cg_rt_i8_ptr(CwCodegen_t* g) {
    return LLVMPointerType(LLVMVoidTypeInContext(cg_ctx(g)), 0);
}

/* ---- 表达式 ---- */

static CwExpr cg_expr(CwCodegen_t* g, cw_value* node);

static CwExpr cg_expr_lit(CwCodegen_t* g, cw_value* node) {
    const char* kind = cg_node_kind(node);
    if (strcmp(kind, "IntLit") == 0) {
        cw_value* v = cw_object_get(node, "value");
        int64_t iv = 0;
        if (!v || cw_as_int(v, &iv) != CW_OK) {
            cg_error(g, "IntLit 缺少 value");
            return (CwExpr){ NULL, NULL };
        }
        return cg_make_scalar(g, cg_i16(g, iv),
                              LLVMInt16TypeInContext(cg_ctx(g)), "Int", 2);
    }
    if (strcmp(kind, "FloatLit") == 0) {
        cw_value* v = cw_object_get(node, "value");
        double dv = 0;
        if (!v || cw_as_double(v, &dv) != CW_OK) {
            cg_error(g, "FloatLit 缺少 value");
            return (CwExpr){ NULL, NULL };
        }
        return cg_make_scalar(g, cg_f32(g, dv),
                              LLVMFloatTypeInContext(cg_ctx(g)), "Float", 4);
    }
    if (strcmp(kind, "BoolLit") == 0) {
        cw_value* v = cw_object_get(node, "value");
        bool bv = false;
        if (!v || cw_as_bool(v, &bv) != CW_OK) {
            cg_error(g, "BoolLit 缺少 value");
            return (CwExpr){ NULL, NULL };
        }
        return cg_make_scalar(g, cg_i8(g, bv ? 1 : 0),
                              LLVMInt8TypeInContext(cg_ctx(g)), "Bool", 1);
    }
    if (strcmp(kind, "StrLit") == 0) {
        cw_value* v = cw_object_get(node, "value");
        size_t len = 0;
        const char* s = v ? cw_string_value(v, &len) : NULL;
        if (!s) {
            cg_error(g, "StrLit 缺少 value");
            return (CwExpr){ NULL, NULL };
        }
        static unsigned str_seq = 0;
        char gname[32];
        snprintf(gname, sizeof(gname), ".str.%u", str_seq++);
        LLVMValueRef gv = LLVMAddGlobal(
            g->ll->module,
            LLVMArrayType(LLVMInt8TypeInContext(cg_ctx(g)), len + 1),
            gname);
        LLVMSetInitializer(
            gv, LLVMConstStringInContext(cg_ctx(g), s, len + 1, true));
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), gv, LLVMInt64TypeInContext(cg_ctx(g)), "s.addr");
        CwExpr e = {
            cg_build_handle(g, cg_i64(g, 0), addr, cg_i64(g, len),
                            cg_i64(g, 0)),
            "String",
        };
        return e;
    }
    cg_error(g, "不支持的字面量: %s", kind ? kind : "?");
    return (CwExpr){ NULL, NULL };
}

static CwExpr cg_expr_name(CwCodegen_t* g, cw_value* node) {
    cw_value* parts = cw_object_get(node, "parts");
    if (parts && cw_typeof(parts) == CW_ARRAY && cw_array_size(parts) == 1) {
        cw_value* p0 = cw_array_get(parts, 0);
        const char* n = (p0 && cw_typeof(p0) == CW_STRING)
            ? cw_string_cstr(p0) : NULL;
        if (n && strcmp(n, "None") == 0) {
            CwExpr e = { cg_null_handle(g), "None" };
            return e;
        }
        CwVar_t* v = n ? cg_var_find(g, n) : NULL;
        if (v) return cg_var_read(g, v);
        cg_error(g, "未声明变量: %s", n ? n : "?");
        return (CwExpr){ NULL, NULL };
    }
    cg_error(g, "暂不支持多段 Name / builtins");
    return (CwExpr){ NULL, NULL };
}

static CwExpr cg_expr_binop(CwCodegen_t* g, cw_value* node) {
    CwExpr l = cg_expr(g, cw_object_get(node, "left"));
    CwExpr r = cg_expr(g, cw_object_get(node, "right"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    cw_value* opv = cw_object_get(node, "op");
    const char* op = (opv && cw_typeof(opv) == CW_STRING)
        ? cw_string_cstr(opv) : "";

    if (strcmp(op, "&&") == 0 || strcmp(op, "||") == 0) {
        LLVMValueRef a = cg_load_value(g, l, LLVMInt8TypeInContext(cg_ctx(g)));
        LLVMValueRef b = cg_load_value(g, r, LLVMInt8TypeInContext(cg_ctx(g)));
        LLVMValueRef res = (strcmp(op, "&&") == 0)
            ? LLVMBuildAnd(cg_b(g), a, b, "and")
            : LLVMBuildOr(cg_b(g), a, b, "or");
        return cg_make_scalar(g, res, LLVMInt8TypeInContext(cg_ctx(g)),
                              "Bool", 1);
    }

    if (cg_is_int(l.type_name) && cg_is_int(r.type_name)) {
        LLVMTypeRef it = LLVMInt16TypeInContext(cg_ctx(g));
        LLVMValueRef a = cg_load_value(g, l, it);
        LLVMValueRef b = cg_load_value(g, r, it);
        const bool uns = cg_is_unsigned(l.type_name)
            || cg_is_unsigned(r.type_name);
        const char* res_type = uns ? "UInt" : "Int";

        if (strcmp(op, "+") == 0)
            return cg_make_scalar(g, LLVMBuildAdd(cg_b(g), a, b, "add"),
                                  it, res_type, 2);
        if (strcmp(op, "-") == 0)
            return cg_make_scalar(g, LLVMBuildSub(cg_b(g), a, b, "sub"),
                                  it, res_type, 2);
        if (strcmp(op, "*") == 0)
            return cg_make_scalar(g, LLVMBuildMul(cg_b(g), a, b, "mul"),
                                  it, res_type, 2);
        if (strcmp(op, "/") == 0) {
            LLVMValueRef q = uns ? LLVMBuildUDiv(cg_b(g), a, b, "div")
                                 : LLVMBuildSDiv(cg_b(g), a, b, "div");
            return cg_make_scalar(g, q, it, res_type, 2);
        }
        if (strcmp(op, "%") == 0) {
            LLVMValueRef q = uns ? LLVMBuildURem(cg_b(g), a, b, "rem")
                                 : LLVMBuildSRem(cg_b(g), a, b, "rem");
            return cg_make_scalar(g, q, it, res_type, 2);
        }
        if (strcmp(op, "<<") == 0)
            return cg_make_scalar(g, LLVMBuildShl(cg_b(g), a, b, "shl"),
                                  it, res_type, 2);
        if (strcmp(op, ">>") == 0) {
            LLVMValueRef q = uns ? LLVMBuildLShr(cg_b(g), a, b, "shr")
                                 : LLVMBuildAShr(cg_b(g), a, b, "shr");
            return cg_make_scalar(g, q, it, res_type, 2);
        }
        if (strcmp(op, "&") == 0)
            return cg_make_scalar(g, LLVMBuildAnd(cg_b(g), a, b, "and"),
                                  it, res_type, 2);
        if (strcmp(op, "|") == 0)
            return cg_make_scalar(g, LLVMBuildOr(cg_b(g), a, b, "or"),
                                  it, res_type, 2);
        if (strcmp(op, "^") == 0)
            return cg_make_scalar(g, LLVMBuildXor(cg_b(g), a, b, "xor"),
                                  it, res_type, 2);

        LLVMIntPredicate pred;
        if (strcmp(op, "==") == 0) pred = LLVMIntEQ;
        else if (strcmp(op, "!=") == 0) pred = LLVMIntNE;
        else if (strcmp(op, "<") == 0) pred = uns ? LLVMIntULT : LLVMIntSLT;
        else if (strcmp(op, "<=") == 0) pred = uns ? LLVMIntULE : LLVMIntSLE;
        else if (strcmp(op, ">") == 0) pred = uns ? LLVMIntUGT : LLVMIntSGT;
        else if (strcmp(op, ">=") == 0) pred = uns ? LLVMIntUGE : LLVMIntSGE;
        else {
            cg_error(g, "不支持的整数运算: %s", op);
            return (CwExpr){ NULL, NULL };
        }
        LLVMValueRef c = LLVMBuildICmp(cg_b(g), pred, a, b, "cmp");
        LLVMValueRef z = LLVMBuildZExt(cg_b(g), c,
                                       LLVMInt8TypeInContext(cg_ctx(g)),
                                       "cmp.b");
        return cg_make_scalar(g, z, LLVMInt8TypeInContext(cg_ctx(g)),
                              "Bool", 1);
    }

    if (strcmp(l.type_name, "Float") == 0
        && strcmp(r.type_name, "Float") == 0) {
        LLVMTypeRef ft = LLVMFloatTypeInContext(cg_ctx(g));
        LLVMValueRef a = cg_load_value(g, l, ft);
        LLVMValueRef b = cg_load_value(g, r, ft);
        if (strcmp(op, "+") == 0)
            return cg_make_scalar(g, LLVMBuildFAdd(cg_b(g), a, b, "fadd"),
                                  ft, "Float", 4);
        if (strcmp(op, "-") == 0)
            return cg_make_scalar(g, LLVMBuildFSub(cg_b(g), a, b, "fsub"),
                                  ft, "Float", 4);
        if (strcmp(op, "*") == 0)
            return cg_make_scalar(g, LLVMBuildFMul(cg_b(g), a, b, "fmul"),
                                  ft, "Float", 4);
        if (strcmp(op, "/") == 0)
            return cg_make_scalar(g, LLVMBuildFDiv(cg_b(g), a, b, "fdiv"),
                                  ft, "Float", 4);
        LLVMRealPredicate pred;
        if (strcmp(op, "==") == 0) pred = LLVMRealOEQ;
        else if (strcmp(op, "!=") == 0) pred = LLVMRealONE;
        else if (strcmp(op, "<") == 0) pred = LLVMRealOLT;
        else if (strcmp(op, "<=") == 0) pred = LLVMRealOLE;
        else if (strcmp(op, ">") == 0) pred = LLVMRealOGT;
        else if (strcmp(op, ">=") == 0) pred = LLVMRealOGE;
        else {
            cg_error(g, "不支持的浮点运算: %s", op);
            return (CwExpr){ NULL, NULL };
        }
        LLVMValueRef c = LLVMBuildFCmp(cg_b(g), pred, a, b, "fcmp");
        LLVMValueRef z = LLVMBuildZExt(cg_b(g), c,
                                       LLVMInt8TypeInContext(cg_ctx(g)),
                                       "fcmp.b");
        return cg_make_scalar(g, z, LLVMInt8TypeInContext(cg_ctx(g)),
                              "Bool", 1);
    }

    cg_error(g, "不支持的 BinOp: %s (%s, %s)", op,
             l.type_name ? l.type_name : "?", r.type_name ? r.type_name : "?");
    return (CwExpr){ NULL, NULL };
}

static CwExpr cg_expr_unary(CwCodegen_t* g, cw_value* node) {
    CwExpr e = cg_expr(g, cw_object_get(node, "operand"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    cw_value* opv = cw_object_get(node, "op");
    const char* op = (opv && cw_typeof(opv) == CW_STRING)
        ? cw_string_cstr(opv) : "";
    if (strcmp(op, "-") == 0) {
        if (cg_is_int(e.type_name)) {
            LLVMTypeRef it = LLVMInt16TypeInContext(cg_ctx(g));
            LLVMValueRef v = cg_load_value(g, e, it);
            return cg_make_scalar(g, LLVMBuildNeg(cg_b(g), v, "neg"),
                                  it, e.type_name, 2);
        }
        if (strcmp(e.type_name, "Float") == 0) {
            LLVMTypeRef ft = LLVMFloatTypeInContext(cg_ctx(g));
            LLVMValueRef v = cg_load_value(g, e, ft);
            return cg_make_scalar(g, LLVMBuildFNeg(cg_b(g), v, "fneg"),
                                  ft, "Float", 4);
        }
    }
    if (strcmp(op, "!") == 0) {
        LLVMValueRef v = cg_load_value(g, e, LLVMInt8TypeInContext(cg_ctx(g)));
        LLVMValueRef n = LLVMBuildXor(cg_b(g), v, cg_i8(g, 1), "not");
        return cg_make_scalar(g, n, LLVMInt8TypeInContext(cg_ctx(g)),
                              "Bool", 1);
    }
    cg_error(g, "不支持的 UnaryOp: %s", op);
    return (CwExpr){ NULL, NULL };
}

static CwExpr cg_expr_call(CwCodegen_t* g, cw_value* node) {
    cw_value* ann = cw_object_get(node, "ann");
    cw_value* call = ann ? cw_object_get(ann, "call") : NULL;
    if (!call) {
        cg_error(g, "Call 缺少 ann.call");
        return (CwExpr){ NULL, NULL };
    }
    cw_value* ck_v = cw_object_get(call, "callee_kind");
    const char* ck = (ck_v && cw_typeof(ck_v) == CW_STRING)
        ? cw_string_cstr(ck_v) : NULL;
    cw_value* ref_v = cw_object_get(call, "callee_ref");

    if (ck && strcmp(ck, "builtin") == 0) {
        const char* bname = (ref_v && cw_typeof(ref_v) == CW_STRING)
            ? cw_string_cstr(ref_v) : NULL;
        if (bname && strcmp(bname, "print") == 0) {
            cw_value* args = cw_object_get(node, "args");
            cw_value* arg0 = (args && cw_typeof(args) == CW_ARRAY
                              && cw_array_size(args) > 0)
                ? cw_array_get(args, 0) : NULL;
            if (!arg0) {
                cg_error(g, "print 需要 1 个参数");
                return (CwExpr){ NULL, NULL };
            }
            CwExpr a = cg_expr(g, cw_object_get(arg0, "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            /* 临时记录: type_id + handle */
            LLVMValueRef rec = LLVMBuildAlloca(cg_b(g), g->ll->rec_type,
                                               "print.rec");
            LLVMValueRef tid = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                   rec, 0, "tid");
            LLVMBuildStore(cg_b(g), cg_i32(g, (uint32_t)cg_type_id(a.type_name)),
                           tid);
            LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                    rec, 3, "h");
            LLVMBuildStore(cg_b(g), a.handle, hptr);
            LLVMTypeRef pr[1] = { cg_rt_i8_ptr(g) };
            LLVMValueRef fn = cg_rt_declare(
                g, "cw_builtin_print", LLVMInt1TypeInContext(cg_ctx(g)),
                pr, 1);
            LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec,
                                                 cg_rt_i8_ptr(g), "");
            LLVMValueRef argsv[1] = { rec8 };
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn,
                           argsv, 1, "");
            CwExpr none = { cg_null_handle(g), "None" };
            return none;
        }
        cg_error(g, "暂不支持 builtin: %s", bname ? bname : "?");
        return (CwExpr){ NULL, NULL };
    }

    if (ck && strcmp(ck, "fn") == 0) {
        int64_t ref = 0;
        if (!ref_v || cw_as_int(ref_v, &ref) != CW_OK) {
            cg_error(g, "函数调用缺少 callee_ref");
            return (CwExpr){ NULL, NULL };
        }
        const CwNode_t* fn_node = cwmodule_node(g->m, ref);
        const char* fname = fn_node ? cwmodule_fn_name(fn_node) : NULL;
        const CwSymEntry_t* sym = fname ? cwsym_find(g->ll->syms, NULL, fname)
                                        : NULL;
        if (!sym) {
            cg_error(g, "找不到函数符号: %s", fname ? fname : "?");
            return (CwExpr){ NULL, NULL };
        }
        LLVMValueRef fn = LLVMGetNamedFunction(g->ll->module, sym->mangled);
        if (!fn) {
            cg_error(g, "函数未声明: %s", sym->mangled);
            return (CwExpr){ NULL, NULL };
        }
        cw_value* args = cw_object_get(node, "args");
        const size_t n = (args && cw_typeof(args) == CW_ARRAY)
            ? cw_array_size(args) : 0;
        LLVMValueRef* argv = (LLVMValueRef*)malloc(
            (n ? n : 1) * sizeof(LLVMValueRef));
        if (!argv) {
            cg_error(g, "参数数组分配失败");
            return (CwExpr){ NULL, NULL };
        }
        for (size_t i = 0; i < n; i++) {
            cw_value* arg = cw_array_get(args, i);
            CwExpr a = cg_expr(g, cw_object_get(arg, "value"));
            if (g->failed) {
                free(argv);
                return (CwExpr){ NULL, NULL };
            }
            argv[i] = a.handle;
        }
        LLVMValueRef h = LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn),
                                        fn, argv, (unsigned)n, "call");
        free(argv);
        const char* ret_type = fn_node ? cwmodule_fn_return_type(fn_node)
                                         ? cg_json_name(
                                               cwmodule_fn_return_type(fn_node))
                                         : NULL
                                       : NULL;
        CwExpr e = { h, ret_type ? ret_type : "Any" };
        return e;
    }

    cg_error(g, "暂不支持调用: %s", ck ? ck : "?");
    return (CwExpr){ NULL, NULL };
}

static CwExpr cg_expr(CwCodegen_t* g, cw_value* node) {
    if (!node || cw_typeof(node) != CW_OBJECT) {
        cg_error(g, "表达式为空");
        return (CwExpr){ NULL, NULL };
    }
    const char* kind = cg_node_kind(node);
    if (!kind) {
        cg_error(g, "表达式缺少 kind");
        return (CwExpr){ NULL, NULL };
    }
    if (strcmp(kind, "IntLit") == 0 || strcmp(kind, "FloatLit") == 0
        || strcmp(kind, "BoolLit") == 0 || strcmp(kind, "StrLit") == 0) {
        return cg_expr_lit(g, node);
    }
    if (strcmp(kind, "Name") == 0) return cg_expr_name(g, node);
    if (strcmp(kind, "BinOp") == 0) return cg_expr_binop(g, node);
    if (strcmp(kind, "UnaryOp") == 0) return cg_expr_unary(g, node);
    if (strcmp(kind, "Call") == 0) return cg_expr_call(g, node);
    cg_error(g, "暂不支持表达式: %s", kind);
    return (CwExpr){ NULL, NULL };
}

/* ---- 语句 ---- */

static void cg_stmt(CwCodegen_t* g, cw_value* node);

static void cg_block(CwCodegen_t* g, cw_value* block) {
    cw_value* stmts = cw_object_get(block, "stmts");
    if (!stmts || cw_typeof(stmts) != CW_ARRAY) {
        cg_error(g, "Block 缺少 stmts");
        return;
    }
    const size_t n = cw_array_size(stmts);
    for (size_t i = 0; i < n && !g->failed; i++) {
        cg_stmt(g, cw_array_get(stmts, i));
    }
}

static void cg_stmt_let(CwCodegen_t* g, cw_value* node) {
    cw_value* name_v = cw_object_get(node, "name");
    const char* name = (name_v && cw_typeof(name_v) == CW_STRING)
        ? cw_string_cstr(name_v) : NULL;
    cw_value* type_v = cw_object_get(node, "type");
    const char* type_name = cg_json_name(type_v);
    if (!type_name) type_name = cg_node_type_name(node);
    if (!name || !type_name) {
        cg_error(g, "LetStmt 缺少 name/type");
        return;
    }
    if (!cg_var_declare(g, name, type_name)) return;
    CwVar_t* v = cg_var_find(g, name);
    CwExpr e = cg_expr(g, cw_object_get(node, "value"));
    if (!g->failed) cg_rec_store(g, v, e);
}

static void cg_stmt_assign(CwCodegen_t* g, cw_value* node) {
    cw_value* opv = cw_object_get(node, "op");
    const char* op = (opv && cw_typeof(opv) == CW_STRING)
        ? cw_string_cstr(opv) : "";
    if (strcmp(op, "=") != 0) {
        cg_error(g, "暂不支持复合赋值: %s", op);
        return;
    }
    cw_value* target = cw_object_get(node, "target");
    if (!target || cw_typeof(target) != CW_OBJECT
        || strcmp(cg_node_kind(target), "Name") != 0) {
        cg_error(g, "暂只支持对 Name 赋值");
        return;
    }
    cw_value* parts = cw_object_get(target, "parts");
    const char* name = (parts && cw_typeof(parts) == CW_ARRAY
                        && cw_array_size(parts) == 1)
        ? cw_string_cstr(cw_array_get(parts, 0)) : NULL;
    CwVar_t* v = name ? cg_var_find(g, name) : NULL;
    if (!v) {
        cg_error(g, "赋值目标未声明: %s", name ? name : "?");
        return;
    }
    CwExpr e = cg_expr(g, cw_object_get(node, "value"));
    if (!g->failed) cg_rec_store(g, v, e);
}

static void cg_stmt_return(CwCodegen_t* g, cw_value* node) {
    cw_value* value = cw_object_get(node, "value");
    if (!value || cw_typeof(value) == CW_NULL) {
        LLVMBuildRet(cg_b(g), cg_null_handle(g));
        return;
    }
    CwExpr e = cg_expr(g, value);
    if (g->failed) return;
    if (g->ret_global && cg_is_scalar(e.type_name)) {
        /* 把标量值拷进全局缓冲, 返回的 handle 指向全局 (跨调用存活) */
        size_t size = 0;
        LLVMTypeRef vt = cg_scalar_type(g, e.type_name, &size);
        LLVMValueRef val = cg_load_value(g, e, vt);
        LLVMBuildStore(cg_b(g), val, g->ret_global);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), g->ret_global, LLVMInt64TypeInContext(cg_ctx(g)),
            "ret.addr");
        LLVMValueRef h = cg_build_handle(g, cg_i64(g, 0), addr,
                                         cg_i64(g, size), cg_i64(g, 0));
        LLVMBuildRet(cg_b(g), h);
        return;
    }
    LLVMBuildRet(cg_b(g), e.handle);
}

static void cg_stmt_if(CwCodegen_t* g, cw_value* node) {
    CwExpr cond = cg_expr(g, cw_object_get(node, "cond"));
    if (g->failed) return;
    LLVMValueRef c = cg_bool_cond(g, cond);

    LLVMBasicBlockRef then_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "if.then");
    LLVMBasicBlockRef else_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "if.else");
    LLVMBasicBlockRef end_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "if.end");
    LLVMBuildCondBr(cg_b(g), c, then_bb, else_bb);

    LLVMPositionBuilderAtEnd(cg_b(g), then_bb);
    cg_block(g, cw_object_get(node, "then"));
    if (!g->failed) LLVMBuildBr(cg_b(g), end_bb);

    LLVMPositionBuilderAtEnd(cg_b(g), else_bb);
    cw_value* elifs = cw_object_get(node, "elifs");
    const size_t nelif = (elifs && cw_typeof(elifs) == CW_ARRAY)
        ? cw_array_size(elifs) : 0;
    if (nelif > 0) {
        for (size_t i = 0; i < nelif && !g->failed; i++) {
            cw_value* elif = cw_array_get(elifs, i);
            CwExpr ec = cg_expr(g, cw_object_get(elif, "cond"));
            if (g->failed) return;
            LLVMBasicBlockRef ethen = LLVMAppendBasicBlockInContext(
                cg_ctx(g), g->current_fn, "elif.then");
            LLVMBasicBlockRef enext = (i + 1 < nelif)
                ? LLVMAppendBasicBlockInContext(cg_ctx(g), g->current_fn,
                                                "elif.next")
                : else_bb;
            LLVMBuildCondBr(cg_b(g), cg_bool_cond(g, ec), ethen, enext);
            LLVMPositionBuilderAtEnd(cg_b(g), ethen);
            cg_block(g, cw_object_get(elif, "then"));
            if (!g->failed) LLVMBuildBr(cg_b(g), end_bb);
            LLVMPositionBuilderAtEnd(cg_b(g), enext);
        }
        cw_value* else_ = cw_object_get(node, "else_");
        if (else_ && cw_typeof(else_) == CW_OBJECT) {
            cg_block(g, else_);
        }
        if (!g->failed) LLVMBuildBr(cg_b(g), end_bb);
    } else {
        cw_value* else_ = cw_object_get(node, "else_");
        if (else_ && cw_typeof(else_) == CW_OBJECT) {
            cg_block(g, else_);
        }
        if (!g->failed) LLVMBuildBr(cg_b(g), end_bb);
    }

    LLVMPositionBuilderAtEnd(cg_b(g), end_bb);
}

static void cg_stmt_while(CwCodegen_t* g, cw_value* node) {
    LLVMBasicBlockRef cond_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "while.cond");
    LLVMBasicBlockRef body_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "while.body");
    LLVMBasicBlockRef end_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "while.end");
    LLVMBuildBr(cg_b(g), cond_bb);

    LLVMPositionBuilderAtEnd(cg_b(g), cond_bb);
    CwExpr cond = cg_expr(g, cw_object_get(node, "cond"));
    if (g->failed) return;
    LLVMBuildCondBr(cg_b(g), cg_bool_cond(g, cond), body_bb, end_bb);

    LLVMPositionBuilderAtEnd(cg_b(g), body_bb);
    cg_block(g, cw_object_get(node, "body"));
    if (!g->failed) LLVMBuildBr(cg_b(g), cond_bb);

    LLVMPositionBuilderAtEnd(cg_b(g), end_bb);
}

static void cg_stmt(CwCodegen_t* g, cw_value* node) {
    if (!node || cw_typeof(node) != CW_OBJECT) {
        cg_error(g, "语句为空");
        return;
    }
    const char* kind = cg_node_kind(node);
    if (!kind) {
        cg_error(g, "语句缺少 kind");
        return;
    }
    if (strcmp(kind, "LetStmt") == 0) { cg_stmt_let(g, node); return; }
    if (strcmp(kind, "Assign") == 0) { cg_stmt_assign(g, node); return; }
    if (strcmp(kind, "ReturnStmt") == 0) { cg_stmt_return(g, node); return; }
    if (strcmp(kind, "IfStmt") == 0) { cg_stmt_if(g, node); return; }
    if (strcmp(kind, "WhileStmt") == 0) { cg_stmt_while(g, node); return; }
    if (strcmp(kind, "ExprStmt") == 0) {
        cg_expr(g, cw_object_get(node, "expr"));
        return;
    }
    cg_error(g, "暂不支持语句: %s", kind);
}

/* ---- 函数与 main 包装 ---- */

static void cg_emit_function(CwCodegen_t* g, const CwSymEntry_t* e) {
    if (e->kind != CW_SYM_FN && e->kind != CW_SYM_METHOD) return;
    if (!e->decl) return;
    cw_value* body = cwmodule_fn_body(e->decl);
    if (!body) return; /* 纯声明 */

    LLVMValueRef fn = LLVMGetNamedFunction(g->ll->module, e->mangled);
    if (!fn) {
        cg_error(g, "函数未声明: %s", e->mangled);
        return;
    }
    g->current_fn = fn;
    g->var_count = 0;
    g->current_ret_type = NULL;
    g->ret_global = NULL;
    cw_value* rtv = cwmodule_fn_return_type(e->decl);
    if (rtv) {
        g->current_ret_type = cg_json_name(rtv);
        if (g->current_ret_type && cg_is_scalar(g->current_ret_type)) {
            size_t size = 0;
            LLVMTypeRef vt = cg_scalar_type(g, g->current_ret_type, &size);
            char gname[64];
            snprintf(gname, sizeof(gname), "fnret.%s", e->mangled);
            g->ret_global = LLVMAddGlobal(g->ll->module, vt, gname);
            LLVMSetInitializer(g->ret_global, LLVMConstNull(vt));
        }
    }
    LLVMBasicBlockRef entry = LLVMAppendBasicBlockInContext(cg_ctx(g), fn,
                                                            "entry");
    LLVMPositionBuilderAtEnd(cg_b(g), entry);

    const unsigned nparams = LLVMCountParams(fn);
    for (unsigned i = 0; i < nparams; i++) {
        LLVMValueRef arg = LLVMGetParam(fn, i);
        cw_value* p = cwmodule_fn_param(e->decl, i);
        const char* pname = p ? cg_json_name(p) : NULL;
        if (!pname) {
            cg_error(g, "函数 %s 参数 %u 缺名", e->mangled, i);
            return;
        }
        cw_value* ptype = p ? cw_object_get(p, "type") : NULL;
        const char* tname = ptype ? cg_json_name(ptype) : NULL;
        if (!tname) tname = cg_node_type_name(p);
        if (!tname) tname = "Any";
        if (!cg_var_declare(g, pname, tname)) return;
        CwVar_t* v = cg_var_find(g, pname);
        if (!v) return;
        LLVMSetValueName2(arg, pname, (unsigned)strlen(pname));
        CwExpr a = { arg, tname };
        if (!cg_rec_store(g, v, a)) return;
    }

    cg_block(g, body);
    if (g->failed) return;
    LLVMBuildRet(cg_b(g), cg_null_handle(g));
}

static void cg_emit_main_wrapper(CwCodegen_t* g) {
    const CwSymEntry_t* main_sym = cwsym_find(g->ll->syms, NULL, "main");
    if (!main_sym) return;
    LLVMValueRef user_main = LLVMGetNamedFunction(g->ll->module,
                                                  main_sym->mangled);
    if (!user_main) return;

    LLVMTypeRef ret_i32 = LLVMInt32TypeInContext(cg_ctx(g));
    LLVMValueRef main_fn = LLVMAddFunction(g->ll->module, "main",
                                           LLVMFunctionType(ret_i32, NULL, 0,
                                                            false));
    LLVMBasicBlockRef entry = LLVMAppendBasicBlockInContext(cg_ctx(g), main_fn,
                                                            "entry");
    LLVMPositionBuilderAtEnd(cg_b(g), entry);
    g->current_fn = main_fn;

    const unsigned nparams = LLVMCountParams(user_main);
    LLVMValueRef* argv = (LLVMValueRef*)malloc(
        (nparams ? nparams : 1) * sizeof(LLVMValueRef));
    if (!argv) {
        cg_error(g, "main 参数数组分配失败");
        return;
    }
    for (unsigned i = 0; i < nparams; i++) argv[i] = cg_null_handle(g);
    LLVMValueRef h = LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(user_main),
                                    user_main, argv, nparams, "main.call");
    free(argv);

    cw_value* rt = cwmodule_fn_return_type(main_sym->decl);
    const char* ret_type = rt ? cg_json_name(rt) : NULL;
    if (ret_type && strcmp(ret_type, "Int") == 0) {
        LLVMValueRef addr = LLVMBuildExtractValue(cg_b(g), h, 1, "addr");
        LLVMValueRef ptr = LLVMBuildIntToPtr(cg_b(g), addr,
                                             LLVMPointerType(
                                                 LLVMVoidTypeInContext(
                                                     cg_ctx(g)),
                                                 0),
                                             "p");
        LLVMValueRef v = LLVMBuildLoad2(cg_b(g),
                                        LLVMInt16TypeInContext(cg_ctx(g)),
                                        ptr, "main.ret");
        LLVMBuildRet(cg_b(g), LLVMBuildSExt(cg_b(g), v, ret_i32, "r"));
    } else {
        LLVMBuildRet(cg_b(g), cg_i32(g, 0));
    }
}

/* ---- 公开 API ---- */

bool cwcodegen_init(CwCodegen_t* g, CwLlvm_t* ll, const CwModule_t* m) {
    if (!g || !ll || !m) return false;
    memset(g, 0, sizeof(*g));
    g->ll = ll;
    g->m = m;
    g->builder = LLVMCreateBuilderInContext(ll->ctx);
    if (!g->builder) return false;
    return true;
}

void cwcodegen_destroy(CwCodegen_t* g) {
    if (!g) return;
    if (g->builder) LLVMDisposeBuilder(g->builder);
    free(g->vars);
    memset(g, 0, sizeof(*g));
}

bool cwcodegen_emit(CwCodegen_t* g) {
    if (!g || g->failed) return false;
    for (size_t i = 0; i < g->ll->syms->count && !g->failed; i++) {
        cg_emit_function(g, &g->ll->syms->items[i]);
    }
    if (!g->failed) cg_emit_main_wrapper(g);
    return !g->failed;
}

const char* cwcodegen_error(const CwCodegen_t* g) {
    return g ? g->error : "?";
}
