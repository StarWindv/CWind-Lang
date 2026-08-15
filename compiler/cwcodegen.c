/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwcodegen.c
 */

#include "cwcodegen.h"

#include "../rt-src/include/object/cwind_type.h"
#include "../rt-src/include/object/cwind_object.h"
#include "../rt-src/include/stl/json/cwind_json.h"

#include <stdarg.h>
#include <math.h>
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

/* 带节点行号的报错 */
static void cg_error_at(CwCodegen_t* g, cw_value* node,
                        const char* fmt, ...) {
    if (g->failed) return;
    char msg[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    int64_t line = 0, col = 0;
    cw_value* lv = node ? cw_object_get(node, "line") : NULL;
    cw_value* cv = node ? cw_object_get(node, "column") : NULL;
    if (lv && cw_typeof(lv) == CW_INT) cw_as_int(lv, &line);
    if (cv && cw_typeof(cv) == CW_INT) cw_as_int(cv, &col);
    snprintf(g->error, sizeof(g->error), "%s (line %lld, column %lld)",
             msg, (long long)line, (long long)col);
    g->failed = true;
}

static const char* cg_type_name_of(CwCodegen_t* g, cw_value* type_obj);
static const char* cg_common_numeric(const char* a, const char* b);
static LLVMValueRef cg_convert_scalar(CwCodegen_t* g, LLVMValueRef v,
                                      const char* from, const char* to);
static const CwNode_t* cg_enum_decl(CwCodegen_t* g, const char* name);
static bool cg_is_enum_type(CwCodegen_t* g, const char* name);
static size_t cg_enum_blob_size(CwCodegen_t* g, const char* name);
static size_t cg_enum_slot_count(CwCodegen_t* g, const char* name);
static LLVMValueRef cg_enum_handle(CwCodegen_t* g, LLVMValueRef blob,
                                   const char* enum_name);
static CwExpr cg_expr_enum_build(CwCodegen_t* g, const char* enum_name,
                                 size_t variant_index,
                                 cw_value* payload_types,
                                 cw_value* args);
static CwExpr cg_expr_match(CwCodegen_t* g, cw_value* node);

static LLVMContextRef cg_ctx(CwCodegen_t* g) {
    return g->ll->ctx;
}

static LLVMBuilderRef cg_b(CwCodegen_t* g) {
    return g->builder;
}

/* alloca 统一进 entry block (LLVM Win64 对非入口 alloca 会逐块发
 * __chkstk+sub rsp, 循环回边不恢复, 长循环会泄漏栈) */
static LLVMValueRef cg_alloca(CwCodegen_t* g, LLVMTypeRef ty,
                              const char* name) {
    LLVMBasicBlockRef entry = LLVMGetEntryBasicBlock(g->current_fn);
    LLVMValueRef first = LLVMGetFirstInstruction(entry);
    if (first) {
        LLVMPositionBuilderBefore(g->alloca_builder, first);
    } else {
        LLVMPositionBuilderAtEnd(g->alloca_builder, entry);
    }
    return LLVMBuildAlloca(g->alloca_builder, ty, name);
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

static LLVMValueRef cg_f64(CwCodegen_t* g, double v) {
    return LLVMConstReal(LLVMDoubleTypeInContext(cg_ctx(g)), v);
}

static cw_value* cg_node_ann_type(cw_value* node) {
    if (!node || cw_typeof(node) != CW_OBJECT) return NULL;
    cw_value* ann = cw_object_get(node, "ann");
    if (!ann || cw_typeof(ann) != CW_OBJECT) return NULL;
    return cw_object_get(ann, "type");
}

static const char* cg_node_type_name(CwCodegen_t* g, cw_value* node) {
    cw_value* t = cg_node_ann_type(node);
    if (!t || cw_typeof(t) != CW_OBJECT) return NULL;
    return cg_type_name_of(g, t);
}

static const char* cg_json_name(cw_value* obj) {
    if (!obj || cw_typeof(obj) != CW_OBJECT) return NULL;
    cw_value* name = cw_object_get(obj, "name");
    return (name && cw_typeof(name) == CW_STRING)
        ? cw_string_cstr(name) : NULL;
}

/* 类型对象 → 基础名; 泛型实例上下文中把 opaque 参数叶替换成实参名 */
static const char* cg_type_name_of(CwCodegen_t* g, cw_value* type_obj) {
    const char* n = cg_json_name(type_obj);
    if (n && g->tcount > 0) {
        for (size_t i = 0; i < g->tcount; i++) {
            if (g->tparam_names[i]
                && strcmp(n, g->tparam_names[i]) == 0) {
                return cwtype_name(g->ll->types, g->targs[i]);
            }
        }
    }
    return n;
}

/* 类型对象 → CwTypeId; 泛型实例上下文中参数叶直接取实参 id */
static CwTypeId cg_type_id_of(CwCodegen_t* g, cw_value* type_obj) {
    const char* n = cg_json_name(type_obj);
    if (n && g->tcount > 0) {
        for (size_t i = 0; i < g->tcount; i++) {
            if (g->tparam_names[i] && strcmp(n, g->tparam_names[i]) == 0) {
                return g->targs[i];
            }
        }
    }
    return cwtype_from_json(g->ll->types, type_obj);
}

static const char* cg_json_kind(cw_value* obj) {
    if (!obj || cw_typeof(obj) != CW_OBJECT) return NULL;
    cw_value* k = cw_object_get(obj, "kind");
    return (k && cw_typeof(k) == CW_STRING) ? cw_string_cstr(k) : NULL;
}

static bool cg_json_bool(cw_value* obj, bool* out) {
    if (!obj || cw_typeof(obj) != CW_BOOL) return false;
    return cw_as_bool(obj, out) == CW_OK;
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

static bool cg_is_scalar(const char* name) {
    const int id = cg_type_id(name);
    return id == CWInt || id == CWUInt || id == CWInt8 || id == CWUInt8
        || id == CWInt32 || id == CWUInt32 || id == CWInt64
        || id == CWUInt64 || id == CWByte || id == CWFloat
        || id == CWFloat64 || id == CWBool;
}

static bool cg_is_int(const char* name) {
    const int id = cg_type_id(name);
    return id == CWInt || id == CWUInt || id == CWInt8 || id == CWUInt8
        || id == CWInt32 || id == CWUInt32 || id == CWInt64
        || id == CWUInt64 || id == CWByte;
}

static bool cg_is_unsigned(const char* name) {
    const int id = cg_type_id(name);
    return id == CWUInt || id == CWUInt8 || id == CWUInt32
        || id == CWUInt64 || id == CWByte;
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
    case CWInt32:
    case CWUInt32:
        if (size) *size = 4;
        return LLVMInt32TypeInContext(cg_ctx(g));
    case CWInt64:
    case CWUInt64:
        if (size) *size = 8;
        return LLVMInt64TypeInContext(cg_ctx(g));
    case CWFloat:
        if (size) *size = 4;
        return LLVMFloatTypeInContext(cg_ctx(g));
    case CWFloat64:
        if (size) *size = 8;
        return LLVMDoubleTypeInContext(cg_ctx(g));
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

/* 当前块是否已以 terminator 结尾 (return/br 之后不能再插指令) */
static bool cg_block_terminated(CwCodegen_t* g) {
    LLVMBasicBlockRef bb = LLVMGetInsertBlock(g->builder);
    return bb && LLVMGetBasicBlockTerminator(bb) != NULL;
}

/* 语句级调用: 若当前块已终止, 开一个死块继续发射 */
static void cg_ensure_block(CwCodegen_t* g) {
    if (!g->failed && cg_block_terminated(g)) {
        LLVMBasicBlockRef nb = LLVMAppendBasicBlockInContext(
            cg_ctx(g), g->current_fn, "dead");
        LLVMPositionBuilderAtEnd(cg_b(g), nb);
    }
}

static CwExpr cg_make_scalar(CwCodegen_t* g, LLVMValueRef value,
                             LLVMTypeRef value_type,
                             const char* type_name, size_t size) {
    LLVMValueRef storage = cg_alloca(g, value_type, "tmp");
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

static CwVar_t* cg_var_find(CwCodegen_t* g, const char* name);
static CwExpr cg_expr(CwCodegen_t* g, cw_value* node);

/* 生成变量名 (栈缓冲) 的稳定副本: 变量表只保存指针, 不能指向局部缓冲 */
static const char* cg_own_name(CwCodegen_t* g, const char* name) {
    if (g->owned_name_count == g->owned_name_cap) {
        const size_t nc = g->owned_name_cap
            ? g->owned_name_cap * 2 : 16;
        char** nn = (char**)realloc(
            g->owned_names, nc * sizeof(char*));
        if (!nn) {
            cg_error(g, "failed to grow the owned-name table");
            return name;
        }
        g->owned_names = nn;
        g->owned_name_cap = nc;
    }
    char* copy = (char*)malloc(strlen(name) + 1);
    if (!copy) {
        cg_error(g, "failed to allocate a generated variable name");
        return name;
    }
    /* 不用 strcpy: UCRT 把它标成 deprecated, 且 memcpy 同样安全
     * (malloc 已按 strlen+1 分配) */
    memcpy(copy, name, strlen(name) + 1);
    g->owned_names[g->owned_name_count++] = copy;
    return copy;
}

static void cg_free_owned_names(CwCodegen_t* g) {
    for (size_t i = 0; i < g->owned_name_count; i++) {
        free(g->owned_names[i]);
    }
    g->owned_name_count = 0;
}

/* 把表达式物化为 40 字节对象记录 (容器元素 / rt 调用参数用) */
static LLVMValueRef cg_materialize_record(CwCodegen_t* g, CwExpr e) {
    const int type_id = cg_type_id(e.type_name);
    if (type_id < 0) {
        cg_error(g, "user structs are not supported as container elements/rt arguments: %s",
                 e.type_name ? e.type_name : "?");
        return NULL;
    }
    LLVMValueRef rec = cg_alloca(g, g->ll->rec_type, "elem.rec");
    LLVMValueRef tid = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                           rec, 0, "tid");
    LLVMBuildStore(cg_b(g), cg_i32(g, (uint32_t)type_id), tid);
    LLVMValueRef gcnt = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                            rec, 1, "gc");
    LLVMBuildStore(cg_b(g), cg_i8(g, 0), gcnt);
    LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                            rec, 3, "h");
    LLVMBuildStore(cg_b(g), e.handle, hptr);
    return rec;
}

/* 取节点对应的容器记录: Name 用变量记录, 其它表达式临时物化 */
static LLVMValueRef cg_expr_record(CwCodegen_t* g, cw_value* node) {
    if (node && strcmp(cg_node_kind(node), "Name") == 0) {
        cw_value* parts = cw_object_get(node, "parts");
        if (parts && cw_typeof(parts) == CW_ARRAY && cw_array_size(parts) == 1) {
            const char* name = cw_string_cstr(cw_array_get(parts, 0));
            CwVar_t* v = cg_var_find(g, name);
            if (v) return v->record;
        }
    }
    CwExpr e = cg_expr(g, node);
    if (g->failed) return NULL;
    return cg_materialize_record(g, e);
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

/* 索引值 → i64: 按自身类型加载, 按符号性扩展 */
static LLVMValueRef cg_index_i64(CwCodegen_t* g, CwExpr idx) {
    LLVMValueRef iv = cg_load_value(
        g, idx, cg_scalar_type(g, idx.type_name, NULL));
    return cg_is_unsigned(idx.type_name)
        ? LLVMBuildZExt(cg_b(g), iv,
                        LLVMInt64TypeInContext(cg_ctx(g)), "idx")
        : LLVMBuildSExt(cg_b(g), iv,
                        LLVMInt64TypeInContext(cg_ctx(g)), "idx");
}

static LLVMValueRef cg_bool_cond(CwCodegen_t* g, CwExpr e) {
    LLVMValueRef v = cg_load_value(g, e, LLVMInt8TypeInContext(cg_ctx(g)));
    return LLVMBuildICmp(cg_b(g), LLVMIntNE, v, cg_i8(g, 0), "cond");
}

/* ---- 变量 ---- */

static const CwLayout_t* cg_struct_layout(CwCodegen_t* g,
                                          cw_value* type_obj);
static bool cg_is_struct_type(CwCodegen_t* g, const char* name);
static LLVMValueRef cg_blob_alloc(CwCodegen_t* g, size_t size,
                                  const char* name);
static LLVMValueRef cg_blob_i8(CwCodegen_t* g, LLVMValueRef blob);
static LLVMValueRef cg_expr_blob_i8(CwCodegen_t* g, CwExpr e);
static size_t cg_struct_blob_size(CwCodegen_t* g, const CwLayout_t* L);
static void cg_rebase_struct_fields(CwCodegen_t* g, LLVMValueRef base,
                                    const CwLayout_t* L);

static CwVar_t* cg_var_find(CwCodegen_t* g, const char* name) {
    for (size_t i = g->var_count; i > 0; i--) {
        CwVar_t* v = &g->vars[i - 1];
        if (v->scope <= g->scope_depth
            && strcmp(v->name, name) == 0) {
            return v;
        }
    }
    return NULL;
}

/* 只在当前作用域里查重: 允许模式绑定遮蔽外层变量 (Rust 风格) */
static bool cg_var_in_current_scope(CwCodegen_t* g, const char* name) {
    for (size_t i = g->var_count; i > 0; i--) {
        CwVar_t* v = &g->vars[i - 1];
        if (v->scope < g->scope_depth) return false;
        if (v->scope == g->scope_depth
            && strcmp(v->name, name) == 0) {
            return true;
        }
    }
    return false;
}

static bool cg_var_declare(CwCodegen_t* g, const char* name,
                           const char* type_name,
                           cw_value* type_obj) {
    if (cg_var_in_current_scope(g, name)) {
        cg_error(g, "duplicate variable: %s (scope %zu, count %zu)",
                 name, g->scope_depth, g->var_count);
        return false;
    }
    if (g->var_count == g->var_cap) {
        const size_t nc = g->var_cap ? g->var_cap * 2 : 16;
        CwVar_t* nv = (CwVar_t*)realloc(g->vars, nc * sizeof(CwVar_t));
        if (!nv) {
            cg_error(g, "failed to grow the variable table");
            return false;
        }
        g->vars = nv;
        g->var_cap = nc;
    }
    CwVar_t* v = &g->vars[g->var_count++];
    v->name = name;
    v->type_name = type_name;
    v->scope = g->scope_depth;
    v->is_enum = false;
    v->record = cg_alloca(g, g->ll->rec_type, name);
    v->blob = NULL;
    v->blob_size = 0;
    v->field_count = 0;
    v->layout = NULL;
    size_t size = 0;
    v->storage = cg_is_scalar(type_name)
        ? cg_alloca(g, cg_scalar_type(g, type_name, &size),
                          "v.storage")
        : NULL;
    if (type_obj && cg_is_struct_type(g, type_name)) {
        const CwLayout_t* L = cg_struct_layout(g, type_obj);
        if (!L) {
            cg_error(g, "unknown struct layout: %s", type_name);
            return false;
        }
        v->field_count = L->field_count;
        v->layout = L;
        v->blob_size = cg_struct_blob_size(g, L);
        v->blob = cg_blob_alloc(g, v->blob_size, "st.blob");
    }
    if (type_obj && cg_is_enum_type(g, type_name)) {
        v->is_enum = true;
        v->blob_size = cg_enum_blob_size(g, type_name);
        v->blob = cg_blob_alloc(g, v->blob_size, "en.blob");
    }
    return true;
}

static void cg_var_push_scope(CwCodegen_t* g) {
    if (g->scope_mark_count == g->scope_mark_cap) {
        const size_t nc = g->scope_mark_cap
            ? g->scope_mark_cap * 2 : 16;
        size_t* nm = (size_t*)realloc(
            g->scope_marks, nc * sizeof(size_t));
        if (!nm) {
            cg_error(g, "failed to grow the scope stack");
            return;
        }
        g->scope_marks = nm;
        g->scope_mark_cap = nc;
    }
    g->scope_marks[g->scope_mark_count++] = g->var_count;
    g->scope_depth++;
}

static void cg_var_pop_scope(CwCodegen_t* g) {
    if (g->scope_depth > 0 && g->scope_mark_count > 0) {
        /* 截断变量表: 兄弟作用域同名绑定不再互相干扰 */
        g->var_count = g->scope_marks[--g->scope_mark_count];
        g->scope_depth--;
    }
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
            cg_error(g, "unknown scalar type: %s", v->type_name);
            return false;
        }
        /* 先按源类型取值, 再转换到目标类型 (字面量现在是宽类型) */
        size_t esize = 0;
        LLVMTypeRef evt = cg_scalar_type(g, e.type_name, &esize);
        LLVMValueRef src = evt ? cg_load_value(g, e, evt) : e.handle;
        LLVMValueRef val = cg_convert_scalar(g, src, e.type_name,
                                             v->type_name);
        if (g->failed) return false;
        LLVMBuildStore(cg_b(g), val, v->storage);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), v->storage, LLVMInt64TypeInContext(cg_ctx(g)), "addr");
        LLVMValueRef obj = LLVMBuildPtrToInt(
            cg_b(g), v->record, LLVMInt64TypeInContext(cg_ctx(g)), "obj");
        handle = cg_build_handle(g, obj, addr, cg_i64(g, size), cg_i64(g, 0));
    } else if (v->is_enum) {
        /* 枚举: 浅拷 blob (载荷句柄已指向 arena/引用型持久值) */
        LLVMValueRef src = cg_expr_blob_i8(g, e);
        LLVMValueRef dst = cg_blob_i8(g, v->blob);
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1,
                        cg_i64(g, (uint64_t)v->blob_size));
        LLVMValueRef obj = LLVMBuildPtrToInt(
            cg_b(g), v->record, LLVMInt64TypeInContext(cg_ctx(g)), "obj");
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), v->blob, LLVMInt64TypeInContext(cg_ctx(g)), "addr");
        handle = cg_build_handle(g, obj, addr,
                                 cg_i64(g, cg_enum_slot_count(g, v->type_name)),
                                 cg_i64(g, 0));
    } else if (v->blob) {
        /* 用户结构体: 深拷贝实例 blob (Rust 值语义) */
        LLVMValueRef src = cg_expr_blob_i8(g, e);
        LLVMValueRef dst = cg_blob_i8(g, v->blob);
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1,
                        cg_i64(g, (uint64_t)v->blob_size));
        cg_rebase_struct_fields(g, dst, v->layout);
        LLVMValueRef obj = LLVMBuildPtrToInt(
            cg_b(g), v->record, LLVMInt64TypeInContext(cg_ctx(g)), "obj");
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), v->blob, LLVMInt64TypeInContext(cg_ctx(g)), "addr");
        handle = cg_build_handle(g, obj, addr,
                                 cg_i64(g, v->field_count), cg_i64(g, 0));
    } else {
        handle = e.handle; /* String / None: 引用语义 */
    }
    LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                            v->record, 3, "h");
    LLVMBuildStore(cg_b(g), handle, hptr);
    return true;
}

/* 直接以 LLVM 值写变量 (复合赋值用): 标量存值 + 更新 handle */
static bool cg_rec_store_value(CwCodegen_t* g, CwVar_t* v,
                               LLVMValueRef val,
                               const char* type_name) {
    if (!v->storage) {
        cg_error(g, "compound assignment supports scalars only: %s", v->name);
        return false;
    }
    size_t size = 0;
    LLVMTypeRef vt = cg_scalar_type(g, type_name, &size);
    if (!vt) {
        cg_error(g, "unknown scalar type: %s", type_name);
        return false;
    }
    LLVMBuildStore(cg_b(g), val, v->storage);
    LLVMValueRef addr = LLVMBuildPtrToInt(
        cg_b(g), v->storage, LLVMInt64TypeInContext(cg_ctx(g)), "addr");
    LLVMValueRef obj = LLVMBuildPtrToInt(
        cg_b(g), v->record, LLVMInt64TypeInContext(cg_ctx(g)), "obj");
    LLVMValueRef handle = cg_build_handle(g, obj, addr, cg_i64(g, size),
                                          cg_i64(g, 0));
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

/* ---- 用户结构体 ----
 * 实例 = 8 字节头 + field_count × 32 字节句柄槽 (与 CwLayout 偏移一致);
 * 值以句柄承载, handle.address 指向实例 blob。
 */

static const CwNode_t* cg_struct_decl(CwCodegen_t* g, const char* name) {
    if (!name) return NULL;
    const CwSymbol_t* sym = cwmodule_find_symbol(g->m, name);
    if (!sym || strcmp(sym->kind, "struct") != 0) return NULL;
    return cwmodule_node(g->m, sym->ref);
}

static bool cg_is_struct_type(CwCodegen_t* g, const char* name) {
    return cg_struct_decl(g, name) != NULL;
}

/* 取类型对象对应的实例布局 (name + args 查符号表与布局缓存) */
static const CwLayout_t* cg_struct_layout(CwCodegen_t* g,
                                          cw_value* type_obj) {
    if (!type_obj || cw_typeof(type_obj) != CW_OBJECT) return NULL;
    const char* name = cg_json_name(type_obj);
    const CwNode_t* decl = cg_struct_decl(g, name);
    if (!decl) return NULL;
    cw_value* args_v = cw_object_get(type_obj, "args");
    const size_t n = (args_v && cw_typeof(args_v) == CW_ARRAY)
        ? cw_array_size(args_v) : 0;
    CwTypeId* ids = n ? (CwTypeId*)malloc(n * sizeof(CwTypeId)) : NULL;
    if (n && !ids) return NULL;
    for (size_t i = 0; i < n; i++) {
        ids[i] = cg_type_id_of(g, cw_array_get(args_v, i));
        if (ids[i] == CW_TYPE_INVALID) {
            free(ids);
            return NULL;
        }
    }
    const CwLayout_t* L = cwlayout_get(g->ll->layouts, g->m, decl, ids, n);
    free(ids);
    return L;
}

static LLVMValueRef cg_blob_alloc(CwCodegen_t* g, size_t size,
                                  const char* name) {
    LLVMTypeRef arr = LLVMArrayType(LLVMInt8TypeInContext(cg_ctx(g)), size);
    return cg_alloca(g, arr, name);
}

static LLVMValueRef cg_blob_i8(CwCodegen_t* g, LLVMValueRef blob) {
    return LLVMBuildBitCast(cg_b(g), blob, cg_rt_i8_ptr(g), "");
}

/* 句柄槽指针: base 为 blob 字节指针, offset 为布局字段偏移 */
static LLVMValueRef cg_struct_slot(CwCodegen_t* g, LLVMValueRef base,
                                   size_t offset) {
    LLVMValueRef idx[1] = { cg_i64(g, (uint64_t)(8 + offset)) };
    LLVMValueRef p = LLVMBuildGEP2(cg_b(g),
                                   LLVMInt8TypeInContext(cg_ctx(g)),
                                   base, idx, 1, "f.slot");
    return LLVMBuildBitCast(cg_b(g), p,
                            LLVMPointerType(g->ll->handle_type, 0), "");
}

static LLVMValueRef cg_struct_handle(CwCodegen_t* g, LLVMValueRef blob,
                                     size_t fields) {
    LLVMValueRef addr = LLVMBuildPtrToInt(cg_b(g), blob,
                                          LLVMInt64TypeInContext(cg_ctx(g)),
                                          "st.addr");
    return cg_build_handle(g, cg_i64(g, 0), addr, cg_i64(g, fields),
                           cg_i64(g, 0));
}

/* 从表达式句柄取 blob 字节指针 */
static LLVMValueRef cg_expr_blob_i8(CwCodegen_t* g, CwExpr e) {
    return LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, e),
                             cg_rt_i8_ptr(g), "st.ptr");
}

/* 标量字段值内联进 blob 载荷区 (句柄槽之后), 使整块拷贝 = 深拷贝 */
static size_t cg_scalar_bytes(const char* name) {
    if (!name) return 0;
    if (strcmp(name, "Int") == 0 || strcmp(name, "UInt") == 0) return 2;
    if (strcmp(name, "Int8") == 0 || strcmp(name, "UInt8") == 0
        || strcmp(name, "Byte") == 0 || strcmp(name, "Bool") == 0) return 1;
    if (strcmp(name, "Int32") == 0 || strcmp(name, "UInt32") == 0) return 4;
    if (strcmp(name, "Int64") == 0 || strcmp(name, "UInt64") == 0
        || strcmp(name, "Float64") == 0) return 8;
    if (strcmp(name, "Float") == 0) return 4;
    return 0;
}

/* 整数宽度档: 同宽类型共享 rank (与前端 _INT_RANK 一致) */
static int cg_int_rank(const char* n) {
    if (!n) return -1;
    if (strcmp(n, "Int8") == 0 || strcmp(n, "UInt8") == 0
        || strcmp(n, "Byte") == 0) return 1;
    if (strcmp(n, "Int") == 0 || strcmp(n, "UInt") == 0) return 2;
    if (strcmp(n, "Int32") == 0 || strcmp(n, "UInt32") == 0) return 3;
    if (strcmp(n, "Int64") == 0 || strcmp(n, "UInt64") == 0) return 4;
    return -1;
}

/* 数值共同类型: 浮点优先, 整数按宽度/符号提升 (与前端 _common_numeric 一致) */
static const char* cg_common_numeric(const char* a, const char* b) {
    if (!a) return b;
    if (!b) return a;
    if (strcmp(a, b) == 0) return a;
    const bool af = strcmp(a, "Float") == 0 || strcmp(a, "Float64") == 0;
    const bool bf = strcmp(b, "Float") == 0 || strcmp(b, "Float64") == 0;
    if (af || bf) {
        if (strcmp(a, "Float64") == 0 || strcmp(b, "Float64") == 0) {
            return "Float64";
        }
        return "Float";
    }
    const int ra = cg_int_rank(a);
    const int rb = cg_int_rank(b);
    if (ra < 0 || rb < 0) return NULL;
    if (ra != rb) return ra > rb ? a : b;
    /* 同宽度: 两个无符号直接取其一; 否则提升到下一档有符号 */
    if (cg_is_unsigned(a) && cg_is_unsigned(b)) return a;
    if (ra <= 1) return "Int";
    if (ra <= 2) return "Int32";
    if (ra <= 3) return "Int64";
    return "Int64";
}

/* 标量值类型转换: 整数截断/符号扩展、浮点宽度、int↔float */
static LLVMValueRef cg_convert_scalar(CwCodegen_t* g, LLVMValueRef v,
                                      const char* from, const char* to) {
    if (!from || !to || strcmp(from, to) == 0) return v;
    if (cg_is_int(from) && cg_is_int(to)) {
        size_t fsz = 0, tsz = 0;
        cg_scalar_type(g, from, &fsz);
        LLVMTypeRef tt = cg_scalar_type(g, to, &tsz);
        if (fsz == tsz) return v;
        if (fsz < tsz) {
            return cg_is_unsigned(from)
                ? LLVMBuildZExt(cg_b(g), v, tt, "zext")
                : LLVMBuildSExt(cg_b(g), v, tt, "sext");
        }
        return LLVMBuildTrunc(cg_b(g), v, tt, "trunc");
    }
    const bool ff = strcmp(from, "Float") == 0
        || strcmp(from, "Float64") == 0;
    const bool tf = strcmp(to, "Float") == 0
        || strcmp(to, "Float64") == 0;
    if (ff && tf) {
        size_t fsz = 0, tsz = 0;
        cg_scalar_type(g, from, &fsz);
        LLVMTypeRef tt = cg_scalar_type(g, to, &tsz);
        if (fsz == tsz) return v;
        if (fsz < tsz) return LLVMBuildFPExt(cg_b(g), v, tt, "fpext");
        return LLVMBuildFPTrunc(cg_b(g), v, tt, "fptrunc");
    }
    if (cg_is_int(from) && tf) {
        LLVMTypeRef tt = cg_scalar_type(g, to, NULL);
        return cg_is_unsigned(from)
            ? LLVMBuildUIToFP(cg_b(g), v, tt, "uitofp")
            : LLVMBuildSIToFP(cg_b(g), v, tt, "sitofp");
    }
    if (ff && cg_is_int(to)) {
        LLVMTypeRef tt = cg_scalar_type(g, to, NULL);
        return cg_is_unsigned(to)
            ? LLVMBuildFPToUI(cg_b(g), v, tt, "fptoui")
            : LLVMBuildFPToSI(cg_b(g), v, tt, "fptosi");
    }
    cg_error(g, "unsupported type conversion: %s -> %s",
             from ? from : "?", to ? to : "?");
    return NULL;
}

/* 把标量表达式物化为目标类型: 类型不同则转换并生成目标宽度的新临时。
 * 调用参数 / 容器元素 / 结构体字段等“按目标类型落槽”的场景必须用它,
 * 否则窄字面量存储被按宽类型读取会读到相邻内存。 */
static CwExpr cg_coerce_scalar(CwCodegen_t* g, CwExpr e, const char* want) {
    if (!want || !e.type_name || strcmp(want, e.type_name) == 0) return e;
    if (!cg_is_scalar(e.type_name) || !cg_is_scalar(want)) return e;
    size_t wsize = 0;
    LLVMTypeRef wvt = cg_scalar_type(g, want, &wsize);
    LLVMValueRef src = cg_load_value(
        g, e, cg_scalar_type(g, e.type_name, NULL));
    LLVMValueRef v = cg_convert_scalar(g, src, e.type_name, want);
    if (g->failed) return e;
    return cg_make_scalar(g, v, wvt, want, wsize);
}

/* 接收者泛型实参名 (Vector<Int> -> arg0="Int", Map<K,V> -> arg1="V") */
static const char* cg_receiver_arg(CwCodegen_t* g, cw_value* objv,
                                   size_t idx) {
    cw_value* t = cg_node_ann_type(objv);
    if (!t) return NULL;
    cw_value* args = cw_object_get(t, "args");
    if (!args || cw_typeof(args) != CW_ARRAY
        || cw_array_size(args) <= idx) {
        return NULL;
    }
    return cg_type_name_of(g, cw_array_get(args, idx));
}

/* 第 i 个字段的载荷偏移 (按字段大小对齐) */
static size_t cg_field_payload_offset(CwCodegen_t* g, const CwLayout_t* L,
                                      size_t i) {
    size_t off = 8 + L->field_count * CWLAYOUT_SLOT_SIZE;
    for (size_t j = 0; j < i; j++) {
        const size_t vsz = cg_scalar_bytes(
            cwtype_name(g->ll->types, L->fields[j].type));
        if (vsz == 0) continue;
        off = (off + vsz - 1) & ~(vsz - 1);
        off += vsz;
    }
    return off;
}

/* 结构体 blob 总字节数: 头 + 句柄槽 + 标量载荷 */
static size_t cg_struct_blob_size(CwCodegen_t* g, const CwLayout_t* L) {
    size_t off = 8 + L->field_count * CWLAYOUT_SLOT_SIZE;
    for (size_t i = 0; i < L->field_count; i++) {
        const size_t vsz = cg_scalar_bytes(
            cwtype_name(g->ll->types, L->fields[i].type));
        if (vsz == 0) continue;
        off = (off + vsz - 1) & ~(vsz - 1);
        off += vsz;
    }
    return off;
}

/* 写一个字段: 标量内联进载荷并生成自指句柄, 引用类型直接存句柄 */
static void cg_store_struct_field(CwCodegen_t* g, LLVMValueRef base,
                                  const CwLayout_t* L, size_t i,
                                  CwExpr val) {
    LLVMValueRef slot = cg_struct_slot(g, base, L->fields[i].offset);
    const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
    const size_t vsz = cg_scalar_bytes(ft);
    if (vsz == 0) {
        LLVMBuildStore(cg_b(g), val.handle, slot);
        return;
    }
    const size_t poff = cg_field_payload_offset(g, L, i);
    LLVMValueRef idx[1] = { cg_i64(g, (uint64_t)poff) };
    LLVMValueRef p = LLVMBuildGEP2(cg_b(g),
                                   LLVMInt8TypeInContext(cg_ctx(g)),
                                   base, idx, 1, "f.val");
    LLVMTypeRef vt = cg_scalar_type(g, ft, NULL);
    LLVMValueRef v = cg_load_value(g, val, vt);
    LLVMBuildStore(cg_b(g), v, LLVMBuildBitCast(
        cg_b(g), p, LLVMPointerType(vt, 0), ""));
    LLVMValueRef addr = LLVMBuildPtrToInt(cg_b(g), p,
                                          LLVMInt64TypeInContext(cg_ctx(g)),
                                          "f.addr");
    LLVMValueRef h = cg_build_handle(g, cg_i64(g, 0), addr,
                                     cg_i64(g, vsz), cg_i64(g, 0));
    LLVMBuildStore(cg_b(g), h, slot);
}

/* 深拷贝后重定向标量字段句柄: 源 blob 内的自指地址要改成目标 blob 载荷 */
static void cg_rebase_struct_fields(CwCodegen_t* g, LLVMValueRef base,
                                    const CwLayout_t* L) {
    for (size_t i = 0; i < L->field_count; i++) {
        const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
        const size_t vsz = cg_scalar_bytes(ft);
        if (vsz == 0) continue;
        const size_t poff = cg_field_payload_offset(g, L, i);
        LLVMValueRef idx[1] = { cg_i64(g, (uint64_t)poff) };
        LLVMValueRef p = LLVMBuildGEP2(cg_b(g),
                                       LLVMInt8TypeInContext(cg_ctx(g)),
                                       base, idx, 1, "f.pay");
        LLVMValueRef addr = LLVMBuildPtrToInt(cg_b(g), p,
                                              LLVMInt64TypeInContext(cg_ctx(g)),
                                              "f.addr");
        LLVMValueRef h = cg_build_handle(g, cg_i64(g, 0), addr,
                                         cg_i64(g, vsz), cg_i64(g, 0));
        LLVMValueRef slot = cg_struct_slot(g, base, L->fields[i].offset);
        LLVMBuildStore(cg_b(g), h, slot);
    }
}

/* ---- 枚举 (Rust 风格带值 enum) ----
 * 实例 blob 布局 (对所有变体统一):
 *   8B 头 + i32 tag (偏移 8) + 4B pad + max_payloads × 32B 句柄槽;
 * 标量/结构体载荷拷进进程期 arena (跨函数存活), 复制只需浅拷 blob;
 * String/容器/嵌套枚举载荷直接存句柄 (其内容已持久)。
 */

#define CWENUM_TAG_OFFSET 8
#define CWENUM_SLOTS_OFFSET 16

static const CwNode_t* cg_enum_decl(CwCodegen_t* g, const char* name) {
    if (!name) return NULL;
    const CwSymbol_t* sym = cwmodule_find_symbol(g->m, name);
    if (!sym || strcmp(sym->kind, "enum") != 0) return NULL;
    return cwmodule_node(g->m, sym->ref);
}

static bool cg_is_enum_type(CwCodegen_t* g, const char* name) {
    return cg_enum_decl(g, name) != NULL;
}

static size_t cg_enum_max_payloads(CwCodegen_t* g, const CwNode_t* decl) {
    if (!decl) return 0;
    cw_value* variants = cw_object_get(decl->value, "variants");
    size_t max = 0;
    if (variants && cw_typeof(variants) == CW_ARRAY) {
        const size_t n = cw_array_size(variants);
        for (size_t i = 0; i < n; i++) {
            cw_value* v = cw_array_get(variants, i);
            cw_value* fields = v ? cw_object_get(v, "fields") : NULL;
            const size_t nf = (fields && cw_typeof(fields) == CW_ARRAY)
                ? cw_array_size(fields) : 0;
            if (nf > max) max = nf;
        }
    }
    return max;
}

static size_t cg_enum_blob_size(CwCodegen_t* g, const char* name) {
    const CwNode_t* decl = cg_enum_decl(g, name);
    if (!decl) return 0;
    return CWENUM_SLOTS_OFFSET
        + cg_enum_max_payloads(g, decl) * CWLAYOUT_SLOT_SIZE;
}

static size_t cg_enum_slot_count(CwCodegen_t* g, const char* name) {
    const CwNode_t* decl = cg_enum_decl(g, name);
    return 1 + cg_enum_max_payloads(g, decl);
}

static bool cg_enum_variant_index(CwCodegen_t* g, const CwNode_t* decl,
                                  const char* vname, size_t* out) {
    if (!decl || !vname || !out) return false;
    cw_value* variants = cw_object_get(decl->value, "variants");
    if (!variants || cw_typeof(variants) != CW_ARRAY) return false;
    const size_t n = cw_array_size(variants);
    for (size_t i = 0; i < n; i++) {
        cw_value* v = cw_array_get(variants, i);
        const char* vn = v ? cg_json_name(v) : NULL;
        if (vn && strcmp(vn, vname) == 0) {
            *out = i;
            return true;
        }
    }
    return false;
}

static LLVMValueRef cg_enum_tag_ptr(CwCodegen_t* g, LLVMValueRef base) {
    LLVMValueRef idx[1] = { cg_i64(g, CWENUM_TAG_OFFSET) };
    LLVMValueRef p = LLVMBuildGEP2(
        cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), base, idx, 1, "e.tag");
    return LLVMBuildBitCast(
        cg_b(g), p,
        LLVMPointerType(LLVMInt32TypeInContext(cg_ctx(g)), 0), "");
}

static LLVMValueRef cg_enum_slot(CwCodegen_t* g, LLVMValueRef base,
                                 size_t i) {
    LLVMValueRef idx[1] = {
        cg_i64(g, CWENUM_SLOTS_OFFSET + i * CWLAYOUT_SLOT_SIZE)
    };
    LLVMValueRef p = LLVMBuildGEP2(
        cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), base, idx, 1, "e.slot");
    return LLVMBuildBitCast(
        cg_b(g), p, LLVMPointerType(g->ll->handle_type, 0), "");
}

static LLVMValueRef cg_enum_handle(CwCodegen_t* g, LLVMValueRef blob,
                                   const char* enum_name) {
    LLVMValueRef addr = LLVMBuildPtrToInt(
        cg_b(g), blob, LLVMInt64TypeInContext(cg_ctx(g)), "en.addr");
    return cg_build_handle(
        g, cg_i64(g, 0), addr,
        cg_i64(g, cg_enum_slot_count(g, enum_name)), cg_i64(g, 0));
}

/* 调 rt arena 分配 (枚举载荷持久单元) */
static LLVMValueRef cg_rt_arena_alloc(CwCodegen_t* g, LLVMValueRef size) {
    LLVMTypeRef pt[1] = { LLVMInt64TypeInContext(cg_ctx(g)) };
    LLVMValueRef f = cg_rt_declare(
        g, "cwrt_arena_alloc", cg_rt_i8_ptr(g), pt, 1);
    LLVMValueRef av[1] = { size };
    return LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 1,
                          "en.cell");
}

/* 把枚举载荷物化为持久句柄: 标量/结构体拷进 arena 单元, 引用类型原样 */
static LLVMValueRef cg_enum_payload_handle(CwCodegen_t* g, CwExpr val,
                                           cw_value* type_obj) {
    const char* t = val.type_name;
    const size_t vsz = cg_scalar_bytes(t);
    if (vsz > 0) {
        LLVMValueRef cell = cg_rt_arena_alloc(g, cg_i64(g, (uint64_t)vsz));
        LLVMValueRef p = LLVMBuildIntToPtr(cg_b(g), cell, cg_rt_i8_ptr(g),
                                           "en.cell.p");
        LLVMTypeRef vt = cg_scalar_type(g, t, NULL);
        LLVMValueRef v = cg_load_value(g, val, vt);
        LLVMBuildStore(cg_b(g), v, LLVMBuildBitCast(
            cg_b(g), p, LLVMPointerType(vt, 0), ""));
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "en.cell.addr");
        return cg_build_handle(g, cg_i64(g, 0), addr,
                               cg_i64(g, vsz), cg_i64(g, 0));
    }
    if (cg_is_struct_type(g, t)) {
        const CwLayout_t* L = cg_struct_layout(g, type_obj);
        if (!L) {
            cg_error(g, "enum payload has an unknown struct layout: %s", t);
            return NULL;
        }
        const size_t size = cg_struct_blob_size(g, L);
        LLVMValueRef cell = cg_rt_arena_alloc(g, cg_i64(g, (uint64_t)size));
        LLVMValueRef dst = LLVMBuildIntToPtr(cg_b(g), cell, cg_rt_i8_ptr(g),
                                             "en.cell.p");
        LLVMValueRef src = cg_expr_blob_i8(g, val);
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1, cg_i64(g, (uint64_t)size));
        cg_rebase_struct_fields(g, dst, L);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), dst, LLVMInt64TypeInContext(cg_ctx(g)), "en.cell.addr");
        return cg_build_handle(g, cg_i64(g, 0), addr,
                               cg_i64(g, L->field_count), cg_i64(g, 0));
    }
    if (cg_is_enum_type(g, t)) {
        const size_t size = cg_enum_blob_size(g, t);
        LLVMValueRef cell = cg_rt_arena_alloc(g, cg_i64(g, (uint64_t)size));
        LLVMValueRef dst = LLVMBuildIntToPtr(cg_b(g), cell, cg_rt_i8_ptr(g),
                                             "en.cell.p");
        LLVMValueRef src = cg_expr_blob_i8(g, val);
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1, cg_i64(g, (uint64_t)size));
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), dst, LLVMInt64TypeInContext(cg_ctx(g)), "en.cell.addr");
        return cg_build_handle(g, cg_i64(g, 0), addr,
                               cg_i64(g, cg_enum_slot_count(g, t)),
                               cg_i64(g, 0));
    }
    return val.handle; /* String / Vector / Map / Set / None: 引用语义 */
}

/* 构造枚举实例: 写 tag + 载荷槽, 返回指向新 blob 的句柄 */
static CwExpr cg_expr_enum_build(CwCodegen_t* g, const char* enum_name,
                                 size_t variant_index,
                                 cw_value* payload_types,
                                 cw_value* args) {
    const CwNode_t* decl = cg_enum_decl(g, enum_name);
    if (!decl) {
        cg_error(g, "unknown enum: %s", enum_name ? enum_name : "?");
        return (CwExpr){ NULL, NULL };
    }
    const size_t size = cg_enum_blob_size(g, enum_name);
    LLVMValueRef blob = cg_blob_alloc(g, size, "en.blob");
    LLVMValueRef base = cg_blob_i8(g, blob);
    LLVMBuildStore(cg_b(g), cg_i32(g, (uint32_t)variant_index),
                   cg_enum_tag_ptr(g, base));
    const size_t nargs = (args && cw_typeof(args) == CW_ARRAY)
        ? cw_array_size(args) : 0;
    for (size_t i = 0; i < nargs && !g->failed; i++) {
        cw_value* arg = cw_array_get(args, i);
        CwExpr val = cg_expr(g, cw_object_get(arg, "value"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        cw_value* ti = (payload_types && cw_typeof(payload_types) == CW_ARRAY
                        && cw_array_size(payload_types) > i)
            ? cw_array_get(payload_types, i) : NULL;
        const char* want = ti ? cg_type_name_of(g, ti) : NULL;
        if (want) val = cg_coerce_scalar(g, val, want);
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef h = cg_enum_payload_handle(g, val, ti);
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMBuildStore(cg_b(g), h, cg_enum_slot(g, base, i));
    }
    return (CwExpr){ cg_enum_handle(g, blob, enum_name), enum_name };
}

/* ---- 静态字段 (Struct::field) ----
 * 静态字段不属于实例布局, 值保存在模块级全局存储里, 由 main 包装在调用
 * 用户 main 之前统一初始化。读取时返回指向全局存储的句柄。 */

static cw_value* cg_static_field(CwCodegen_t* g, const char* owner,
                                 const char* fname,
                                 cw_value** type_out) {
    const CwNode_t* decl = cg_struct_decl(g, owner);
    if (!decl) return NULL;
    cw_value* fields = cw_object_get(decl->value, "fields");
    if (!fields || cw_typeof(fields) != CW_ARRAY) return NULL;
    const size_t n = cw_array_size(fields);
    for (size_t i = 0; i < n; i++) {
        cw_value* f = cw_array_get(fields, i);
        bool is_static = false;
        cg_json_bool(cw_object_get(f, "static"), &is_static);
        if (!is_static) continue;
        const char* fn = cg_json_name(f);
        if (!fn || strcmp(fn, fname) != 0) continue;
        cw_value* t = cw_object_get(f, "type");
        cw_value* ann = cw_object_get(f, "ann");
        cw_value* at = ann ? cw_object_get(ann, "type") : NULL;
        if (at && cw_typeof(at) == CW_OBJECT) t = at;
        if (type_out) *type_out = t;
        return f;
    }
    return NULL;
}

/* Self:: 在方法体内指代当前所属 struct */
static const char* cg_static_owner(CwCodegen_t* g, const char* owner) {
    if (owner && strcmp(owner, "Self") == 0 && g->current_owner) {
        return g->current_owner;
    }
    return owner;
}

static LLVMValueRef cg_static_storage(CwCodegen_t* g, const char* owner,
                                      const char* fname,
                                      const char* type_name,
                                      cw_value* type_obj) {
    char gname[256];
    if (cg_is_scalar(type_name)) {
        LLVMTypeRef vt = cg_scalar_type(g, type_name, NULL);
        if (!vt) return NULL;
        snprintf(gname, sizeof(gname), "cwind.static.%s.%s.val",
                 owner, fname);
        LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
        if (gv) return gv;
        gv = LLVMAddGlobal(g->ll->module, vt, gname);
        LLVMSetInitializer(gv, LLVMConstNull(vt));
        return gv;
    }
    if (cg_is_struct_type(g, type_name)) {
        const CwLayout_t* L = cg_struct_layout(g, type_obj);
        if (!L) return NULL;
        snprintf(gname, sizeof(gname), "cwind.static.%s.%s.blob",
                 owner, fname);
        LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
        if (gv) return gv;
        LLVMTypeRef arr = LLVMArrayType(
            LLVMInt8TypeInContext(cg_ctx(g)),
            cg_struct_blob_size(g, L));
        gv = LLVMAddGlobal(g->ll->module, arr, gname);
        LLVMSetInitializer(gv, LLVMConstNull(arr));
        return gv;
    }
    snprintf(gname, sizeof(gname), "cwind.static.%s.%s.rec",
             owner, fname);
    LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
    if (gv) return gv;
    gv = LLVMAddGlobal(g->ll->module, g->ll->rec_type, gname);
    LLVMSetInitializer(gv, LLVMConstNull(g->ll->rec_type));
    return gv;
}

/* 把表达式值写进静态字段的全局存储 */
static bool cg_static_store(CwCodegen_t* g, const char* owner,
                            const char* fname, CwExpr e,
                            const char* type_name,
                            cw_value* type_obj) {
    if (cg_is_scalar(type_name)) {
        LLVMValueRef gv = cg_static_storage(
            g, owner, fname, type_name, type_obj);
        if (!gv) return false;
        e = cg_coerce_scalar(g, e, type_name);
        if (g->failed) return false;
        LLVMValueRef v = cg_load_value(
            g, e, cg_scalar_type(g, type_name, NULL));
        LLVMBuildStore(cg_b(g), v, gv);
        return true;
    }
    if (cg_is_struct_type(g, type_name)) {
        const CwLayout_t* L = cg_struct_layout(g, type_obj);
        LLVMValueRef gb = cg_static_storage(
            g, owner, fname, type_name, type_obj);
        if (!L || !gb) return false;
        LLVMValueRef src = cg_expr_blob_i8(g, e);
        LLVMValueRef dst = cg_blob_i8(g, gb);
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1,
                        cg_i64(g, (uint64_t)cg_struct_blob_size(g, L)));
        cg_rebase_struct_fields(g, dst, L);
        return true;
    }
    LLVMValueRef gr = cg_static_storage(
        g, owner, fname, type_name, type_obj);
    if (!gr) return false;
    LLVMValueRef rec = cg_materialize_record(g, e);
    if (!rec) return false;
    LLVMValueRef gr8 = LLVMBuildBitCast(cg_b(g), gr, cg_rt_i8_ptr(g), "");
    LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g), "");
    LLVMBuildMemCpy(cg_b(g), gr8, 1, rec8, 1,
                    cg_i64(g, CWIND_OBJECT_RECORD_SIZE));
    return true;
}

static LLVMValueRef cg_static_init_fn(CwCodegen_t* g, const char* owner,
                                      const char* fname, cw_value* field,
                                      const char* type_name,
                                      cw_value* type_obj) {
    char fname_buf[256];
    snprintf(fname_buf, sizeof(fname_buf),
             "cwind.static.%s.%s.init", owner, fname);
    LLVMValueRef existing = LLVMGetNamedFunction(g->ll->module, fname_buf);
    if (existing) return existing;

    LLVMTypeRef ft = LLVMFunctionType(
        LLVMVoidTypeInContext(cg_ctx(g)), NULL, 0, false);
    LLVMValueRef fn = LLVMAddFunction(g->ll->module, fname_buf, ft);
    LLVMBasicBlockRef saved_block = LLVMGetInsertBlock(g->builder);
    LLVMValueRef saved_fn = g->current_fn;
    g->current_fn = fn;
    LLVMBasicBlockRef entry = LLVMAppendBasicBlockInContext(
        cg_ctx(g), fn, "entry");
    LLVMPositionBuilderAtEnd(cg_b(g), entry);

    cw_value* init = field ? cw_object_get(field, "initializer") : NULL;
    if (init && cw_typeof(init) == CW_OBJECT && !g->failed) {
        CwExpr e = cg_expr(g, init);
        if (!g->failed) {
            cg_static_store(g, owner, fname, e, type_name, type_obj);
        }
    }
    if (!g->failed) LLVMBuildRetVoid(cg_b(g));

    g->current_fn = saved_fn;
    if (saved_block) LLVMPositionBuilderAtEnd(cg_b(g), saved_block);
    return fn;
}

static CwExpr cg_static_read(CwCodegen_t* g, const char* owner,
                             const char* fname, const char* type_name,
                             cw_value* type_obj) {
    if (cg_is_scalar(type_name)) {
        LLVMValueRef gv = cg_static_storage(
            g, owner, fname, type_name, type_obj);
        if (!gv) return (CwExpr){ NULL, NULL };
        size_t size = 0;
        cg_scalar_type(g, type_name, &size);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), gv, LLVMInt64TypeInContext(cg_ctx(g)), "st.addr");
        return (CwExpr){
            cg_build_handle(g, cg_i64(g, 0), addr,
                            cg_i64(g, size), cg_i64(g, 0)),
            type_name,
        };
    }
    if (cg_is_struct_type(g, type_name)) {
        const CwLayout_t* L = cg_struct_layout(g, type_obj);
        LLVMValueRef gb = cg_static_storage(
            g, owner, fname, type_name, type_obj);
        if (!L || !gb) return (CwExpr){ NULL, NULL };
        return (CwExpr){ cg_struct_handle(g, gb, L->field_count),
                         type_name };
    }
    LLVMValueRef gr = cg_static_storage(
        g, owner, fname, type_name, type_obj);
    if (!gr) return (CwExpr){ NULL, NULL };
    LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                          gr, 3, "st.h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                    hp, "st.vh");
    return (CwExpr){ h, type_name };
}

/* ---- 表达式 ---- */

static CwExpr cg_expr(CwCodegen_t* g, cw_value* node);

/* 把一段字节建成全局字符串常量, 返回 String 句柄 (address -> 全局变量) */
static CwExpr cg_string_lit(CwCodegen_t* g, const char* s, size_t len) {
    static unsigned str_seq = 0;
    char gname[32];
    snprintf(gname, sizeof(gname), ".str.%u", str_seq++);
    LLVMValueRef gv = LLVMAddGlobal(
        g->ll->module,
        LLVMArrayType(LLVMInt8TypeInContext(cg_ctx(g)), len + 1),
        gname);
    LLVMSetInitializer(
        gv, LLVMConstStringInContext(cg_ctx(g), s, len, false));
    LLVMValueRef addr = LLVMBuildPtrToInt(
        cg_b(g), gv, LLVMInt64TypeInContext(cg_ctx(g)), "s.addr");
    return (CwExpr){
        cg_build_handle(g, cg_i64(g, 0), addr, cg_i64(g, len),
                        cg_i64(g, 0)),
        "String",
    };
}

static CwExpr cg_expr_lit(CwCodegen_t* g, cw_value* node) {
    const char* kind = cg_node_kind(node);
    if (strcmp(kind, "IntLit") == 0) {
        cw_value* v = cw_object_get(node, "value");
        int64_t iv = 0;
        uint64_t uv = 0;
        if (v && cw_as_int(v, &iv) == CW_OK) {
            uv = (uint64_t)iv;
        } else {
            /* 超过 int64 时 stl JSON DOM 把数字降成 double (丢精度);
             * 改用字面量的 raw 原始文本精确解析 (u64 语义) */
            cw_value* raw = cw_object_get(node, "raw");
            const char* rs = raw ? cw_string_cstr(raw) : NULL;
            if (!rs || rs[0] == '\0') {
                double dv = 0;
                if (!v || cw_as_double(v, &dv) != CW_OK || !isfinite(dv)) {
                    cg_error(g, "IntLit is missing a valid value/raw");
                    return (CwExpr){ NULL, NULL };
                }
                uv = (uint64_t)dv;
                iv = (int64_t)uv;
            } else if (rs[0] == '-') {
                /* 防御: 手写 JSON / 未来解析器可能把负号并入 raw */
                iv = strtoll(rs, NULL, 10);
                uv = (uint64_t)iv;
            } else {
                uv = strtoull(rs, NULL, 10);
                iv = (int64_t)uv;
            }
        }
        /* 小字面量保持 Int 语义 (type_of/容器记录不变), 大数自动宽化 */
        if (iv >= -32768 && iv <= 32767) {
            return cg_make_scalar(g, cg_i16(g, iv),
                                  LLVMInt16TypeInContext(cg_ctx(g)),
                                  "Int", 2);
        }
        return cg_make_scalar(g, cg_i64(g, uv),
                              LLVMInt64TypeInContext(cg_ctx(g)), "Int64", 8);
    }
    if (strcmp(kind, "FloatLit") == 0) {
        cw_value* v = cw_object_get(node, "value");
        double dv = 0;
        if (!v || cw_as_double(v, &dv) != CW_OK) {
            cg_error(g, "FloatLit is missing value");
            return (CwExpr){ NULL, NULL };
        }
        /* f32 可精确表示时保持 Float, 否则用 Float64 保真 */
        const float fv = (float)dv;
        if ((double)fv == dv) {
            return cg_make_scalar(g,
                                  LLVMConstReal(LLVMFloatTypeInContext(cg_ctx(g)),
                                                fv),
                                  LLVMFloatTypeInContext(cg_ctx(g)),
                                  "Float", 4);
        }
        return cg_make_scalar(g, cg_f64(g, dv),
                              LLVMDoubleTypeInContext(cg_ctx(g)), "Float64", 8);
    }
    if (strcmp(kind, "BoolLit") == 0) {
        cw_value* v = cw_object_get(node, "value");
        bool bv = false;
        if (!v || cw_as_bool(v, &bv) != CW_OK) {
            cg_error(g, "BoolLit is missing value");
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
            cg_error(g, "StrLit is missing value");
            return (CwExpr){ NULL, NULL };
        }
        return cg_string_lit(g, s, len);
    }
    if (strcmp(kind, "VectorLit") == 0) {
        LLVMValueRef rec = cg_alloca(g, g->ll->rec_type,
                                           "vec.rec");
        LLVMValueRef tid = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                               rec, 0, "tid");
        LLVMBuildStore(cg_b(g), cg_i32(g, CWVector), tid);
        LLVMValueRef gcnt = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                rec, 1, "gc");
        LLVMBuildStore(cg_b(g), cg_i8(g, 0), gcnt);
        /* cwvec_init 要求 handle.address == 0, 先清零句柄 */
        LLVMValueRef hz = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                              rec, 3, "hz");
        LLVMBuildStore(cg_b(g), cg_null_handle(g), hz);
        LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g),
                                             "");
        LLVMTypeRef pr_init[2] = { cg_rt_i8_ptr(g),
                                   LLVMInt64TypeInContext(cg_ctx(g)) };
        LLVMValueRef init = cg_rt_declare(g, "cwvec_init",
                                          LLVMInt1TypeInContext(cg_ctx(g)),
                                          pr_init, 2);
        LLVMValueRef init_args[2] = { rec8, cg_i64(g, 0) };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(init), init,
                       init_args, 2, "");
        LLVMTypeRef pr_push[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
        LLVMValueRef push = cg_rt_declare(g, "cwvec_push",
                                          LLVMInt1TypeInContext(cg_ctx(g)),
                                          pr_push, 2);
        cw_value* elems = cw_object_get(node, "elems");
        const size_t ne = (elems && cw_typeof(elems) == CW_ARRAY)
            ? cw_array_size(elems) : 0;
        cw_value* ann = cw_object_get(node, "ann");
        cw_value* et = ann ? cw_object_get(ann, "element_type") : NULL;
        const char* elem_name = et ? cg_type_name_of(g, et) : NULL;
        for (size_t i = 0; i < ne && !g->failed; i++) {
            CwExpr e = cg_expr(g, cw_array_get(elems, i));
            if (g->failed) return (CwExpr){ NULL, NULL };
            e = cg_coerce_scalar(g, e, elem_name);
            LLVMValueRef er = cg_materialize_record(g, e);
            LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                cg_rt_i8_ptr(g), "");
            LLVMValueRef pargs[2] = { rec8, er8 };
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(push), push,
                           pargs, 2, "");
        }
        LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                rec, 3, "h");
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hptr,
                                        "vh");
        CwExpr e = { h, "Vector" };
        return e;
    }
    if (strcmp(kind, "MapLit") == 0) {
        LLVMValueRef rec = cg_alloca(g, g->ll->rec_type,
                                           "map.rec");
        LLVMValueRef tid = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                               rec, 0, "tid");
        LLVMBuildStore(cg_b(g), cg_i32(g, CWMap), tid);
        LLVMValueRef gcnt = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                rec, 1, "gc");
        LLVMBuildStore(cg_b(g), cg_i8(g, 0), gcnt);
        /* cwmap_init 要求 handle.address == 0, 先清零句柄 */
        LLVMValueRef hz = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                              rec, 3, "hz");
        LLVMBuildStore(cg_b(g), cg_null_handle(g), hz);
        LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g),
                                             "");
        LLVMTypeRef pr_init[1] = { cg_rt_i8_ptr(g) };
        LLVMValueRef init = cg_rt_declare(g, "cwmap_init",
                                          LLVMInt1TypeInContext(cg_ctx(g)),
                                          pr_init, 1);
        LLVMValueRef init_args[1] = { rec8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(init), init,
                       init_args, 1, "");
        LLVMTypeRef pr_put[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                  cg_rt_i8_ptr(g) };
        LLVMValueRef put = cg_rt_declare(g, "cwmap_put",
                                         LLVMInt1TypeInContext(cg_ctx(g)),
                                         pr_put, 3);
        cw_value* entries = cw_object_get(node, "entries");
        const size_t ne = (entries && cw_typeof(entries) == CW_ARRAY)
            ? cw_array_size(entries) : 0;
        for (size_t i = 0; i < ne && !g->failed; i++) {
            cw_value* entry = cw_array_get(entries, i);
            cw_value* eann = cw_object_get(entry, "ann");
            cw_value* kt = eann ? cw_object_get(eann, "key_type") : NULL;
            cw_value* vt = eann ? cw_object_get(eann, "value_type") : NULL;
            const char* kn = kt ? cg_type_name_of(g, kt) : NULL;
            const char* vn = vt ? cg_type_name_of(g, vt) : NULL;
            CwExpr k = cg_expr(g, cw_object_get(entry, "key"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            CwExpr v = cg_expr(g, cw_object_get(entry, "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            k = cg_coerce_scalar(g, k, kn);
            v = cg_coerce_scalar(g, v, vn);
            LLVMValueRef kr = cg_materialize_record(g, k);
            LLVMValueRef vr = cg_materialize_record(g, v);
            LLVMValueRef kr8 = LLVMBuildBitCast(cg_b(g), kr,
                                                cg_rt_i8_ptr(g), "");
            LLVMValueRef vr8 = LLVMBuildBitCast(cg_b(g), vr,
                                                cg_rt_i8_ptr(g), "");
            LLVMValueRef pargs[3] = { rec8, kr8, vr8 };
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(put), put,
                           pargs, 3, "");
        }
        LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                rec, 3, "h");
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hptr,
                                        "vh");
        CwExpr e = { h, "Map" };
        return e;
    }
    if (strcmp(kind, "TupleLit") == 0) {
        LLVMValueRef rec = cg_alloca(g, g->ll->rec_type, "tup.rec");
        LLVMValueRef tid = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                               rec, 0, "tid");
        LLVMBuildStore(cg_b(g), cg_i32(g, CWTuple), tid);
        LLVMValueRef gcnt = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                rec, 1, "gc");
        LLVMBuildStore(cg_b(g), cg_i8(g, 0), gcnt);
        /* cwtuple_init 要求 handle.address == 0, 先清零句柄 */
        LLVMValueRef hz = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                              rec, 3, "hz");
        LLVMBuildStore(cg_b(g), cg_null_handle(g), hz);
        LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g),
                                             "");
        cw_value* elems = cw_object_get(node, "elems");
        const size_t ne = (elems && cw_typeof(elems) == CW_ARRAY)
            ? cw_array_size(elems) : 0;
        cw_value* ann = cw_object_get(node, "ann");
        cw_value* ets = ann ? cw_object_get(ann, "element_types") : NULL;
        LLVMValueRef arr8 = NULL;
        if (ne > 0) {
            LLVMValueRef arr = cg_alloca(
                g, LLVMArrayType(g->ll->rec_type, (unsigned)ne),
                "tup.elems");
            arr8 = LLVMBuildBitCast(cg_b(g), arr, cg_rt_i8_ptr(g), "");
            for (size_t i = 0; i < ne && !g->failed; i++) {
                const char* en = NULL;
                if (ets && cw_typeof(ets) == CW_ARRAY
                    && i < cw_array_size(ets)) {
                    en = cg_type_name_of(g, cw_array_get(ets, i));
                }
                CwExpr e = cg_expr(g, cw_array_get(elems, i));
                if (g->failed) return (CwExpr){ NULL, NULL };
                e = cg_coerce_scalar(g, e, en);
                LLVMValueRef er = cg_materialize_record(g, e);
                LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                    cg_rt_i8_ptr(g), "");
                LLVMValueRef slot_idx[1] = {
                    cg_i64(g, (uint64_t)i * CWIND_OBJECT_RECORD_SIZE)
                };
                LLVMValueRef slot = LLVMBuildGEP2(
                    cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), arr8,
                    slot_idx, 1, "tup.slot");
                LLVMBuildMemCpy(cg_b(g), slot, 1, er8, 1,
                                cg_i64(g, CWIND_OBJECT_RECORD_SIZE));
            }
        }
        LLVMTypeRef pr_init[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                   LLVMInt64TypeInContext(cg_ctx(g)) };
        LLVMValueRef init = cg_rt_declare(
            g, "cwtuple_init", LLVMInt1TypeInContext(cg_ctx(g)), pr_init, 3);
        LLVMValueRef init_args[3] = {
            rec8, arr8 ? arr8 : LLVMConstPointerNull(cg_rt_i8_ptr(g)),
            cg_i64(g, ne)
        };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(init), init,
                       init_args, 3, "");
        LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                rec, 3, "h");
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hptr,
                                        "th");
        CwExpr e = { h, "Tuple" };
        return e;
    }
    if (strcmp(kind, "StructConstruct") == 0) {
        cw_value* ann = cw_object_get(node, "ann");
        cw_value* t = ann ? cw_object_get(ann, "type") : NULL;
        if (!t) t = cw_object_get(node, "type");
        const char* tname = t ? cg_type_name_of(g, t) : NULL;
        if (!tname) {
            cg_error_at(g, node, "StructConstruct is missing a type");
            return (CwExpr){ NULL, NULL };
        }
        const CwLayout_t* L = cg_struct_layout(g, t);
        if (!L) {
            cg_error_at(g, node, "unknown struct: %s", tname);
            return (CwExpr){ NULL, NULL };
        }
        const size_t nf = L->field_count;
        cw_value* args = cw_object_get(node, "args");
        const size_t na = (args && cw_typeof(args) == CW_ARRAY)
            ? cw_array_size(args) : 0;
        if (na != nf) {
            cg_error_at(g, node, "struct %s expects %zu field value(s), got %zu",
                       tname, nf, na);
            return (CwExpr){ NULL, NULL };
        }
        LLVMValueRef blob = cg_blob_alloc(g, cg_struct_blob_size(g, L),
                                          "st.lit");
        LLVMValueRef base = cg_blob_i8(g, blob);
        LLVMBuildMemSet(cg_b(g), base, cg_i8(g, 0),
                        cg_i64(g, cg_struct_blob_size(g, L)), 1);
        for (size_t i = 0; i < nf && !g->failed; i++) {
            CwExpr a = cg_expr(g, cw_array_get(args, i));
            if (g->failed) return (CwExpr){ NULL, NULL };
            const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
            a = cg_coerce_scalar(g, a, ft);
            cg_store_struct_field(g, base, L, i, a);
        }
        CwExpr e = { cg_struct_handle(g, blob, nf), tname };
        return e;
    }
    cg_error(g, "unsupported literal: %s", kind ? kind : "?");
    return (CwExpr){ NULL, NULL };
}

static CwExpr cg_expr_name(CwCodegen_t* g, cw_value* node) {
    cw_value* parts = cw_object_get(node, "parts");
    if (parts && cw_typeof(parts) == CW_ARRAY) {
        const size_t np = cw_array_size(parts);
        if (np == 1) {
            cw_value* p0 = cw_array_get(parts, 0);
            const char* n = (p0 && cw_typeof(p0) == CW_STRING)
                ? cw_string_cstr(p0) : NULL;
            if (n && strcmp(n, "None") == 0) {
                CwExpr e = { cg_null_handle(g), "None" };
                return e;
            }
            CwVar_t* v = n ? cg_var_find(g, n) : NULL;
            if (v) return cg_var_read(g, v);
            cg_error(g, "undeclared variable: %s", n ? n : "?");
            return (CwExpr){ NULL, NULL };
        }
        if (np == 2) {
            cw_value* p0 = cw_array_get(parts, 0);
            cw_value* p1 = cw_array_get(parts, 1);
            const char* owner = (p0 && cw_typeof(p0) == CW_STRING)
                ? cw_string_cstr(p0) : NULL;
            const char* member = (p1 && cw_typeof(p1) == CW_STRING)
                ? cw_string_cstr(p1) : NULL;
            if (owner && member) {
                cw_value* ann = cw_object_get(node, "ann");
                cw_value* binding = ann ? cw_object_get(ann, "binding") : NULL;
                const char* bk = binding ? cg_json_kind(binding) : NULL;
                if (bk && strcmp(bk, "field") == 0) {
                    cw_value* type_obj = NULL;
                    const char* real_owner =
                        cg_static_owner(g, owner);
                    if (cg_static_field(g, real_owner, member, &type_obj)) {
                        const char* t = cg_node_type_name(g, node);
                        if (!t) t = type_obj
                            ? cg_type_name_of(g, type_obj) : NULL;
                        if (t) {
                            return cg_static_read(g, real_owner, member,
                                                  t, type_obj);
                        }
                    }
                }
                if (bk && strcmp(bk, "variant") == 0) {
                    int64_t vidx = -1;
                    cw_value* vi = ann
                        ? cw_object_get(ann, "variant_index") : NULL;
                    if (!vi || cw_as_int(vi, &vidx) != CW_OK || vidx < 0) {
                        const CwNode_t* ed = cg_enum_decl(g, owner);
                        size_t idx = 0;
                        if (!ed || !cg_enum_variant_index(
                                g, ed, member, &idx)) {
                            cg_error(g, "unknown enum variant: %s::%s",
                                     owner ? owner : "?", member);
                            return (CwExpr){ NULL, NULL };
                        }
                        vidx = (int64_t)idx;
                    }
                    return cg_expr_enum_build(
                        g, owner, (size_t)vidx, NULL, NULL);
                }
            }
        }
    }
    cg_error(g, "multi-part Name / builtins are not supported yet");
    return (CwExpr){ NULL, NULL };
}

/* String 拼接: 调 rt cw_builtin_concat, 结果句柄指向 arena 中的新字节流 */
static CwExpr cg_builtin_concat(CwCodegen_t* g, CwExpr l, CwExpr r) {
    LLVMValueRef lr = cg_materialize_record(g, l);
    LLVMValueRef rr = cg_materialize_record(g, r);
    LLVMValueRef out = cg_alloca(g, g->ll->rec_type, "cat.rec");
    LLVMValueRef lr8 = LLVMBuildBitCast(cg_b(g), lr, cg_rt_i8_ptr(g), "");
    LLVMValueRef rr8 = LLVMBuildBitCast(cg_b(g), rr, cg_rt_i8_ptr(g), "");
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out, cg_rt_i8_ptr(g), "");
    LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                          cg_rt_i8_ptr(g) };
    LLVMValueRef f = cg_rt_declare(g, "cw_builtin_concat",
                                   LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
    LLVMValueRef av[3] = { lr8, rr8, out8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
    LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type, out, 3,
                                          "h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hp, "vh");
    CwExpr e = { h, "String" };
    return e;
}

/* String::format: 模板交给 rt 的栈机扫描 (cw_builtin_format)。
 * 字符串字面量模板传 raw (转义保留), 非字面量模板传已解码 value。 */
static CwExpr cg_expr_format_call(CwCodegen_t* g, cw_value* node) {
    cw_value* callee = cw_object_get(node, "callee");
    if (!callee || strcmp(cg_node_kind(callee), "Attribute") != 0) {
        cg_error(g, "format requires a string receiver");
        return (CwExpr){ NULL, NULL };
    }
    cw_value* tmpl_node = cw_object_get(callee, "obj");
    CwExpr self;
    if (tmpl_node && strcmp(cg_node_kind(tmpl_node), "StrLit") == 0) {
        cw_value* raw = cw_object_get(tmpl_node, "raw");
        size_t raw_len = 0;
        const char* s = raw ? cw_string_value(raw, &raw_len) : NULL;
        if (s && raw_len >= 2
            && (s[0] == '"' || s[0] == '\'') && s[raw_len - 1] == s[0]) {
            self = cg_string_lit(g, s + 1, raw_len - 2);
        } else {
            cg_error(g, "format literal template is missing raw");
            return (CwExpr){ NULL, NULL };
        }
    } else {
        self = cg_expr(g, tmpl_node);
    }
    if (g->failed) return (CwExpr){ NULL, NULL };

    LLVMValueRef self_rec = cg_materialize_record(g, self);
    LLVMValueRef self8 = LLVMBuildBitCast(cg_b(g), self_rec,
                                          cg_rt_i8_ptr(g), "");

    cw_value* args = cw_object_get(node, "args");
    const size_t na = (args && cw_typeof(args) == CW_ARRAY)
        ? cw_array_size(args) : 0;
    LLVMValueRef arr8 = NULL;
    if (na > 0) {
        /* rt 约定: args 是"指向记录"的指针数组 */
        LLVMValueRef arr = cg_alloca(
            g, LLVMArrayType(cg_rt_i8_ptr(g), (unsigned)na), "fmt.args");
        arr8 = LLVMBuildBitCast(cg_b(g), arr, cg_rt_i8_ptr(g), "");
        for (size_t i = 0; i < na && !g->failed; i++) {
            cw_value* arg = cw_array_get(args, i);
            CwExpr a = cg_expr(g, cw_object_get(arg, "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            LLVMValueRef ar = cg_materialize_record(g, a);
            LLVMValueRef ar8 = LLVMBuildBitCast(cg_b(g), ar,
                                                cg_rt_i8_ptr(g), "");
            LLVMValueRef idx[1] = { cg_i64(g, (uint64_t)i * 8) };
            LLVMValueRef slot = LLVMBuildGEP2(
                cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), arr8,
                idx, 1, "fmt.slot");
            LLVMBuildStore(cg_b(g), ar8, slot);
        }
    }

    LLVMValueRef out = cg_alloca(g, g->ll->rec_type, "fmt.out");
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                         cg_rt_i8_ptr(g), "");
    LLVMTypeRef pt[4] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                          LLVMInt64TypeInContext(cg_ctx(g)),
                          cg_rt_i8_ptr(g) };
    LLVMValueRef f = cg_rt_declare(g, "cw_builtin_format",
                                   LLVMInt1TypeInContext(cg_ctx(g)), pt, 4);
    LLVMValueRef null_ptr = LLVMBuildIntToPtr(
        cg_b(g), cg_i64(g, 0), cg_rt_i8_ptr(g), "fmt.null");
    LLVMValueRef av[4] = { self8, arr8 ? arr8 : null_ptr,
                           cg_i64(g, na), out8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 4, "");
    LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type, out, 3,
                                          "h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hp, "vh");
    return (CwExpr){ h, "String" };
}

/* 调用点结果保全: fnret 全局缓冲会被同函数的下一次调用覆盖,
 * 标量/结构体结果在这里立即拷进调用方本地临时 (引用类型直接复用句柄) */
static CwExpr cg_fixup_call_result(CwCodegen_t* g, LLVMValueRef h,
                                   const char* t,
                                   cw_value* type_obj) {
    if (!t) return (CwExpr){ h, "Any" };
    if (cg_is_scalar(t)) {
        size_t size = 0;
        LLVMTypeRef vt = cg_scalar_type(g, t, &size);
        LLVMValueRef v = cg_load_value(g, (CwExpr){ h, t }, vt);
        return cg_make_scalar(g, v, vt, t, size);
    }
    if (cg_is_struct_type(g, t)) {
        const CwLayout_t* L = cg_struct_layout(g, type_obj);
        if (L) {
            const size_t size = cg_struct_blob_size(g, L);
            LLVMValueRef blob = cg_blob_alloc(g, size, "call.ret");
            LLVMValueRef dst = cg_blob_i8(g, blob);
            LLVMValueRef src = LLVMBuildIntToPtr(
                cg_b(g), cg_handle_addr(g, (CwExpr){ h, t }),
                cg_rt_i8_ptr(g), "ret.src");
            LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1, cg_i64(g, size));
            cg_rebase_struct_fields(g, dst, L);
            return (CwExpr){ cg_struct_handle(g, blob, L->field_count), t };
        }
    }
    if (cg_is_enum_type(g, t)) {
        const size_t size = cg_enum_blob_size(g, t);
        LLVMValueRef blob = cg_blob_alloc(g, size, "call.enum");
        LLVMValueRef dst = cg_blob_i8(g, blob);
        LLVMValueRef src = LLVMBuildIntToPtr(
            cg_b(g), cg_handle_addr(g, (CwExpr){ h, t }),
            cg_rt_i8_ptr(g), "ret.src");
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1, cg_i64(g, size));
        return (CwExpr){ cg_enum_handle(g, blob, t), t };
    }
    return (CwExpr){ h, t };
}

static CwExpr cg_expr_binop(CwCodegen_t* g, cw_value* node) {
    CwExpr l = cg_expr(g, cw_object_get(node, "left"));
    CwExpr r = cg_expr(g, cw_object_get(node, "right"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    cw_value* opv = cw_object_get(node, "op");
    const char* op = (opv && cw_typeof(opv) == CW_STRING)
        ? cw_string_cstr(opv) : "";

    if (strcmp(op, "+") == 0 && strcmp(l.type_name, "String") == 0
        && strcmp(r.type_name, "String") == 0) {
        return cg_builtin_concat(g, l, r);
    }

    if (strcmp(op, "&&") == 0 || strcmp(op, "||") == 0) {
        /* 短路求值: 左值决定是否求右值 */
        LLVMTypeRef i8 = LLVMInt8TypeInContext(cg_ctx(g));
        LLVMValueRef res = cg_alloca(g, i8, "logical");
        LLVMValueRef a = cg_load_value(g, l, i8);
        const bool is_and = strcmp(op, "&&") == 0;
        LLVMValueRef a_cond = LLVMBuildICmp(cg_b(g),
                                            is_and ? LLVMIntNE : LLVMIntEQ,
                                            a, cg_i8(g, 0), "l.cond");
        LLVMBasicBlockRef rhs_bb = LLVMAppendBasicBlockInContext(
            cg_ctx(g), g->current_fn, "logical.rhs");
        LLVMBasicBlockRef short_bb = LLVMAppendBasicBlockInContext(
            cg_ctx(g), g->current_fn, "logical.short");
        LLVMBasicBlockRef merge_bb = LLVMAppendBasicBlockInContext(
            cg_ctx(g), g->current_fn, "logical.merge");
        LLVMBuildCondBr(cg_b(g), a_cond, rhs_bb, short_bb);

        LLVMPositionBuilderAtEnd(cg_b(g), rhs_bb);
        LLVMValueRef b = cg_load_value(g, r, i8);
        LLVMBuildStore(cg_b(g), b, res);
        LLVMBuildBr(cg_b(g), merge_bb);

        LLVMPositionBuilderAtEnd(cg_b(g), short_bb);
        LLVMBuildStore(cg_b(g), cg_i8(g, is_and ? 0 : 1), res);
        LLVMBuildBr(cg_b(g), merge_bb);

        LLVMPositionBuilderAtEnd(cg_b(g), merge_bb);
        LLVMValueRef out = LLVMBuildLoad2(cg_b(g), i8, res, "lres");
        return cg_make_scalar(g, out, i8, "Bool", 1);
    }

    /* Bool 相等/不等 */
    if (strcmp(l.type_name, "Bool") == 0
        && strcmp(r.type_name, "Bool") == 0) {
        if (strcmp(op, "==") == 0 || strcmp(op, "!=") == 0) {
            LLVMTypeRef i8 = LLVMInt8TypeInContext(cg_ctx(g));
            LLVMValueRef a = cg_load_value(g, l, i8);
            LLVMValueRef b = cg_load_value(g, r, i8);
            LLVMValueRef c = LLVMBuildICmp(
                cg_b(g), strcmp(op, "==") == 0 ? LLVMIntEQ : LLVMIntNE,
                a, b, "b.cmp");
            LLVMValueRef z = LLVMBuildZExt(cg_b(g), c, i8, "b.z");
            return cg_make_scalar(g, z, i8, "Bool", 1);
        }
        cg_error(g, "unsupported Bool operation: %s", op);
        return (CwExpr){ NULL, NULL };
    }

    /* 数值运算: 先提升到共同类型 (任意整数宽度 + Float/Float64 混合) */
    const bool lnum = l.type_name && cg_is_scalar(l.type_name)
        && strcmp(l.type_name, "Bool") != 0;
    const bool rnum = r.type_name && cg_is_scalar(r.type_name)
        && strcmp(r.type_name, "Bool") != 0;
    if (lnum && rnum) {
        const char* common = cg_common_numeric(l.type_name, r.type_name);
        if (!common) {
            cg_error(g, "unsupported numeric operation: %s (%s, %s)", op,
                     l.type_name, r.type_name);
            return (CwExpr){ NULL, NULL };
        }
        size_t csz = 0;
        LLVMTypeRef ct = cg_scalar_type(g, common, &csz);
        LLVMValueRef a = cg_load_value(
            g, l, cg_scalar_type(g, l.type_name, NULL));
        LLVMValueRef b = cg_load_value(
            g, r, cg_scalar_type(g, r.type_name, NULL));
        a = cg_convert_scalar(g, a, l.type_name, common);
        b = cg_convert_scalar(g, b, r.type_name, common);
        if (g->failed) return (CwExpr){ NULL, NULL };

        const bool is_float = strcmp(common, "Float") == 0
            || strcmp(common, "Float64") == 0;
        if (is_float) {
            if (strcmp(op, "+") == 0)
                return cg_make_scalar(g, LLVMBuildFAdd(cg_b(g), a, b, "fadd"),
                                      ct, common, csz);
            if (strcmp(op, "-") == 0)
                return cg_make_scalar(g, LLVMBuildFSub(cg_b(g), a, b, "fsub"),
                                      ct, common, csz);
            if (strcmp(op, "*") == 0)
                return cg_make_scalar(g, LLVMBuildFMul(cg_b(g), a, b, "fmul"),
                                      ct, common, csz);
            if (strcmp(op, "/") == 0)
                return cg_make_scalar(g, LLVMBuildFDiv(cg_b(g), a, b, "fdiv"),
                                      ct, common, csz);
            LLVMRealPredicate pred;
            if (strcmp(op, "==") == 0) pred = LLVMRealOEQ;
            else if (strcmp(op, "!=") == 0) pred = LLVMRealONE;
            else if (strcmp(op, "<") == 0) pred = LLVMRealOLT;
            else if (strcmp(op, "<=") == 0) pred = LLVMRealOLE;
            else if (strcmp(op, ">") == 0) pred = LLVMRealOGT;
            else if (strcmp(op, ">=") == 0) pred = LLVMRealOGE;
            else {
                cg_error(g, "unsupported float operation: %s", op);
                return (CwExpr){ NULL, NULL };
            }
            LLVMValueRef c = LLVMBuildFCmp(cg_b(g), pred, a, b, "fcmp");
            LLVMValueRef z = LLVMBuildZExt(
                cg_b(g), c, LLVMInt8TypeInContext(cg_ctx(g)), "fcmp.b");
            return cg_make_scalar(g, z,
                                  LLVMInt8TypeInContext(cg_ctx(g)), "Bool", 1);
        }

        const bool uns = cg_is_unsigned(common);
        if (strcmp(op, "+") == 0)
            return cg_make_scalar(g, LLVMBuildAdd(cg_b(g), a, b, "add"),
                                  ct, common, csz);
        if (strcmp(op, "-") == 0)
            return cg_make_scalar(g, LLVMBuildSub(cg_b(g), a, b, "sub"),
                                  ct, common, csz);
        if (strcmp(op, "*") == 0)
            return cg_make_scalar(g, LLVMBuildMul(cg_b(g), a, b, "mul"),
                                  ct, common, csz);
        if (strcmp(op, "/") == 0) {
            LLVMValueRef q = uns ? LLVMBuildUDiv(cg_b(g), a, b, "div")
                                 : LLVMBuildSDiv(cg_b(g), a, b, "div");
            return cg_make_scalar(g, q, ct, common, csz);
        }
        if (strcmp(op, "%") == 0) {
            LLVMValueRef q = uns ? LLVMBuildURem(cg_b(g), a, b, "rem")
                                 : LLVMBuildSRem(cg_b(g), a, b, "rem");
            return cg_make_scalar(g, q, ct, common, csz);
        }
        if (strcmp(op, "<<") == 0)
            return cg_make_scalar(g, LLVMBuildShl(cg_b(g), a, b, "shl"),
                                  ct, common, csz);
        if (strcmp(op, ">>") == 0) {
            LLVMValueRef q = uns ? LLVMBuildLShr(cg_b(g), a, b, "shr")
                                 : LLVMBuildAShr(cg_b(g), a, b, "shr");
            return cg_make_scalar(g, q, ct, common, csz);
        }
        if (strcmp(op, "&") == 0)
            return cg_make_scalar(g, LLVMBuildAnd(cg_b(g), a, b, "and"),
                                  ct, common, csz);
        if (strcmp(op, "|") == 0)
            return cg_make_scalar(g, LLVMBuildOr(cg_b(g), a, b, "or"),
                                  ct, common, csz);
        if (strcmp(op, "^") == 0)
            return cg_make_scalar(g, LLVMBuildXor(cg_b(g), a, b, "xor"),
                                  ct, common, csz);

        LLVMIntPredicate pred;
        if (strcmp(op, "==") == 0) pred = LLVMIntEQ;
        else if (strcmp(op, "!=") == 0) pred = LLVMIntNE;
        else if (strcmp(op, "<") == 0) pred = uns ? LLVMIntULT : LLVMIntSLT;
        else if (strcmp(op, "<=") == 0) pred = uns ? LLVMIntULE : LLVMIntSLE;
        else if (strcmp(op, ">") == 0) pred = uns ? LLVMIntUGT : LLVMIntSGT;
        else if (strcmp(op, ">=") == 0) pred = uns ? LLVMIntUGE : LLVMIntSGE;
        else {
            cg_error(g, "unsupported integer operation: %s", op);
            return (CwExpr){ NULL, NULL };
        }
        LLVMValueRef c = LLVMBuildICmp(cg_b(g), pred, a, b, "cmp");
        LLVMValueRef z = LLVMBuildZExt(
            cg_b(g), c, LLVMInt8TypeInContext(cg_ctx(g)), "cmp.b");
        return cg_make_scalar(g, z,
                              LLVMInt8TypeInContext(cg_ctx(g)), "Bool", 1);
    }

    cg_error(g, "unsupported BinOp: %s (%s, %s)", op,
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
            size_t sz = 0;
            LLVMTypeRef it = cg_scalar_type(g, e.type_name, &sz);
            LLVMValueRef v = cg_load_value(g, e, it);
            return cg_make_scalar(g, LLVMBuildNeg(cg_b(g), v, "neg"),
                                  it, e.type_name, sz);
        }
        if (strcmp(e.type_name, "Float") == 0
            || strcmp(e.type_name, "Float64") == 0) {
            size_t sz = 0;
            LLVMTypeRef ft = cg_scalar_type(g, e.type_name, &sz);
            LLVMValueRef v = cg_load_value(g, e, ft);
            return cg_make_scalar(g, LLVMBuildFNeg(cg_b(g), v, "fneg"),
                                  ft, e.type_name, sz);
        }
    }
    if (strcmp(op, "!") == 0) {
        LLVMValueRef v = cg_load_value(g, e, LLVMInt8TypeInContext(cg_ctx(g)));
        LLVMValueRef n = LLVMBuildXor(cg_b(g), v, cg_i8(g, 1), "not");
        return cg_make_scalar(g, n, LLVMInt8TypeInContext(cg_ctx(g)),
                              "Bool", 1);
    }
    cg_error(g, "unsupported UnaryOp: %s", op);
    return (CwExpr){ NULL, NULL };
}

static CwExpr cg_expr_call(CwCodegen_t* g, cw_value* node) {
    cw_value* ann = cw_object_get(node, "ann");
    cw_value* call = ann ? cw_object_get(ann, "call") : NULL;
    if (!call) {
        cg_error(g, "Call is missing ann.call");
        return (CwExpr){ NULL, NULL };
    }
    cw_value* ck_v = cw_object_get(call, "callee_kind");
    const char* ck = (ck_v && cw_typeof(ck_v) == CW_STRING)
        ? cw_string_cstr(ck_v) : NULL;
    cw_value* ref_v = cw_object_get(call, "callee_ref");

    if (ck && strcmp(ck, "enum_variant") == 0) {
        cw_value* callee = cw_object_get(node, "callee");
        cw_value* parts = callee ? cw_object_get(callee, "parts") : NULL;
        if (!parts || cw_typeof(parts) != CW_ARRAY
            || cw_array_size(parts) != 2) {
            cg_error(g, "enum variant call is missing its callee path");
            return (CwExpr){ NULL, NULL };
        }
        cw_value* p0 = cw_array_get(parts, 0);
        cw_value* p1 = cw_array_get(parts, 1);
        const char* enum_name = (p0 && cw_typeof(p0) == CW_STRING)
            ? cw_string_cstr(p0) : NULL;
        const char* vname = (p1 && cw_typeof(p1) == CW_STRING)
            ? cw_string_cstr(p1) : NULL;
        const CwNode_t* ed = cg_enum_decl(g, enum_name);
        size_t vidx = 0;
        if (!ed || !cg_enum_variant_index(g, ed, vname, &vidx)) {
            cg_error(g, "unknown enum variant: %s::%s",
                     enum_name ? enum_name : "?", vname ? vname : "?");
            return (CwExpr){ NULL, NULL };
        }
        cw_value* ann = cw_object_get(node, "ann");
        cw_value* pts = ann ? cw_object_get(ann, "payload_types") : NULL;
        return cg_expr_enum_build(
            g, enum_name, vidx, pts, cw_object_get(node, "args"));
    }

    if (ck && strcmp(ck, "builtin") == 0) {
        const char* bname = (ref_v && cw_typeof(ref_v) == CW_STRING)
            ? cw_string_cstr(ref_v) : NULL;
        if (bname && strcmp(bname, "print") == 0) {
            cw_value* args = cw_object_get(node, "args");
            cw_value* arg0 = (args && cw_typeof(args) == CW_ARRAY
                              && cw_array_size(args) > 0)
                ? cw_array_get(args, 0) : NULL;
            if (!arg0) {
                cg_error(g, "print expects 1 argument");
                return (CwExpr){ NULL, NULL };
            }
            CwExpr a = cg_expr(g, cw_object_get(arg0, "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            /* 临时记录: type_id + handle */
            LLVMValueRef rec = cg_alloca(g, g->ll->rec_type,
                                               "print.rec");
            LLVMValueRef tid = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                   rec, 0, "tid");
            LLVMBuildStore(cg_b(g), cg_i32(g, (uint32_t)cg_type_id(a.type_name)),
                           tid);
            LLVMValueRef gcnt = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                    rec, 1, "gc");
            LLVMBuildStore(cg_b(g), cg_i8(g, 0), gcnt);
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
        if (bname && strcmp(bname, "type_of") == 0) {
            cw_value* args = cw_object_get(node, "args");
            cw_value* arg0 = (args && cw_typeof(args) == CW_ARRAY
                              && cw_array_size(args) > 0)
                ? cw_array_get(args, 0) : NULL;
            if (!arg0) {
                cg_error(g, "type_of expects 1 argument");
                return (CwExpr){ NULL, NULL };
            }
            CwExpr a = cg_expr(g, cw_object_get(arg0, "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            LLVMValueRef rec = cg_materialize_record(g, a);
            LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec,
                                                 cg_rt_i8_ptr(g), "");
            LLVMValueRef out = cg_alloca(g, g->ll->rec_type,
                                               "type.rec");
            LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                                 cg_rt_i8_ptr(g), "");
            LLVMTypeRef pr[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
            LLVMValueRef fn = cg_rt_declare(
                g, "cw_builtin_type_of_owned",
                LLVMInt1TypeInContext(cg_ctx(g)), pr, 2);
            LLVMValueRef av[2] = { rec8, out8 };
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 2,
                           "");
            LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                  out, 3, "h");
            LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hp,
                                            "vh");
            CwExpr e = { h, "String" };
            return e;
        }
        if (bname && strcmp(bname, "readline") == 0) {
            LLVMValueRef out = cg_alloca(g, g->ll->rec_type,
                                               "read.rec");
            LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                                 cg_rt_i8_ptr(g), "");
            LLVMTypeRef pr[1] = { cg_rt_i8_ptr(g) };
            LLVMValueRef fn = cg_rt_declare(
                g, "cw_builtin_readline", LLVMInt1TypeInContext(cg_ctx(g)),
                pr, 1);
            LLVMValueRef av[1] = { out8 };
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 1,
                           "");
            LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                  out, 3, "h");
            LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hp,
                                            "vh");
            CwExpr e = { h, "String" };
            return e;
        }
        if (bname && strcmp(bname, "exit") == 0) {
            cw_value* args = cw_object_get(node, "args");
            cw_value* arg0 = (args && cw_typeof(args) == CW_ARRAY
                              && cw_array_size(args) > 0)
                ? cw_array_get(args, 0) : NULL;
            if (!arg0) {
                cg_error(g, "exit expects 1 argument");
                return (CwExpr){ NULL, NULL };
            }
            CwExpr a = cg_expr(g, cw_object_get(arg0, "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            LLVMValueRef iv = cg_load_value(
                g, a, cg_scalar_type(g, a.type_name, NULL));
            LLVMValueRef code = cg_convert_scalar(g, iv, a.type_name,
                                                  "Int32");
            if (g->failed) return (CwExpr){ NULL, NULL };
            LLVMTypeRef pr[1] = { LLVMInt32TypeInContext(cg_ctx(g)) };
            LLVMValueRef fn = cg_rt_declare(
                g, "cw_builtin_exit", LLVMVoidTypeInContext(cg_ctx(g)),
                pr, 1);
            LLVMValueRef av[1] = { code };
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 1,
                           "");
            CwExpr none = { cg_null_handle(g), "None" };
            return none;
        }
        if (bname && strcmp(bname, "format") == 0) {
            /* String::format: 模板 + 参数数组交给 rt 栈机扫描 */
            cw_value* attr = cw_object_get(node, "callee");
            if (attr && strcmp(cg_node_kind(attr), "Attribute") == 0) {
                return cg_expr_format_call(g, node);
            }
            cg_error(g, "format requires a string receiver");
            return (CwExpr){ NULL, NULL };
        }
        if (bname && strcmp(bname, "new") == 0) {
            /* 静态构造: Vector::new() / Map::new() / Set::new() */
            cw_value* callee = cw_object_get(node, "callee");
            const char* owner = NULL;
            if (callee && strcmp(cg_node_kind(callee), "Name") == 0) {
                cw_value* parts = cw_object_get(callee, "parts");
                if (parts && cw_typeof(parts) == CW_ARRAY
                    && cw_array_size(parts) >= 2) {
                    owner = cw_string_cstr(cw_array_get(parts, 0));
                }
            }
            const char* init_fn = NULL;
            if (owner && strcmp(owner, "Vector") == 0) init_fn = "cwvec_init";
            else if (owner && strcmp(owner, "Map") == 0) init_fn = "cwmap_init";
            else if (owner && strcmp(owner, "Set") == 0) init_fn = "cwset_init";
            if (!init_fn) {
                cg_error_at(g, node, "static construction not supported yet: %s",
                            owner ? owner : "?");
                return (CwExpr){ NULL, NULL };
            }
            LLVMValueRef rec = cg_alloca(g, g->ll->rec_type,
                                               "new.rec");
            LLVMValueRef tid = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                   rec, 0, "tid");
            LLVMBuildStore(cg_b(g), cg_i32(g, (uint32_t)cg_type_id(owner)),
                           tid);
            LLVMValueRef gcnt = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                    rec, 1, "gc");
            LLVMBuildStore(cg_b(g), cg_i8(g, 0), gcnt);
            LLVMValueRef hz = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                  rec, 3, "hz");
            LLVMBuildStore(cg_b(g), cg_null_handle(g), hz);
            LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec,
                                                 cg_rt_i8_ptr(g), "");
            if (strcmp(init_fn, "cwvec_init") == 0) {
                /* cwvec_init(obj, reserve) 双参数, 与字面量路径一致 */
                LLVMTypeRef pr[2] = { cg_rt_i8_ptr(g),
                                      LLVMInt64TypeInContext(cg_ctx(g)) };
                LLVMValueRef fn = cg_rt_declare(
                    g, init_fn, LLVMInt1TypeInContext(cg_ctx(g)), pr, 2);
                LLVMValueRef av[2] = { rec8, cg_i64(g, 0) };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av,
                               2, "");
            } else {
                LLVMTypeRef pr[1] = { cg_rt_i8_ptr(g) };
                LLVMValueRef fn = cg_rt_declare(
                    g, init_fn, LLVMInt1TypeInContext(cg_ctx(g)), pr, 1);
                LLVMValueRef av[1] = { rec8 };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av,
                               1, "");
            }
            LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                    rec, 3, "h");
            LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                            hptr, "vh");
            CwExpr e = { h, owner };
            return e;
        }
        /* 容器/字符串内置方法: 接收者是 Attribute, 走下方方法分派 */
        cw_value* attr = cw_object_get(node, "callee");
        if (attr && strcmp(cg_node_kind(attr), "Attribute") == 0) {
            ck = "method";
        } else {
            cg_error(g, "builtin not supported yet: %s", bname ? bname : "?");
            return (CwExpr){ NULL, NULL };
        }
    }

    if (ck && strcmp(ck, "fn") == 0) {
        int64_t ref = 0;
        if (!ref_v || cw_as_int(ref_v, &ref) != CW_OK) {
            cg_error(g, "function call is missing callee_ref");
            return (CwExpr){ NULL, NULL };
        }
        const CwNode_t* fn_node = cwmodule_node(g->m, ref);
        const char* fname = fn_node ? cwmodule_fn_name(fn_node) : NULL;
        const CwSymEntry_t* sym = fname ? cwsym_find(g->ll->syms, NULL, fname)
                                        : NULL;
        if (!sym) {
            cg_error(g, "function symbol not found: %s", fname ? fname : "?");
            return (CwExpr){ NULL, NULL };
        }
        const char* target_mangled = sym->mangled;
        if (sym->kind == CW_SYM_TEMPLATE) {
            /* 泛型函数: 按调用点 type_args 登记/复用具体实例 */
            cw_value* ann = cw_object_get(node, "ann");
            cw_value* call = ann ? cw_object_get(ann, "call") : NULL;
            cw_value* ta = call ? cw_object_get(call, "type_args") : NULL;
            cw_value* tp = fn_node ? cw_object_get(fn_node->value,
                                                   "type_params") : NULL;
            const size_t ntp = (tp && cw_typeof(tp) == CW_ARRAY)
                ? cw_array_size(tp) : 0;
            if (!ta || ntp == 0) {
                cg_error(g, "generic function is missing type_args: %s", fname ? fname : "?");
                return (CwExpr){ NULL, NULL };
            }
            CwTypeId* ids = (CwTypeId*)malloc(ntp * sizeof(CwTypeId));
            if (!ids) {
                cg_error(g, "failed to allocate generic arguments");
                return (CwExpr){ NULL, NULL };
            }
            bool ok = true;
            for (size_t i = 0; i < ntp; i++) {
                const char* pn = cg_json_name(cw_array_get(tp, i));
                cw_value* at = pn ? cw_object_get(ta, pn) : NULL;
                if (!pn || !at) {
                    cg_error(g, "generic function is missing argument %s", pn ? pn : "?");
                    ok = false;
                    break;
                }
                ids[i] = cg_type_id_of(g, at);
                if (ids[i] == CW_TYPE_INVALID) {
                    cg_error(g, "invalid generic argument: %s = %s",
                             pn, cg_json_name(at) ? cg_json_name(at) : "?");
                    ok = false;
                    break;
                }
            }
            if (!ok) {
                free(ids);
                return (CwExpr){ NULL, NULL };
            }
            char base[256];
            snprintf(base, sizeof(base), "cwind.fn.%s", fname);
            char im[512];
            if (!cw_mangle_instance(im, sizeof(im), base,
                                    g->ll->types, ids, ntp)) {
                cg_error(g, "failed to mangle the generic instance name: %s", fname);
                free(ids);
                return (CwExpr){ NULL, NULL };
            }
            const CwSymEntry_t* inst = cwsym_find_mangled(g->ll->syms, im);
            if (!inst) {
                inst = cwsym_add(g->ll->syms, im, fname, CW_SYM_INSTANCE,
                                 NULL, NULL, ids, ntp, fn_node);
                if (!inst) {
                    cg_error(g, "failed to register the generic instance: %s", im);
                    free(ids);
                    return (CwExpr){ NULL, NULL };
                }
                cwllvm_declare_function(g->ll, im,
                                        cwmodule_fn_param_count(fn_node));
            }
            target_mangled = inst->mangled;
            free(ids);
        }
        LLVMValueRef fn = LLVMGetNamedFunction(g->ll->module,
                                               target_mangled);
        if (!fn) {
            cg_error(g, "function is not declared: %s", target_mangled);
            return (CwExpr){ NULL, NULL };
        }
        cw_value* args = cw_object_get(node, "args");
        const size_t n = (args && cw_typeof(args) == CW_ARRAY)
            ? cw_array_size(args) : 0;
        LLVMValueRef* argv = (LLVMValueRef*)malloc(
            (n ? n : 1) * sizeof(LLVMValueRef));
        if (!argv) {
            cg_error(g, "failed to allocate the argument array");
            return (CwExpr){ NULL, NULL };
        }
        for (size_t i = 0; i < n; i++) {
            cw_value* arg = cw_array_get(args, i);
            CwExpr a = cg_expr(g, cw_object_get(arg, "value"));
            if (g->failed) {
                free(argv);
                return (CwExpr){ NULL, NULL };
            }
            cw_value* p = fn_node ? cwmodule_fn_param(fn_node, i) : NULL;
            cw_value* pt = p ? cw_object_get(p, "type") : NULL;
            const char* want = (pt && cw_typeof(pt) == CW_OBJECT)
                ? cg_type_name_of(g, pt) : NULL;
            a = cg_coerce_scalar(g, a, want);
            argv[i] = a.handle;
        }
        LLVMValueRef h = LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn),
                                        fn, argv, (unsigned)n, "call");
        free(argv);
        const char* ret_type = cg_node_type_name(g, node);
        return cg_fixup_call_result(g, h, ret_type,
                                    cg_node_ann_type(node));
    }

    if (ck && strcmp(ck, "method") == 0) {
        /* 用户方法: callee_ref 是 bindings 表 id (int) */
        if (ref_v && cw_typeof(ref_v) == CW_INT) {
            int64_t bref = 0;
            if (cw_as_int(ref_v, &bref) != CW_OK) {
                cg_error(g, "method call is missing binding id");
                return (CwExpr){ NULL, NULL };
            }
            const CwBinding_t* b = NULL;
            for (size_t i = 0; i < cwmodule_binding_count(g->m); i++) {
                const CwBinding_t* x = cwmodule_binding(g->m, i);
                if (x->id == bref) {
                    b = x;
                    break;
                }
            }
            if (!b) {
                cg_error(g, "method binding not found: %lld", (long long)bref);
                return (CwExpr){ NULL, NULL };
            }
            const CwNode_t* decl = cwmodule_node(g->m, b->fn_id);
            const char* fname = decl ? cwmodule_fn_name(decl) : NULL;
            char mangled[512];
            /* 泛型方法: owner params (ExtraDecl/ImplDecl) + 方法自身 type_params */
            const CwNode_t* owner_decl = cwmodule_node(g->m, b->decl_id);
            cw_value* owner_tp = owner_decl
                ? cw_object_get(owner_decl->value, "params") : NULL;
            cw_value* fn_tp = decl ? cw_object_get(decl->value, "type_params")
                                   : NULL;
            const size_t n_owner = (owner_tp && cw_typeof(owner_tp) == CW_ARRAY)
                ? cw_array_size(owner_tp) : 0;
            const size_t n_fn = (fn_tp && cw_typeof(fn_tp) == CW_ARRAY)
                ? cw_array_size(fn_tp) : 0;
            const char* target_mangled = NULL;
            if (n_owner + n_fn > 0) {
                /* 实例化: type_args 按 owner 参数在前、方法参数在后的顺序取 */
                const size_t nt = n_owner + n_fn;
                CwTypeId* ids = (CwTypeId*)malloc(nt * sizeof(CwTypeId));
                if (!ids) {
                    cg_error(g, "failed to allocate generic method arguments");
                    return (CwExpr){ NULL, NULL };
                }
                cw_value* ann = cw_object_get(node, "ann");
                cw_value* call = ann ? cw_object_get(ann, "call") : NULL;
                cw_value* ta = call ? cw_object_get(call, "type_args") : NULL;
                if (!ta) {
                    cg_error(g, "generic method is missing type_args: %s",
                             fname ? fname : "?");
                    free(ids);
                    return (CwExpr){ NULL, NULL };
                }
                bool ok = true;
                size_t k = 0;
                for (size_t pass = 0; pass < 2 && ok; pass++) {
                    cw_value* plist = (pass == 0) ? owner_tp : fn_tp;
                    const size_t np = (pass == 0) ? n_owner : n_fn;
                    for (size_t i = 0; i < np && ok; i++) {
                        const char* pn = cg_json_name(cw_array_get(plist, i));
                        cw_value* at = pn ? cw_object_get(ta, pn) : NULL;
                        if (!pn || !at) {
                            cg_error(g, "generic method is missing argument %s",
                                     pn ? pn : "?");
                            ok = false;
                            break;
                        }
                        ids[k] = cg_type_id_of(g, at);
                        if (ids[k] == CW_TYPE_INVALID) {
                            cg_error(g, "invalid generic method argument: %s = %s",
                                     pn, cg_json_name(at) ? cg_json_name(at)
                                                          : "?");
                            ok = false;
                            break;
                        }
                        k++;
                    }
                }
                if (ok) {
                    char base[256];
                    snprintf(base, sizeof(base), "cwind.method.%s",
                             b->owner ? b->owner : "?");
                    if (!cw_mangle_instance(mangled, sizeof(mangled), base,
                                            g->ll->types, ids, nt)) {
                        cg_error(g, "failed to mangle the generic method instance name: %s",
                                 fname ? fname : "?");
                        ok = false;
                    } else {
                        const size_t ml = strlen(mangled);
                        if (ml + strlen(fname) + 2 <= sizeof(mangled)) {
                            snprintf(mangled + ml, sizeof(mangled) - ml,
                                     ".%s", fname);
                            const CwSymEntry_t* inst =
                                cwsym_find_mangled(g->ll->syms, mangled);
                            if (!inst) {
                                inst = cwsym_add(
                                    g->ll->syms, mangled, fname,
                                    CW_SYM_INSTANCE, b->owner, b->trait,
                                    ids, nt, decl);
                                if (!inst) {
                                    cg_error(g, "failed to register the generic method instance: %s",
                                             mangled);
                                    ok = false;
                                } else {
                                    cwllvm_declare_function(
                                        g->ll, mangled,
                                        cwmodule_fn_param_count(decl));
                                }
                            }
                            if (ok) target_mangled = inst->mangled;
                        } else {
                            cg_error(g, "generic method instance name is too long: %s", fname);
                            ok = false;
                        }
                    }
                }
                free(ids);
                if (!ok || !target_mangled) return (CwExpr){ NULL, NULL };
            } else {
                if (!fname || !cw_mangle_method(mangled, sizeof(mangled),
                                                b->owner, fname)) {
                    cg_error(g, "failed to mangle the method name: %s.%s",
                             b->owner ? b->owner : "?", fname ? fname : "?");
                    return (CwExpr){ NULL, NULL };
                }
                target_mangled = mangled;
            }
            LLVMValueRef fn = LLVMGetNamedFunction(g->ll->module,
                                                   target_mangled);
            if (!fn) {
                cg_error(g, "method is not declared: %s", target_mangled);
                return (CwExpr){ NULL, NULL };
            }
            cw_value* callee = cw_object_get(node, "callee");
            const bool is_instance = callee
                && strcmp(cg_node_kind(callee), "Attribute") == 0;
            cw_value* args = cw_object_get(node, "args");
            const size_t na = (args && cw_typeof(args) == CW_ARRAY)
                ? cw_array_size(args) : 0;
            const size_t nparams = decl ? cwmodule_fn_param_count(decl) : 0;
            if (na + (is_instance ? 1u : 0u) != nparams) {
                cg_error(g, "method %s argument count mismatch (%zu vs %zu)",
                         mangled, na + (is_instance ? 1u : 0u), nparams);
                return (CwExpr){ NULL, NULL };
            }
            LLVMValueRef* argv = (LLVMValueRef*)malloc(
                (nparams ? nparams : 1) * sizeof(LLVMValueRef));
            if (!argv) {
                cg_error(g, "failed to allocate the method argument array");
                return (CwExpr){ NULL, NULL };
            }
            size_t ai = 0;
            if (is_instance) {
                CwExpr recv = cg_expr(g, cw_object_get(callee, "obj"));
                if (g->failed) {
                    free(argv);
                    return (CwExpr){ NULL, NULL };
                }
                cw_value* sp = decl ? cwmodule_fn_param(decl, 0) : NULL;
                cw_value* spt = sp ? cw_object_get(sp, "type") : NULL;
                const char* swant = (spt && cw_typeof(spt) == CW_OBJECT)
                    ? cg_type_name_of(g, spt) : NULL;
                recv = cg_coerce_scalar(g, recv, swant);
                argv[ai++] = recv.handle;
            }
            for (size_t i = 0; i < na; i++) {
                CwExpr a = cg_expr(g, cw_object_get(
                    cw_array_get(args, i), "value"));
                if (g->failed) {
                    free(argv);
                    return (CwExpr){ NULL, NULL };
                }
                const size_t pi = is_instance ? i + 1 : i;
                cw_value* p = decl ? cwmodule_fn_param(decl, pi) : NULL;
                cw_value* pt = p ? cw_object_get(p, "type") : NULL;
                const char* want = (pt && cw_typeof(pt) == CW_OBJECT)
                    ? cg_type_name_of(g, pt) : NULL;
                a = cg_coerce_scalar(g, a, want);
                argv[ai++] = a.handle;
            }
            LLVMValueRef h = LLVMBuildCall2(
                cg_b(g), LLVMGlobalGetValueType(fn), fn, argv,
                (unsigned)ai, "mcall");
            free(argv);
            const char* t = cg_node_type_name(g, node);
            return cg_fixup_call_result(g, h, t,
                                        cg_node_ann_type(node));
        }
        cw_value* attr = cw_object_get(node, "callee");
        cw_value* objv = attr ? cw_object_get(attr, "obj") : NULL;
        const char* owner = objv ? cg_node_type_name(g, objv) : NULL;
        const char* mname = attr ? cg_json_name(attr) : NULL;
        if (!owner || !mname) {
            cg_error(g, "method call is missing receiver type/method name");
            return (CwExpr){ NULL, NULL };
        }
        LLVMValueRef rec = cg_expr_record(g, objv);
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g),
                                             "");
        cw_value* args = cw_object_get(node, "args");
        const size_t nargs = (args && cw_typeof(args) == CW_ARRAY)
            ? cw_array_size(args) : 0;

        if (strcmp(owner, "Vector") == 0) {
            if (strcmp(mname, "push_back") == 0 && nargs == 1) {
                CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                    "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
                LLVMValueRef er = cg_materialize_record(g, a);
                LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                    cg_rt_i8_ptr(g), "");
                LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cwvec_push", LLVMInt1TypeInContext(cg_ctx(g)), pt, 2);
                LLVMValueRef av[2] = { rec8, er8 };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2,
                               "");
                CwExpr none = { cg_null_handle(g), "None" };
                return none;
            }
            if (strcmp(mname, "pop_back") == 0 && nargs == 0) {
                LLVMValueRef out = cg_alloca(g, g->ll->rec_type,
                                                   "pop.rec");
                LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                                     cg_rt_i8_ptr(g), "");
                LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cwvec_pop", LLVMInt1TypeInContext(cg_ctx(g)), pt, 2);
                LLVMValueRef av[2] = { rec8, out8 };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2,
                               "");
                LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g),
                                                       g->ll->rec_type,
                                                       out, 3, "h");
                LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                                hp, "vh");
                const char* t = cg_node_type_name(g, node);
                CwExpr e = { h, t ? t : "Any" };
                return e;
            }
            if ((strcmp(mname, "get") == 0 || strcmp(mname, "set") == 0)
                && nargs >= 1) {
                CwExpr idx = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                      "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                LLVMValueRef ix = cg_index_i64(g, idx);
                if (strcmp(mname, "get") == 0 && nargs == 1) {
                    LLVMValueRef out = cg_alloca(g,
                                                       g->ll->rec_type,
                                                       "get.rec");
                    LLVMValueRef out8 = LLVMBuildBitCast(
                        cg_b(g), out, cg_rt_i8_ptr(g), "");
                    LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g),
                                          LLVMInt64TypeInContext(cg_ctx(g)),
                                          cg_rt_i8_ptr(g) };
                    LLVMValueRef f = cg_rt_declare(
                        g, "cwvec_at", LLVMInt1TypeInContext(cg_ctx(g)),
                        pt, 3);
                    LLVMValueRef av[3] = { rec8, ix, out8 };
                    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f,
                                   av, 3, "");
                    LLVMValueRef hp = LLVMBuildStructGEP2(
                        cg_b(g), g->ll->rec_type, out, 3, "h");
                    LLVMValueRef h = LLVMBuildLoad2(
                        cg_b(g), g->ll->handle_type, hp, "vh");
                    const char* t = cg_node_type_name(g, node);
                    CwExpr e = { h, t ? t : "Any" };
                    return e;
                }
                if (strcmp(mname, "set") == 0 && nargs == 2) {
                    CwExpr v = cg_expr(g, cw_object_get(
                        cw_array_get(args, 1), "value"));
                    if (g->failed) return (CwExpr){ NULL, NULL };
                    v = cg_coerce_scalar(g, v, cg_receiver_arg(g, objv, 0));
                    LLVMValueRef er = cg_materialize_record(g, v);
                    LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                        cg_rt_i8_ptr(g), "");
                    LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g),
                                          LLVMInt64TypeInContext(cg_ctx(g)),
                                          cg_rt_i8_ptr(g) };
                    LLVMValueRef f = cg_rt_declare(
                        g, "cwvec_set", LLVMInt1TypeInContext(cg_ctx(g)),
                        pt, 3);
                    LLVMValueRef av[3] = { rec8, ix, er8 };
                    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f,
                                   av, 3, "");
                    CwExpr none = { cg_null_handle(g), "None" };
                    return none;
                }
            }
            if (strcmp(mname, "length") == 0 && nargs == 0) {
                LLVMValueRef slot = cg_alloca(
                    g, LLVMInt64TypeInContext(cg_ctx(g)), "len");
                LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g),
                                      LLVMPointerType(
                                          LLVMVoidTypeInContext(cg_ctx(g)), 0) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cw_builtin_length", LLVMInt1TypeInContext(cg_ctx(g)),
                    pt, 2);
                LLVMValueRef av[2] = { rec8, slot };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2,
                               "");
                LLVMValueRef v = LLVMBuildLoad2(
                    cg_b(g), LLVMInt64TypeInContext(cg_ctx(g)), slot, "lenv");
                LLVMValueRef t = LLVMBuildTrunc(
                    cg_b(g), v, LLVMInt16TypeInContext(cg_ctx(g)), "len16");
                return cg_make_scalar(g, t,
                                      LLVMInt16TypeInContext(cg_ctx(g)),
                                      "UInt", 2);
            }
            if (strcmp(mname, "clear") == 0 && nargs == 0) {
                LLVMTypeRef pt[1] = { cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cwvec_clear", LLVMVoidTypeInContext(cg_ctx(g)), pt, 1);
                LLVMValueRef av[1] = { rec8 };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 1,
                               "");
                CwExpr none = { cg_null_handle(g), "None" };
                return none;
            }
            if (strcmp(mname, "contains") == 0 && nargs == 1) {
                CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                    "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
                LLVMValueRef er = cg_materialize_record(g, a);
                LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                    cg_rt_i8_ptr(g), "");
                LLVMValueRef slot = cg_alloca(
                    g, LLVMInt8TypeInContext(cg_ctx(g)), "found");
                LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                      cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cw_builtin_contains",
                    LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
                LLVMValueRef av[3] = { rec8, er8, slot };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3,
                               "");
                LLVMValueRef v = LLVMBuildLoad2(
                    cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), slot, "b");
                return cg_make_scalar(g, v,
                                      LLVMInt8TypeInContext(cg_ctx(g)),
                                      "Bool", 1);
            }
            if (strcmp(mname, "extend_with") == 0 && nargs == 1) {
                CwExpr o = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                    "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                LLVMValueRef or_ = cg_materialize_record(g, o);
                LLVMValueRef or8 = LLVMBuildBitCast(cg_b(g), or_,
                                                    cg_rt_i8_ptr(g), "");
                LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cwvec_extend_with",
                    LLVMInt1TypeInContext(cg_ctx(g)), pt, 2);
                LLVMValueRef av[2] = { rec8, or8 };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2,
                               "");
                CwExpr none = { cg_null_handle(g), "None" };
                return none;
            }
            if (strcmp(mname, "insert_at") == 0 && nargs == 2) {
                CwExpr idx = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                      "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                LLVMValueRef ix = cg_index_i64(g, idx);
                CwExpr v = cg_expr(g, cw_object_get(cw_array_get(args, 1),
                                                    "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                v = cg_coerce_scalar(g, v, cg_receiver_arg(g, objv, 0));
                LLVMValueRef er = cg_materialize_record(g, v);
                LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                    cg_rt_i8_ptr(g), "");
                LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g),
                                      LLVMInt64TypeInContext(cg_ctx(g)),
                                      cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cwvec_insert_at", LLVMInt1TypeInContext(cg_ctx(g)),
                    pt, 3);
                LLVMValueRef av[3] = { rec8, ix, er8 };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3,
                               "");
                CwExpr none = { cg_null_handle(g), "None" };
                return none;
            }
            if (strcmp(mname, "index_of") == 0 && nargs == 1) {
                CwExpr v = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                    "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                v = cg_coerce_scalar(g, v, cg_receiver_arg(g, objv, 0));
                LLVMValueRef er = cg_materialize_record(g, v);
                LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                    cg_rt_i8_ptr(g), "");
                LLVMValueRef slot = cg_alloca(
                    g, LLVMInt64TypeInContext(cg_ctx(g)), "pos");
                LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                      cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cwvec_index_of", LLVMInt1TypeInContext(cg_ctx(g)),
                    pt, 3);
                LLVMValueRef av[3] = { rec8, er8, slot };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3,
                               "");
                LLVMValueRef vv = LLVMBuildLoad2(
                    cg_b(g), LLVMInt64TypeInContext(cg_ctx(g)), slot, "posv");
                LLVMValueRef t = LLVMBuildTrunc(
                    cg_b(g), vv, LLVMInt16TypeInContext(cg_ctx(g)), "pos16");
                return cg_make_scalar(g, t,
                                      LLVMInt16TypeInContext(cg_ctx(g)),
                                      "UInt", 2);
            }
            if (strcmp(mname, "remove_at") == 0 && nargs == 1) {
                CwExpr idx = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                      "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                LLVMValueRef ix = cg_index_i64(g, idx);
                LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g),
                                      LLVMInt64TypeInContext(cg_ctx(g)),
                                      cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cwvec_remove_at", LLVMInt1TypeInContext(cg_ctx(g)),
                    pt, 3);
                LLVMValueRef av[3] = { rec8, ix,
                                       LLVMConstPointerNull(
                                           cg_rt_i8_ptr(g)) };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3,
                               "");
                CwExpr none = { cg_null_handle(g), "None" };
                return none;
            }
        }
        if (strcmp(owner, "String") == 0) {
            if (strcmp(mname, "length") == 0 && nargs == 0) {
                LLVMValueRef slot = cg_alloca(
                    g, LLVMInt64TypeInContext(cg_ctx(g)), "len");
                LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g),
                                      LLVMPointerType(
                                          LLVMVoidTypeInContext(cg_ctx(g)), 0) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cw_builtin_length", LLVMInt1TypeInContext(cg_ctx(g)),
                    pt, 2);
                LLVMValueRef av[2] = { rec8, slot };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2,
                               "");
                LLVMValueRef v = LLVMBuildLoad2(
                    cg_b(g), LLVMInt64TypeInContext(cg_ctx(g)), slot, "lenv");
                LLVMValueRef t = LLVMBuildTrunc(
                    cg_b(g), v, LLVMInt16TypeInContext(cg_ctx(g)), "len16");
                return cg_make_scalar(g, t,
                                      LLVMInt16TypeInContext(cg_ctx(g)),
                                      "UInt", 2);
            }
            if (strcmp(mname, "contains") == 0 && nargs == 1) {
                CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                    "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
                LLVMValueRef er = cg_materialize_record(g, a);
                LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                    cg_rt_i8_ptr(g), "");
                LLVMValueRef slot = cg_alloca(
                    g, LLVMInt8TypeInContext(cg_ctx(g)), "found");
                LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                      cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cw_builtin_contains",
                    LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
                LLVMValueRef av[3] = { rec8, er8, slot };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3,
                               "");
                LLVMValueRef v = LLVMBuildLoad2(
                    cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), slot, "b");
                return cg_make_scalar(g, v,
                                      LLVMInt8TypeInContext(cg_ctx(g)),
                                      "Bool", 1);
            }
        }
        if (strcmp(owner, "Map") == 0) {
            if ((strcmp(mname, "get") == 0 || strcmp(mname, "set") == 0)
                && nargs >= 1) {
                CwExpr k = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                    "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                k = cg_coerce_scalar(g, k, cg_receiver_arg(g, objv, 0));
                LLVMValueRef kr = cg_materialize_record(g, k);
                LLVMValueRef kr8 = LLVMBuildBitCast(cg_b(g), kr,
                                                    cg_rt_i8_ptr(g), "");
                if (strcmp(mname, "get") == 0 && nargs == 1) {
                    LLVMValueRef out = cg_alloca(g,
                                                       g->ll->rec_type,
                                                       "get.rec");
                    LLVMValueRef out8 = LLVMBuildBitCast(
                        cg_b(g), out, cg_rt_i8_ptr(g), "");
                    LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                          cg_rt_i8_ptr(g) };
                    LLVMValueRef f = cg_rt_declare(
                        g, "cwmap_get", LLVMInt1TypeInContext(cg_ctx(g)),
                        pt, 3);
                    LLVMValueRef av[3] = { rec8, kr8, out8 };
                    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f,
                                   av, 3, "");
                    LLVMValueRef hp = LLVMBuildStructGEP2(
                        cg_b(g), g->ll->rec_type, out, 3, "h");
                    LLVMValueRef h = LLVMBuildLoad2(
                        cg_b(g), g->ll->handle_type, hp, "vh");
                    const char* t = cg_node_type_name(g, node);
                    CwExpr e = { h, t ? t : "Any" };
                    return e;
                }
                if (strcmp(mname, "set") == 0 && nargs == 2) {
                    CwExpr v = cg_expr(g, cw_object_get(
                        cw_array_get(args, 1), "value"));
                    if (g->failed) return (CwExpr){ NULL, NULL };
                    v = cg_coerce_scalar(g, v, cg_receiver_arg(g, objv, 1));
                    LLVMValueRef vr = cg_materialize_record(g, v);
                    LLVMValueRef vr8 = LLVMBuildBitCast(cg_b(g), vr,
                                                        cg_rt_i8_ptr(g), "");
                    LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                          cg_rt_i8_ptr(g) };
                    LLVMValueRef f = cg_rt_declare(
                        g, "cwmap_put", LLVMInt1TypeInContext(cg_ctx(g)),
                        pt, 3);
                    LLVMValueRef av[3] = { rec8, kr8, vr8 };
                    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f,
                                   av, 3, "");
                    CwExpr none = { cg_null_handle(g), "None" };
                    return none;
                }
            }
            if (strcmp(mname, "clear") == 0 && nargs == 0) {
                LLVMTypeRef pt[1] = { cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cwmap_clear", LLVMVoidTypeInContext(cg_ctx(g)), pt, 1);
                LLVMValueRef av[1] = { rec8 };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 1,
                               "");
                CwExpr none = { cg_null_handle(g), "None" };
                return none;
            }
            if (strcmp(mname, "length") == 0 && nargs == 0) {
                LLVMValueRef slot = cg_alloca(
                    g, LLVMInt64TypeInContext(cg_ctx(g)), "len");
                LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g),
                                      LLVMPointerType(
                                          LLVMVoidTypeInContext(cg_ctx(g)), 0) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cw_builtin_length", LLVMInt1TypeInContext(cg_ctx(g)),
                    pt, 2);
                LLVMValueRef av[2] = { rec8, slot };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2,
                               "");
                LLVMValueRef v = LLVMBuildLoad2(
                    cg_b(g), LLVMInt64TypeInContext(cg_ctx(g)), slot, "lenv");
                LLVMValueRef t = LLVMBuildTrunc(
                    cg_b(g), v, LLVMInt16TypeInContext(cg_ctx(g)), "len16");
                return cg_make_scalar(g, t,
                                      LLVMInt16TypeInContext(cg_ctx(g)),
                                      "UInt", 2);
            }
            if (strcmp(mname, "contains") == 0 && nargs == 1) {
                CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                    "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                LLVMValueRef er = cg_materialize_record(g, a);
                LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                    cg_rt_i8_ptr(g), "");
                LLVMValueRef slot = cg_alloca(
                    g, LLVMInt8TypeInContext(cg_ctx(g)), "found");
                LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                      cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cw_builtin_contains",
                    LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
                LLVMValueRef av[3] = { rec8, er8, slot };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3,
                               "");
                LLVMValueRef v = LLVMBuildLoad2(
                    cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), slot, "b");
                return cg_make_scalar(g, v,
                                      LLVMInt8TypeInContext(cg_ctx(g)),
                                      "Bool", 1);
            }
        }
        if (strcmp(owner, "Set") == 0) {
            if ((strcmp(mname, "add") == 0 || strcmp(mname, "remove") == 0)
                && nargs == 1) {
                CwExpr a = cg_expr(g, cw_object_get(
                    cw_array_get(args, 0), "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
                LLVMValueRef er = cg_materialize_record(g, a);
                LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                    cg_rt_i8_ptr(g), "");
                LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, strcmp(mname, "add") == 0
                        ? "cwset_add" : "cwset_remove",
                    LLVMInt1TypeInContext(cg_ctx(g)), pt, 2);
                LLVMValueRef av[2] = { rec8, er8 };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f,
                               av, 2, "");
                CwExpr none = { cg_null_handle(g), "None" };
                return none;
            }
            if (strcmp(mname, "length") == 0 && nargs == 0) {
                LLVMValueRef slot = cg_alloca(
                    g, LLVMInt64TypeInContext(cg_ctx(g)), "len");
                LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g),
                                      LLVMPointerType(
                                          LLVMVoidTypeInContext(cg_ctx(g)), 0) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cw_builtin_length", LLVMInt1TypeInContext(cg_ctx(g)),
                    pt, 2);
                LLVMValueRef av[2] = { rec8, slot };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2,
                               "");
                LLVMValueRef v = LLVMBuildLoad2(
                    cg_b(g), LLVMInt64TypeInContext(cg_ctx(g)), slot, "lenv");
                LLVMValueRef t = LLVMBuildTrunc(
                    cg_b(g), v, LLVMInt16TypeInContext(cg_ctx(g)), "len16");
                return cg_make_scalar(g, t,
                                      LLVMInt16TypeInContext(cg_ctx(g)),
                                      "UInt", 2);
            }
            if (strcmp(mname, "clear") == 0 && nargs == 0) {
                LLVMTypeRef pt[1] = { cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cwset_clear", LLVMVoidTypeInContext(cg_ctx(g)), pt, 1);
                LLVMValueRef av[1] = { rec8 };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 1,
                               "");
                CwExpr none = { cg_null_handle(g), "None" };
                return none;
            }
            if (strcmp(mname, "contains") == 0 && nargs == 1) {
                CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                    "value"));
                if (g->failed) return (CwExpr){ NULL, NULL };
                LLVMValueRef er = cg_materialize_record(g, a);
                LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                    cg_rt_i8_ptr(g), "");
                LLVMValueRef slot = cg_alloca(
                    g, LLVMInt8TypeInContext(cg_ctx(g)), "found");
                LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                      cg_rt_i8_ptr(g) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cw_builtin_contains",
                    LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
                LLVMValueRef av[3] = { rec8, er8, slot };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3,
                               "");
                LLVMValueRef v = LLVMBuildLoad2(
                    cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), slot, "b");
                return cg_make_scalar(g, v,
                                      LLVMInt8TypeInContext(cg_ctx(g)),
                                      "Bool", 1);
            }
        }
        if (strcmp(owner, "Tuple") == 0) {
            if (strcmp(mname, "length") == 0 && nargs == 0) {
                LLVMValueRef slot = cg_alloca(
                    g, LLVMInt64TypeInContext(cg_ctx(g)), "len");
                LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g),
                                      LLVMPointerType(
                                          LLVMVoidTypeInContext(cg_ctx(g)), 0) };
                LLVMValueRef f = cg_rt_declare(
                    g, "cw_builtin_length", LLVMInt1TypeInContext(cg_ctx(g)),
                    pt, 2);
                LLVMValueRef av[2] = { rec8, slot };
                LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2,
                               "");
                LLVMValueRef v = LLVMBuildLoad2(
                    cg_b(g), LLVMInt64TypeInContext(cg_ctx(g)), slot, "lenv");
                LLVMValueRef t = LLVMBuildTrunc(
                    cg_b(g), v, LLVMInt16TypeInContext(cg_ctx(g)), "len16");
                return cg_make_scalar(g, t,
                                      LLVMInt16TypeInContext(cg_ctx(g)),
                                      "UInt", 2);
            }
        }
        cg_error_at(g, node, "method not supported yet: %s.%s", owner, mname);
        return (CwExpr){ NULL, NULL };
    }

    cg_error(g, "call not supported yet: %s", ck ? ck : "?");
    return (CwExpr){ NULL, NULL };
}

static CwExpr cg_expr_index(CwCodegen_t* g, cw_value* node) {
    cw_value* obj = cw_object_get(node, "obj");
    const char* ot = cg_node_type_name(g, obj);
    if (!ot) {
        cg_error(g, "index read is missing the container type");
        return (CwExpr){ NULL, NULL };
    }
    LLVMValueRef rec = cg_expr_record(g, obj);
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g), "");
    LLVMValueRef out = cg_alloca(g, g->ll->rec_type, "idx.rec");
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out, cg_rt_i8_ptr(g), "");
    if (strcmp(ot, "Vector") == 0) {
        CwExpr idx = cg_expr(g, cw_object_get(node, "index"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef ix = cg_index_i64(g, idx);
        LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g),
                              LLVMInt64TypeInContext(cg_ctx(g)),
                              cg_rt_i8_ptr(g) };
        LLVMValueRef f = cg_rt_declare(g, "cwvec_at",
                                       LLVMInt1TypeInContext(cg_ctx(g)),
                                       pt, 3);
        LLVMValueRef av[3] = { rec8, ix, out8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
    } else if (strcmp(ot, "Map") == 0) {
        CwExpr k = cg_expr(g, cw_object_get(node, "index"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef kr = cg_materialize_record(g, k);
        LLVMValueRef kr8 = LLVMBuildBitCast(cg_b(g), kr, cg_rt_i8_ptr(g),
                                            "");
        LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                              cg_rt_i8_ptr(g) };
        LLVMValueRef f = cg_rt_declare(g, "cwmap_get",
                                       LLVMInt1TypeInContext(cg_ctx(g)),
                                       pt, 3);
        LLVMValueRef av[3] = { rec8, kr8, out8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
    } else if (strcmp(ot, "Tuple") == 0) {
        /* 索引必须是编译期常量 (SA 已校验), 直接读 ann.tuple_index */
        cw_value* ann = cw_object_get(node, "ann");
        cw_value* ti = ann ? cw_object_get(ann, "tuple_index") : NULL;
        int64_t idx = 0;
        if (!ti || cw_as_int(ti, &idx) != CW_OK || idx < 0) {
            cg_error_at(g, node, "tuple index requires a compile-time constant");
            return (CwExpr){ NULL, NULL };
        }
        LLVMTypeRef pt_at[3] = { cg_rt_i8_ptr(g),
                                 LLVMInt64TypeInContext(cg_ctx(g)),
                                 cg_rt_i8_ptr(g) };
        LLVMValueRef f = cg_rt_declare(
            g, "cwtuple_at", LLVMInt1TypeInContext(cg_ctx(g)), pt_at, 3);
        LLVMValueRef av[3] = { rec8, cg_i64(g, (uint64_t)idx), out8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
    } else {
        cg_error(g, "only Vector/Map/Tuple index reads are supported (got %s)", ot);
        return (CwExpr){ NULL, NULL };
    }
    LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type, out, 3,
                                          "h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hp, "vh");
    const char* t = cg_node_type_name(g, node);
    CwExpr e = { h, t ? t : "Any" };
    return e;
}

static CwExpr cg_expr_attribute(CwCodegen_t* g, cw_value* node) {
    cw_value* ann = cw_object_get(node, "ann");
    cw_value* member = ann ? cw_object_get(ann, "member") : NULL;
    const char* mkind = member ? cg_json_kind(member) : NULL;
    if (mkind && strcmp(mkind, "tuple_elem") == 0) {
        cw_value* index_v = cw_object_get(member, "index");
        int64_t idx = 0;
        if (!index_v || cw_as_int(index_v, &idx) != CW_OK || idx < 0) {
            cg_error_at(g, node, "tuple element access is missing a valid index");
            return (CwExpr){ NULL, NULL };
        }
        CwExpr obj = cg_expr(g, cw_object_get(node, "obj"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef trec = cg_materialize_record(g, obj);
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef trec8 = LLVMBuildBitCast(cg_b(g), trec,
                                              cg_rt_i8_ptr(g), "");
        LLVMValueRef out = cg_alloca(g, g->ll->rec_type, "tup.at");
        LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                             cg_rt_i8_ptr(g), "");
        LLVMTypeRef pt_at[3] = { cg_rt_i8_ptr(g),
                                 LLVMInt64TypeInContext(cg_ctx(g)),
                                 cg_rt_i8_ptr(g) };
        LLVMValueRef at = cg_rt_declare(
            g, "cwtuple_at", LLVMInt1TypeInContext(cg_ctx(g)), pt_at, 3);
        LLVMValueRef at_args[3] = { trec8, cg_i64(g, (uint64_t)idx), out8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(at), at,
                       at_args, 3, "");
        LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                              out, 3, "h");
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hp,
                                        "th");
        const char* t = cg_node_type_name(g, node);
        CwExpr e = { h, t ? t : "Any" };
        return e;
    }
    if (!mkind || strcmp(mkind, "field") != 0) {
        cg_error_at(g, node, "only field access is supported here (attribute/method calls go elsewhere)");
        return (CwExpr){ NULL, NULL };
    }
    const char* fname = cg_json_name(node);
    if (!fname) {
        cg_error_at(g, node, "field access is missing a field name");
        return (CwExpr){ NULL, NULL };
    }
    CwExpr obj = cg_expr(g, cw_object_get(node, "obj"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    cw_value* ot = cg_node_ann_type(cw_object_get(node, "obj"));
    const CwLayout_t* L = cg_struct_layout(g, ot);
    if (!L) {
        cg_error_at(g, node, "field access target is not a known struct");
        return (CwExpr){ NULL, NULL };
    }
    size_t off = 0;
    bool found = false;
    for (size_t i = 0; i < L->field_count; i++) {
        if (strcmp(L->fields[i].name, fname) == 0) {
            off = L->fields[i].offset;
            found = true;
            break;
        }
    }
    if (!found) {
        cg_error_at(g, node, "struct has no field %s", fname);
        return (CwExpr){ NULL, NULL };
    }
    LLVMValueRef base = cg_expr_blob_i8(g, obj);
    LLVMValueRef slot = cg_struct_slot(g, base, off);
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, slot, "fh");
    const char* t = cg_node_type_name(g, node);
    if (!t) {
        /* 泛型方法内字段访问的 ann.type 可能是 null/opaque (前端留白),
         * 布局里已做实参替换, 用它兜底 (也让标量返回走 fnret 全局) */
        for (size_t i = 0; i < L->field_count; i++) {
            if (strcmp(L->fields[i].name, fname) == 0) {
                t = cwtype_name(g->ll->types, L->fields[i].type);
                break;
            }
        }
    }
    CwExpr e = { h, t ? t : "Any" };
    return e;
}

static CwExpr cg_expr(CwCodegen_t* g, cw_value* node) {
    if (!node || cw_typeof(node) != CW_OBJECT) {
        cg_error(g, "expression is empty");
        return (CwExpr){ NULL, NULL };
    }
    const char* kind = cg_node_kind(node);
    if (!kind) {
        cg_error(g, "expression is missing kind");
        return (CwExpr){ NULL, NULL };
    }
    if (strcmp(kind, "IntLit") == 0 || strcmp(kind, "FloatLit") == 0
        || strcmp(kind, "BoolLit") == 0 || strcmp(kind, "StrLit") == 0
        || strcmp(kind, "VectorLit") == 0 || strcmp(kind, "MapLit") == 0
        || strcmp(kind, "TupleLit") == 0
        || strcmp(kind, "StructConstruct") == 0) {
        return cg_expr_lit(g, node);
    }
    if (strcmp(kind, "Name") == 0) return cg_expr_name(g, node);
    if (strcmp(kind, "Attribute") == 0) return cg_expr_attribute(g, node);
    if (strcmp(kind, "BinOp") == 0) return cg_expr_binop(g, node);
    if (strcmp(kind, "UnaryOp") == 0) return cg_expr_unary(g, node);
    if (strcmp(kind, "Call") == 0) return cg_expr_call(g, node);
    if (strcmp(kind, "Index") == 0) return cg_expr_index(g, node);
    if (strcmp(kind, "MatchStmt") == 0) return cg_expr_match(g, node);
    cg_error_at(g, node, "expression not supported yet: %s", kind);
    return (CwExpr){ NULL, NULL };
}

/* ---- 语句 ---- */

static void cg_stmt(CwCodegen_t* g, cw_value* node);

/* for 迭代变量类型: ForStmt.type 优先, 否则 iterable 的 Vector<T> 实参 */
static const char* cg_elem_type(CwCodegen_t* g, cw_value* node) {
    cw_value* ft = cw_object_get(node, "type");
    if (ft && cw_typeof(ft) == CW_OBJECT) {
        const char* n = cg_type_name_of(g, ft);
        if (n) return n;
    }
    cw_value* it = cw_object_get(node, "iterable");
    cw_value* t = it ? cg_node_ann_type(it) : NULL;
    if (t && cw_typeof(t) == CW_OBJECT) {
        const char* tn = cg_json_name(t);
        if (tn && strcmp(tn, "Map") == 0) return "Tuple";
        cw_value* args = cw_object_get(t, "args");
        if (args && cw_typeof(args) == CW_ARRAY && cw_array_size(args) > 0) {
            const char* n = cg_type_name_of(g, cw_array_get(args, 0));
            if (n) return n;
        }
    }
    return NULL;
}

static void cg_block(CwCodegen_t* g, cw_value* block) {
    cw_value* stmts = cw_object_get(block, "stmts");
    if (!stmts || cw_typeof(stmts) != CW_ARRAY) {
        cg_error(g, "Block is missing stmts");
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
    const char* type_name = cg_type_name_of(g, type_v);
    if (!type_name) type_name = cg_node_type_name(g, node);
    if (!name || !type_name) {
        cg_error(g, "LetStmt is missing name/type");
        return;
    }
    if (!cg_var_declare(g, name, type_name, type_v)) return;
    CwVar_t* v = cg_var_find(g, name);
    CwExpr e = cg_expr(g, cw_object_get(node, "value"));
    if (!g->failed) cg_rec_store(g, v, e);
}

static void cg_stmt_assign(CwCodegen_t* g, cw_value* node) {
    cw_value* opv = cw_object_get(node, "op");
    const char* op = (opv && cw_typeof(opv) == CW_STRING)
        ? cw_string_cstr(opv) : "";
    cw_value* target = cw_object_get(node, "target");
    if (target && cw_typeof(target) == CW_OBJECT
        && strcmp(cg_node_kind(target), "Index") == 0) {
        if (strcmp(op, "=") != 0) {
            cg_error(g, "compound assignment to an index is not supported yet: %s", op);
            return;
        }
        cw_value* tobj = cw_object_get(target, "obj");
        const char* ot = cg_node_type_name(g, tobj);
        if (!ot) {
            cg_error(g, "index assignment is missing the container type");
            return;
        }
        LLVMValueRef rec = cg_expr_record(g, tobj);
        if (g->failed) return;
        LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g),
                                             "");
        if (strcmp(ot, "Vector") == 0) {
            CwExpr idx = cg_expr(g, cw_object_get(target, "index"));
            if (g->failed) return;
            LLVMValueRef ix = cg_index_i64(g, idx);
            CwExpr val = cg_expr(g, cw_object_get(node, "value"));
            if (g->failed) return;
            LLVMValueRef er = cg_materialize_record(g, val);
            LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er, cg_rt_i8_ptr(g),
                                                "");
            LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g),
                                  LLVMInt64TypeInContext(cg_ctx(g)),
                                  cg_rt_i8_ptr(g) };
            LLVMValueRef f = cg_rt_declare(g, "cwvec_set",
                                           LLVMInt1TypeInContext(cg_ctx(g)),
                                           pt, 3);
            LLVMValueRef av[3] = { rec8, ix, er8 };
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
        } else if (strcmp(ot, "Map") == 0) {
            CwExpr k = cg_expr(g, cw_object_get(target, "index"));
            if (g->failed) return;
            LLVMValueRef kr = cg_materialize_record(g, k);
            LLVMValueRef kr8 = LLVMBuildBitCast(cg_b(g), kr, cg_rt_i8_ptr(g),
                                                "");
            CwExpr val = cg_expr(g, cw_object_get(node, "value"));
            if (g->failed) return;
            LLVMValueRef vr = cg_materialize_record(g, val);
            LLVMValueRef vr8 = LLVMBuildBitCast(cg_b(g), vr, cg_rt_i8_ptr(g),
                                                "");
            LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                  cg_rt_i8_ptr(g) };
            LLVMValueRef f = cg_rt_declare(g, "cwmap_put",
                                           LLVMInt1TypeInContext(cg_ctx(g)),
                                           pt, 3);
            LLVMValueRef av[3] = { rec8, kr8, vr8 };
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
        } else {
            cg_error(g, "only Vector/Map index assignment is supported (got %s)", ot);
            return;
        }
        return;
    }
    if (target && cw_typeof(target) == CW_OBJECT
        && strcmp(cg_node_kind(target), "Attribute") == 0) {
        if (strcmp(op, "=") != 0) {
            cg_error(g, "compound field assignment is not supported yet: %s", op);
            return;
        }
        cw_value* ann = cw_object_get(target, "ann");
        cw_value* member = ann ? cw_object_get(ann, "member") : NULL;
        const char* mkind = member ? cg_json_kind(member) : NULL;
        if (!mkind || strcmp(mkind, "field") != 0) {
            cg_error(g, "only field assignment is supported");
            return;
        }
        const char* fname = cg_json_name(target);
        if (!fname) {
            cg_error(g, "field assignment is missing a field name");
            return;
        }
        CwExpr obj = cg_expr(g, cw_object_get(target, "obj"));
        if (g->failed) return;
        cw_value* ot = cg_node_ann_type(cw_object_get(target, "obj"));
        const CwLayout_t* L = cg_struct_layout(g, ot);
        if (!L) {
            cg_error(g, "field assignment target is not a known struct");
            return;
        }
        size_t off = 0;
        bool found = false;
        for (size_t i = 0; i < L->field_count; i++) {
            if (strcmp(L->fields[i].name, fname) == 0) {
                off = L->fields[i].offset;
                found = true;
                break;
            }
        }
        if (!found) {
            cg_error(g, "struct has no field %s", fname);
            return;
        }
        CwExpr val = cg_expr(g, cw_object_get(node, "value"));
        if (g->failed) return;
        LLVMValueRef base = cg_expr_blob_i8(g, obj);
        size_t fi = 0;
        for (size_t i = 0; i < L->field_count; i++) {
            if (L->fields[i].offset == off) {
                fi = i;
                break;
            }
        }
        cg_store_struct_field(g, base, L, fi, val);
        return;
    }
    if (target && cw_typeof(target) == CW_OBJECT
        && strcmp(cg_node_kind(target), "Name") == 0) {
        cw_value* parts = cw_object_get(target, "parts");
        if (parts && cw_typeof(parts) == CW_ARRAY
            && cw_array_size(parts) == 2) {
            cw_value* p0 = cw_array_get(parts, 0);
            cw_value* p1 = cw_array_get(parts, 1);
            const char* owner = (p0 && cw_typeof(p0) == CW_STRING)
                ? cw_string_cstr(p0) : NULL;
            const char* member = (p1 && cw_typeof(p1) == CW_STRING)
                ? cw_string_cstr(p1) : NULL;
            cw_value* ann = cw_object_get(target, "ann");
            cw_value* binding = ann ? cw_object_get(ann, "binding") : NULL;
            const char* bk = binding ? cg_json_kind(binding) : NULL;
            if (owner && member && bk && strcmp(bk, "field") == 0) {
                const char* real_owner = cg_static_owner(g, owner);
                cw_value* type_obj = NULL;
                if (!cg_static_field(g, real_owner, member, &type_obj)) {
                    cg_error(g, "static field not found: %s::%s",
                             real_owner ? real_owner : owner, member);
                    return;
                }
                const char* t = cg_node_type_name(g, target);
                if (!t) t = type_obj
                    ? cg_type_name_of(g, type_obj) : NULL;
                if (!t) {
                    cg_error(g, "static field is missing a type: %s::%s",
                             real_owner ? real_owner : owner, member);
                    return;
                }
                CwExpr val = cg_expr(g, cw_object_get(node, "value"));
                if (g->failed) return;
                if (strcmp(op, "=") == 0) {
                    if (!cg_static_store(g, real_owner, member, val,
                                         t, type_obj)) {
                        cg_error(g, "cannot assign static field: %s::%s",
                                 real_owner ? real_owner : owner, member);
                    }
                    return;
                }
                if (!cg_is_scalar(t)) {
                    cg_error(g,
                        "compound assignment to a static field supports scalars only: %s::%s =%s",
                        real_owner ? real_owner : owner, member, op);
                    return;
                }
                LLVMValueRef gv = cg_static_storage(
                    g, real_owner, member, t, type_obj);
                if (!gv) {
                    cg_error(g, "cannot locate static field: %s::%s",
                             real_owner ? real_owner : owner, member);
                    return;
                }
                LLVMTypeRef vt = cg_scalar_type(g, t, NULL);
                LLVMValueRef cur = LLVMBuildLoad2(
                    cg_b(g), vt, gv, "st.cur");
                val = cg_coerce_scalar(g, val, t);
                if (g->failed) return;
                LLVMValueRef rhs = cg_load_value(g, val, vt);
                const bool is_float = strcmp(t, "Float") == 0
                    || strcmp(t, "Float64") == 0;
                const bool uns = cg_is_unsigned(t);
                LLVMValueRef res = NULL;
                if (is_float) {
                    if (strcmp(op, "+=") == 0)
                        res = LLVMBuildFAdd(cg_b(g), cur, rhs, "acc");
                    else if (strcmp(op, "-=") == 0)
                        res = LLVMBuildFSub(cg_b(g), cur, rhs, "acc");
                    else if (strcmp(op, "*=") == 0)
                        res = LLVMBuildFMul(cg_b(g), cur, rhs, "acc");
                    else if (strcmp(op, "/=") == 0)
                        res = LLVMBuildFDiv(cg_b(g), cur, rhs, "acc");
                    else {
                        cg_error(g,
                            "float compound assignment to a static field is not supported: %s",
                            op);
                        return;
                    }
                } else if (strcmp(op, "+=") == 0)
                    res = LLVMBuildAdd(cg_b(g), cur, rhs, "acc");
                else if (strcmp(op, "-=") == 0)
                    res = LLVMBuildSub(cg_b(g), cur, rhs, "acc");
                else if (strcmp(op, "*=") == 0)
                    res = LLVMBuildMul(cg_b(g), cur, rhs, "acc");
                else if (strcmp(op, "/=") == 0)
                    res = uns ? LLVMBuildUDiv(cg_b(g), cur, rhs, "acc")
                              : LLVMBuildSDiv(cg_b(g), cur, rhs, "acc");
                else if (strcmp(op, "%=") == 0)
                    res = uns ? LLVMBuildURem(cg_b(g), cur, rhs, "acc")
                              : LLVMBuildSRem(cg_b(g), cur, rhs, "acc");
                else if (strcmp(op, "<<=") == 0)
                    res = LLVMBuildShl(cg_b(g), cur, rhs, "acc");
                else if (strcmp(op, ">>=") == 0)
                    res = uns ? LLVMBuildLShr(cg_b(g), cur, rhs, "acc")
                              : LLVMBuildAShr(cg_b(g), cur, rhs, "acc");
                else if (strcmp(op, "&=") == 0)
                    res = LLVMBuildAnd(cg_b(g), cur, rhs, "acc");
                else if (strcmp(op, "|=") == 0)
                    res = LLVMBuildOr(cg_b(g), cur, rhs, "acc");
                else if (strcmp(op, "^=") == 0)
                    res = LLVMBuildXor(cg_b(g), cur, rhs, "acc");
                else {
                    cg_error(g,
                        "unsupported compound assignment to a static field: %s",
                        op);
                    return;
                }
                LLVMBuildStore(cg_b(g), res, gv);
                return;
            }
        }
    }
    if (!target || cw_typeof(target) != CW_OBJECT
        || strcmp(cg_node_kind(target), "Name") != 0) {
        cg_error(g, "only Name / Vector/Map index / field assignment is supported");
        return;
    }
    cw_value* parts = cw_object_get(target, "parts");
    const char* name = (parts && cw_typeof(parts) == CW_ARRAY
                        && cw_array_size(parts) == 1)
        ? cw_string_cstr(cw_array_get(parts, 0)) : NULL;
    CwVar_t* v = name ? cg_var_find(g, name) : NULL;
    if (!v) {
        cg_error(g, "assignment target is not declared: %s", name ? name : "?");
        return;
    }
    CwExpr e = cg_expr(g, cw_object_get(node, "value"));
    if (g->failed) return;

    if (strcmp(op, "=") == 0) {
        cg_rec_store(g, v, e);
        return;
    }

    /* String += : 拼接当前值 + 右值, 结果引用语义写回变量 */
    if (strcmp(op, "+=") == 0 && strcmp(v->type_name, "String") == 0) {
        CwExpr cur = cg_var_read(g, v);
        CwExpr res = cg_builtin_concat(g, cur, e);
        if (g->failed) return;
        cg_rec_store(g, v, res);
        return;
    }

    /* 复合赋值: 仅标量 */
    if (!v->storage || !cg_is_scalar(v->type_name)) {
        cg_error(g, "compound assignment supports scalars only: %s =%s", v->name, op);
        return;
    }
    size_t size = 0;
    LLVMTypeRef vt = cg_scalar_type(g, v->type_name, &size);
    LLVMValueRef cur = LLVMBuildLoad2(cg_b(g), vt, v->storage, "cur");
    LLVMValueRef rhs_src = cg_load_value(
        g, e, cg_scalar_type(g, e.type_name, NULL));
    LLVMValueRef rhs = cg_convert_scalar(g, rhs_src, e.type_name,
                                         v->type_name);
    if (g->failed) return;
    const bool is_float = strcmp(v->type_name, "Float") == 0
        || strcmp(v->type_name, "Float64") == 0;
    const bool uns = cg_is_unsigned(v->type_name);
    LLVMValueRef res = NULL;
    if (is_float) {
        if (strcmp(op, "+=") == 0) res = LLVMBuildFAdd(cg_b(g), cur, rhs, "acc");
        else if (strcmp(op, "-=") == 0) res = LLVMBuildFSub(cg_b(g), cur, rhs, "acc");
        else if (strcmp(op, "*=") == 0) res = LLVMBuildFMul(cg_b(g), cur, rhs, "acc");
        else if (strcmp(op, "/=") == 0) res = LLVMBuildFDiv(cg_b(g), cur, rhs, "acc");
        else {
            cg_error(g, "float compound assignment is not supported: %s", op);
            return;
        }
    } else if (strcmp(op, "+=") == 0) res = LLVMBuildAdd(cg_b(g), cur, rhs, "acc");
    else if (strcmp(op, "-=") == 0) res = LLVMBuildSub(cg_b(g), cur, rhs, "acc");
    else if (strcmp(op, "*=") == 0) res = LLVMBuildMul(cg_b(g), cur, rhs, "acc");
    else if (strcmp(op, "/=") == 0)
        res = uns ? LLVMBuildUDiv(cg_b(g), cur, rhs, "acc")
                  : LLVMBuildSDiv(cg_b(g), cur, rhs, "acc");
    else if (strcmp(op, "%=") == 0)
        res = uns ? LLVMBuildURem(cg_b(g), cur, rhs, "acc")
                  : LLVMBuildSRem(cg_b(g), cur, rhs, "acc");
    else if (strcmp(op, "<<=") == 0) res = LLVMBuildShl(cg_b(g), cur, rhs, "acc");
    else if (strcmp(op, ">>=") == 0)
        res = uns ? LLVMBuildLShr(cg_b(g), cur, rhs, "acc")
                  : LLVMBuildAShr(cg_b(g), cur, rhs, "acc");
    else if (strcmp(op, "&=") == 0) res = LLVMBuildAnd(cg_b(g), cur, rhs, "acc");
    else if (strcmp(op, "|=") == 0) res = LLVMBuildOr(cg_b(g), cur, rhs, "acc");
    else if (strcmp(op, "^=") == 0) res = LLVMBuildXor(cg_b(g), cur, rhs, "acc");
    else {
        cg_error(g, "unsupported compound assignment: %s", op);
        return;
    }
    cg_rec_store_value(g, v, res, v->type_name);
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
        size_t rsize = 0;
        cg_scalar_type(g, g->current_ret_type, &rsize);
        size_t esize = 0;
        LLVMTypeRef evt = cg_scalar_type(g, e.type_name, &esize);
        LLVMValueRef src = evt ? cg_load_value(g, e, evt) : e.handle;
        LLVMValueRef val = cg_convert_scalar(g, src, e.type_name,
                                             g->current_ret_type);
        if (g->failed) return;
        LLVMBuildStore(cg_b(g), val, g->ret_global);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), g->ret_global, LLVMInt64TypeInContext(cg_ctx(g)),
            "ret.addr");
        LLVMValueRef h = cg_build_handle(g, cg_i64(g, 0), addr,
                                         cg_i64(g, rsize), cg_i64(g, 0));
        LLVMBuildRet(cg_b(g), h);
        return;
    }
    if (g->ret_struct_global && g->current_ret_type
        && strcmp(e.type_name, g->current_ret_type) == 0
        && cg_is_struct_type(g, g->current_ret_type)) {
        /* 结构体值拷进全局缓冲再返回, 避免指向已失效的 callee alloca */
        LLVMValueRef src = cg_expr_blob_i8(g, e);
        LLVMValueRef dst = cg_blob_i8(g, g->ret_struct_global);
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1,
                        cg_i64(g, (uint64_t)g->ret_struct_size));
        cg_rebase_struct_fields(g, dst, g->ret_struct_layout);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), g->ret_struct_global, LLVMInt64TypeInContext(cg_ctx(g)),
            "ret.addr");
        LLVMValueRef h = cg_build_handle(g, cg_i64(g, 0), addr,
                                         cg_i64(g, g->ret_struct_fields),
                                         cg_i64(g, 0));
        LLVMBuildRet(cg_b(g), h);
        return;
    }
    if (g->ret_struct_global && g->current_ret_type
        && strcmp(e.type_name, g->current_ret_type) == 0
        && cg_is_enum_type(g, g->current_ret_type)) {
        LLVMValueRef src = cg_expr_blob_i8(g, e);
        LLVMValueRef dst = cg_blob_i8(g, g->ret_struct_global);
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1,
                        cg_i64(g, (uint64_t)g->ret_struct_size));
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), g->ret_struct_global, LLVMInt64TypeInContext(cg_ctx(g)),
            "ret.addr");
        LLVMValueRef h = cg_build_handle(
            g, cg_i64(g, 0), addr,
            cg_i64(g, cg_enum_slot_count(g, g->current_ret_type)),
            cg_i64(g, 0));
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
    if (!g->failed && !cg_block_terminated(g)) LLVMBuildBr(cg_b(g), end_bb);

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
            if (!g->failed && !cg_block_terminated(g)) {
                LLVMBuildBr(cg_b(g), end_bb);
            }
            LLVMPositionBuilderAtEnd(cg_b(g), enext);
        }
        cw_value* else_ = cw_object_get(node, "else_");
        if (else_ && cw_typeof(else_) == CW_OBJECT) {
            cg_block(g, else_);
        }
        if (!g->failed && !cg_block_terminated(g)) {
            LLVMBuildBr(cg_b(g), end_bb);
        }
    } else {
        cw_value* else_ = cw_object_get(node, "else_");
        if (else_ && cw_typeof(else_) == CW_OBJECT) {
            cg_block(g, else_);
        }
        if (!g->failed && !cg_block_terminated(g)) {
            LLVMBuildBr(cg_b(g), end_bb);
        }
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
    g->loops = (CwLoop_t*)realloc(
        g->loops, (g->loop_count + 1) * sizeof(CwLoop_t));
    g->loops[g->loop_count++] = (CwLoop_t){ end_bb, cond_bb };
    cg_block(g, cw_object_get(node, "body"));
    g->loop_count--;
    if (!g->failed && !cg_block_terminated(g)) {
        LLVMBuildBr(cg_b(g), cond_bb);
    }

    LLVMPositionBuilderAtEnd(cg_b(g), end_bb);
}

static void cg_stmt_for(CwCodegen_t* g, cw_value* node) {
    cw_value* var_v = cw_object_get(node, "var");
    const char* var = (var_v && cw_typeof(var_v) == CW_STRING)
        ? cw_string_cstr(var_v) : NULL;
    if (!var) {
        cg_error(g, "ForStmt is missing the iteration variable name");
        return;
    }
    const char* elem_type = cg_elem_type(g, node);
    if (!elem_type) elem_type = "Any";

    cw_value* iterable = cw_object_get(node, "iterable");
    const char* it_type = iterable ? cg_node_type_name(g, iterable) : NULL;
    const bool is_set = it_type && strcmp(it_type, "Set") == 0;
    const bool is_map = it_type && strcmp(it_type, "Map") == 0;
    if (!it_type || (strcmp(it_type, "Vector") != 0 && !is_set && !is_map)) {
        cg_error_at(g, node, "only Vector/Set/Map iteration is supported (got %s)",
                    it_type ? it_type : "?");
        return;
    }

    LLVMValueRef rec = cg_expr_record(g, iterable);
    if (g->failed) return;
    LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g), "");

    LLVMTypeRef iter_elems[2] = {
        cg_rt_i8_ptr(g),
        is_map ? cg_rt_i8_ptr(g)
               : LLVMInt64TypeInContext(cg_ctx(g))
    };
    LLVMTypeRef iter_type = LLVMStructTypeInContext(cg_ctx(g), iter_elems, 2,
                                                    false);
    LLVMValueRef it_slot = cg_alloca(g, iter_type, "it");
    LLVMTypeRef pt_begin[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
    const char* pf = is_set ? "cwset" : (is_map ? "cwmap" : "cwvec");
    char bname[64], vname[64], iname[64], nname[64];
    snprintf(bname, sizeof(bname), "%s_iter_begin", pf);
    snprintf(vname, sizeof(vname), "%s_iter_valid", pf);
    snprintf(iname, sizeof(iname), "%s_iter_%s", pf,
             is_set ? "item" : (is_map ? "key" : "value"));
    snprintf(nname, sizeof(nname), "%s_iter_next", pf);
    LLVMValueRef begin = cg_rt_declare(g, bname,
                                       LLVMVoidTypeInContext(cg_ctx(g)),
                                       pt_begin, 2);
    LLVMValueRef it8 = LLVMBuildBitCast(cg_b(g), it_slot, cg_rt_i8_ptr(g),
                                        "");
    LLVMValueRef begin_args[2] = { rec8, it8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(begin), begin, begin_args,
                   2, "");

    LLVMBasicBlockRef cond_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "for.cond");
    LLVMBasicBlockRef body_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "for.body");
    LLVMBasicBlockRef next_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "for.next");
    LLVMBasicBlockRef end_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "for.end");
    LLVMBuildBr(cg_b(g), cond_bb);

    LLVMPositionBuilderAtEnd(cg_b(g), cond_bb);
    LLVMValueRef valid = cg_rt_declare(g, vname,
                                       LLVMInt1TypeInContext(cg_ctx(g)),
                                       pt_begin, 1);
    LLVMValueRef valid_args[1] = { it8 };
    LLVMValueRef v0 = LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(valid),
                                     valid, valid_args, 1, "valid");
    LLVMBuildCondBr(cg_b(g), v0, body_bb, end_bb);

    LLVMPositionBuilderAtEnd(cg_b(g), body_bb);
    if (is_map) {
        /* Map 迭代变量 = 每轮构造的 (key, value) Tuple */
        LLVMValueRef key_out = cg_alloca(g, g->ll->rec_type, "map.key");
        LLVMValueRef val_out = cg_alloca(g, g->ll->rec_type, "map.val");
        LLVMValueRef key8 = LLVMBuildBitCast(cg_b(g), key_out,
                                             cg_rt_i8_ptr(g), "");
        LLVMValueRef val8 = LLVMBuildBitCast(cg_b(g), val_out,
                                             cg_rt_i8_ptr(g), "");
        LLVMTypeRef pt_kv[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
        LLVMValueRef kf = cg_rt_declare(
            g, "cwmap_iter_key", LLVMInt1TypeInContext(cg_ctx(g)),
            pt_kv, 2);
        LLVMValueRef vf = cg_rt_declare(
            g, "cwmap_iter_value", LLVMInt1TypeInContext(cg_ctx(g)),
            pt_kv, 2);
        LLVMValueRef kv_args[2] = { it8, key8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(kf), kf,
                       kv_args, 2, "");
        kv_args[1] = val8;
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(vf), vf,
                       kv_args, 2, "");

        LLVMValueRef trec = cg_alloca(g, g->ll->rec_type, "tup.rec");
        LLVMValueRef tid = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                               trec, 0, "tid");
        LLVMBuildStore(cg_b(g), cg_i32(g, CWTuple), tid);
        LLVMValueRef gcnt = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                trec, 1, "gc");
        LLVMBuildStore(cg_b(g), cg_i8(g, 0), gcnt);
        LLVMValueRef hz = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                              trec, 3, "hz");
        LLVMBuildStore(cg_b(g), cg_null_handle(g), hz);
        LLVMValueRef trec8 = LLVMBuildBitCast(cg_b(g), trec,
                                              cg_rt_i8_ptr(g), "");
        LLVMValueRef pair = cg_alloca(
            g, LLVMArrayType(g->ll->rec_type, 2), "map.pair");
        LLVMValueRef pair8 = LLVMBuildBitCast(cg_b(g), pair,
                                              cg_rt_i8_ptr(g), "");
        LLVMValueRef k_idx[1] = { cg_i64(g, 0) };
        LLVMValueRef kslot = LLVMBuildGEP2(
            cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), pair8,
            k_idx, 1, "pair.k");
        LLVMBuildMemCpy(cg_b(g), kslot, 1, key8, 1,
                        cg_i64(g, CWIND_OBJECT_RECORD_SIZE));
        LLVMValueRef v_idx[1] = {
            cg_i64(g, CWIND_OBJECT_RECORD_SIZE)
        };
        LLVMValueRef vslot = LLVMBuildGEP2(
            cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), pair8,
            v_idx, 1, "pair.v");
        LLVMBuildMemCpy(cg_b(g), vslot, 1, val8, 1,
                        cg_i64(g, CWIND_OBJECT_RECORD_SIZE));
        LLVMTypeRef pr_init[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                   LLVMInt64TypeInContext(cg_ctx(g)) };
        LLVMValueRef init = cg_rt_declare(
            g, "cwtuple_init", LLVMInt1TypeInContext(cg_ctx(g)),
            pr_init, 3);
        LLVMValueRef init_args[3] = { trec8, pair8, cg_i64(g, 2) };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(init), init,
                       init_args, 3, "");
        LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                                trec, 3, "h");
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hptr,
                                        "th");
        const char* map_elem = (elem_type && strcmp(elem_type, "Any") != 0)
            ? elem_type : "Tuple";
        if (!cg_var_declare(g, var, map_elem, NULL)) return;
        CwVar_t* v = cg_var_find(g, var);
        CwExpr te = { h, "Tuple" };
        cg_rec_store(g, v, te);
    } else {
        LLVMValueRef out = cg_alloca(g, g->ll->rec_type, "elem");
        LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                             cg_rt_i8_ptr(g), "");
        LLVMTypeRef pt_val[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
        LLVMValueRef val = cg_rt_declare(g, iname,
                                         LLVMInt1TypeInContext(cg_ctx(g)),
                                         pt_val, 2);
        LLVMValueRef val_args[2] = { it8, out8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(val), val,
                       val_args, 2, "");

        if (!cg_var_declare(g, var, elem_type, NULL)) return;
        CwVar_t* v = cg_var_find(g, var);
        v->record = out; /* 复用迭代元素记录 */
    }
    g->loops = (CwLoop_t*)realloc(
        g->loops, (g->loop_count + 1) * sizeof(CwLoop_t));
    g->loops[g->loop_count++] = (CwLoop_t){ end_bb, next_bb };
    cg_block(g, cw_object_get(node, "body"));
    g->loop_count--;
    if (!g->failed && !cg_block_terminated(g)) {
        LLVMBuildBr(cg_b(g), next_bb);
    }

    LLVMPositionBuilderAtEnd(cg_b(g), next_bb);
    LLVMValueRef next = cg_rt_declare(g, nname,
                                      LLVMVoidTypeInContext(cg_ctx(g)),
                                      pt_begin, 1);
    LLVMValueRef next_args[1] = { it8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(next), next, next_args,
                   1, "");
    LLVMBuildBr(cg_b(g), cond_bb);

    LLVMPositionBuilderAtEnd(cg_b(g), end_bb);
}

/* ---- 模式匹配 ---- */

/* 一个待创建的绑定: 模式完全匹配后统一声明/写值 */
typedef struct CwPatBind {
    const char* name;
    const char* type_name;
    cw_value* type_obj;
    LLVMValueRef handle;
} CwPatBind_t;

static void cg_pattern_prepare(CwCodegen_t* g, cw_value* pat, CwExpr subj,
                               LLVMBasicBlockRef fail_bb,
                               CwPatBind_t** binds, size_t* nb);

/* 字面量模式比较: 返回 i1, 相等则模式继续 */
static LLVMValueRef cg_pattern_cmp_literal(CwCodegen_t* g, CwExpr subj,
                                           cw_value* lit) {
    const char* lkind = cg_node_kind(lit);
    if (!lkind) {
        cg_error(g, "literal pattern is missing its literal");
        return NULL;
    }
    if (strcmp(lkind, "BoolLit") == 0) {
        bool bv = false;
        cg_json_bool(cw_object_get(lit, "value"), &bv);
        LLVMValueRef a = cg_load_value(
            g, subj, LLVMInt8TypeInContext(cg_ctx(g)));
        LLVMValueRef b = cg_i8(g, bv ? 1 : 0);
        return LLVMBuildICmp(cg_b(g), LLVMIntEQ, a, b, "pat.b");
    }
    if (strcmp(lkind, "IntLit") == 0 || strcmp(lkind, "FloatLit") == 0) {
        CwExpr le = cg_expr(g, lit);
        if (g->failed) return NULL;
        const char* st = subj.type_name;
        if (!st || !cg_is_scalar(st)) {
            cg_error(g, "literal pattern requires a scalar subject (got %s)",
                     st ? st : "?");
            return NULL;
        }
        LLVMTypeRef stt = cg_scalar_type(g, st, NULL);
        if (!stt) {
            cg_error(g, "unknown scalar subject type: %s", st);
            return NULL;
        }
        LLVMValueRef a = cg_load_value(g, subj, stt);
        LLVMValueRef b = cg_load_value(
            g, le, cg_scalar_type(g, le.type_name, NULL));
        b = cg_convert_scalar(g, b, le.type_name, st);
        if (g->failed) return NULL;
        const bool is_float = strcmp(st, "Float") == 0
            || strcmp(st, "Float64") == 0;
        return is_float
            ? LLVMBuildFCmp(cg_b(g), LLVMRealOEQ, a, b, "pat.f")
            : LLVMBuildICmp(cg_b(g), LLVMIntEQ, a, b, "pat.i");
    }
    if (strcmp(lkind, "StrLit") == 0) {
        CwExpr le = cg_expr(g, lit);
        if (g->failed) return NULL;
        LLVMValueRef sr = cg_materialize_record(g, subj);
        LLVMValueRef lr = cg_materialize_record(g, le);
        if (g->failed) return NULL;
        LLVMValueRef sr8 = LLVMBuildBitCast(cg_b(g), sr, cg_rt_i8_ptr(g), "");
        LLVMValueRef lr8 = LLVMBuildBitCast(cg_b(g), lr, cg_rt_i8_ptr(g), "");
        LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
        LLVMValueRef f = cg_rt_declare(
            g, "cwobj_equal", LLVMInt1TypeInContext(cg_ctx(g)), pt, 2);
        LLVMValueRef av[2] = { sr8, lr8 };
        return LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2,
                              "pat.s");
    }
    cg_error(g, "unsupported literal pattern: %s", lkind);
    return NULL;
}

/* 元组模式第 idx 个元素 → 子表达式 (句柄 + 元素类型) */
static CwExpr cg_pattern_tuple_elem(CwCodegen_t* g, CwExpr tup, int64_t idx,
                                    const char* elem_type) {
    LLVMValueRef trec = cg_materialize_record(g, tup);
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef trec8 = LLVMBuildBitCast(cg_b(g), trec, cg_rt_i8_ptr(g), "");
    LLVMValueRef out = cg_alloca(g, g->ll->rec_type, "pat.tup");
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out, cg_rt_i8_ptr(g), "");
    LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g),
                          LLVMInt64TypeInContext(cg_ctx(g)),
                          cg_rt_i8_ptr(g) };
    LLVMValueRef f = cg_rt_declare(
        g, "cwtuple_at", LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
    LLVMValueRef av[3] = { trec8, cg_i64(g, (uint64_t)idx), out8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
    LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type, out, 3,
                                          "h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hp, "th");
    return (CwExpr){ h, elem_type };
}

/* 结构体模式某字段 → 子表达式 (句柄 + 字段类型) */
static CwExpr cg_pattern_struct_field(CwCodegen_t* g, CwExpr obj,
                                      const CwLayout_t* L,
                                      const char* fname,
                                      const char* ftype) {
    size_t off = 0;
    bool found = false;
    for (size_t i = 0; i < L->field_count; i++) {
        if (strcmp(L->fields[i].name, fname) == 0) {
            off = L->fields[i].offset;
            found = true;
            break;
        }
    }
    if (!found) {
        cg_error(g, "struct pattern has no field %s", fname);
        return (CwExpr){ NULL, NULL };
    }
    LLVMValueRef base = cg_expr_blob_i8(g, obj);
    LLVMValueRef slot = cg_struct_slot(g, base, off);
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, slot, "fh");
    return (CwExpr){ h, ftype };
}

static void cg_pattern_bind_add(CwCodegen_t* g, CwPatBind_t** binds,
                                size_t* nb, const char* name,
                                const char* type_name, cw_value* type_obj,
                                LLVMValueRef handle) {
    CwPatBind_t* nbinds = (CwPatBind_t*)realloc(
        *binds, (*nb + 1) * sizeof(CwPatBind_t));
    if (!nbinds) {
        cg_error(g, "failed to grow the pattern binding list");
        return;
    }
    *binds = nbinds;
    (*binds)[*nb] = (CwPatBind_t){ name, type_name, type_obj, handle };
    (*nb)++;
}

/* 在当前块内逐项测试 pattern:
 *  - 字面量不匹配 → 无条件跳 fail_bb;
 *  - 元组/结构体递归解构;
 *  - 绑定只登记到 binds, 等整个模式成功后再统一创建变量。
 */
static void cg_pattern_prepare(CwCodegen_t* g, cw_value* pat, CwExpr subj,
                               LLVMBasicBlockRef fail_bb,
                               CwPatBind_t** binds, size_t* nb) {
    const char* kind = cg_node_kind(pat);
    if (!kind) {
        cg_error(g, "pattern is missing kind");
        return;
    }
    if (strcmp(kind, "WildcardPattern") == 0) return;
    if (strcmp(kind, "BindPattern") == 0) {
        cw_value* name_v = cw_object_get(pat, "name");
        const char* name = (name_v && cw_typeof(name_v) == CW_STRING)
            ? cw_string_cstr(name_v) : NULL;
        const char* type_name = cg_node_type_name(g, pat);
        if (!name || !type_name) {
            cg_error_at(g, pat, "binding pattern is missing its name/type");
            return;
        }
        cg_pattern_bind_add(g, binds, nb, name, type_name,
                            cg_node_ann_type(pat), subj.handle);
        return;
    }
    if (strcmp(kind, "LitPattern") == 0) {
        cw_value* lit = cw_object_get(pat, "value");
        LLVMValueRef c = cg_pattern_cmp_literal(g, subj, lit);
        if (g->failed) return;
        LLVMBasicBlockRef cont = LLVMAppendBasicBlockInContext(
            cg_ctx(g), g->current_fn, "pat.cont");
        LLVMBuildCondBr(cg_b(g), c, cont, fail_bb);
        LLVMPositionBuilderAtEnd(cg_b(g), cont);
        return;
    }
    if (strcmp(kind, "TuplePattern") == 0) {
        cw_value* elems = cw_object_get(pat, "elems");
        const size_t n = (elems && cw_typeof(elems) == CW_ARRAY)
            ? cw_array_size(elems) : 0;
        cw_value* ets = NULL;
        cw_value* ann = cw_object_get(pat, "ann");
        if (ann) ets = cw_object_get(ann, "element_types");
        for (size_t i = 0; i < n; i++) {
            cw_value* elem = cw_array_get(elems, i);
            const char* et = cg_node_type_name(g, elem);
            if (!et && ets && cw_typeof(ets) == CW_ARRAY
                && cw_array_size(ets) > i) {
                et = cg_type_name_of(g, cw_array_get(ets, i));
            }
            if (!et) {
                cg_error_at(g, pat,
                            "tuple pattern element %zu is missing its type",
                            i);
                return;
            }
            CwExpr sub = cg_pattern_tuple_elem(g, subj, (int64_t)i, et);
            if (g->failed) return;
            cg_pattern_prepare(g, elem, sub, fail_bb, binds, nb);
            if (g->failed) return;
        }
        return;
    }
    if (strcmp(kind, "StructPattern") == 0) {
        cw_value* type_v = cw_object_get(pat, "type");
        const CwLayout_t* L = cg_struct_layout(g, type_v);
        if (!L) {
            cg_error_at(g, pat, "struct pattern has an unknown layout");
            return;
        }
        cw_value* fields = cw_object_get(pat, "fields");
        const size_t n = (fields && cw_typeof(fields) == CW_ARRAY)
            ? cw_array_size(fields) : 0;
        for (size_t i = 0; i < n; i++) {
            cw_value* sf = cw_array_get(fields, i);
            const char* fname = cg_json_name(sf);
            if (!fname) {
                cg_error_at(g, sf, "struct pattern field is missing its name");
                return;
            }
            const char* ftype = NULL;
            for (size_t j = 0; j < L->field_count; j++) {
                if (strcmp(L->fields[j].name, fname) == 0) {
                    ftype = cwtype_name(g->ll->types, L->fields[j].type);
                    break;
                }
            }
            if (!ftype) {
                cg_error_at(g, sf, "struct pattern has no field %s", fname);
                return;
            }
            CwExpr sub = cg_pattern_struct_field(g, subj, L, fname, ftype);
            if (g->failed) return;
            cw_value* subpat = cw_object_get(sf, "pattern");
            if (subpat && cw_typeof(subpat) == CW_OBJECT) {
                cg_pattern_prepare(g, subpat, sub, fail_bb, binds, nb);
            } else {
                const char* st = cg_node_type_name(g, sf);
                if (!st) st = ftype;
                cg_pattern_bind_add(g, binds, nb, fname, st,
                                    cg_node_ann_type(sf), sub.handle);
            }
            if (g->failed) return;
        }
        return;
    }
    if (strcmp(kind, "EnumPattern") == 0) {
        cw_value* ann = cw_object_get(pat, "ann");
        cw_value* ev = ann ? cw_object_get(ann, "enum") : NULL;
        const char* enum_name = (ev && cw_typeof(ev) == CW_STRING)
            ? cw_string_cstr(ev) : NULL;
        int64_t vidx = -1;
        cw_value* vi = ann ? cw_object_get(ann, "variant_index") : NULL;
        if (!enum_name || !vi || cw_as_int(vi, &vidx) != CW_OK || vidx < 0) {
            cg_error_at(g, pat, "enum pattern is missing its variant index");
            return;
        }
        LLVMValueRef base = cg_expr_blob_i8(g, subj);
        if (g->failed) return;
        LLVMValueRef tag = LLVMBuildLoad2(
            cg_b(g), LLVMInt32TypeInContext(cg_ctx(g)),
            cg_enum_tag_ptr(g, base), "e.tag");
        LLVMValueRef c = LLVMBuildICmp(
            cg_b(g), LLVMIntEQ, tag, cg_i32(g, (uint32_t)vidx), "pat.en");
        LLVMBasicBlockRef cont = LLVMAppendBasicBlockInContext(
            cg_ctx(g), g->current_fn, "pat.en.cont");
        LLVMBuildCondBr(cg_b(g), c, cont, fail_bb);
        LLVMPositionBuilderAtEnd(cg_b(g), cont);
        cw_value* elems = cw_object_get(pat, "elems");
        const size_t n = (elems && cw_typeof(elems) == CW_ARRAY)
            ? cw_array_size(elems) : 0;
        for (size_t i = 0; i < n; i++) {
            cw_value* elem = cw_array_get(elems, i);
            const char* et = cg_node_type_name(g, elem);
            if (!et) {
                cg_error_at(g, elem,
                            "enum payload pattern %zu is missing its type",
                            i);
                return;
            }
            LLVMValueRef slot = cg_enum_slot(g, base, i);
            LLVMValueRef h = LLVMBuildLoad2(
                cg_b(g), g->ll->handle_type, slot, "eh");
            CwExpr sub = { h, et };
            cg_pattern_prepare(g, elem, sub, fail_bb, binds, nb);
            if (g->failed) return;
        }
        return;
    }
    cg_error_at(g, pat, "unsupported pattern: %s", kind);
}

/* 模式成功: 按登记顺序创建变量并写值 (此时所在块 = 模式全部通过) */
static bool cg_pattern_bind_all(CwCodegen_t* g, CwPatBind_t* binds,
                                size_t nb) {
    for (size_t k = 0; k < nb; k++) {
        CwPatBind_t* b = &binds[k];
        if (!cg_var_declare(g, b->name, b->type_name, b->type_obj)) {
            return false;
        }
    }
    for (size_t k = 0; k < nb; k++) {
        CwPatBind_t* b = &binds[k];
        CwVar_t* v = cg_var_find(g, b->name);
        if (!v) {
            cg_error(g, "pattern binding was not declared: %s", b->name);
            return false;
        }
        CwExpr be = { b->handle, b->type_name };
        if (!cg_rec_store(g, v, be)) return false;
    }
    return true;
}

/* Rust 风格 match 表达式: 每个臂体求值后汇入一个隐藏临时变量 */
static CwExpr cg_expr_match(CwCodegen_t* g, cw_value* node) {
    CwExpr subj = cg_expr(g, cw_object_get(node, "subject"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    cw_value* arms = cw_object_get(node, "arms");
    const size_t n = (arms && cw_typeof(arms) == CW_ARRAY)
        ? cw_array_size(arms) : 0;
    if (n == 0) {
        cg_error_at(g, node, "match must have at least one arm");
        return (CwExpr){ NULL, NULL };
    }
    cw_value* arm0 = cw_array_get(arms, 0);
    cw_value* body0 = cw_object_get(arm0, "body");
    const char* rtype = cg_node_type_name(g, body0);
    if (!rtype) {
        cg_error_at(g, node, "match expression is missing its result type");
        return (CwExpr){ NULL, NULL };
    }
    char vname[64];
    snprintf(vname, sizeof(vname), "$m.%zu", g->var_count);
    const char* stable = cg_own_name(g, vname);
    if (!cg_var_declare(g, stable, rtype, cg_node_ann_type(body0))) {
        return (CwExpr){ NULL, NULL };
    }
    CwVar_t* rv = cg_var_find(g, vname);
    if (!rv) return (CwExpr){ NULL, NULL };

    LLVMBasicBlockRef end_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "matchx.end");
    LLVMBasicBlockRef eval_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "matchx.arm0");
    LLVMBuildBr(cg_b(g), eval_bb);

    for (size_t i = 0; i < n && !g->failed; i++) {
        LLVMPositionBuilderAtEnd(cg_b(g), eval_bb);
        cw_value* arm = cw_array_get(arms, i);
        cw_value* pat = cw_object_get(arm, "pattern");
        cw_value* guard = cw_object_get(arm, "guard");
        cw_value* body = cw_object_get(arm, "body");
        LLVMBasicBlockRef fail_bb = (i + 1 < n)
            ? LLVMAppendBasicBlockInContext(cg_ctx(g), g->current_fn,
                                            "matchx.next")
            : end_bb;
        LLVMBasicBlockRef body_bb = LLVMAppendBasicBlockInContext(
            cg_ctx(g), g->current_fn, "matchx.body");
        cg_var_push_scope(g);
        CwPatBind_t* binds = NULL;
        size_t nb = 0;
        cg_pattern_prepare(g, pat, subj, fail_bb, &binds, &nb);
        if (g->failed) {
            free(binds);
            return (CwExpr){ NULL, NULL };
        }
        if (!cg_pattern_bind_all(g, binds, nb)) {
            free(binds);
            return (CwExpr){ NULL, NULL };
        }
        free(binds);
        if (guard && cw_typeof(guard) == CW_OBJECT) {
            CwExpr ge = cg_expr(g, guard);
            if (g->failed) return (CwExpr){ NULL, NULL };
            LLVMBuildCondBr(cg_b(g), cg_bool_cond(g, ge), body_bb, fail_bb);
        } else {
            LLVMBuildBr(cg_b(g), body_bb);
        }
        LLVMPositionBuilderAtEnd(cg_b(g), body_bb);
        CwExpr val = cg_expr(g, body);
        if (g->failed) return (CwExpr){ NULL, NULL };
        if (!cg_rec_store(g, rv, val)) {
            return (CwExpr){ NULL, NULL };
        }
        LLVMBuildBr(cg_b(g), end_bb);
        cg_var_pop_scope(g);
        eval_bb = fail_bb;
    }
    LLVMPositionBuilderAtEnd(cg_b(g), end_bb);
    return cg_var_read(g, rv);
}

static void cg_stmt_match(CwCodegen_t* g, cw_value* node) {
    CwExpr subj = cg_expr(g, cw_object_get(node, "subject"));
    if (g->failed) return;
    cw_value* arms = cw_object_get(node, "arms");
    const size_t n = (arms && cw_typeof(arms) == CW_ARRAY)
        ? cw_array_size(arms) : 0;
    if (n == 0) {
        cg_error_at(g, node, "match must have at least one arm");
        return;
    }
    LLVMBasicBlockRef end_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "match.end");
    LLVMBasicBlockRef eval_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "match.arm0");
    LLVMBuildBr(cg_b(g), eval_bb);

    for (size_t i = 0; i < n && !g->failed; i++) {
        LLVMPositionBuilderAtEnd(cg_b(g), eval_bb);
        cw_value* arm = cw_array_get(arms, i);
        cw_value* pat = cw_object_get(arm, "pattern");
        cw_value* guard = cw_object_get(arm, "guard");
        cw_value* body = cw_object_get(arm, "body");
        LLVMBasicBlockRef fail_bb = (i + 1 < n)
            ? LLVMAppendBasicBlockInContext(cg_ctx(g), g->current_fn,
                                            "match.next")
            : end_bb;
        LLVMBasicBlockRef body_bb = LLVMAppendBasicBlockInContext(
            cg_ctx(g), g->current_fn, "match.body");
        cg_var_push_scope(g);
        CwPatBind_t* binds = NULL;
        size_t nb = 0;
        cg_pattern_prepare(g, pat, subj, fail_bb, &binds, &nb);
        if (g->failed) {
            free(binds);
            return;
        }
        if (!cg_pattern_bind_all(g, binds, nb)) {
            free(binds);
            return;
        }
        free(binds);
        if (guard && cw_typeof(guard) == CW_OBJECT) {
            CwExpr ge = cg_expr(g, guard);
            if (g->failed) return;
            LLVMBuildCondBr(cg_b(g), cg_bool_cond(g, ge), body_bb, fail_bb);
        } else {
            LLVMBuildBr(cg_b(g), body_bb);
        }
        LLVMPositionBuilderAtEnd(cg_b(g), body_bb);
        if (body && strcmp(cg_node_kind(body), "Block") == 0) {
            cg_block(g, body);
            if (!g->failed && !cg_block_terminated(g)) {
                LLVMBuildBr(cg_b(g), end_bb);
            }
        } else {
            /* 表达式臂 (Rust 风格): 语句上下文里丢弃值 */
            cg_expr(g, body);
            if (!g->failed) LLVMBuildBr(cg_b(g), end_bb);
        }
        cg_var_pop_scope(g);
        eval_bb = fail_bb;
    }
    LLVMPositionBuilderAtEnd(cg_b(g), end_bb);
}

static void cg_stmt_if_let(CwCodegen_t* g, cw_value* node) {
    CwExpr subj = cg_expr(g, cw_object_get(node, "value"));
    if (g->failed) return;
    cw_value* then_body = cw_object_get(node, "then");
    cw_value* else_v = cw_object_get(node, "else_");
    const bool has_else = else_v && cw_typeof(else_v) == CW_OBJECT;
    LLVMBasicBlockRef end_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "iflet.end");
    LLVMBasicBlockRef match_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "iflet.match");
    cw_value* elifs = cw_object_get(node, "elifs");
    const size_t nelif = (elifs && cw_typeof(elifs) == CW_ARRAY)
        ? cw_array_size(elifs) : 0;
    LLVMBasicBlockRef fallthrough = has_else
        ? LLVMAppendBasicBlockInContext(cg_ctx(g), g->current_fn,
                                        "iflet.else")
        : end_bb;
    LLVMBasicBlockRef elif_entry = NULL;
    LLVMBasicBlockRef fail_bb = fallthrough;
    if (nelif > 0) {
        elif_entry = LLVMAppendBasicBlockInContext(
            cg_ctx(g), g->current_fn, "iflet.elif");
        fail_bb = elif_entry;
    }
    LLVMBuildBr(cg_b(g), match_bb);

    /* then 分支 */
    LLVMPositionBuilderAtEnd(cg_b(g), match_bb);
    LLVMBasicBlockRef body_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "iflet.then");
    cg_var_push_scope(g);
    CwPatBind_t* binds = NULL;
    size_t nb = 0;
    cg_pattern_prepare(g, cw_object_get(node, "pattern"), subj,
                       fail_bb, &binds, &nb);
    if (g->failed) {
        free(binds);
        return;
    }
    if (!cg_pattern_bind_all(g, binds, nb)) {
        free(binds);
        return;
    }
    free(binds);
    LLVMBuildBr(cg_b(g), body_bb);
    LLVMPositionBuilderAtEnd(cg_b(g), body_bb);
    cg_block(g, then_body);
    if (!g->failed && !cg_block_terminated(g)) {
        LLVMBuildBr(cg_b(g), end_bb);
    }
    cg_var_pop_scope(g);

    /* elif 链与 else */
    if (nelif > 0) {
        LLVMPositionBuilderAtEnd(cg_b(g), elif_entry);
        LLVMBasicBlockRef eval_bb = elif_entry;
        for (size_t i = 0; i < nelif && !g->failed; i++) {
            LLVMPositionBuilderAtEnd(cg_b(g), eval_bb);
            cw_value* br = cw_array_get(elifs, i);
            cw_value* cond = cw_object_get(br, "cond");
            cw_value* bval = cw_object_get(br, "value");
            cw_value* bpat = cw_object_get(br, "pattern");
            cw_value* bbody = cw_object_get(br, "body");
            LLVMBasicBlockRef next_fail = (i + 1 < nelif)
                ? LLVMAppendBasicBlockInContext(cg_ctx(g), g->current_fn,
                                                "iflet.next")
                : fallthrough;
            LLVMBasicBlockRef ebody_bb = LLVMAppendBasicBlockInContext(
                cg_ctx(g), g->current_fn, "iflet.elifbody");
            if (cond && cw_typeof(cond) == CW_OBJECT) {
                CwExpr ce = cg_expr(g, cond);
                if (g->failed) return;
                LLVMBuildCondBr(cg_b(g), cg_bool_cond(g, ce), ebody_bb,
                                next_fail);
            } else {
                CwExpr bsubj = cg_expr(g, bval);
                if (g->failed) return;
                cg_var_push_scope(g);
                CwPatBind_t* bbinds = NULL;
                size_t bnb = 0;
                cg_pattern_prepare(g, bpat, bsubj, next_fail,
                                   &bbinds, &bnb);
                if (g->failed) {
                    free(bbinds);
                    return;
                }
                if (!cg_pattern_bind_all(g, bbinds, bnb)) {
                    free(bbinds);
                    return;
                }
                free(bbinds);
                LLVMBuildBr(cg_b(g), ebody_bb);
            }
            LLVMPositionBuilderAtEnd(cg_b(g), ebody_bb);
            cg_block(g, bbody);
            if (!g->failed && !cg_block_terminated(g)) {
                LLVMBuildBr(cg_b(g), end_bb);
            }
            if (!(cond && cw_typeof(cond) == CW_OBJECT)) {
                cg_var_pop_scope(g);
            }
            eval_bb = next_fail;
        }
    }
    if (has_else) {
        LLVMPositionBuilderAtEnd(cg_b(g), fallthrough);
        cg_block(g, else_v);
        if (!g->failed && !cg_block_terminated(g)) {
            LLVMBuildBr(cg_b(g), end_bb);
        }
    }
    LLVMPositionBuilderAtEnd(cg_b(g), end_bb);
}

static void cg_stmt_break(CwCodegen_t* g) {
    if (g->loop_count == 0) {
        cg_error(g, "break outside a loop");
        return;
    }
    LLVMBuildBr(cg_b(g), g->loops[g->loop_count - 1].break_bb);
}

static void cg_stmt_continue(CwCodegen_t* g) {
    if (g->loop_count == 0) {
        cg_error(g, "continue outside a loop");
        return;
    }
    LLVMBuildBr(cg_b(g), g->loops[g->loop_count - 1].continue_bb);
}

static void cg_stmt(CwCodegen_t* g, cw_value* node) {
    if (!node || cw_typeof(node) != CW_OBJECT) {
        cg_error(g, "statement is empty");
        return;
    }
    cg_ensure_block(g);
    const char* kind = cg_node_kind(node);
    if (!kind) {
        cg_error(g, "statement is missing kind");
        return;
    }
    if (strcmp(kind, "LetStmt") == 0) { cg_stmt_let(g, node); return; }
    if (strcmp(kind, "Assign") == 0) { cg_stmt_assign(g, node); return; }
    if (strcmp(kind, "ReturnStmt") == 0) { cg_stmt_return(g, node); return; }
    if (strcmp(kind, "IfStmt") == 0) { cg_stmt_if(g, node); return; }
    if (strcmp(kind, "IfLetStmt") == 0) { cg_stmt_if_let(g, node); return; }
    if (strcmp(kind, "MatchStmt") == 0) { cg_stmt_match(g, node); return; }
    if (strcmp(kind, "WhileStmt") == 0) { cg_stmt_while(g, node); return; }
    if (strcmp(kind, "ForStmt") == 0) { cg_stmt_for(g, node); return; }
    if (strcmp(kind, "BreakStmt") == 0) { cg_stmt_break(g); return; }
    if (strcmp(kind, "ContinueStmt") == 0) { cg_stmt_continue(g); return; }
    if (strcmp(kind, "ExprStmt") == 0) {
        cw_value* expr = cw_object_get(node, "expr");
        /* 前端把赋值包装成 ExprStmt(Assign), 按语句处理 */
        if (expr && strcmp(cg_node_kind(expr), "Assign") == 0) {
            cg_stmt_assign(g, expr);
        } else {
            cg_expr(g, expr);
        }
        return;
    }
    cg_error_at(g, node, "statement not supported yet: %s", kind);
}

/* ---- 函数与 main 包装 ---- */

static void cg_emit_function(CwCodegen_t* g, const CwSymEntry_t* e) {
    if (e->kind != CW_SYM_FN && e->kind != CW_SYM_METHOD
        && e->kind != CW_SYM_INSTANCE) return;
    if (!e->decl) return;
    cw_value* body = cwmodule_fn_body(e->decl);
    if (!body) return; /* 纯声明 */

    LLVMValueRef fn = LLVMGetNamedFunction(g->ll->module, e->mangled);
    if (!fn) {
        cg_error(g, "function is not declared: %s", e->mangled);
        return;
    }
    g->current_fn = fn;
    g->current_owner = e->owner;
    g->scope_depth = 0;
    g->scope_mark_count = 0;
    /* 泛型实例上下文: 模板参数名 -> 实参 (函数体类型替换用) */
    g->tparam_names = NULL;
    g->targs = NULL;
    g->tcount = 0;
    if (e->kind == CW_SYM_INSTANCE) {
        /* 方法实例: 参数名 = owner (ExtraDecl/ImplDecl) params + 方法 type_params */
        const CwNode_t* owner_decl = NULL;
        if (e->owner) {
            for (size_t i = 0; i < cwmodule_binding_count(g->m); i++) {
                const CwBinding_t* bx = cwmodule_binding(g->m, i);
                if (bx->owner && strcmp(bx->owner, e->owner) == 0
                    && bx->fn_id == e->decl->id) {
                    owner_decl = cwmodule_node(g->m, bx->decl_id);
                    break;
                }
            }
        }
        cw_value* otp = owner_decl
            ? cw_object_get(owner_decl->value, "params") : NULL;
        cw_value* ftp = cw_object_get(e->decl->value, "type_params");
        const size_t n_owner = (otp && cw_typeof(otp) == CW_ARRAY)
            ? cw_array_size(otp) : 0;
        const size_t n_fn = (ftp && cw_typeof(ftp) == CW_ARRAY)
            ? cw_array_size(ftp) : 0;
        const size_t ntp = n_owner + n_fn;
        if (ntp > 0) {
            const char** names = (const char**)malloc(
                ntp * sizeof(const char*));
            if (!names) {
                cg_error(g, "failed to allocate generic parameter names");
                return;
            }
            size_t k = 0;
            for (size_t pass = 0; pass < 2; pass++) {
                cw_value* plist = (pass == 0) ? otp : ftp;
                const size_t np = (pass == 0) ? n_owner : n_fn;
                for (size_t i = 0; i < np; i++) {
                    names[k++] = cg_json_name(cw_array_get(plist, i));
                }
            }
            g->tparam_names = names;
            g->targs = e->inst_args;
            g->tcount = ntp;
        }
    }
    g->var_count = 0;
    g->scope_depth = 0;
    g->scope_mark_count = 0;
    cg_free_owned_names(g);
    g->current_ret_type = NULL;
    g->ret_global = NULL;
    g->ret_struct_global = NULL;
    g->ret_struct_size = 0;
    g->ret_struct_fields = 0;
    g->ret_struct_layout = NULL;
    cw_value* rtv = cwmodule_fn_return_type(e->decl);
    if (rtv) {
        g->current_ret_type = cg_type_name_of(g, rtv);
        if (g->current_ret_type && cg_is_scalar(g->current_ret_type)) {
            size_t size = 0;
            LLVMTypeRef vt = cg_scalar_type(g, g->current_ret_type, &size);
            char gname[64];
            snprintf(gname, sizeof(gname), "fnret.%s", e->mangled);
            g->ret_global = LLVMAddGlobal(g->ll->module, vt, gname);
            LLVMSetInitializer(g->ret_global, LLVMConstNull(vt));
        } else if (g->current_ret_type
                   && cg_is_struct_type(g, g->current_ret_type)) {
            const CwLayout_t* L = cg_struct_layout(g, rtv);
            if (!L) {
                cg_error(g, "unknown return type layout: %s", g->current_ret_type);
                return;
            }
            g->ret_struct_fields = L->field_count;
            g->ret_struct_layout = L;
            g->ret_struct_size = cg_struct_blob_size(g, L);
            char gname[64];
            snprintf(gname, sizeof(gname), "fnret.%s", e->mangled);
            LLVMTypeRef arr = LLVMArrayType(
                LLVMInt8TypeInContext(cg_ctx(g)), g->ret_struct_size);
            g->ret_struct_global = LLVMAddGlobal(g->ll->module, arr, gname);
            LLVMSetInitializer(g->ret_struct_global, LLVMConstNull(arr));
        } else if (g->current_ret_type
                   && cg_is_enum_type(g, g->current_ret_type)) {
            g->ret_struct_size = cg_enum_blob_size(g, g->current_ret_type);
            char gname[64];
            snprintf(gname, sizeof(gname), "fnret.%s", e->mangled);
            LLVMTypeRef arr = LLVMArrayType(
                LLVMInt8TypeInContext(cg_ctx(g)), g->ret_struct_size);
            g->ret_struct_global = LLVMAddGlobal(g->ll->module, arr, gname);
            LLVMSetInitializer(g->ret_struct_global, LLVMConstNull(arr));
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
            cg_error(g, "function %s parameter %u is missing a name", e->mangled, i);
            return;
        }
        cw_value* ptype = p ? cw_object_get(p, "type") : NULL;
        const bool ptype_ok = ptype && cw_typeof(ptype) == CW_OBJECT;
        const char* tname = ptype_ok ? cg_type_name_of(g, ptype) : NULL;
        if (!tname) tname = cg_node_type_name(g, p);
        if (!tname) tname = "Any";
        cw_value* ptype_obj = ptype_ok ? ptype : cg_node_ann_type(p);
        if (!cg_var_declare(g, pname, tname, ptype_obj)) return;
        CwVar_t* v = cg_var_find(g, pname);
        if (!v) return;
        LLVMSetValueName2(arg, pname, (unsigned)strlen(pname));
        CwExpr a = { arg, tname };
        if (!cg_rec_store(g, v, a)) return;
    }

    cg_block(g, body);
    if (g->failed) return;
    if (!cg_block_terminated(g)) {
        LLVMBuildRet(cg_b(g), cg_null_handle(g));
    }
    free((char**)g->tparam_names);
    g->tparam_names = NULL;
    g->targs = NULL;
    g->tcount = 0;
}

/* 调用所有静态字段的初始化函数 (main 入口处, 先于用户 main) */
static void cg_emit_static_inits(CwCodegen_t* g) {
    const size_t nsym = cwmodule_symbol_count(g->m);
    for (size_t i = 0; i < nsym && !g->failed; i++) {
        const CwSymbol_t* s = cwmodule_symbol(g->m, i);
        if (!s || strcmp(s->kind, "struct") != 0) continue;
        const CwNode_t* decl = cwmodule_node(g->m, s->ref);
        if (!decl) continue;
        cw_value* fields = cw_object_get(decl->value, "fields");
        if (!fields || cw_typeof(fields) != CW_ARRAY) continue;
        const size_t nf = cw_array_size(fields);
        for (size_t j = 0; j < nf && !g->failed; j++) {
            cw_value* f = cw_array_get(fields, j);
            bool is_static = false;
            cg_json_bool(cw_object_get(f, "static"), &is_static);
            if (!is_static) continue;
            const char* fname = cg_json_name(f);
            if (!fname) {
                cg_error(g, "static field is missing a name in struct %s",
                         s->name);
                return;
            }
            cw_value* type_obj = cw_object_get(f, "type");
            cw_value* ann = cw_object_get(f, "ann");
            cw_value* at = ann ? cw_object_get(ann, "type") : NULL;
            if (at && cw_typeof(at) == CW_OBJECT) type_obj = at;
            const char* tname = type_obj
                ? cg_type_name_of(g, type_obj) : NULL;
            if (!tname) {
                cg_error(g, "static field %s::%s is missing a type",
                         s->name, fname);
                return;
            }
            LLVMValueRef fn = cg_static_init_fn(
                g, s->name, fname, f, tname, type_obj);
            if (!fn || g->failed) return;
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn),
                           fn, NULL, 0, "");
        }
    }
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
    g->current_owner = NULL;

    cg_emit_static_inits(g);
    if (g->failed) return;

    const unsigned nparams = LLVMCountParams(user_main);
    LLVMValueRef* argv = (LLVMValueRef*)malloc(
        (nparams ? nparams : 1) * sizeof(LLVMValueRef));
    if (!argv) {
        cg_error(g, "failed to allocate the main argument array");
        return;
    }
    for (unsigned i = 0; i < nparams; i++) argv[i] = cg_null_handle(g);
    LLVMValueRef h = LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(user_main),
                                    user_main, argv, nparams, "main.call");
    free(argv);

    cw_value* rt = cwmodule_fn_return_type(main_sym->decl);
    const char* ret_type = rt ? cg_type_name_of(g, rt) : NULL;
    if (ret_type && cg_is_int(ret_type)) {
        LLVMValueRef addr = LLVMBuildExtractValue(cg_b(g), h, 1, "addr");
        LLVMValueRef ptr = LLVMBuildIntToPtr(cg_b(g), addr,
                                             LLVMPointerType(
                                                 LLVMVoidTypeInContext(
                                                     cg_ctx(g)),
                                                 0),
                                             "p");
        LLVMValueRef v = LLVMBuildLoad2(cg_b(g),
                                        cg_scalar_type(g, ret_type, NULL),
                                        ptr, "main.ret");
        LLVMValueRef r = cg_convert_scalar(g, v, ret_type, "Int32");
        if (!g->failed) LLVMBuildRet(cg_b(g), r);
        else LLVMBuildRet(cg_b(g), cg_i32(g, 0));
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
    g->alloca_builder = LLVMCreateBuilderInContext(ll->ctx);
    if (!g->alloca_builder) {
        LLVMDisposeBuilder(g->builder);
        g->builder = NULL;
        return false;
    }
    return true;
}

void cwcodegen_destroy(CwCodegen_t* g) {
    if (!g) return;
    if (g->builder) LLVMDisposeBuilder(g->builder);
    if (g->alloca_builder) LLVMDisposeBuilder(g->alloca_builder);
    free(g->loops);
    free(g->vars);
    free(g->scope_marks);
    cg_free_owned_names(g);
    free(g->owned_names);
    memset(g, 0, sizeof(*g));
}

bool cwcodegen_emit(CwCodegen_t* g) {
    if (!g || g->failed) return false;
    for (size_t i = 0; i < g->ll->syms->count && !g->failed; i++) {
        cg_emit_function(g, &g->ll->syms->items[i]);
    }
    /* 主体生成过程中可能新增泛型实例, 逐轮补齐 (实例内还可能再实例化) */
    size_t emitted = g->ll->syms->count;
    while (!g->failed) {
        const size_t cur = g->ll->syms->count;
        if (cur == emitted) break;
        for (size_t i = emitted; i < cur && !g->failed; i++) {
            cg_emit_function(g, &g->ll->syms->items[i]);
        }
        emitted = cur;
    }
    if (!g->failed) cg_emit_main_wrapper(g);
    return !g->failed;
}

const char* cwcodegen_error(const CwCodegen_t* g) {
    return g ? g->error : "?";
}
