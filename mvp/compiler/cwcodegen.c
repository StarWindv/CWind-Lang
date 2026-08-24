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

static void cg_error(
    CwCodegen_t* g,
    const char* fmt,
    ...
) {
    if (g->failed) return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g->error, sizeof(g->error), fmt, ap);
    va_end(ap);
    g->failed = true;
}

/* 带节点行号的报错 */
static void cg_error_at(
    CwCodegen_t* g, const cw_value*node,
    const char* fmt, ...
) {
    if (g->failed) return;
    char msg[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    int64_t line = 0;
    int64_t col = 0;
    cw_value* lv = node ? cw_object_get(node, "line") : NULL;
    cw_value* cv = node ? cw_object_get(node, "column") : NULL;
    if (lv && cw_typeof(lv) == CW_INT) cw_as_int(lv, &line);
    if (cv && cw_typeof(cv) == CW_INT) cw_as_int(cv, &col);
    snprintf(g->error, sizeof(g->error), "%s (line %lld, column %lld)",
             msg, (long long)line, (long long)col);
    g->failed = true;
}

static const char* cg_type_name_of(
    const CwCodegen_t* g,
    const cw_value*type_obj
);
static const char* cg_common_numeric(
    const char* a,
    const char* b
);
static LLVMValueRef cg_convert_scalar(
    CwCodegen_t* g, LLVMValueRef v,
    const char* from, const char* to
);
static const CwNode_t* cg_enum_decl(
    const CwCodegen_t* g,
    const char* name
);
static bool cg_is_enum_type(
    CwCodegen_t* g,
    const char* name
);
static size_t cg_enum_blob_size(
    CwCodegen_t* g,
    const char* name
);
static size_t cg_enum_slot_count(
    CwCodegen_t* g,
    const char* name
);
static LLVMValueRef cg_enum_handle(
    CwCodegen_t* g, LLVMValueRef blob,
    const char* enum_name
);
static CwExpr cg_expr_enum_build(
    CwCodegen_t* g, const char* enum_name,
    size_t variant_index,
    const cw_value*payload_types,
    const cw_value*args
);
static CwExpr cg_expr_match(
    CwCodegen_t* g,
    const cw_value*node
);
static LLVMValueRef cg_load_value(
    CwCodegen_t* g, CwExpr e,
    LLVMTypeRef value_type
);
static LLVMValueRef cg_rt_arena_alloc(
    CwCodegen_t* g,
    LLVMValueRef size
);
static LLVMTypeRef cg_rt_i8_ptr(
    CwCodegen_t* g
);
static LLVMValueRef cg_enum_payload_handle(
    CwCodegen_t* g, CwExpr val,
    const cw_value*type_obj
);
static bool cg_is_struct_type(
    CwCodegen_t* g,
    const char* name
);

static LLVMContextRef cg_ctx(
    CwCodegen_t* g
) {
    return g->ll->ctx;
}

static LLVMBuilderRef cg_b(
    CwCodegen_t* g
) {
    return g->builder;
}

/* alloca 统一进 entry block (LLVM Win64 对非入口 alloca 会逐块发
 * __chkstk+sub rsp, 循环回边不恢复, 长循环会泄漏栈) */
static LLVMValueRef cg_alloca(
    CwCodegen_t* g, LLVMTypeRef ty,
    const char* name
) {
    LLVMBasicBlockRef entry = LLVMGetEntryBasicBlock(g->current_fn);
    LLVMValueRef first = LLVMGetFirstInstruction(entry);
    if (first) {
        LLVMPositionBuilderBefore(g->alloca_builder, first);
    } else {
        LLVMPositionBuilderAtEnd(g->alloca_builder, entry);
    }
    return LLVMBuildAlloca(g->alloca_builder, ty, name);
}

static LLVMValueRef cg_i64(
    CwCodegen_t* g,
    uint64_t v
) {
    return LLVMConstInt(LLVMInt64TypeInContext(cg_ctx(g)), v, 0);
}

static LLVMValueRef cg_i32(
    CwCodegen_t* g,
    uint32_t v
) {
    return LLVMConstInt(LLVMInt32TypeInContext(cg_ctx(g)), v, 0);
}

static LLVMValueRef cg_i8(
    CwCodegen_t* g,
    uint8_t v
) {
    return LLVMConstInt(LLVMInt8TypeInContext(cg_ctx(g)), v, 0);
}

static LLVMValueRef cg_i16(
    CwCodegen_t* g,
    int64_t v
) {
    return LLVMConstInt(LLVMInt16TypeInContext(cg_ctx(g)),
                        (uint64_t)(int16_t)v, 1);
}

static LLVMValueRef cg_f64(
    CwCodegen_t* g,
    double v
) {
    return LLVMConstReal(LLVMDoubleTypeInContext(cg_ctx(g)), v);
}

static cw_value* cg_node_ann_type(
    const cw_value*node
) {
    if (!node || cw_typeof(node) != CW_OBJECT) return NULL;
    cw_value* ann = cw_object_get(node, "ann");
    if (!ann || cw_typeof(ann) != CW_OBJECT) return NULL;
    return cw_object_get(ann, "type");
}

static const char* cg_node_type_name(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* t = cg_node_ann_type(node);
    if (!t || cw_typeof(t) != CW_OBJECT) return NULL;
    return cg_type_name_of(g, t);
}

static const char* cg_json_name(
    const cw_value*obj
) {
    if (!obj || cw_typeof(obj) != CW_OBJECT) return NULL;
    cw_value* name = cw_object_get(obj, "name");
    return (name && cw_typeof(name) == CW_STRING)
        ? cw_string_cstr(name) : NULL;
}

/* 类型对象 → 基础名; 泛型实例上下文中把 opaque 参数叶替换成实参名.
   SA 已在 Type 节点的 ann.type 写出解析后的类型 (别名展开/精化还原),
   优先采用; 但 Self 节点除外 —— 其实例实参只存在于泛型上下文中,
   必须走 current_owner + targs 绑定. */
static const char* cg_type_name_of(
    const CwCodegen_t* g,
    const cw_value*type_obj
) {
    const char* raw = cg_json_name(type_obj);
    const char* n = NULL;
    if (!raw || strcmp(raw, "Self") != 0) {
        cw_value* resolved = cg_node_ann_type(type_obj);
        n = (resolved && cw_typeof(resolved) == CW_OBJECT)
            ? cg_json_name(resolved) : NULL;
    }
    if (!n) n = raw;
    if (!n) return NULL;
    if (strcmp(n, "Self") == 0 && g->current_owner) {
        n = g->current_owner; /* 方法内 Self -> 所属类型 */
    }
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

/* 类型对象 → CwTypeId; 泛型实例上下文中参数叶直接取实参 id.
   与 cg_type_name_of 一致: 优先采用 SA 解析后的 ann.type (Self 除外). */
static CwTypeId cg_type_id_of(
    const CwCodegen_t* g,
    const cw_value*type_obj
) {
    const char* raw = cg_json_name(type_obj);
    if (!raw || strcmp(raw, "Self") != 0) {
        cw_value* resolved = cg_node_ann_type(type_obj);
        if (resolved && cw_typeof(resolved) == CW_OBJECT
            && cg_json_name(resolved)) {
            type_obj = resolved;
        }
    }
    const char* n = cg_json_name(type_obj);
    if (n && strcmp(n, "Self") == 0 && g->current_owner) {
        /* Self -> owner 实例 (含当前实例上下文实参) */
        return cwtype_intern(g->ll->types, g->current_owner,
                             g->targs, g->tcount);
    }
    if (n && g->tcount > 0) {
        for (size_t i = 0; i < g->tcount; i++) {
            if (g->tparam_names[i] && strcmp(n, g->tparam_names[i]) == 0) {
                return g->targs[i];
            }
        }
    }
    return cwtype_from_json(g->ll->types, type_obj);
}

static const char* cg_json_kind(
    const cw_value*obj
) {
    if (!obj || cw_typeof(obj) != CW_OBJECT) return NULL;
    cw_value* k = cw_object_get(obj, "kind");
    return (k && cw_typeof(k) == CW_STRING) ? cw_string_cstr(k) : NULL;
}

static bool cg_json_bool(
    const cw_value*obj,
    bool* out
) {
    if (!obj || cw_typeof(obj) != CW_BOOL) return false;
    return cw_as_bool(obj, out) == CW_OK;
}

static bool cg_type_is_ref(
    const cw_value*type_obj
) {
    if (!type_obj || cw_typeof(type_obj) != CW_OBJECT) return false;
    bool ref = false;
    cg_json_bool(cw_object_get(type_obj, "ref"), &ref);
    if (!ref) {
        cw_value* ann = cw_object_get(type_obj, "ann");
        cw_value* t = ann ? cw_object_get(ann, "type") : NULL;
        cg_json_bool(t ? cw_object_get(t, "ref") : NULL, &ref);
    }
    return ref;
}

static const char* cg_node_kind(
    const cw_value*node
) {
    if (!node || cw_typeof(node) != CW_OBJECT) return NULL;
    cw_value* k = cw_object_get(node, "kind");
    return (k && cw_typeof(k) == CW_STRING) ? cw_string_cstr(k) : NULL;
}

/* ---- 类型映射 ---- */

static int cg_type_id(
    const char* name
) {
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

static bool cg_is_scalar(
    const char* name
) {
    const int id = cg_type_id(name);
    return id == CWInt || id == CWUInt || id == CWInt8 || id == CWUInt8
        || id == CWInt32 || id == CWUInt32 || id == CWInt64
        || id == CWUInt64 || id == CWByte || id == CWFloat
        || id == CWFloat64 || id == CWBool;
}

/* 函数指针类型 (`fn(Int) -> Int`): 值是 8 字节地址, 存储语义同标量 */
static bool cg_is_fnptr(
    const char* name
) {
    return name && strncmp(name, "fn(", 3) == 0;
}

/* 原始指针类型 (`*const T` / `*mut T`): 值是地址, 存储语义同标量 */
static bool cg_is_rawptr(
    const char* name
) {
    return name && (strncmp(name, "*const ", 7) == 0
                    || strncmp(name, "*mut ", 5) == 0);
}

static bool cg_is_int(
    const char* name
) {
    const int id = cg_type_id(name);
    return id == CWInt || id == CWUInt || id == CWInt8 || id == CWUInt8
        || id == CWInt32 || id == CWUInt32 || id == CWInt64
        || id == CWUInt64 || id == CWByte;
}

static bool cg_is_unsigned(
    const char* name
) {
    const int id = cg_type_id(name);
    return id == CWUInt || id == CWUInt8 || id == CWUInt32
        || id == CWUInt64 || id == CWByte;
}

static LLVMTypeRef cg_scalar_type(
    CwCodegen_t* g, const char* name,
    size_t* size
) {
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

static LLVMValueRef cg_build_handle(
    CwCodegen_t* g,
    LLVMValueRef object,
    LLVMValueRef address,
    LLVMValueRef length,
    LLVMValueRef cursor
) {
    LLVMValueRef h = LLVMGetUndef(g->ll->handle_type);
    h = LLVMBuildInsertValue(cg_b(g), h, object, 0, "h.obj");
    h = LLVMBuildInsertValue(cg_b(g), h, address, 1, "h.addr");
    h = LLVMBuildInsertValue(cg_b(g), h, length, 2, "h.len");
    h = LLVMBuildInsertValue(cg_b(g), h, cursor, 3, "h.cur");
    return h;
}

static LLVMValueRef cg_null_handle(
    CwCodegen_t* g
) {
    return LLVMConstNull(g->ll->handle_type);
}

/* 当前块是否已以 terminator 结尾 (return/br 之后不能再插指令) */
static bool cg_block_terminated(
    CwCodegen_t* g
) {
    LLVMBasicBlockRef bb = LLVMGetInsertBlock(g->builder);
    return bb && LLVMGetBasicBlockTerminator(bb) != NULL;
}

/* 语句级调用: 若当前块已终止, 开一个死块继续发射 */
static void cg_ensure_block(
    CwCodegen_t* g
) {
    if (!g->failed && cg_block_terminated(g)) {
        LLVMBasicBlockRef nb = LLVMAppendBasicBlockInContext(
            cg_ctx(g), g->current_fn, "dead");
        LLVMPositionBuilderAtEnd(cg_b(g), nb);
    }
}

static CwExpr cg_make_scalar(
    CwCodegen_t* g, LLVMValueRef value,
    LLVMTypeRef value_type,
    const char* type_name, size_t size
) {
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

static CwVar_t* cg_var_find(
    CwCodegen_t* g,
    const char* name
);
static CwExpr cg_expr(
    CwCodegen_t* g,
    const cw_value*node
);
static bool cg_binding_is(
    const cw_value*ann, const char* kind
);
static const CwNode_t* cg_extern_static_node(
    CwCodegen_t* g, const cw_value*node
);
static CwExpr cg_expr_extern_static_read(
    CwCodegen_t* g,
    const cw_value*node
); /* todo-56 */
static bool cg_assign_extern_static(
    CwCodegen_t* g, const cw_value*node,
    const cw_value*target, const char* op
); /* todo-56 */
static LLVMValueRef cg_extern_thunk(
    CwCodegen_t* g, const CwSymEntry_t* sym
); /* todo-54 */
static LLVMValueRef cg_compound_arith(
    CwCodegen_t* g, const char* op,
    LLVMValueRef l, LLVMValueRef r,
    const char* type_name, bool int_only,
    const char* err_float, const char* err_unsupported
);

static void cg_emit_closure_body(
    CwCodegen_t* g,
    const CwClosure_t* c
);

/* 生成变量名 (栈缓冲) 的稳定副本: 变量表只保存指针, 不能指向局部缓冲 */
static const char* cg_own_name(
    CwCodegen_t* g,
    const char* name
) {
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

static void cg_free_owned_names(
    CwCodegen_t* g
) {
    for (size_t i = 0; i < g->owned_name_count; i++) {
        free(g->owned_names[i]);
    }
    g->owned_name_count = 0;
}

static void cg_closure_reset(CwCodegen_t* g) {
    g->closure_count = 0;
}

/* 分配对象记录并写入 tid / gc / 句柄槽 (40B 值语义记录, rt 调用通用;
 * type_id 允许为 -1: 用户结构体/枚举元素按约定存 UINT32_MAX) */
static LLVMValueRef cg_new_record(
    CwCodegen_t* g, int type_id,
    LLVMValueRef handle, const char* name
) {
    LLVMValueRef rec = cg_alloca(g, g->ll->rec_type, name);
    LLVMValueRef tid = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                           rec, 0, "tid");
    LLVMBuildStore(cg_b(g), cg_i32(g, (uint32_t)type_id), tid);
    LLVMValueRef gcnt = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                            rec, 1, "gc");
    LLVMBuildStore(cg_b(g), cg_i8(g, 0), gcnt);
    LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                            rec, 3, "h");
    LLVMBuildStore(cg_b(g), handle, hptr);
    return rec;
}

/* 把表达式物化为 40 字节对象记录 (容器元素 / rt 调用参数用) */
static LLVMValueRef cg_materialize_record(
    CwCodegen_t* g,
    CwExpr e
) {
    const int type_id = cg_type_id(e.type_name);
    if (type_id < 0) {
        cg_error(g, "user structs are not supported as container elements/rt arguments: %s",
                 e.type_name ? e.type_name : "?");
        return NULL;
    }
    return cg_new_record(g, type_id, e.handle, "elem.rec");
}

/* 容器元素的值语义记录: 标量拷进 arena 单元 (进程期存活, GC 页落地后替换),
 * 避免循环里 `v.push_back(x)` 复用同一 entry alloca, 旧元素随 x 变化
 * (实测素数筛 for-in 读到同一个 num)。引用类型 (String/容器) 仍浅拷。 */
static LLVMValueRef cg_container_value_record(
    CwCodegen_t* g,
    CwExpr e
) {
    const int type_id = cg_type_id(e.type_name);
    if (type_id < 0) {
        cg_error(g, "user structs are not supported as container elements/rt arguments: %s",
                 e.type_name ? e.type_name : "?");
        return NULL;
    }
    LLVMValueRef handle = e.handle;
    if (cg_is_scalar(e.type_name)) {
        size_t size = 0;
        LLVMTypeRef vt = cg_scalar_type(g, e.type_name, &size);
        if (vt) {
            LLVMValueRef cell = cg_rt_arena_alloc(g, cg_i64(g, (uint64_t)size));
            LLVMValueRef p = LLVMBuildIntToPtr(cg_b(g), cell, cg_rt_i8_ptr(g),
                                               "elem.cell");
            LLVMValueRef v = cg_load_value(g, e, vt);
            LLVMBuildStore(cg_b(g), v, LLVMBuildBitCast(
                cg_b(g), p, LLVMPointerType(vt, 0), ""));
            LLVMValueRef addr = LLVMBuildPtrToInt(
                cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "elem.addr");
            handle = cg_build_handle(g, cg_i64(g, 0), addr,
                                     cg_i64(g, size), cg_i64(g, 0));
        }
    }
    return cg_new_record(g, type_id, handle, "elem.rec");
}

/* Vector/Tuple 元素记录: 允许用户结构体/枚举作为元素 (值语义),
 * 载荷统一经 cg_enum_payload_handle 深拷贝进 arena 单元;
 * Map/Set 键仍用 cg_container_value_record (rt 的哈希/相等未定义用户类型)。 */
static LLVMValueRef cg_vector_value_record(
    CwCodegen_t* g, CwExpr e,
    const cw_value*type_obj
) {
    const int type_id = cg_type_id(e.type_name);
    if (type_id < 0 && !cg_is_struct_type(g, e.type_name)
        && !cg_is_enum_type(g, e.type_name)) {
        cg_error(g, "unsupported container element type: %s",
                 e.type_name ? e.type_name : "?");
        return NULL;
    }
    LLVMValueRef handle = cg_enum_payload_handle(g, e, type_obj);
    if (g->failed) return NULL;
    return cg_new_record(g, type_id, handle, "elem.rec");
}

/* 取节点对应的容器记录: Name 用变量记录, 其它表达式临时物化 */
static LLVMValueRef cg_expr_record(
    CwCodegen_t* g,
    const cw_value*node
) {
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

static LLVMValueRef cg_handle_addr(
    CwCodegen_t* g,
    CwExpr e
) {
    return LLVMBuildExtractValue(cg_b(g), e.handle, 1, "addr");
}

static LLVMValueRef cg_load_value(
    CwCodegen_t* g, CwExpr e,
    LLVMTypeRef value_type
) {
    LLVMValueRef ptr = LLVMBuildIntToPtr(
        cg_b(g), cg_handle_addr(g, e),
        LLVMPointerType(LLVMVoidTypeInContext(cg_ctx(g)), 0), "p");
    return LLVMBuildLoad2(cg_b(g), value_type, ptr, "v");
}

/* 索引值 → i64: 按自身类型加载, 按符号性扩展 */
static LLVMValueRef cg_index_i64(
    CwCodegen_t* g,
    CwExpr idx
) {
    LLVMValueRef iv = cg_load_value(
        g, idx, cg_scalar_type(g, idx.type_name, NULL));
    return cg_is_unsigned(idx.type_name)
        ? LLVMBuildZExt(cg_b(g), iv,
                        LLVMInt64TypeInContext(cg_ctx(g)), "idx")
        : LLVMBuildSExt(cg_b(g), iv,
                        LLVMInt64TypeInContext(cg_ctx(g)), "idx");
}

static LLVMValueRef cg_bool_cond(
    CwCodegen_t* g,
    CwExpr e
) {
    LLVMValueRef v = cg_load_value(g, e, LLVMInt8TypeInContext(cg_ctx(g)));
    return LLVMBuildICmp(cg_b(g), LLVMIntNE, v, cg_i8(g, 0), "cond");
}

/* ---- 变量 ---- */

static const CwLayout_t* cg_struct_layout(
    CwCodegen_t* g,
    const cw_value*type_obj
);
static LLVMValueRef cg_blob_alloc(
    CwCodegen_t* g, size_t size,
    const char* name
);
static LLVMValueRef cg_blob_i8(
    CwCodegen_t* g,
    LLVMValueRef blob
);
static LLVMValueRef cg_expr_blob_i8(
    CwCodegen_t* g,
    CwExpr e
);
static size_t cg_struct_blob_size(
    CwCodegen_t* g,
    const CwLayout_t* L
);
static void cg_rebase_struct_fields(
    CwCodegen_t* g, LLVMValueRef base,
    const CwLayout_t* L
);

static CwVar_t* cg_var_find(
    CwCodegen_t* g,
    const char* name
) {
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
static bool cg_var_in_current_scope(
    CwCodegen_t* g,
    const char* name
) {
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

static bool cg_var_declare(
    CwCodegen_t* g, const char* name,
    const char* type_name,
    const cw_value*type_obj
) {
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

static void cg_var_push_scope(
    CwCodegen_t* g
) {
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

static void cg_var_pop_scope(
    CwCodegen_t* g
) {
    if (g->scope_depth > 0 && g->scope_mark_count > 0) {
        /* 截断变量表: 兄弟作用域同名绑定不再互相干扰 */
        g->var_count = g->scope_marks[--g->scope_mark_count];
        g->scope_depth--;
    }
}

/* 把表达式写进变量记录: 标量拷值, 引用类型拷 handle */
static bool cg_rec_store(
    CwCodegen_t* g,
    CwVar_t* v,
    CwExpr e
) {
    if (e.type_name && strcmp(e.type_name, "!") == 0) {
        return true; /* never 值: 调用点发散, 存储永远不会执行 */
    }
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
static bool cg_rec_store_value(
    CwCodegen_t* g, CwVar_t* v,
    LLVMValueRef val,
    const char* type_name
) {
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

static CwExpr cg_var_read(
    CwCodegen_t* g,
    CwVar_t* v
) {
    LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                            v->record, 3, "h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hptr, "vh");
    CwExpr e = { h, v->type_name };
    return e;
}

/* ---- rt 声明 ---- */

static LLVMValueRef cg_rt_declare(
    CwCodegen_t* g, const char* name,
    LLVMTypeRef ret,
    LLVMTypeRef* params, size_t n
) {
    LLVMValueRef existing = LLVMGetNamedFunction(g->ll->module, name);
    if (existing) return existing;
    LLVMTypeRef ft = LLVMFunctionType(ret, params, (unsigned)n, false);
    return LLVMAddFunction(g->ll->module, name, ft);
}

static LLVMTypeRef cg_rt_i8_ptr(
    CwCodegen_t* g
) {
    return LLVMPointerType(LLVMVoidTypeInContext(cg_ctx(g)), 0);
}

/* ---- 用户结构体 ----
 * 实例 = 8 字节头 + field_count × 32 字节句柄槽 (与 CwLayout 偏移一致);
 * 值以句柄承载, handle.address 指向实例 blob。
 */

static const CwNode_t* cg_struct_decl(
    const CwCodegen_t* g,
    const char* name
) {
    if (!name) return NULL;
    const CwSymbol_t* sym = cwmodule_find_symbol(g->m, name);
    if (!sym || strcmp(sym->kind, "struct") != 0) return NULL;
    return cwmodule_node(g->m, sym->ref);
}

static bool cg_is_struct_type(
    CwCodegen_t* g,
    const char* name
) {
    return cg_struct_decl(g, name) != NULL;
}

/* 取类型对象对应的实例布局 (name + args 查符号表与布局缓存) */
static const CwLayout_t* cg_struct_layout(
    CwCodegen_t* g,
    const cw_value*type_obj
) {
    if (!type_obj || cw_typeof(type_obj) != CW_OBJECT) return NULL;
    const char* name = cg_json_name(type_obj);
    const CwNode_t* decl = cg_struct_decl(g, name);
    CwTypeId* ids = NULL;
    size_t n_ids = 0;
    if (!decl) {
        /* 裸泛型参数 (泛型方法实例返回 T): 经类型表取解析后的 name+args */
        CwTypeId inst = cg_type_id_of(g, type_obj);
        if (inst == CW_TYPE_INVALID) return NULL;
        const CwType_t* t = cwtype_get(g->ll->types, inst);
        if (!t) return NULL;
        decl = cg_struct_decl(g, t->name);
        if (!decl) return NULL;
        n_ids = t->arg_count;
        if (n_ids > 0) {
            ids = (CwTypeId*)malloc(n_ids * sizeof(CwTypeId));
            if (!ids) return NULL;
            memcpy(ids, t->args, n_ids * sizeof(CwTypeId));
        }
    } else {
        cw_value* args_v = cw_object_get(type_obj, "args");
        n_ids = (args_v && cw_typeof(args_v) == CW_ARRAY)
            ? cw_array_size(args_v) : 0;
        if (n_ids > 0) {
            ids = (CwTypeId*)malloc(n_ids * sizeof(CwTypeId));
            if (!ids) return NULL;
            for (size_t i = 0; i < n_ids; i++) {
                ids[i] = cg_type_id_of(g, cw_array_get(args_v, i));
                if (ids[i] == CW_TYPE_INVALID) {
                    free(ids);
                    return NULL;
                }
            }
        }
    }
    const CwLayout_t* L = cwlayout_get(
        g->ll->layouts, g->m, decl, ids, n_ids);
    free(ids);
    return L;
}

static LLVMValueRef cg_blob_alloc(
    CwCodegen_t* g, size_t size,
    const char* name
) {
    LLVMTypeRef arr = LLVMArrayType(
        LLVMInt8TypeInContext(cg_ctx(g)), (unsigned)size);
    return cg_alloca(g, arr, name);
}

static LLVMValueRef cg_blob_i8(
    CwCodegen_t* g,
    LLVMValueRef blob
) {
    return LLVMBuildBitCast(cg_b(g), blob, cg_rt_i8_ptr(g), "");
}

/* 句柄槽指针: base 为 blob 字节指针, offset 为布局字段偏移 */
static LLVMValueRef cg_struct_slot(
    CwCodegen_t* g, LLVMValueRef base,
    size_t offset
) {
    LLVMValueRef idx[1] = { cg_i64(g, (uint64_t)(8 + offset)) };
    LLVMValueRef p = LLVMBuildGEP2(cg_b(g),
                                   LLVMInt8TypeInContext(cg_ctx(g)),
                                   base, idx, 1, "f.slot");
    return LLVMBuildBitCast(cg_b(g), p,
                            LLVMPointerType(g->ll->handle_type, 0), "");
}

static LLVMValueRef cg_struct_handle(
    CwCodegen_t* g, LLVMValueRef blob,
    size_t fields
) {
    LLVMValueRef addr = LLVMBuildPtrToInt(cg_b(g), blob,
                                          LLVMInt64TypeInContext(cg_ctx(g)),
                                          "st.addr");
    return cg_build_handle(g, cg_i64(g, 0), addr, cg_i64(g, fields),
                           cg_i64(g, 0));
}

/* 从表达式句柄取 blob 字节指针 */
static LLVMValueRef cg_expr_blob_i8(
    CwCodegen_t* g,
    CwExpr e
) {
    return LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, e),
                             cg_rt_i8_ptr(g), "st.ptr");
}

/* 标量字段值内联进 blob 载荷区 (句柄槽之后), 使整块拷贝 = 深拷贝 */
static size_t cg_scalar_bytes(
    const char* name
) {
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
static int cg_int_rank(
    const char* n
) {
    if (!n) return -1;
    if (strcmp(n, "Int8") == 0 || strcmp(n, "UInt8") == 0
        || strcmp(n, "Byte") == 0) return 1;
    if (strcmp(n, "Int") == 0 || strcmp(n, "UInt") == 0) return 2;
    if (strcmp(n, "Int32") == 0 || strcmp(n, "UInt32") == 0) return 3;
    if (strcmp(n, "Int64") == 0 || strcmp(n, "UInt64") == 0) return 4;
    return -1;
}

/* 数值共同类型: 浮点优先, 整数按宽度/符号提升 (与前端 _common_numeric 一致) */
static const char* cg_common_numeric(
    const char* a,
    const char* b
) {
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
    return "Int64";
}

/* 标量值类型转换: 整数截断/符号扩展、浮点宽度、int↔float */
static LLVMValueRef cg_convert_scalar(
    CwCodegen_t* g, LLVMValueRef v,
    const char* from, const char* to
) {
    if (!from || !to || strcmp(from, to) == 0) return v;
    if (cg_is_rawptr(from) || cg_is_rawptr(to)) return v;
    if (cg_is_int(from) && cg_is_int(to)) {
        size_t fsz = 0;
        size_t tsz = 0;
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
        size_t fsz = 0;
        size_t tsz = 0;
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
static CwExpr cg_coerce_scalar(
    CwCodegen_t* g,
    CwExpr e,
    const char* want
) {
    if (e.type_name && strcmp(e.type_name, "!") == 0) return e;
    if (!want || !e.type_name || strcmp(want, e.type_name) == 0) return e;
    /* 原始指针是 i64 地址, 不做数值转换; 类型一致性由 SA 保证 */
    if (cg_is_rawptr(want)) return e;
    if (cg_is_rawptr(e.type_name)) return e;
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
static const char* cg_receiver_arg(
    CwCodegen_t* g, const cw_value*objv,
    size_t idx
) {
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
static size_t cg_field_payload_offset(
    CwCodegen_t* g, const CwLayout_t* L,
    size_t i
) {
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
static size_t cg_struct_blob_size(
    CwCodegen_t* g,
    const CwLayout_t* L
) {
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
static void cg_store_struct_field(
    CwCodegen_t* g, LLVMValueRef base,
    const CwLayout_t* L, size_t i,
    CwExpr val
) {
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
static void cg_rebase_struct_fields(
    CwCodegen_t* g, LLVMValueRef base,
    const CwLayout_t* L
) {
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

static const CwNode_t* cg_enum_decl(
    const CwCodegen_t* g,
    const char* name
) {
    if (!name) return NULL;
    const CwSymbol_t* sym = cwmodule_find_symbol(g->m, name);
    if (!sym || strcmp(sym->kind, "enum") != 0) return NULL;
    return cwmodule_node(g->m, sym->ref);
}

static bool cg_is_enum_type(
    CwCodegen_t* g,
    const char* name
) {
    return cg_enum_decl(g, name) != NULL;
}

static size_t cg_enum_max_payloads(
    const CwNode_t* decl
) {
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

static size_t cg_enum_blob_size(
    CwCodegen_t* g,
    const char* name
) {
    const CwNode_t* decl = cg_enum_decl(g, name);
    if (!decl) return 0;
    return CWENUM_SLOTS_OFFSET
        + cg_enum_max_payloads(decl) * CWLAYOUT_SLOT_SIZE;
}

static size_t cg_enum_slot_count(
    CwCodegen_t* g,
    const char* name
) {
    const CwNode_t* decl = cg_enum_decl(g, name);
    return 1 + cg_enum_max_payloads(decl);
}

static bool cg_enum_variant_index(
    CwCodegen_t* g, const CwNode_t* decl,
    const char* vname, size_t* out
) {
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

static LLVMValueRef cg_enum_tag_ptr(
    CwCodegen_t* g,
    LLVMValueRef base
) {
    LLVMValueRef idx[1] = { cg_i64(g, CWENUM_TAG_OFFSET) };
    LLVMValueRef p = LLVMBuildGEP2(
        cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), base, idx, 1, "e.tag");
    return LLVMBuildBitCast(
        cg_b(g), p,
        LLVMPointerType(LLVMInt32TypeInContext(cg_ctx(g)), 0), "");
}

static LLVMValueRef cg_enum_slot(
    CwCodegen_t* g, LLVMValueRef base,
    size_t i
) {
    LLVMValueRef idx[1] = {
        cg_i64(g, CWENUM_SLOTS_OFFSET + i * CWLAYOUT_SLOT_SIZE)
    };
    LLVMValueRef p = LLVMBuildGEP2(
        cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), base, idx, 1, "e.slot");
    return LLVMBuildBitCast(
        cg_b(g), p, LLVMPointerType(g->ll->handle_type, 0), "");
}

static LLVMValueRef cg_enum_handle(
    CwCodegen_t* g, LLVMValueRef blob,
    const char* enum_name
) {
    LLVMValueRef addr = LLVMBuildPtrToInt(
        cg_b(g), blob, LLVMInt64TypeInContext(cg_ctx(g)), "en.addr");
    return cg_build_handle(
        g, cg_i64(g, 0), addr,
        cg_i64(g, cg_enum_slot_count(g, enum_name)), cg_i64(g, 0));
}

/* 调 rt arena 分配 (枚举载荷持久单元) */
static LLVMValueRef cg_rt_arena_alloc(
    CwCodegen_t* g,
    LLVMValueRef size
) {
    LLVMTypeRef pt[1] = { LLVMInt64TypeInContext(cg_ctx(g)) };
    LLVMValueRef f = cg_rt_declare(
        g, "cwrt_arena_alloc", cg_rt_i8_ptr(g), pt, 1);
    LLVMValueRef av[1] = { size };
    return LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 1,
                          "en.cell");
}

/* 把枚举载荷物化为持久句柄: 标量/结构体拷进 arena 单元, 引用类型原样 */
static LLVMValueRef cg_enum_payload_handle(
    CwCodegen_t* g, CwExpr val,
    const cw_value*type_obj
) {
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
    return val.handle; /* String / 容器 / None: 引用语义 (数据已持久) */
}

/* 构造枚举实例: 写 tag + 载荷槽, 返回指向新 blob 的句柄 */
static CwExpr cg_expr_enum_build(
    CwCodegen_t* g, const char* enum_name,
    size_t variant_index,
    const cw_value*payload_types,
    const cw_value*args
) {
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

static cw_value* cg_static_field(
    const CwCodegen_t* g, const char* owner,
    const char* fname,
    cw_value** type_out
) {
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
static const char* cg_static_owner(
    const CwCodegen_t* g,
    const char* owner
) {
    if (owner && strcmp(owner, "Self") == 0 && g->current_owner) {
        return g->current_owner;
    }
    return owner;
}

static LLVMValueRef cg_static_storage(
    CwCodegen_t* g, const char* owner,
    const char* fname,
    const char* type_name,
    const cw_value*type_obj
) {
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
            (unsigned)cg_struct_blob_size(g, L));
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
static bool cg_static_store(
    CwCodegen_t* g, const char* owner,
    const char* fname, CwExpr e,
    const char* type_name,
    const cw_value*type_obj
) {
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

static LLVMValueRef cg_static_init_fn(
    CwCodegen_t* g, const char* owner,
    const char* fname, const cw_value* field,
    const char* type_name,
    const cw_value* type_obj
) {
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

static CwExpr cg_static_read(
    CwCodegen_t* g, const char* owner,
    const char* fname, const char* type_name,
    const cw_value*type_obj
) {
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

/* ---- 顶层 const ---- */

static cw_value* cg_const_decl(
    const CwCodegen_t* g, const char* name,
    cw_value** type_out
) {
    const CwSymbol_t* s = cwmodule_find_symbol(g->m, name);
    if (!s || strcmp(s->kind, "const") != 0) return NULL;
    const CwNode_t* decl = cwmodule_node(g->m, s->ref);
    if (!decl) return NULL;
    cw_value* t = cw_object_get(decl->value, "type");
    cw_value* ann = cw_object_get(decl->value, "ann");
    cw_value* at = ann ? cw_object_get(ann, "type") : NULL;
    if (at && cw_typeof(at) == CW_OBJECT) t = at;
    if (type_out) *type_out = t;
    return decl->value;
}

static LLVMValueRef cg_const_storage(
    CwCodegen_t* g, const char* name,
    const char* type_name,
    const cw_value*type_obj
) {
    char gname[256];
    if (cg_is_scalar(type_name)) {
        LLVMTypeRef vt = cg_scalar_type(g, type_name, NULL);
        if (!vt) return NULL;
        snprintf(gname, sizeof(gname), "cwind.const.%s.val", name);
        LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
        if (gv) return gv;
        gv = LLVMAddGlobal(g->ll->module, vt, gname);
        LLVMSetInitializer(gv, LLVMConstNull(vt));
        return gv;
    }
    if (cg_is_struct_type(g, type_name)) {
        const CwLayout_t* L = cg_struct_layout(g, type_obj);
        if (!L) return NULL;
        snprintf(gname, sizeof(gname), "cwind.const.%s.blob", name);
        LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
        if (gv) return gv;
        LLVMTypeRef arr = LLVMArrayType(
            LLVMInt8TypeInContext(cg_ctx(g)),
            (unsigned)cg_struct_blob_size(g, L));
        gv = LLVMAddGlobal(g->ll->module, arr, gname);
        LLVMSetInitializer(gv, LLVMConstNull(arr));
        return gv;
    }
    snprintf(gname, sizeof(gname), "cwind.const.%s.rec", name);
    LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
    if (gv) return gv;
    gv = LLVMAddGlobal(g->ll->module, g->ll->rec_type, gname);
    LLVMSetInitializer(gv, LLVMConstNull(g->ll->rec_type));
    return gv;
}

static bool cg_const_store(
    CwCodegen_t* g, const char* name, CwExpr e,
    const char* type_name, const cw_value* type_obj
) {
    if (cg_is_scalar(type_name)) {
        LLVMValueRef gv = cg_const_storage(
            g, name, type_name, type_obj);
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
        LLVMValueRef gb = cg_const_storage(
            g, name, type_name, type_obj);
        if (!L || !gb) return false;
        LLVMValueRef src = cg_expr_blob_i8(g, e);
        LLVMValueRef dst = cg_blob_i8(g, gb);
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1,
                        cg_i64(g, (uint64_t)cg_struct_blob_size(g, L)));
        cg_rebase_struct_fields(g, dst, L);
        return true;
    }
    LLVMValueRef gr = cg_const_storage(
        g, name, type_name, type_obj);
    if (!gr) return false;
    LLVMValueRef rec = cg_materialize_record(g, e);
    if (!rec) return false;
    LLVMValueRef gr8 = LLVMBuildBitCast(cg_b(g), gr, cg_rt_i8_ptr(g), "");
    LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g), "");
    LLVMBuildMemCpy(cg_b(g), gr8, 1, rec8, 1,
                    cg_i64(g, CWIND_OBJECT_RECORD_SIZE));
    return true;
}

static LLVMValueRef cg_const_init_fn(
    CwCodegen_t* g, const char* name,
    const cw_value*decl,
    const char* type_name,
    const cw_value*type_obj
) {
    char fname_buf[256];
    snprintf(fname_buf, sizeof(fname_buf),
             "cwind.const.%s.init", name);
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

    cw_value* init = decl ? cw_object_get(decl, "value") : NULL;
    if (init && cw_typeof(init) == CW_OBJECT && !g->failed) {
        CwExpr e = cg_expr(g, init);
        if (!g->failed) {
            cg_const_store(g, name, e, type_name, type_obj);
        }
    }
    if (!g->failed) LLVMBuildRetVoid(cg_b(g));

    g->current_fn = saved_fn;
    if (saved_block) LLVMPositionBuilderAtEnd(cg_b(g), saved_block);
    return fn;
}

static CwExpr cg_const_read(
    CwCodegen_t* g, const char* name,
    const char* type_name, cw_value* type_obj
) {
    if (cg_is_scalar(type_name)) {
        LLVMValueRef gv = cg_const_storage(
            g, name, type_name, type_obj);
        if (!gv) return (CwExpr){ NULL, NULL };
        size_t size = 0;
        cg_scalar_type(g, type_name, &size);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), gv, LLVMInt64TypeInContext(cg_ctx(g)), "c.addr");
        return (CwExpr){
            cg_build_handle(g, cg_i64(g, 0), addr,
                            cg_i64(g, size), cg_i64(g, 0)),
            type_name,
        };
    }
    if (cg_is_struct_type(g, type_name)) {
        const CwLayout_t* L = cg_struct_layout(g, type_obj);
        LLVMValueRef gb = cg_const_storage(
            g, name, type_name, type_obj);
        if (!L || !gb) return (CwExpr){ NULL, NULL };
        return (CwExpr){ cg_struct_handle(g, gb, L->field_count),
                         type_name };
    }
    LLVMValueRef gr = cg_const_storage(
        g, name, type_name, type_obj);
    if (!gr) return (CwExpr){ NULL, NULL };
    LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                          gr, 3, "c.h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                    hp, "c.vh");
    return (CwExpr){ h, type_name };
}

/* ---- 表达式 ---- */

/* 把一段字节建成全局字符串常量, 返回 String 句柄 (address -> 全局变量) */
static CwExpr cg_string_lit(
    CwCodegen_t* g,
    const char* s,
    size_t len
) {
    static unsigned str_seq = 0;
    char gname[32];
    snprintf(gname, sizeof(gname), ".str.%u", str_seq++);
    LLVMValueRef gv = LLVMAddGlobal(
        g->ll->module,
        LLVMArrayType(LLVMInt8TypeInContext(cg_ctx(g)),
                      (unsigned)(len + 1)),
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

/* 整数字面量: 小值保持 Int 语义, 大数自动宽化为 Int64 */
static CwExpr cg_lit_int(
    CwCodegen_t* g,
    const cw_value*node
) {
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

/* 浮点字面量: f32 可精确表示时保持 Float, 否则用 Float64 保真 */
static CwExpr cg_lit_float(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* v = cw_object_get(node, "value");
    double dv = 0;
    if (!v || cw_as_double(v, &dv) != CW_OK) {
        cg_error(g, "FloatLit is missing value");
        return (CwExpr){ NULL, NULL };
    }
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

static CwExpr cg_lit_bool(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* v = cw_object_get(node, "value");
    bool bv = false;
    if (!v || cw_as_bool(v, &bv) != CW_OK) {
        cg_error(g, "BoolLit is missing value");
        return (CwExpr){ NULL, NULL };
    }
    return cg_make_scalar(g, cg_i8(g, bv ? 1 : 0),
                          LLVMInt8TypeInContext(cg_ctx(g)), "Bool", 1);
}

static CwExpr cg_lit_str(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* v = cw_object_get(node, "value");
    size_t len = 0;
    const char* s = v ? cw_string_value(v, &len) : NULL;
    if (!s) {
        cg_error(g, "StrLit is missing value");
        return (CwExpr){ NULL, NULL };
    }
    return cg_string_lit(g, s, len);
}

static CwExpr cg_lit_vector(
    CwCodegen_t* g,
    const cw_value*node
) {
    /* cwvec_init 要求 handle.address == 0, 先清零句柄 */
    LLVMValueRef rec = cg_new_record(
        g, CWVector, cg_null_handle(g), "vec.rec");
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
        cw_value* elem = cw_array_get(elems, i);
        CwExpr e = cg_expr(g, elem);
        if (g->failed) return (CwExpr){ NULL, NULL };
        e = cg_coerce_scalar(g, e, elem_name);
        LLVMValueRef er = cg_vector_value_record(
            g, e, cg_node_ann_type(elem));
        if (g->failed) return (CwExpr){ NULL, NULL };
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

static CwExpr cg_lit_map(
    CwCodegen_t* g,
    const cw_value*node
) {
    /* cwmap_init 要求 handle.address == 0, 先清零句柄 */
    LLVMValueRef rec = cg_new_record(
        g, CWMap, cg_null_handle(g), "map.rec");
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
        LLVMValueRef kr = cg_container_value_record(g, k);
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef vr = cg_vector_value_record(
            g, v, cg_node_ann_type(cw_object_get(entry, "value")));
        if (g->failed) return (CwExpr){ NULL, NULL };
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

static CwExpr cg_lit_tuple(
    CwCodegen_t* g,
    const cw_value*node
) {
    /* cwtuple_init 要求 handle.address == 0, 先清零句柄 */
    LLVMValueRef rec = cg_new_record(
        g, CWTuple, cg_null_handle(g), "tup.rec");
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
            LLVMValueRef er = cg_vector_value_record(
                g, e, cg_node_ann_type(cw_array_get(elems, i)));
            if (g->failed) return (CwExpr){ NULL, NULL };
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

static CwExpr cg_lit_struct(
    CwCodegen_t* g,
    const cw_value*node
) {
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

static CwExpr cg_expr_lit(
    CwCodegen_t* g,
    const cw_value*node
) {
    const char* kind = cg_node_kind(node);
    if (strcmp(kind, "IntLit") == 0) return cg_lit_int(g, node);
    if (strcmp(kind, "FloatLit") == 0) return cg_lit_float(g, node);
    if (strcmp(kind, "BoolLit") == 0) return cg_lit_bool(g, node);
    if (strcmp(kind, "StrLit") == 0) return cg_lit_str(g, node);
    if (strcmp(kind, "VectorLit") == 0) return cg_lit_vector(g, node);
    if (strcmp(kind, "MapLit") == 0) return cg_lit_map(g, node);
    if (strcmp(kind, "TupleLit") == 0) return cg_lit_tuple(g, node);
    if (strcmp(kind, "StructConstruct") == 0) return cg_lit_struct(g, node);
    cg_error(g, "unsupported literal: %s", kind ? kind : "?");
    return (CwExpr){ NULL, NULL };
}


/* 裸函数名引用: `let p: fn(Int) -> Int = inc;`
 * 生成函数指针值 (标量 i64 = 函数地址); 泛型函数无单值, 拒绝 */
static CwExpr cg_expr_fn_ref(
    CwCodegen_t* g, const cw_value*node,
    const cw_value*binding, const char* n
) {
    cw_value* refv = cw_object_get(binding, "ref");
    int64_t ref = 0;
    const CwNode_t* fn_node =
        (refv && cw_typeof(refv) == CW_INT
         && cw_as_int(refv, &ref) == CW_OK)
        ? cwmodule_node(g->m, ref) : NULL;
    const char* fname = fn_node ? cwmodule_fn_name(fn_node) : n;
    const CwSymEntry_t* sym =
        fname ? cwsym_find(g->ll->syms, NULL, fname) : NULL;
    if (!sym || sym->kind == CW_SYM_TEMPLATE) {
        cg_error_at(g, node,
                    "generic function '%s' has no single "
                    "value; call it with concrete arguments "
                    "first", fname ? fname : n);
        return (CwExpr){ NULL, NULL };
    }
    LLVMValueRef fn;
    if (sym->kind == CW_SYM_EXTERN) {
        /* todo-54: extern 函数地址经 CWind-ABI thunk 包装,
         * 使其可作为普通 fn 值被间接调用 */
        fn = cg_extern_thunk(g, sym);
        if (!fn) return (CwExpr){ NULL, NULL };
    } else {
        fn = LLVMGetNamedFunction(g->ll->module, sym->mangled);
        if (!fn) {
            cg_error(g, "function is not declared: %s",
                     sym->mangled);
            return (CwExpr){ NULL, NULL };
        }
    }
    LLVMValueRef fp = LLVMBuildPtrToInt(
        cg_b(g), fn,
        LLVMInt64TypeInContext(cg_ctx(g)), "fp");
    const char* sig = cg_node_type_name(g, node);
    return cg_make_scalar(
        g, fp, LLVMInt64TypeInContext(cg_ctx(g)),
        (sig && strcmp(sig, "Fn") != 0) ? sig : "Fn",
        8);
}

/* 单段 Name: None 字面量 / 局部变量 / 顶层 const / 裸函数名 */
static CwExpr cg_name_simple(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* parts = cw_object_get(node, "parts");
    cw_value* p0 = cw_array_get(parts, 0);
    const char* n = (p0 && cw_typeof(p0) == CW_STRING)
        ? cw_string_cstr(p0) : NULL;
    if (n && strcmp(n, "None") == 0) {
        CwExpr e = { cg_null_handle(g), "None" };
        return e;
    }
    CwVar_t* v = n ? cg_var_find(g, n) : NULL;
    if (v) return cg_var_read(g, v);
    cw_value* const_type = NULL;
    if (n && cg_const_decl(g, n, &const_type) != NULL) {
        const char* t = cg_node_type_name(g, node);
        if (!t && const_type) t = cg_type_name_of(g, const_type);
        if (!t) t = "Any";
        return cg_const_read(g, n, t, const_type);
    }
    if (n) {
        cw_value* ann = cw_object_get(node, "ann");
        cw_value* binding = ann ? cw_object_get(ann, "binding") : NULL;
        const char* bk = binding ? cg_json_kind(binding) : NULL;
        if (bk && strcmp(bk, "fn") == 0) {
            return cg_expr_fn_ref(g, node, binding, n);
        }
        if (bk && strcmp(bk, "extern_static") == 0) {
            /* todo-56: extern 静态变量读取 */
            return cg_expr_extern_static_read(g, node);
        }
    }
    cg_error(g, "undeclared variable: %s", n ? n : "?");
    return (CwExpr){ NULL, NULL };
}

/* 两段 Name (Owner::member): 静态字段读取或枚举变体构造。
 * 未命中任何形式时落入底部的统一报错。 */
static CwExpr cg_name_member(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* parts = cw_object_get(node, "parts");
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
    cg_error(g, "multi-part Name / builtins are not supported yet");
    return (CwExpr){ NULL, NULL };
}

static CwExpr cg_expr_name(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* parts = cw_object_get(node, "parts");
    if (parts && cw_typeof(parts) == CW_ARRAY) {
        const size_t np = cw_array_size(parts);
        if (np == 1) return cg_name_simple(g, node);
        if (np == 2) return cg_name_member(g, node);
    }
    cg_error(g, "multi-part Name / builtins are not supported yet");
    return (CwExpr){ NULL, NULL };
}


/* String 拼接: 调 rt cw_builtin_concat, 结果句柄指向 arena 中的新字节流 */
static CwExpr cg_builtin_concat(
    CwCodegen_t* g,
    CwExpr l,
    CwExpr r
) {
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
static CwExpr cg_expr_format_call(
    CwCodegen_t* g,
    const cw_value*node
) {
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
static CwExpr cg_fixup_call_result(
    CwCodegen_t* g, LLVMValueRef h,
    const char* t,
    const cw_value*type_obj
) {
    if (!t) return (CwExpr){ h, "Any" };
    if (cg_is_fnptr(t)) {
        /* 函数指针结果立即拷进本地临时, 与标量同理:
         * 全局缓冲会被同函数的下一次调用覆盖 */
        LLVMValueRef fp = cg_load_value(
            g, (CwExpr){ h, t }, LLVMInt64TypeInContext(cg_ctx(g)));
        return cg_make_scalar(g, fp,
                              LLVMInt64TypeInContext(cg_ctx(g)), t, 8);
    }
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

/* 短路求值 (&&/||): 右操作数只在需要时求值 —— 不能在函数开头急切求值,
 * 否则 `i > 1 && self.max_heap[i].unwrap()...` 里的 unwrap (可能
 * panic) 会在左值为假时仍被调用。 */
static CwExpr cg_short_circuit(
    CwCodegen_t* g, const cw_value*node,
    const char* op
) {
    CwExpr l = cg_expr(g, cw_object_get(node, "left"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    if (!l.type_name
        || (strcmp(l.type_name, "Bool") != 0
            && strcmp(l.type_name, "!") != 0)) {
        cg_error_at(g, node, "'%s' requires a Bool left operand, got %s",
                    op, l.type_name ? l.type_name : "?");
        return (CwExpr){ NULL, NULL };
    }
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
    CwExpr r = cg_expr(g, cw_object_get(node, "right"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    if (!r.type_name
        || (strcmp(r.type_name, "Bool") != 0
            && strcmp(r.type_name, "!") != 0)) {
        cg_error_at(g, node, "'%s' requires a Bool right operand, got %s",
                    op, r.type_name ? r.type_name : "?");
        return (CwExpr){ NULL, NULL };
    }
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

/* 浮点共同类型上的算术与比较 */
static CwExpr cg_float_binop(
    CwCodegen_t* g, const char* op,
    LLVMValueRef a, LLVMValueRef b,
    LLVMTypeRef ct, const char* tn, size_t csz
) {
    if (strcmp(op, "+") == 0)
        return cg_make_scalar(g, LLVMBuildFAdd(cg_b(g), a, b, "fadd"),
                              ct, tn, csz);
    if (strcmp(op, "-") == 0)
        return cg_make_scalar(g, LLVMBuildFSub(cg_b(g), a, b, "fsub"),
                              ct, tn, csz);
    if (strcmp(op, "*") == 0)
        return cg_make_scalar(g, LLVMBuildFMul(cg_b(g), a, b, "fmul"),
                              ct, tn, csz);
    if (strcmp(op, "/") == 0)
        return cg_make_scalar(g, LLVMBuildFDiv(cg_b(g), a, b, "fdiv"),
                              ct, tn, csz);
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

/* 整数共同类型上的算术与比较 (按符号性选择指令) */
static CwExpr cg_int_binop(
    CwCodegen_t* g, const char* op,
    LLVMValueRef a, LLVMValueRef b,
    LLVMTypeRef ct, const char* tn, size_t csz
) {
    const bool uns = cg_is_unsigned(tn);
    if (strcmp(op, "+") == 0)
        return cg_make_scalar(g, LLVMBuildAdd(cg_b(g), a, b, "add"),
                              ct, tn, csz);
    if (strcmp(op, "-") == 0)
        return cg_make_scalar(g, LLVMBuildSub(cg_b(g), a, b, "sub"),
                              ct, tn, csz);
    if (strcmp(op, "*") == 0)
        return cg_make_scalar(g, LLVMBuildMul(cg_b(g), a, b, "mul"),
                              ct, tn, csz);
    if (strcmp(op, "/") == 0) {
        LLVMValueRef q = uns ? LLVMBuildUDiv(cg_b(g), a, b, "div")
                             : LLVMBuildSDiv(cg_b(g), a, b, "div");
        return cg_make_scalar(g, q, ct, tn, csz);
    }
    if (strcmp(op, "%") == 0) {
        LLVMValueRef q = uns ? LLVMBuildURem(cg_b(g), a, b, "rem")
                             : LLVMBuildSRem(cg_b(g), a, b, "rem");
        return cg_make_scalar(g, q, ct, tn, csz);
    }
    if (strcmp(op, "<<") == 0)
        return cg_make_scalar(g, LLVMBuildShl(cg_b(g), a, b, "shl"),
                              ct, tn, csz);
    if (strcmp(op, ">>") == 0) {
        LLVMValueRef q = uns ? LLVMBuildLShr(cg_b(g), a, b, "shr")
                             : LLVMBuildAShr(cg_b(g), a, b, "shr");
        return cg_make_scalar(g, q, ct, tn, csz);
    }
    if (strcmp(op, "&") == 0)
        return cg_make_scalar(g, LLVMBuildAnd(cg_b(g), a, b, "and"),
                              ct, tn, csz);
    if (strcmp(op, "|") == 0)
        return cg_make_scalar(g, LLVMBuildOr(cg_b(g), a, b, "or"),
                              ct, tn, csz);
    if (strcmp(op, "^") == 0)
        return cg_make_scalar(g, LLVMBuildXor(cg_b(g), a, b, "xor"),
                              ct, tn, csz);

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

/* Bool 相等/不等 */
static CwExpr cg_bool_eq(
    CwCodegen_t* g, const char* op,
    CwExpr l, CwExpr r
) {
    LLVMTypeRef i8 = LLVMInt8TypeInContext(cg_ctx(g));
    LLVMValueRef a = cg_load_value(g, l, i8);
    LLVMValueRef b = cg_load_value(g, r, i8);
    LLVMValueRef c = LLVMBuildICmp(
        cg_b(g), strcmp(op, "==") == 0 ? LLVMIntEQ : LLVMIntNE,
        a, b, "b.cmp");
    LLVMValueRef z = LLVMBuildZExt(cg_b(g), c, i8, "b.z");
    return cg_make_scalar(g, z, i8, "Bool", 1);
}

static CwExpr cg_expr_binop(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* opv = cw_object_get(node, "op");
    const char* op = (opv && cw_typeof(opv) == CW_STRING)
        ? cw_string_cstr(opv) : "";

    if (strcmp(op, "&&") == 0 || strcmp(op, "||") == 0) {
        return cg_short_circuit(g, node, op);
    }

    CwExpr l = cg_expr(g, cw_object_get(node, "left"));
    CwExpr r = cg_expr(g, cw_object_get(node, "right"));
    if (g->failed) return (CwExpr){ NULL, NULL };

    if (strcmp(op, "+") == 0 && strcmp(l.type_name, "String") == 0
        && strcmp(r.type_name, "String") == 0) {
        return cg_builtin_concat(g, l, r);
    }

    /* Bool 相等/不等 */
    if (strcmp(l.type_name, "Bool") == 0
        && strcmp(r.type_name, "Bool") == 0) {
        if (strcmp(op, "==") == 0 || strcmp(op, "!=") == 0) {
            return cg_bool_eq(g, op, l, r);
        }
        cg_error(g, "unsupported Bool operation: %s", op);
        return (CwExpr){ NULL, NULL };
    }

    /* 数值运算: 先提升到共同类型 (任意整数宽度 + Float/Float64 混合) */
    const bool lnum = l.type_name && cg_is_scalar(l.type_name)
        && strcmp(l.type_name, "Bool") != 0;
    const bool rnum = r.type_name && cg_is_scalar(r.type_name)
        && strcmp(r.type_name, "Bool") != 0;
    if (!lnum || !rnum) {
        cg_error(g, "unsupported BinOp: %s (%s, %s)", op,
                 l.type_name ? l.type_name : "?",
                 r.type_name ? r.type_name : "?");
        return (CwExpr){ NULL, NULL };
    }
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
    return is_float
        ? cg_float_binop(g, op, a, b, ct, common, csz)
        : cg_int_binop(g, op, a, b, ct, common, csz);
}


static CwExpr cg_expr_unary(
    CwCodegen_t* g,
    const cw_value*node
) {
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
    if (strcmp(op, "&") == 0) {
        /* 借用表达式在 ABI 层就是同一个句柄; 引用检查由前端负责 */
        return e;
    }
    if (strcmp(op, "*") == 0) {
        /* 解引用: 指针值是地址, 标量 load 出值 / 结构体返回 blob 句柄。
         * const/mut 约束由前端 SA 负责。 */
        if (!cg_is_rawptr(e.type_name)) {
            cg_error_at(g, node,
                        "cannot dereference non-scalar pointer type: %s",
                        e.type_name ? e.type_name : "?");
            return (CwExpr){ NULL, NULL };
        }
        const char* pointee = strchr(e.type_name, ' ') + 1;
        size_t size = 0;
        LLVMValueRef addr = cg_handle_addr(g, e);

        if (cg_is_struct_type(g, pointee)) {
            /* 结构体解引用: 地址即 blob 起始, 直接构造句柄 */
            return (CwExpr){ cg_struct_handle(g, LLVMBuildIntToPtr(
                cg_b(g), addr, cg_rt_i8_ptr(g), "deref.st"), 0),
                pointee };
        }

        LLVMTypeRef vt = cg_scalar_type(g, pointee, &size);
        if (!vt) {
            cg_error_at(g, node,
                        "unsupported dereference target type: %s", e.type_name);
            return (CwExpr){ NULL, NULL };
        }
        LLVMValueRef ptr = LLVMBuildIntToPtr(
            cg_b(g), addr,
            LLVMPointerType(vt, 0), "deref.p");
        LLVMValueRef val = LLVMBuildLoad2(cg_b(g), vt, ptr, "deref.v");
        return cg_make_scalar(g, val, vt, pointee, size);
    }
    cg_error(g, "unsupported UnaryOp: %s", op);
    return (CwExpr){ NULL, NULL };
}

/* 内置 length(): rt cw_builtin_length 读长度并截断成 UInt */
static CwExpr cg_method_length(
    CwCodegen_t* g,
    LLVMValueRef rec8
) {
    LLVMValueRef slot = cg_alloca(
        g, LLVMInt64TypeInContext(cg_ctx(g)), "len");
    LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g),
                          LLVMPointerType(
                              LLVMVoidTypeInContext(cg_ctx(g)), 0) };
    LLVMValueRef f = cg_rt_declare(
        g, "cw_builtin_length", LLVMInt1TypeInContext(cg_ctx(g)), pt, 2);
    LLVMValueRef av[2] = { rec8, slot };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2, "");
    LLVMValueRef v = LLVMBuildLoad2(
        cg_b(g), LLVMInt64TypeInContext(cg_ctx(g)), slot, "lenv");
    LLVMValueRef t = LLVMBuildTrunc(
        cg_b(g), v, LLVMInt16TypeInContext(cg_ctx(g)), "len16");
    return cg_make_scalar(g, t,
                          LLVMInt16TypeInContext(cg_ctx(g)),
                          "UInt", 2);
}

/* contains 的公共尾部: 参数已物化, 调 cw_builtin_contains 并读出 Bool */
static CwExpr cg_method_contains_rec(
    CwCodegen_t* g,
    LLVMValueRef rec8,
    CwExpr a
) {
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
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
    LLVMValueRef v = LLVMBuildLoad2(
        cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)), slot, "b");
    return cg_make_scalar(g, v,
                          LLVMInt8TypeInContext(cg_ctx(g)),
                          "Bool", 1);
}

/* 内置 clear(): 调容器的 cw*_clear, 返回 None */
static CwExpr cg_method_clear(
    CwCodegen_t* g,
    LLVMValueRef rec8,
    const char* clear_fn
) {
    LLVMTypeRef pt[1] = { cg_rt_i8_ptr(g) };
    LLVMValueRef f = cg_rt_declare(
        g, clear_fn, LLVMVoidTypeInContext(cg_ctx(g)), pt, 1);
    LLVMValueRef av[1] = { rec8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 1, "");
    CwExpr none = { cg_null_handle(g), "None" };
    return none;
}

/* 调用点的第 0 个实参节点 (无实参返回 NULL) */
static cw_value* cg_call_arg0(
    const cw_value*node
) {
    cw_value* args = cw_object_get(node, "args");
    return (args && cw_typeof(args) == CW_ARRAY
            && cw_array_size(args) > 0)
        ? cw_array_get(args, 0) : NULL;
}

/* 调 rt cw_builtin_parse_owned: 字符串记录 -> 目标标量记录, 读出结果句柄 */
static LLVMValueRef cg_parse_owned_handle(
    CwCodegen_t* g, LLVMValueRef ar8,
    int tid, LLVMValueRef out
) {
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                         cg_rt_i8_ptr(g), "");
    LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g),
                          LLVMInt32TypeInContext(cg_ctx(g)),
                          cg_rt_i8_ptr(g) };
    LLVMValueRef f = cg_rt_declare(
        g, "cw_builtin_parse_owned",
        LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
    LLVMValueRef av[3] = { ar8, cg_i32(g, (uint32_t)tid), out8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
    LLVMValueRef hp = LLVMBuildStructGEP2(
        cg_b(g), g->ll->rec_type, out, 3, "h");
    return LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hp, "ph");
}

/* Enum::Variant(args) 调用点 -> 构造枚举实例 */
static CwExpr cg_call_enum_variant(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* ann = cw_object_get(node, "ann");
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
    cw_value* pts = ann ? cw_object_get(ann, "payload_types") : NULL;
    return cg_expr_enum_build(
        g, enum_name, vidx, pts, cw_object_get(node, "args"));
}

/* T::from(String) -> T: 数值目标类型来自 callee parts[0] */
static CwExpr cg_builtin_from(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* callee = cw_object_get(node, "callee");
    cw_value* parts = callee ? cw_object_get(callee, "parts") : NULL;
    const char* target = NULL;
    if (parts && cw_typeof(parts) == CW_ARRAY
        && cw_array_size(parts) >= 2) {
        cw_value* p0 = cw_array_get(parts, 0);
        if (p0 && cw_typeof(p0) == CW_STRING) {
            target = cw_string_cstr(p0);
        }
    }
    const int tid = cg_type_id(target);
    if (!target || tid < 0 || !cg_is_scalar(target)
        || strcmp(target, "Bool") == 0) {
        cg_error_at(g, node, "unsupported from() target: %s",
                    target ? target : "?");
        return (CwExpr){ NULL, NULL };
    }
    cw_value* arg0 = cg_call_arg0(node);
    if (!arg0) {
        cg_error(g, "from expects 1 argument");
        return (CwExpr){ NULL, NULL };
    }
    CwExpr a = cg_expr(g, cw_object_get(arg0, "value"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef ar = cg_materialize_record(g, a);
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef ar8 = LLVMBuildBitCast(cg_b(g), ar, cg_rt_i8_ptr(g), "");
    LLVMValueRef out = cg_alloca(g, g->ll->rec_type, "parse.out");
    LLVMValueRef h = cg_parse_owned_handle(g, ar8, tid, out);
    return (CwExpr){ h, target };
}

/* 调 rt cw_builtin_to_string_owned: 记录 -> 新 String 记录 */
static CwExpr cg_call_to_string_owned(
    CwCodegen_t* g, LLVMValueRef val8, const char* out_name
) {
    LLVMValueRef out = cg_alloca(g, g->ll->rec_type, out_name);
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                         cg_rt_i8_ptr(g), "");
    LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
    LLVMValueRef f = cg_rt_declare(
        g, "cw_builtin_to_string_owned",
        LLVMInt1TypeInContext(cg_ctx(g)), pt, 2);
    LLVMValueRef av[2] = { val8, out8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2, "");
    LLVMValueRef hp = LLVMBuildStructGEP2(
        cg_b(g), g->ll->rec_type, out, 3, "h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hp, "vh");
    return (CwExpr){ h, "String" };
}

/* x.into(): String -> 数值 (目标来自调用点 ann.type) 或数值 -> String */
static CwExpr cg_builtin_into(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* callee = cw_object_get(node, "callee");
    cw_value* obj = callee ? cw_object_get(callee, "obj") : NULL;
    const char* rt = obj ? cg_node_type_name(g, obj) : NULL;
    const char* tt = cg_node_type_name(g, node);
    if (!rt || !tt) {
        cg_error_at(g, node,
                    "into() is missing its source/target type");
        return (CwExpr){ NULL, NULL };
    }
    CwExpr a = cg_expr(g, obj);
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef ar = cg_materialize_record(g, a);
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef ar8 = LLVMBuildBitCast(cg_b(g), ar, cg_rt_i8_ptr(g), "");
    if (strcmp(rt, "String") == 0) {
        const int tid = cg_type_id(tt);
        if (!tt || tid < 0 || !cg_is_scalar(tt)
            || strcmp(tt, "Bool") == 0) {
            cg_error_at(g, node,
                        "unsupported String conversion target: %s",
                        tt);
            return (CwExpr){ NULL, NULL };
        }
        LLVMValueRef out = cg_alloca(g, g->ll->rec_type, "conv.out");
        LLVMValueRef h = cg_parse_owned_handle(g, ar8, tid, out);
        return (CwExpr){ h, tt };
    }
    if (strcmp(tt, "String") == 0) {
        return cg_call_to_string_owned(g, ar8, "conv.out");
    }
    cg_error_at(g, node, "unsupported into() conversion: %s -> %s",
                rt, tt);
    return (CwExpr){ NULL, NULL };
}

/* builtins::print(value): 物化临时记录后调 rt 打印 */
static CwExpr cg_builtin_print(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* arg0 = cg_call_arg0(node);
    if (!arg0) {
        cg_error(g, "print expects 1 argument");
        return (CwExpr){ NULL, NULL };
    }
    CwExpr a = cg_expr(g, cw_object_get(arg0, "value"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    /* 临时记录: type_id + handle */
    LLVMValueRef rec = cg_new_record(
        g, cg_type_id(a.type_name), a.handle, "print.rec");
    LLVMTypeRef pr[1] = { cg_rt_i8_ptr(g) };
    LLVMValueRef fn = cg_rt_declare(
        g, "cw_builtin_print", LLVMInt1TypeInContext(cg_ctx(g)), pr, 1);
    LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec,
                                         cg_rt_i8_ptr(g), "");
    LLVMValueRef argsv[1] = { rec8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, argsv, 1, "");
    CwExpr none = { cg_null_handle(g), "None" };
    return none;
}

/* builtins::type_of(value): rt 反射值类型名 */
static CwExpr cg_builtin_type_of(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* arg0 = cg_call_arg0(node);
    if (!arg0) {
        cg_error(g, "type_of expects 1 argument");
        return (CwExpr){ NULL, NULL };
    }
    CwExpr a = cg_expr(g, cw_object_get(arg0, "value"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef rec = cg_materialize_record(g, a);
    LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec,
                                         cg_rt_i8_ptr(g), "");
    LLVMValueRef out = cg_alloca(g, g->ll->rec_type, "type.rec");
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                         cg_rt_i8_ptr(g), "");
    LLVMTypeRef pr[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
    LLVMValueRef fn = cg_rt_declare(
        g, "cw_builtin_type_of_owned",
        LLVMInt1TypeInContext(cg_ctx(g)), pr, 2);
    LLVMValueRef av[2] = { rec8, out8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 2, "");
    LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                          out, 3, "h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hp, "vh");
    CwExpr e = { h, "String" };
    return e;
}

/* builtins::readline(): 预置 None 记录兜底后调 rt 读一行 */
static CwExpr cg_builtin_readline(
    CwCodegen_t* g,
    const cw_value*node
) {
    (void)node;
    /* 失败兜底: 预置成 None 记录, rt 无论成败 out 都有效 */
    LLVMValueRef out = cg_new_record(
        g, cg_type_id("None"), cg_null_handle(g), "read.rec");
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                         cg_rt_i8_ptr(g), "");
    LLVMTypeRef pr[1] = { cg_rt_i8_ptr(g) };
    LLVMValueRef fn = cg_rt_declare(
        g, "cw_builtin_readline", LLVMInt1TypeInContext(cg_ctx(g)),
        pr, 1);
    LLVMValueRef av[1] = { out8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 1, "");
    LLVMValueRef hp = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                          out, 3, "h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, hp, "vh");
    CwExpr e = { h, "String" };
    return e;
}

/* builtins::exit(code): 转 Int32 后调 rt 退出; 表达式类型为 never ("!") */
static CwExpr cg_builtin_exit(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* arg0 = cg_call_arg0(node);
    if (!arg0) {
        cg_error(g, "exit expects 1 argument");
        return (CwExpr){ NULL, NULL };
    }
    CwExpr a = cg_expr(g, cw_object_get(arg0, "value"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef iv = cg_load_value(
        g, a, cg_scalar_type(g, a.type_name, NULL));
    LLVMValueRef code = cg_convert_scalar(g, iv, a.type_name, "Int32");
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMTypeRef pr[1] = { LLVMInt32TypeInContext(cg_ctx(g)) };
    LLVMValueRef fn = cg_rt_declare(
        g, "cw_builtin_exit", LLVMVoidTypeInContext(cg_ctx(g)), pr, 1);
    LLVMValueRef av[1] = { code };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 1, "");
    /* exit 是 never 类型: 调用之后不可达, 表达式类型记 "!" */
    CwExpr never = { cg_null_handle(g), "!" };
    return never;
}

/* 静态构造: Vector::new() / Map::new() / Set::new() */
static CwExpr cg_builtin_new(
    CwCodegen_t* g,
    const cw_value*node
) {
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
    LLVMValueRef rec = cg_new_record(
        g, cg_type_id(owner), cg_null_handle(g), "new.rec");
    LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec,
                                         cg_rt_i8_ptr(g), "");
    if (strcmp(init_fn, "cwvec_init") == 0) {
        /* cwvec_init(obj, reserve) 双参数, 与字面量路径一致 */
        LLVMTypeRef pr[2] = { cg_rt_i8_ptr(g),
                              LLVMInt64TypeInContext(cg_ctx(g)) };
        LLVMValueRef fn = cg_rt_declare(
            g, init_fn, LLVMInt1TypeInContext(cg_ctx(g)), pr, 2);
        LLVMValueRef av[2] = { rec8, cg_i64(g, 0) };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 2, "");
    } else {
        LLVMTypeRef pr[1] = { cg_rt_i8_ptr(g) };
        LLVMValueRef fn = cg_rt_declare(
            g, init_fn, LLVMInt1TypeInContext(cg_ctx(g)), pr, 1);
        LLVMValueRef av[1] = { rec8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 1, "");
    }
    LLVMValueRef hptr = LLVMBuildStructGEP2(cg_b(g), g->ll->rec_type,
                                            rec, 3, "h");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                    hptr, "vh");
    CwExpr e = { h, owner };
    return e;
}

/* 从调用点 type_args 里解析单个实参 id (param_node 为形参名节点);
 * 失败报错 (错误文案由调用方传入以区分 function/method) 并返回 INVALID */
static CwTypeId cg_generic_arg_id(
    CwCodegen_t* g, const cw_value*ta,
    const cw_value*param_node,
    const char* err_missing_fmt,
    const char* err_invalid_fmt
) {
    const char* pn = cg_json_name(param_node);
    cw_value* at = pn ? cw_object_get(ta, pn) : NULL;
    if (!pn || !at) {
        cg_error(g, err_missing_fmt, pn ? pn : "?");
        return CW_TYPE_INVALID;
    }
    CwTypeId id = cg_type_id_of(g, at);
    if (id == CW_TYPE_INVALID) {
        cg_error(g, err_invalid_fmt, pn,
                 cg_json_name(at) ? cg_json_name(at) : "?");
    }
    return id;
}

/* 泛型函数调用点: 按 type_params 顺序解析 type_args 到 ids; 失败报错 */
static bool cg_generic_fn_args(
    CwCodegen_t* g, const cw_value*ta,
    const CwNode_t* fn_node,
    size_t ntp, CwTypeId* ids
) {
    cw_value* tp = fn_node
        ? cw_object_get(fn_node->value, "type_params") : NULL;
    for (size_t i = 0; i < ntp; i++) {
        ids[i] = cg_generic_arg_id(g, ta, cw_array_get(tp, i),
                                   "generic function is missing argument %s",
                                   "invalid generic argument: %s = %s");
        if (ids[i] == CW_TYPE_INVALID) return false;
    }
    return true;
}

/* 直接函数调用 (含泛型单态化: 按调用点 type_args 登记/复用具体实例) */
/* ---- extern "C" 调用 (todo-48) ---- */

/* extern ABI 类型映射 (todo-48/51/52/54): 标量 -> 对应 LLVM 标量,
 * 原始指针 (*const T / *mut T) -> 指向标量的 LLVM 指针,
 * String -> i8* (char*, 句柄 address 直传),
 * fn(A, B) -> R -> C 函数指针,
 * 全标量非泛型结构体 -> LLVM 字面结构体 (C 布局由 LLVM 按 ABI 降级),
 * 无载荷枚举 -> i32 判别值, 其余 NULL */
#define CG_EXT_MAX_FIELDS 16

/* 字段类型名 (todo-52): 布局里的字段类型 id 经类型表取名字;
 * 若是泛型 opaque 叶子则返回 NULL (无法映射到 C) */
static const char* cg_ext_field_type_name(
    CwCodegen_t* g, const CwLayout_t* L, size_t i
) {
    const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
    if (!ft) return NULL;
    const CwType_t* t = cwtype_get(g->ll->types, L->fields[i].type);
    if (t && t->opaque) return NULL;
    return ft;
}

/* 全标量字段的非泛型结构体布局 (todo-52 聚合映射的前提条件);
 * 不满足返回 NULL */
/* 全标量字段的非泛型结构体布局 (todo-52 聚合映射的前提条件):
 *  - 全部字段为同一宽度的标量且总大小 <= 8 字节,
 *    这样聚合可以打包成单个整数按 Win64/SysV 寄存器传递
 *    (与 clang/gcc 的 C 布局降级一致, 避免编译器间的小结构体
 *    寄存器拆分差异); 不满足返回 NULL。
 * out_elem 输出单字段字节数。 */
static const CwLayout_t* cg_ext_struct_layout(
    CwCodegen_t* g, const char* tname, size_t* out_elem
) {
    const CwNode_t* decl = cg_struct_decl(g, tname);
    if (!decl) return NULL;
    /* 泛型结构体未实例化时没有确定的字段宽度, 拒绝映射 */
    cw_value* tp = cw_object_get(decl->value, "params");
    if (tp && cw_typeof(tp) == CW_ARRAY && cw_array_size(tp) > 0) {
        return NULL;
    }
    const CwLayout_t* L = cwlayout_get(
        g->ll->layouts, g->m, decl, NULL, 0);
    if (!L || L->field_count == 0
        || L->field_count > CG_EXT_MAX_FIELDS) {
        return NULL;
    }
    size_t elem = 0;
    for (size_t i = 0; i < L->field_count; i++) {
        const char* ft = cg_ext_field_type_name(g, L, i);
        if (ft == NULL || cg_is_scalar(ft) == false) return NULL;
        const size_t w = cg_scalar_bytes(ft);
        if (i == 0) elem = w;
        else if (w != elem) return NULL; /* 宽度不一致 */
    }
    if (elem * L->field_count > 8) return NULL; /* 超过单寄存器 */
    if (out_elem) *out_elem = elem;
    return L;
}

static LLVMTypeRef cg_ext_struct_llvm_type(
    CwCodegen_t* g, const CwLayout_t* L, size_t elem
) {
    return LLVMIntTypeInContext(
        cg_ctx(g), (unsigned)(elem * L->field_count * 8));
}

/* 无载荷枚举 (所有变体都不带字段) */
static bool cg_ext_is_fieldless_enum(
    CwCodegen_t* g, const char* tname
) {
    const CwNode_t* d = cg_enum_decl(g, tname);
    return d && cg_enum_max_payloads(d) == 0;
}

/* CWind 结构体句柄 -> C 值 (todo-52):
 * 逐字段经槽位句柄读出标量载荷, 按字段序打包成单个整数
 * (低字节在前), 与 clang/gcc 对 <=8 字节同宽结构体的
 * 寄存器传递方式一致。 */
static LLVMValueRef cg_ext_struct_to_c(
    CwCodegen_t* g, CwExpr e,
    const CwLayout_t* L, size_t elem
) {
    LLVMValueRef base = LLVMBuildIntToPtr(
        cg_b(g), cg_handle_addr(g, e), cg_rt_i8_ptr(g), "agg.base");
    LLVMTypeRef ity = cg_ext_struct_llvm_type(g, L, elem);
    LLVMValueRef acc = LLVMConstNull(LLVMInt64TypeInContext(cg_ctx(g)));
    for (size_t i = 0; i < L->field_count; i++) {
        const char* ft = cg_ext_field_type_name(g, L, i);
        if (!ft) {
            cg_error(g, "extern aggregate has an opaque field");
            return NULL;
        }
        LLVMTypeRef vt = cg_scalar_type(g, ft, NULL);
        LLVMValueRef slot = cg_struct_slot(g, base, L->fields[i].offset);
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                        slot, "agg.f.h");
        LLVMValueRef faddr = LLVMBuildExtractValue(
            cg_b(g), h, 1, "agg.f.addr");
        LLVMValueRef fp = LLVMBuildIntToPtr(
            cg_b(g), faddr, LLVMPointerType(vt, 0), "agg.f.p");
        LLVMValueRef v = LLVMBuildLoad2(cg_b(g), vt, fp, "agg.f.v");
        /* 零扩展到 i64 -> 按字段序移位 -> 按位或累加 */
        LLVMValueRef wide = LLVMBuildZExt(cg_b(g), v,
                                          LLVMInt64TypeInContext(cg_ctx(g)),
                                          "agg.f.w");
        if (i > 0) {
            wide = LLVMBuildShl(cg_b(g), wide,
                                cg_i64(g, i * elem * 8), "agg.f.sh");
        }
        acc = LLVMBuildOr(cg_b(g), acc, wide, "agg.or");
    }
    return LLVMBuildTrunc(cg_b(g), acc, ity, "agg.pack");
}

/* 无载荷枚举句柄 -> i32 判别值 (tag = 变体序号) */
static LLVMValueRef cg_ext_enum_to_c(
    CwCodegen_t* g, CwExpr e
) {
    LLVMValueRef base = LLVMBuildIntToPtr(
        cg_b(g), cg_handle_addr(g, e), cg_rt_i8_ptr(g), "en.base");
    return LLVMBuildLoad2(cg_b(g), LLVMInt32TypeInContext(cg_ctx(g)),
                          cg_enum_tag_ptr(g, base), "en.tag");
}

/* C 值 -> CWind 结构体句柄 (todo-52): 收到打包整数, 逐字段拆出,
 * 分配 blob 写入载荷区并生成自指槽位句柄 (与结构体字面量同一布局纪律)。 */
static CwExpr cg_ext_c_to_struct(
    CwCodegen_t* g, LLVMValueRef v,
    const char* tname
) {
    size_t elem = 0;
    const CwLayout_t* L = cg_ext_struct_layout(g, tname, &elem);
    if (!L || !elem) {
        cg_error(g, "extern aggregate has an unsupported layout: %s",
                 tname ? tname : "?");
        return (CwExpr){ NULL, NULL };
    }
    const size_t size = cg_struct_blob_size(g, L);
    LLVMValueRef blob = cg_blob_alloc(g, size, "ext.ret.st");
    LLVMValueRef base = cg_blob_i8(g, blob);
    /* 先零扩展到 i64 统一拆包宽度 */
    LLVMValueRef wide = LLVMBuildZExt(cg_b(g), v,
                                      LLVMInt64TypeInContext(cg_ctx(g)),
                                      "ext.st.w");
    for (size_t i = 0; i < L->field_count; i++) {
        const char* ft = cg_ext_field_type_name(g, L, i);
        if (!ft) {
            cg_error(g, "extern aggregate has an opaque field");
            return (CwExpr){ NULL, NULL };
        }
        const size_t vsz = cg_scalar_bytes(ft);
        LLVMTypeRef vt = cg_scalar_type(g, ft, NULL);
        LLVMValueRef bits = wide;
        if (i > 0) {
            bits = LLVMBuildLShr(cg_b(g), bits,
                                 cg_i64(g, i * elem * 8), "ext.st.shr");
        }
        LLVMValueRef val = LLVMBuildTrunc(cg_b(g), bits, vt, "ext.st.v");
        const size_t poff = cg_field_payload_offset(g, L, i);
        LLVMValueRef idx[1] = { cg_i64(g, (uint64_t)poff) };
        LLVMValueRef p = LLVMBuildGEP2(cg_b(g),
                                       LLVMInt8TypeInContext(cg_ctx(g)),
                                       base, idx, 1, "ext.st.pay");
        LLVMBuildStore(cg_b(g), val,
                       LLVMBuildBitCast(cg_b(g), p,
                                        LLVMPointerType(vt, 0), ""));
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "ext.st.addr");
        LLVMValueRef h = cg_build_handle(g, cg_i64(g, 0), addr,
                                         cg_i64(g, vsz), cg_i64(g, 0));
        LLVMValueRef slot = cg_struct_slot(g, base, L->fields[i].offset);
        LLVMBuildStore(cg_b(g), h, slot);
    }
    return (CwExpr){ cg_struct_handle(g, blob, L->field_count), tname };
}

/* i32 判别值 -> 无载荷枚举句柄 (blob 只含 tag, 槽位数 = 1) */
static CwExpr cg_ext_c_to_enum(
    CwCodegen_t* g, LLVMValueRef tag,
    const char* tname
) {
    const size_t size = cg_enum_blob_size(g, tname);
    LLVMValueRef blob = cg_blob_alloc(g, size, "ext.ret.en");
    LLVMValueRef tp = cg_enum_tag_ptr(g, cg_blob_i8(g, blob));
    LLVMBuildStore(cg_b(g), tag, tp);
    return (CwExpr){ cg_enum_handle(g, blob, tname), tname };
}


#define CG_FN_SIG_MAX 8
static bool cg_ext_fn_sig_fty(
    CwCodegen_t* g, const char* sig,
    char* buf, size_t buf_cap,
    const char** params, size_t* out_n,
    const char** out_ret,
    LLVMTypeRef pt[], LLVMTypeRef* out_fty
);

static LLVMTypeRef cg_extern_llvm_type(
    CwCodegen_t* g,
    const char* tname
) {
    if (strcmp(tname, "String") == 0) {
        return LLVMPointerType(LLVMInt8TypeInContext(cg_ctx(g)), 0);
    }
    if (strncmp(tname, "fn(", 3) == 0) {
        /* todo-54: C 回调参数 */
        char buf[256];
        const char* params[CG_FN_SIG_MAX];
        size_t n = 0;
        const char* ret = NULL;
        LLVMTypeRef pt[CG_FN_SIG_MAX];
        LLVMTypeRef fty = NULL;
        if (!cg_ext_fn_sig_fty(g, tname, buf, sizeof(buf),
                               params, &n, &ret, pt, &fty)) {
            return NULL;
        }
        return LLVMPointerType(fty, 0);
    }
    if (cg_is_rawptr(tname)) {
        const char* pointee = strchr(tname, ' ');
        pointee = pointee ? pointee + 1 : "";
        LLVMTypeRef pt = cg_scalar_type(g, pointee, NULL);
        if (!pt) pt = LLVMVoidTypeInContext(cg_ctx(g));
        return LLVMPointerType(pt, 0);
    }
    if (cg_is_struct_type(g, tname)) {
        size_t elem = 0;
        const CwLayout_t* L = cg_ext_struct_layout(g, tname, &elem);
        if (!L) return NULL;
        return cg_ext_struct_llvm_type(g, L, elem);
    }
    if (cg_is_enum_type(g, tname)) {
        if (!cg_ext_is_fieldless_enum(g, tname)) return NULL;
        return LLVMInt32TypeInContext(cg_ctx(g));
    }
    return cg_scalar_type(g, tname, NULL);
}

/* 声明 (或复用) libc strlen: extern 返回 String 时按 C 字符串约定取长 */
static LLVMValueRef cg_extern_declare_strlen(
    CwCodegen_t* g
) {
    LLVMValueRef f = LLVMGetNamedFunction(g->ll->module, "strlen");
    if (f) return f;
    LLVMTypeRef pt[1] = {
        LLVMPointerType(LLVMInt8TypeInContext(cg_ctx(g)), 0)
    };
    return LLVMAddFunction(
        g->ll->module, "strlen",
        LLVMFunctionType(LLVMInt64TypeInContext(cg_ctx(g)), pt, 1, false));
}

/* ---- extern 静态变量绑定 (todo-56) ----
 * `static [mut] NAME: Type;` 把 C 全局变量绑进 CWind:
 *  - LLVM 侧声明同名外部全局 (无初始化器, 符号交给链接库);
 *  - 读 = load 后按类型包装: 标量直接成值; *const/*mut 与 String
 *    取地址字段 (String 按 NUL 约定 strlen 取长);
 *  - 写 (仅 static mut) = 把右值存回全局;
 *  - 复合赋值仅限标量 (读-改-写)。 */

static bool cg_binding_is(
    const cw_value*ann, const char* kind
) {
    cw_value* binding = ann ? cw_object_get(ann, "binding") : NULL;
    const char* k = binding ? cg_json_kind(binding) : NULL;
    return k && strcmp(k, kind) == 0;
}

static const CwNode_t* cg_extern_static_node(
    CwCodegen_t* g, const cw_value*node
) {
    if (!cg_binding_is(cw_object_get(node, "ann"), "extern_static")) {
        return NULL;
    }
    cw_value* ann = cw_object_get(node, "ann");
    cw_value* binding = cw_object_get(ann, "binding");
    cw_value* refv = cw_object_get(binding, "ref");
    int64_t ref = 0;
    if (!refv || cw_typeof(refv) != CW_INT
        || cw_as_int(refv, &ref) != CW_OK) {
        cg_error(g, "extern static reference is missing its node id");
        return NULL;
    }
    const CwNode_t* n = cwmodule_node(g->m, ref);
    if (!n || strcmp(n->kind, "ExternStatic") != 0) {
        cg_error(g, "extern static declaration not found (id=%lld)",
                 (long long)ref);
        return NULL;
    }
    return n;
}

/* 解析 ExternStatic 节点的符号名与声明类型 */
static bool cg_extern_static_info(
    CwCodegen_t* g, const CwNode_t* n,
    const char** out_name, const char** out_type, bool* out_mutable
) {
    cw_value* nv = cw_object_get(n->value, "name");
    cw_value* tv = cw_object_get(n->value, "type");
    *out_name = (nv && cw_typeof(nv) == CW_STRING)
        ? cw_string_cstr(nv) : NULL;
    *out_type = (tv && cw_typeof(tv) == CW_OBJECT)
        ? cg_type_name_of(g, tv) : NULL;
    *out_mutable = false;
    cw_value* mv = cw_object_get(n->value, "mutable");
    if (mv && cw_typeof(mv) == CW_BOOL) {
        bool b = false;
        if (cw_as_bool(mv, &b) == CW_OK) *out_mutable = b;
    }
    if (!*out_name || !*out_type) {
        cg_error(g, "extern static is missing its name or type");
        return false;
    }
    return true;
}

static LLVMValueRef cg_extern_static_global(
    CwCodegen_t* g, const char* sname, LLVMTypeRef et
) {
    LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, sname);
    if (!gv) {
        /* 无初始化器 => 外部链接声明, 定义来自被链接的库 */
        gv = LLVMAddGlobal(g->ll->module, et, sname);
    }
    return gv;
}

/* 读 extern 静态: 按声明类型包装成 CWind 句柄 */
static CwExpr cg_expr_extern_static_read(
    CwCodegen_t* g,
    const cw_value*node
) {
    const CwNode_t* n = cg_extern_static_node(g, node);
    if (!n) return (CwExpr){ NULL, NULL };
    const char* sname = NULL;
    const char* tname = NULL;
    bool mutable_decl = false;
    if (!cg_extern_static_info(g, n, &sname, &tname, &mutable_decl)) {
        return (CwExpr){ NULL, NULL };
    }
    LLVMTypeRef et = cg_extern_llvm_type(g, tname);
    if (!et) {
        cg_error(g, "extern static '%s' has no C-ABI mapping: %s",
                 sname, tname);
        return (CwExpr){ NULL, NULL };
    }
    LLVMValueRef gv = cg_extern_static_global(g, sname, et);
    if (cg_is_scalar(tname)) {
        size_t sz = 0;
        LLVMTypeRef vt = cg_scalar_type(g, tname, &sz);
        LLVMValueRef v = LLVMBuildLoad2(cg_b(g), vt, gv, "ext.stat");
        return cg_make_scalar(g, v, vt, tname, sz);
    }
    /* 原始指针 / String: 全局里存的是地址 */
    LLVMValueRef p = LLVMBuildLoad2(cg_b(g), et, gv, "ext.stat.ptr");
    LLVMValueRef addr = LLVMBuildPtrToInt(
        cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "ext.stat.addr");
    if (strcmp(tname, "String") == 0) {
        LLVMValueRef sl = cg_extern_declare_strlen(g);
        LLVMValueRef len = LLVMBuildCall2(
            cg_b(g), LLVMGlobalGetValueType(sl), sl, &p, 1,
            "ext.stat.len");
        return (CwExpr){
            cg_build_handle(g, cg_i64(g, 0), addr, len, cg_i64(g, 0)),
            "String",
        };
    }
    return (CwExpr){
        cg_build_handle(g, cg_i64(g, 0), addr, cg_i64(g, 0),
                        cg_i64(g, 0)),
        tname,
    };
}

/* 写 extern 静态 (= / 标量复合赋值); 目标不是 extern 静态时返回 false */
static bool cg_assign_extern_static(
    CwCodegen_t* g, const cw_value*node,
    const cw_value*target, const char* op
) {
    if (!target || strcmp(cg_node_kind(target), "Name") != 0) {
        return false;
    }
    if (!cg_binding_is(cw_object_get(target, "ann"), "extern_static")) {
        return false;
    }
    const CwNode_t* n = cg_extern_static_node(g, target);
    if (!n) return true; /* 错误已在 cg_extern_static_node 内上报 */
    const char* sname = NULL;
    const char* tname = NULL;
    bool mutable_decl = false;
    if (!cg_extern_static_info(g, n, &sname, &tname, &mutable_decl)) {
        return true;
    }
    if (!mutable_decl) {
        cg_error(g,
                 "cannot assign to extern static '%s'; declare it with "
                 "'mut'", sname);
        return true;
    }
    LLVMTypeRef et = cg_extern_llvm_type(g, tname);
    if (!et) {
        cg_error(g, "extern static '%s' has no C-ABI mapping: %s",
                 sname, tname);
        return true;
    }
    LLVMValueRef gv = cg_extern_static_global(g, sname, et);
    CwExpr val = cg_expr(g, cw_object_get(node, "value"));
    if (g->failed) return true;

    if (strcmp(op, "=") != 0) {
        if (!cg_is_scalar(tname)) {
            cg_error(g,
                     "compound assignment to an extern static supports "
                     "scalars only: %s %s", sname, op);
            return true;
        }
        LLVMTypeRef vt = cg_scalar_type(g, tname, NULL);
        LLVMValueRef cur = LLVMBuildLoad2(cg_b(g), vt, gv, "ext.stat.cur");
        LLVMValueRef rhs_src = cg_load_value(
            g, val, cg_scalar_type(g, val.type_name, NULL));
        LLVMValueRef rhs = cg_convert_scalar(g, rhs_src, val.type_name,
                                             tname);
        if (g->failed) return true;
        LLVMValueRef res = cg_compound_arith(
            g, op, cur, rhs, tname, true,
            "float compound assignment to an extern static is not "
            "supported: %s",
            "unsupported compound assignment to an extern static: %s");
        if (!res) return true;
        LLVMBuildStore(cg_b(g), res, gv);
        return true;
    }

    if (cg_is_scalar(tname)) {
        val = cg_coerce_scalar(g, val, tname);
        if (g->failed) return true;
        size_t sz = 0;
        LLVMTypeRef vt = cg_scalar_type(g, tname, &sz);
        LLVMValueRef v = cg_load_value(g, val, vt);
        if (strcmp(val.type_name, tname) != 0) {
            v = cg_convert_scalar(g, v, val.type_name, tname);
            if (g->failed) return true;
        }
        LLVMBuildStore(cg_b(g), v, gv);
        return true;
    }
    /* 原始指针 / String: 存句柄 address 字段承载的指针值 */
    LLVMValueRef addr = cg_handle_addr(g, val);
    LLVMValueRef p = LLVMBuildIntToPtr(cg_b(g), addr, et, "ext.stat.p");
    LLVMBuildStore(cg_b(g), p, gv);
    return true;
}

/* ---- 函数指针互通 (todo-54) ----
 * extern 块里允许声明 fn 类型形参 (C 回调), CWind 裸函数名可取地址:
 *  - extern 函数名 -> 直接传其真实 C 地址 (C-to-C 完全互通);
 *  - CWind 函数名 -> 现场生成 C-ABI 适配器 cwind.cb.<name>.<sig>,
 *    把 C 标量/指针参数包成句柄调进 CWind, 结果解包回 C;
 *  - fn 指针变量等非字面量实参 v0 拒绝 (无法静态确定目标 ABI)。
 * 反方向 (extern 函数名赋给 fn 变量在 CWind 内调用) 由
 * cg_extern_thunk 生成的 CWind-ABI thunk 支撑。 */

/* 原地把 "fn(A, B) -> R" 拆成分段指针; 返回参数个数,
 * 出错返回 SIZE_MAX。段内不允许嵌套 fn (SA 已拒绝)。 */
static size_t cg_fn_sig_split(
    char* buf,
    const char** params, size_t max_params,
    const char** out_ret
) {
    char* open = strchr(buf, '(');
    char* close = strrchr(buf, ')');
    if (!open || !close || close < open + 1) {
        /* "fn()" 也合法: close == open+1 */
        if (!(open && close && close == open + 1)) return SIZE_MAX;
    }
    *close = '\0';
    const char* ret = NULL;
    if (close[1] != '\0') {
        char* r = close + 1;
        while (*r == ' ') r++;
        if (strncmp(r, "->", 2) != 0) return SIZE_MAX;
        r += 2;
        while (*r == ' ') r++;
        if (*r == '\0') return SIZE_MAX;
        ret = r;
    }
    *out_ret = ret;
    size_t n = 0;
    char* p = open + 1;
    while (*p) {
        while (*p == ' ') p++;
        if (*p == '\0') break;
        if (n >= max_params) return SIZE_MAX;
        params[n++] = p;
        char* comma = strchr(p, ',');
        if (!comma) break;
        *comma = '\0';
        /* 去掉段尾空格 */
        char* end = comma - 1;
        while (end >= p && *end == ' ') { *end = '\0'; end--; }
        p = comma + 1;
    }
    return n;
}

/* 解析 fn 签名并构造 C-ABI 函数类型。
 * buf/buf_cap 是签名工作副本缓冲 (调用方在 params/pt 使用期间保持有效);
 * 成功时填 params[*out_n] (段指针指向 buf), *out_ret (可为 NULL),
 * pt[] 各参数 LLVM 类型与 *out_fty 完整函数类型。 */
static bool cg_ext_fn_sig_fty(
    CwCodegen_t* g, const char* sig,
    char* buf, size_t buf_cap,
    const char** params, size_t* out_n,
    const char** out_ret,
    LLVMTypeRef pt[], LLVMTypeRef* out_fty
) {
    if (strlen(sig) >= buf_cap) return false;
    memcpy(buf, sig, strlen(sig) + 1);
    const char* ret = NULL;
    const size_t n = cg_fn_sig_split(buf, params, CG_FN_SIG_MAX, &ret);
    if (n == SIZE_MAX) return false;
    for (size_t i = 0; i < n; i++) {
        pt[i] = cg_extern_llvm_type(g, params[i]);
        if (!pt[i]) return false;
    }
    LLVMTypeRef rvt = ret ? cg_extern_llvm_type(g, ret)
                          : LLVMVoidTypeInContext(cg_ctx(g));
    if (ret && !rvt) return false;
    *out_n = n;
    *out_ret = ret;
    *out_fty = LLVMFunctionType(rvt, pt, (unsigned)n, false);
    return true;
}

/* 句柄 -> C 值 (thunk 调 C 时转换实参用) */
static LLVMValueRef cg_ext_arg_from_handle(
    CwCodegen_t* g, LLVMValueRef h,
    const char* tname, LLVMTypeRef pt
) {
    CwExpr e = { h, tname };
    if (cg_is_scalar(tname)) {
        return cg_load_value(g, e, cg_scalar_type(g, tname, NULL));
    }
    /* 指针 / String: address 即指针值 */
    return LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, e), pt, "th.arg");
}

/* C 值 -> 句柄; 标量结果经全局缓冲中转 (buf_name),
 * 与普通调用返回同一纪律: 调用点立即 fixup 拷出。 */
static CwExpr cg_ext_c_to_handle(
    CwCodegen_t* g, LLVMValueRef v,
    const char* tname, const char* buf_name
) {
    if (strcmp(tname, "String") == 0) {
        LLVMValueRef sl = cg_extern_declare_strlen(g);
        LLVMValueRef len = LLVMBuildCall2(
            cg_b(g), LLVMGlobalGetValueType(sl), sl, &v, 1, "th.strlen");
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), v, LLVMInt64TypeInContext(cg_ctx(g)), "th.addr");
        return (CwExpr){
            cg_build_handle(g, cg_i64(g, 0), addr, len, cg_i64(g, 0)),
            "String",
        };
    }
    if (cg_is_rawptr(tname)) {
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), v, LLVMInt64TypeInContext(cg_ctx(g)), "th.addr");
        return (CwExpr){
            cg_build_handle(g, cg_i64(g, 0), addr, cg_i64(g, 0),
                            cg_i64(g, 0)),
            tname,
        };
    }
    size_t sz = 0;
    LLVMTypeRef vt = cg_scalar_type(g, tname, &sz);
    LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, buf_name);
    if (!gv) {
        gv = LLVMAddGlobal(g->ll->module, vt, buf_name);
        LLVMSetInitializer(gv, LLVMConstNull(vt));
    }
    LLVMBuildStore(cg_b(g), v, gv);
    LLVMValueRef addr = LLVMBuildPtrToInt(
        cg_b(g), gv, LLVMInt64TypeInContext(cg_ctx(g)), "th.ret.addr");
    return (CwExpr){
        cg_build_handle(g, cg_i64(g, 0), addr, cg_i64(g, sz),
                        cg_i64(g, 0)),
        tname,
    };
}

/* 按 extern 声明构造 C-ABI 函数类型并确保模块内已声明;
 * 返回函数值, 失败 NULL。 */
static LLVMValueRef cg_extern_ensure_declared(
    CwCodegen_t* g, const CwSymEntry_t* sym
) {
    LLVMValueRef ef = LLVMGetNamedFunction(g->ll->module, sym->mangled);
    if (ef) return ef;
    const CwNode_t* decl = sym->decl;
    if (!decl) {
        cg_error(g, "extern declaration is missing: %s", sym->mangled);
        return NULL;
    }
    cw_value* rtv = cwmodule_fn_return_type(decl);
    const char* ret_name = rtv ? cg_type_name_of(g, rtv) : "None";
    const bool ret_void = !ret_name || strcmp(ret_name, "None") == 0;
    if (!ret_void && !cg_extern_llvm_type(g, ret_name)) {
        cg_error(g, "extern function %s has an unsupported return type: %s",
                 sym->mangled, ret_name);
        return NULL;
    }
    const size_t n = cwmodule_fn_param_count(decl);
    LLVMTypeRef* pt = (LLVMTypeRef*)malloc((n ? n : 1) * sizeof(LLVMTypeRef));
    if (!pt) {
        cg_error(g, "failed to allocate the extern declaration buffers");
        return NULL;
    }
    bool ok = true;
    for (size_t i = 0; i < n && ok; i++) {
        cw_value* p = cwmodule_fn_param(decl, i);
        cw_value* ptype = p ? cw_object_get(p, "type") : NULL;
        const char* want = (ptype && cw_typeof(ptype) == CW_OBJECT)
            ? cg_type_name_of(g, ptype) : NULL;
        pt[i] = want ? cg_extern_llvm_type(g, want) : NULL;
        if (!pt[i]) {
            cg_error(g, "extern function %s has an unsupported parameter "
                        "type: %s", sym->mangled, want ? want : "?");
            ok = false;
        }
    }
    if (!ok) {
        free(pt);
        return NULL;
    }
    ef = LLVMAddFunction(
        g->ll->module, sym->mangled,
        LLVMFunctionType(
            ret_void ? LLVMVoidTypeInContext(cg_ctx(g))
                     : cg_extern_llvm_type(g, ret_name),
            pt, (unsigned)n, false));
    free(pt);
    return ef;
}

/* extern 函数的 CWind-ABI thunk (todo-54):
 * N 个句柄入参 -> 按 extern 声明转成 C 类型 -> 调 C 函数 ->
 * 结果包回句柄。使 extern 函数能作为普通 fn 值参与间接调用。
 * 返回 thunk 函数值, 失败 NULL。 */
static LLVMValueRef cg_extern_thunk(
    CwCodegen_t* g, const CwSymEntry_t* sym
) {
    char tn[300];
    snprintf(tn, sizeof(tn), "cwind.ext.thunk.%s", sym->mangled);
    LLVMValueRef existing = LLVMGetNamedFunction(g->ll->module, tn);
    if (existing) return existing;

    cw_value* rtv = sym->decl
        ? cwmodule_fn_return_type(sym->decl) : NULL;
    const char* ret_name = rtv ? cg_type_name_of(g, rtv) : "None";
    const bool ret_void = !ret_name || strcmp(ret_name, "None") == 0;
    if (ret_void) ret_name = NULL;

    const size_t n = sym->decl
        ? cwmodule_fn_param_count(sym->decl) : 0;
    if (n > CG_FN_SIG_MAX) {
        cg_error(g, "extern callbacks support at most %d parameters",
                 CG_FN_SIG_MAX);
        return NULL;
    }
    LLVMValueRef ef = cg_extern_ensure_declared(g, sym);
    if (!ef) return NULL;

    /* 重新收集参数类型 (ensure_declared 内部数组已释放) */
    LLVMTypeRef* hts = (LLVMTypeRef*)malloc((n ? n : 1)
                                             * sizeof(LLVMTypeRef));
    const char** want = (const char**)malloc((n ? n : 1)
                                              * sizeof(const char*));
    LLVMTypeRef* pt = (LLVMTypeRef*)malloc((n ? n : 1) * sizeof(LLVMTypeRef));
    if (!hts || !want || !pt) {
        free(hts); free(want); free(pt);
        cg_error(g, "failed to allocate the extern thunk buffers");
        return NULL;
    }
    for (size_t i = 0; i < n; i++) {
        cw_value* p = cwmodule_fn_param(sym->decl, i);
        cw_value* ptype = p ? cw_object_get(p, "type") : NULL;
        want[i] = (ptype && cw_typeof(ptype) == CW_OBJECT)
            ? cg_type_name_of(g, ptype) : NULL;
        pt[i] = want[i] ? cg_extern_llvm_type(g, want[i]) : NULL;
        hts[i] = g->ll->handle_type;
        if (!pt[i]) {
            cg_error(g, "extern function %s has an unsupported parameter "
                        "type: %s", sym->mangled,
                     want[i] ? want[i] : "?");
            free(hts); free(want); free(pt);
            return NULL;
        }
    }

    LLVMValueRef th = LLVMAddFunction(
        g->ll->module, tn,
        LLVMFunctionType(g->ll->handle_type, hts, (unsigned)n, false));

    LLVMBasicBlockRef saved_block = LLVMGetInsertBlock(g->builder);
    LLVMValueRef saved_fn = g->current_fn;
    g->current_fn = th;
    LLVMBasicBlockRef entry = LLVMAppendBasicBlockInContext(
        cg_ctx(g), th, "entry");
    LLVMPositionBuilderAtEnd(cg_b(g), entry);

    LLVMValueRef argv[CG_FN_SIG_MAX];
    for (size_t i = 0; i < n; i++) {
        argv[i] = cg_ext_arg_from_handle(g, LLVMGetParam(th, (unsigned)i),
                                         want[i], pt[i]);
    }
    LLVMValueRef res = LLVMBuildCall2(cg_b(g),
                                      LLVMGlobalGetValueType(ef), ef, argv,
                                      (unsigned)n, "th.call");
    CwExpr out;
    if (ret_name) {
        char gname[192];
        snprintf(gname, sizeof(gname), "fnret.thunk.%s", sym->mangled);
        out = cg_ext_c_to_handle(g, res, ret_name, gname);
    } else {
        out = (CwExpr){ cg_null_handle(g), "None" };
    }
    LLVMBuildRet(cg_b(g), out.handle);

    g->current_fn = saved_fn;
    if (saved_block) LLVMPositionBuilderAtEnd(cg_b(g), saved_block);
    free(hts); free(want); free(pt);
    return th;
}

/* 把签名串清洗成可作符号名的形态 (字母数字保留, 其余 '_') */
static void cg_sanitize_sig(
    const char* sig, char* out, size_t cap
) {
    size_t j = 0;
    for (size_t i = 0; sig[i] != '\0' && j + 1 < cap; i++) {
        const char c = sig[i];
        const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
            || (c >= '0' && c <= '9');
        out[j++] = ok ? c : '_';
    }
    out[j] = '\0';
}

/* CWind 函数的 C-ABI 适配器 (todo-54): 让 CWind 函数能当回调传给 C。
 * 以声明的回调签名生成 C-ABI 函数: 参数包成句柄 -> 调 CWind 函数 ->
 * 结果解包为 C 类型。返回适配器函数值, 失败 NULL。 */
static LLVMValueRef cg_callback_adapter(
    CwCodegen_t* g, const CwSymEntry_t* target, const char* sig
) {
    char sbuf[128];
    cg_sanitize_sig(sig, sbuf, sizeof(sbuf));
    char aname[384];
    snprintf(aname, sizeof(aname), "cwind.cb.%s.%s", target->name, sbuf);
    LLVMValueRef existing = LLVMGetNamedFunction(g->ll->module, aname);
    if (existing) return existing;

    char buf[256];
    const char* params[CG_FN_SIG_MAX];
    size_t n = 0;
    const char* ret = NULL;
    LLVMTypeRef pt[CG_FN_SIG_MAX];
    LLVMTypeRef fty = NULL;
    if (!cg_ext_fn_sig_fty(g, sig, buf, sizeof(buf),
                           params, &n, &ret, pt, &fty)) {
        cg_error(g, "unsupported callback signature: %s", sig);
        return NULL;
    }
    LLVMValueRef tfn = LLVMGetNamedFunction(g->ll->module, target->mangled);
    if (!tfn) {
        cg_error(g, "callback target is not declared: %s", target->mangled);
        return NULL;
    }
    LLVMValueRef ad = LLVMAddFunction(g->ll->module, aname, fty);

    LLVMBasicBlockRef saved_block = LLVMGetInsertBlock(g->builder);
    LLVMValueRef saved_fn = g->current_fn;
    g->current_fn = ad;
    LLVMBasicBlockRef entry = LLVMAppendBasicBlockInContext(
        cg_ctx(g), ad, "entry");
    LLVMPositionBuilderAtEnd(cg_b(g), entry);

    LLVMValueRef argv[CG_FN_SIG_MAX];
    for (size_t i = 0; i < n; i++) {
        LLVMValueRef p = LLVMGetParam(ad, (unsigned)i);
        if (cg_is_scalar(params[i])) {
            /* C 标量 -> 槽位句柄 (alloca 在入口块, 存活整个适配器) */
            size_t sz = 0;
            LLVMTypeRef vt = cg_scalar_type(g, params[i], &sz);
            LLVMValueRef slot = cg_alloca(g, vt, "cb.slot");
            LLVMBuildStore(cg_b(g), p, slot);
            LLVMValueRef addr = LLVMBuildPtrToInt(
                cg_b(g), slot, LLVMInt64TypeInContext(cg_ctx(g)),
                "cb.slot.addr");
            argv[i] = cg_build_handle(g, cg_i64(g, 0), addr,
                                      cg_i64(g, sz), cg_i64(g, 0));
        } else if (strcmp(params[i], "String") == 0) {
            /* char* -> String 句柄: strlen 取长 (NUL 约定) */
            LLVMValueRef sl = cg_extern_declare_strlen(g);
            LLVMValueRef len = LLVMBuildCall2(
                cg_b(g), LLVMGlobalGetValueType(sl), sl, &p, 1,
                "cb.strlen");
            LLVMValueRef addr = LLVMBuildPtrToInt(
                cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "cb.addr");
            argv[i] = cg_build_handle(g, cg_i64(g, 0), addr, len,
                                      cg_i64(g, 0));
        } else {
            /* *const T / *mut T: 地址即值 */
            LLVMValueRef addr = LLVMBuildPtrToInt(
                cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "cb.addr");
            argv[i] = cg_build_handle(g, cg_i64(g, 0), addr,
                                      cg_i64(g, 0), cg_i64(g, 0));
        }
    }
    LLVMValueRef res = LLVMBuildCall2(cg_b(g),
                                      LLVMGlobalGetValueType(tfn), tfn,
                                      argv, (unsigned)n, "cb.call");
    if (ret != NULL) {
        /* 解包句柄结果为 C 值 */
        LLVMValueRef addr = LLVMBuildExtractValue(
            cg_b(g), res, 1, "cb.ret.addr");
        LLVMTypeRef rvt = cg_extern_llvm_type(g, ret);
        LLVMValueRef rp = LLVMBuildIntToPtr(
            cg_b(g), addr, LLVMPointerType(rvt, 0), "cb.ret.p");
        LLVMValueRef v = LLVMBuildLoad2(cg_b(g), rvt, rp, "cb.ret.v");
        LLVMBuildRet(cg_b(g), v);
    } else {
        LLVMBuildRetVoid(cg_b(g));
    }

    g->current_fn = saved_fn;
    if (saved_block) LLVMPositionBuilderAtEnd(cg_b(g), saved_block);
    return ad;
}

/* 解析传给 extern fn 类型形参的实参 (todo-54):
 * 仅接受裸函数名 —— extern 函数直传 C 地址; CWind 函数经适配器包装。 */
static LLVMValueRef cg_callback_argument(
    CwCodegen_t* g, const cw_value*arg_node, const char* sig
) {
    cw_value* v = cw_object_get(arg_node, "value");
    const char* nm = NULL;
    if (v && strcmp(cg_node_kind(v), "Name") == 0) {
        cw_value* parts = cw_object_get(v, "parts");
        if (parts && cw_typeof(parts) == CW_ARRAY
            && cw_array_size(parts) == 1) {
            cw_value* p0 = cw_array_get(parts, 0);
            nm = (p0 && cw_typeof(p0) == CW_STRING)
                ? cw_string_cstr(p0) : NULL;
        }
    }
    cw_value* ann = v ? cw_object_get(v, "ann") : NULL;
    if (!nm || !cg_binding_is(ann, "fn")) {
        cg_error(g,
                 "only a bare function name can be passed as a C "
                 "callback (variables are not supported yet)");
        return NULL;
    }
    const CwSymEntry_t* sym = cwsym_find(g->ll->syms, NULL, nm);
    if (!sym) {
        cg_error(g, "function symbol not found: %s", nm);
        return NULL;
    }
    if (sym->kind == CW_SYM_EXTERN) {
        /* C-to-C: 直接传 extern 函数的真实地址 */
        LLVMValueRef ef = cg_extern_ensure_declared(g, sym);
        if (!ef) return NULL;
        return ef;
    }
    if (sym->kind != CW_SYM_FN) {
        cg_error(g,
                 "generic or method targets cannot be passed as C "
                 "callbacks yet: %s", nm);
        return NULL;
    }
    LLVMValueRef ad = cg_callback_adapter(g, sym, sig);
    if (!ad) return NULL;
    /* 目标类型是按签名构造的函数指针, 位域一致, 直接 bitcast 对齐 */
    char buf[256];
    const char* params[CG_FN_SIG_MAX];
    size_t n = 0;
    const char* ret = NULL;
    LLVMTypeRef pt[CG_FN_SIG_MAX];
    LLVMTypeRef fty = NULL;
    if (!cg_ext_fn_sig_fty(g, sig, buf, sizeof(buf),
                           params, &n, &ret, pt, &fty)) {
        cg_error(g, "unsupported callback signature: %s", sig);
        return NULL;
    }
    return LLVMBuildBitCast(cg_b(g), ad, LLVMPointerType(fty, 0),
                            "cb.cast");
}

/* extern 函数调用:
 * CWind 值是句柄, extern 函数吃原生 C 类型, 调用点做双向转换:
 *  - 标量实参: 从句柄地址 load 出原始值 (先按形参宽度 coerce);
 *  - 指针实参: 句柄 address 字段即指针值, inttoptr 后直传;
 *  - String 实参 (todo-51): 句柄 address 即字节指针 (字面量与拼接
 *    结果都以 NUL 结尾), 按 char* 直传;
 *  - None 返回: void 函数, 结果为空句柄;
 *  - 标量返回: 写进 fnret.ext.<name> 专用全局缓冲再按标量语义取回
 *    (cg_fixup_call_result 立即拷进调用方本地临时, -O3 安全;
 *    不复用 g->ret_global —— 那是外层函数自身 return 语句的缓冲);
 *  - 指针返回: 地址即值, 句柄 address 直接承载;
 *  - String 返回 (todo-51): C 返回 char*, strlen 取长后构造成
 *    String 句柄 (内存归 C 所有, 只读使用). */
static CwExpr cg_call_extern(
    CwCodegen_t* g,
    const cw_value*node,
    const CwSymEntry_t* sym
) {
    const CwNode_t* decl = sym->decl;
    if (!decl) {
        cg_error(g, "extern declaration is missing: %s", sym->mangled);
        return (CwExpr){ NULL, NULL };
    }
    cw_value* rtv = cwmodule_fn_return_type(decl);
    const char* ret_name = rtv ? cg_type_name_of(g, rtv) : "None";
    const bool ret_void = !ret_name || strcmp(ret_name, "None") == 0;
    if (!ret_void && strncmp(ret_name, "fn(", 3) == 0) {
        cg_error(g,
                 "extern function %s cannot return a function pointer "
                 "(callbacks are parameter-only)", sym->mangled);
        return (CwExpr){ NULL, NULL };
    }
    if (!ret_void && !cg_extern_llvm_type(g, ret_name)) {
        cg_error(g, "extern function %s has an unsupported return type: %s",
                 sym->mangled, ret_name);
        return (CwExpr){ NULL, NULL };
    }

    const size_t n = cwmodule_fn_param_count(decl);
    LLVMTypeRef* pt = (LLVMTypeRef*)malloc((n ? n : 1)
                                           * sizeof(LLVMTypeRef));
    const char** want = (const char**)malloc((n ? n : 1)
                                             * sizeof(const char*));
    LLVMValueRef* argv = (LLVMValueRef*)malloc((n ? n : 1)
                                               * sizeof(LLVMValueRef));
    if (!pt || !want || !argv) {
        free(pt); free(want); free(argv);
        cg_error(g, "failed to allocate the extern call buffers");
        return (CwExpr){ NULL, NULL };
    }
    for (size_t i = 0; i < n; i++) {
        cw_value* p = cwmodule_fn_param(decl, i);
        cw_value* ptype = p ? cw_object_get(p, "type") : NULL;
        want[i] = (ptype && cw_typeof(ptype) == CW_OBJECT)
            ? cg_type_name_of(g, ptype) : NULL;
        pt[i] = want[i] ? cg_extern_llvm_type(g, want[i]) : NULL;
        if (!pt[i]) {
            cg_error(g,
                     "extern function %s has an unsupported parameter "
                     "type: %s",
                     sym->mangled, want[i] ? want[i] : "?");
            free(pt); free(want); free(argv);
            return (CwExpr){ NULL, NULL };
        }
    }

    LLVMTypeRef fty = LLVMFunctionType(
        ret_void ? LLVMVoidTypeInContext(cg_ctx(g))
                 : cg_extern_llvm_type(g, ret_name),
        pt, (unsigned)n, false);
    LLVMValueRef fn = LLVMGetNamedFunction(g->ll->module, sym->mangled);
    if (!fn) {
        fn = LLVMAddFunction(g->ll->module, sym->mangled, fty);
    }

    cw_value* args = cw_object_get(node, "args");
    const size_t nargs = (args && cw_typeof(args) == CW_ARRAY)
        ? cw_array_size(args) : 0;
    if (nargs != n) {
        cg_error(g, "extern function %s expects %zu argument(s), got %zu",
                 sym->mangled, n, nargs);
        free(pt); free(want); free(argv);
        return (CwExpr){ NULL, NULL };
    }
    for (size_t i = 0; i < nargs; i++) {
        cw_value* arg = cw_array_get(args, i);
        if (strncmp(want[i], "fn(", 3) == 0) {
            /* todo-54: C 回调形参 (裸 extern 函数名直传地址,
             * 裸 CWind 函数名经适配器包装) */
            argv[i] = cg_callback_argument(g, arg, want[i]);
            if (g->failed) {
                free(pt); free(want); free(argv);
                return (CwExpr){ NULL, NULL };
            }
            continue;
        }
        CwExpr a = cg_expr(g, cw_object_get(arg, "value"));
        if (g->failed) {
            free(pt); free(want); free(argv);
            return (CwExpr){ NULL, NULL };
        }
        a = cg_coerce_scalar(g, a, want[i]);
        if (g->failed) {
            free(pt); free(want); free(argv);
            return (CwExpr){ NULL, NULL };
        }
        if (cg_is_rawptr(want[i])) {
            /* 句柄 address 即指针值 */
            argv[i] = LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, a),
                                        pt[i], "ext.ptr");
        } else if (strcmp(want[i], "String") == 0) {
            /* todo-51: String 句柄 address 即字节指针, 按 char* 直传 */
            argv[i] = LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, a),
                                        pt[i], "ext.str");
        } else if (cg_is_struct_type(g, want[i])) {
            /* todo-52: 结构体按值传给 C (打包成单寄存器整数) */
            size_t elem = 0;
            const CwLayout_t* AL =
                cg_ext_struct_layout(g, want[i], &elem);
            if (!AL) {
                cg_error(g,
                         "extern function %s has an unsupported aggregate "
                         "parameter type: %s (v0 maps only uniform-width "
                         "all-scalar structs up to 8 bytes)",
                         sym->mangled, want[i]);
                free(pt); free(want); free(argv);
                return (CwExpr){ NULL, NULL };
            }
            argv[i] = cg_ext_struct_to_c(g, a, AL, elem);
        } else if (cg_is_enum_type(g, want[i])) {
            /* todo-52: 无载荷枚举按 i32 判别值传递 */
            argv[i] = cg_ext_enum_to_c(g, a);
        } else {
            argv[i] = cg_load_value(
                g, a, cg_scalar_type(g, want[i], NULL));
        }
    }

    LLVMValueRef res = LLVMBuildCall2(cg_b(g), fty, fn, argv,
                                      (unsigned)nargs,
                                      ret_void ? "" : "ext.call");
    free(pt);
    free(want);
    free(argv);

    if (ret_void) {
        return (CwExpr){ cg_null_handle(g), "None" };
    }
    if (cg_is_struct_type(g, ret_name)) {
        /* todo-52: C 按值返回结构体 -> 重组为 CWind blob */
        return cg_ext_c_to_struct(g, res, ret_name);
    }
    if (cg_is_enum_type(g, ret_name)) {
        /* todo-52: i32 判别值 -> 无载荷枚举句柄 */
        return cg_ext_c_to_enum(g, res, ret_name);
    }
    if (cg_is_rawptr(ret_name)) {
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), res, LLVMInt64TypeInContext(cg_ctx(g)), "ext.addr");
        return (CwExpr){
            cg_build_handle(g, cg_i64(g, 0), addr, cg_i64(g, 0),
                            cg_i64(g, 0)),
            ret_name,
        };
    }
    if (strcmp(ret_name, "String") == 0) {
        /* todo-51: C 返回 char*, 按 NUL 结尾约定取 strlen 后
         * 构造 String 句柄 (数据归 C 所有) */
        LLVMValueRef sl = cg_extern_declare_strlen(g);
        LLVMValueRef len = LLVMBuildCall2(
            cg_b(g), LLVMGlobalGetValueType(sl), sl, &res, 1, "ext.strlen");
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), res, LLVMInt64TypeInContext(cg_ctx(g)), "ext.addr");
        return (CwExpr){
            cg_build_handle(g, cg_i64(g, 0), addr, len, cg_i64(g, 0)),
            "String",
        };
    }
    size_t rsz = 0;
    LLVMTypeRef rvt = cg_scalar_type(g, ret_name, &rsz);
    char gname[192];
    snprintf(gname, sizeof(gname), "fnret.ext.%s", sym->mangled);
    LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
    if (!gv) {
        gv = LLVMAddGlobal(g->ll->module, rvt, gname);
        LLVMSetInitializer(gv, LLVMConstNull(rvt));
    }
    LLVMBuildStore(cg_b(g), res, gv);
    LLVMValueRef addr = LLVMBuildPtrToInt(
        cg_b(g), gv, LLVMInt64TypeInContext(cg_ctx(g)), "fnret.addr");
    LLVMValueRef h = cg_build_handle(g, cg_i64(g, 0), addr,
                                     cg_i64(g, rsz), cg_i64(g, 0));
    return cg_fixup_call_result(g, h, ret_name, cg_node_ann_type(node));
}

static CwExpr cg_call_fn(
    CwCodegen_t* g,
    const cw_value*node,
    const cw_value*ref_v
) {
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
    if (sym->kind == CW_SYM_EXTERN) {
        return cg_call_extern(g, node, sym);
    }
    const char* target_mangled = sym->mangled;
    if (sym->kind == CW_SYM_TEMPLATE) {
        cw_value* ann = cw_object_get(node, "ann");
        cw_value* call = ann ? cw_object_get(ann, "call") : NULL;
        cw_value* ta = call ? cw_object_get(call, "type_args") : NULL;
        cw_value* tp = fn_node ? cw_object_get(fn_node->value,
                                               "type_params") : NULL;
        const size_t ntp = (tp && cw_typeof(tp) == CW_ARRAY)
            ? cw_array_size(tp) : 0;
        if (!ta || ntp == 0) {
            cg_error(g, "generic function is missing type_args: %s",
                     fname ? fname : "?");
            return (CwExpr){ NULL, NULL };
        }
        CwTypeId* ids = (CwTypeId*)malloc(ntp * sizeof(CwTypeId));
        if (!ids) {
            cg_error(g, "failed to allocate generic arguments");
            return (CwExpr){ NULL, NULL };
        }
        if (!cg_generic_fn_args(g, ta, fn_node, ntp, ids)) {
            free(ids);
            return (CwExpr){ NULL, NULL };
        }
        char base[256];
        snprintf(base, sizeof(base), "cwind.fn.%s", fname);
        char im[512];
        if (!cw_mangle_instance(im, sizeof(im), base,
                                g->ll->types, ids, ntp)) {
            cg_error(g, "failed to mangle the generic instance name: %s",
                     fname);
            free(ids);
            return (CwExpr){ NULL, NULL };
        }
        const CwSymEntry_t* inst = cwsym_find_mangled(g->ll->syms, im);
        if (!inst) {
            inst = cwsym_add(g->ll->syms, im, fname, CW_SYM_INSTANCE,
                             NULL, NULL, ids, ntp, fn_node);
            if (!inst) {
                cg_error(g, "failed to register the generic instance: %s",
                         im);
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

/* 间接调用: callee 是持有函数指针值的表达式 (变量/参数),
 * ABI 与普通函数一致 (N 个句柄入参 -> 句柄返回)。 */
static CwExpr cg_call_indirect(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* callee = cw_object_get(node, "callee");
    if (!callee) {
        cg_error(g, "indirect call is missing its callee");
        return (CwExpr){ NULL, NULL };
    }
    CwExpr f = cg_expr(g, callee);
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef fp = cg_load_value(
        g, f, LLVMInt64TypeInContext(cg_ctx(g)));
    /* todo-54: 被调方可能是 extern thunk / C-ABI 适配器,
     * 它们按声明的标量宽度读写槽位 —— 实参必须先按静态签名
     * coerce 到目标宽度, 否则窄类型实参会被宽读取读到相邻内存 */
    const char* callee_sig = f.type_name;
    char sigbuf[256];
    const char* sig_params[CG_FN_SIG_MAX];
    size_t sig_n = 0;
    const char* sig_ret = NULL;
    LLVMTypeRef sig_pt[CG_FN_SIG_MAX];
    LLVMTypeRef sig_fty = NULL;
    bool have_sig = false;
    if (callee_sig && strncmp(callee_sig, "fn(", 3) == 0
        && strlen(callee_sig) < sizeof(sigbuf)) {
        have_sig = cg_ext_fn_sig_fty(
            g, callee_sig, sigbuf, sizeof(sigbuf),
            sig_params, &sig_n, &sig_ret, sig_pt, &sig_fty);
    }
    cw_value* args = cw_object_get(node, "args");
    const size_t n = (args && cw_typeof(args) == CW_ARRAY)
        ? cw_array_size(args) : 0;
    LLVMTypeRef fty;
    if (n > 0) {
        LLVMTypeRef pt[8];
        if (n > 8) {
            cg_error_at(g, node,
                        "indirect calls support at most 8 arguments");
            return (CwExpr){ NULL, NULL };
        }
        for (size_t i = 0; i < n; i++) pt[i] = g->ll->handle_type;
        fty = LLVMFunctionType(g->ll->handle_type, pt, (unsigned)n, false);
    } else {
        fty = LLVMFunctionType(g->ll->handle_type, NULL, 0, false);
    }
    LLVMValueRef callable = LLVMBuildIntToPtr(
        cg_b(g), fp, LLVMPointerType(fty, 0), "callable");
    LLVMValueRef* argv = (LLVMValueRef*)malloc(
        (n ? n : 1) * sizeof(LLVMValueRef));
    if (!argv) {
        cg_error(g, "failed to allocate the argument array");
        return (CwExpr){ NULL, NULL };
    }
    for (size_t i = 0; i < n; i++) {
        CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, i),
                                            "value"));
        if (g->failed) {
            free(argv);
            return (CwExpr){ NULL, NULL };
        }
        if (have_sig && i < sig_n) {
            a = cg_coerce_scalar(g, a, sig_params[i]);
            if (g->failed) {
                free(argv);
                return (CwExpr){ NULL, NULL };
            }
        }
        argv[i] = a.handle;
    }
    LLVMValueRef h = LLVMBuildCall2(cg_b(g), fty, callable, argv,
                                    (unsigned)n, "indirect.call");
    free(argv);
    const char* ret_type = cg_node_type_name(g, node);
    return cg_fixup_call_result(g, h, ret_type,
                                cg_node_ann_type(node));
}

/* 解析绑定方法调用的目标符号名:
 * 泛型方法 (owner 参数在前 + 方法 type_params 在后) 按调用点 type_args
 * 单态化并登记实例; 非泛型方法直接 mangle。失败时报错并返回 NULL。 */
static const char* cg_method_target(
    CwCodegen_t* g, const cw_value*call,
    const CwBinding_t* b, const CwNode_t* decl,
    const char* fname, char* mangled, size_t cap
) {
    const CwNode_t* owner_decl = cwmodule_node(g->m, b->decl_id);
    cw_value* owner_tp = owner_decl
        ? cw_object_get(owner_decl->value, "params") : NULL;
    cw_value* fn_tp = decl ? cw_object_get(decl->value, "type_params")
                           : NULL;
    const size_t n_owner = (owner_tp && cw_typeof(owner_tp) == CW_ARRAY)
        ? cw_array_size(owner_tp) : 0;
    const size_t n_fn = (fn_tp && cw_typeof(fn_tp) == CW_ARRAY)
        ? cw_array_size(fn_tp) : 0;
    if (n_owner + n_fn == 0) {
        if (!fname || !cw_mangle_method(mangled, cap, b->owner, fname)) {
            cg_error(g, "failed to mangle the method name: %s.%s",
                     b->owner ? b->owner : "?", fname ? fname : "?");
            return NULL;
        }
        return mangled;
    }

    /* 实例化: type_args 按 owner 参数在前、方法参数在后的顺序取 */
    const size_t nt = n_owner + n_fn;
    CwTypeId* ids = (CwTypeId*)malloc(nt * sizeof(CwTypeId));
    if (!ids) {
        cg_error(g, "failed to allocate generic method arguments");
        return NULL;
    }
    cw_value* ta = call ? cw_object_get(call, "type_args") : NULL;
    if (!ta) {
        cg_error(g, "generic method is missing type_args: %s",
                 fname ? fname : "?");
        free(ids);
        return NULL;
    }
    const char* target = NULL;
    bool ok = true;
    size_t k = 0;
    for (size_t pass = 0; pass < 2 && ok; pass++) {
        cw_value* plist = (pass == 0) ? owner_tp : fn_tp;
        const size_t np = (pass == 0) ? n_owner : n_fn;
        for (size_t i = 0; i < np && ok; i++) {
            ids[k] = cg_generic_arg_id(
                g, ta, cw_array_get(plist, i),
                "generic method is missing argument %s",
                "invalid generic method argument: %s = %s");
            if (ids[k] == CW_TYPE_INVALID) {
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
        if (!cw_mangle_instance(mangled, cap, base,
                                g->ll->types, ids, nt)) {
            cg_error(g, "failed to mangle the generic method instance name: %s",
                     fname ? fname : "?");
            ok = false;
        }
    }
    if (ok) {
        const size_t ml = strlen(mangled);
        if (ml + strlen(fname) + 2 > cap) {
            cg_error(g, "generic method instance name is too long: %s",
                     fname);
            ok = false;
        } else {
            snprintf(mangled + ml, cap - ml, ".%s", fname);
            const CwSymEntry_t* inst =
                cwsym_find_mangled(g->ll->syms, mangled);
            if (!inst) {
                inst = cwsym_add(g->ll->syms, mangled, fname,
                                 CW_SYM_INSTANCE, b->owner, b->trait,
                                 ids, nt, decl);
                if (!inst) {
                    cg_error(g, "failed to register the generic method instance: %s",
                             mangled);
                    ok = false;
                } else {
                    cwllvm_declare_function(
                        g->ll, mangled, cwmodule_fn_param_count(decl));
                }
            }
            if (ok) target = inst->mangled;
        }
    }
    free(ids);
    return target;
}

/* 绑定方法调用 (callee_ref 为 bindings 表 id):
 * 解析目标符号后装配接收者 (显式/隐式 self) 与实参并发射调用 */
static CwExpr cg_call_bound_method(
    CwCodegen_t* g, const cw_value*node,
    const cw_value*ref_v
) {
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
    cw_value* ann = cw_object_get(node, "ann");
    cw_value* call = ann ? cw_object_get(ann, "call") : NULL;
    const char* target_mangled = cg_method_target(
        g, call, b, decl, fname, mangled, sizeof(mangled));
    if (!target_mangled) return (CwExpr){ NULL, NULL };
    LLVMValueRef fn = LLVMGetNamedFunction(g->ll->module,
                                           target_mangled);
    if (!fn) {
        cg_error(g, "method is not declared: %s", target_mangled);
        return (CwExpr){ NULL, NULL };
    }
    cw_value* callee = cw_object_get(node, "callee");
    const bool is_instance = callee
        && strcmp(cg_node_kind(callee), "Attribute") == 0;
    /* `Self::shift_up(...)` 这类静态写法调用实例方法时, SA 已按
     * 隐式 self 检查参数; codegen 需补传当前函数的 self。 */
    bool has_self = decl && cwmodule_fn_param_count(decl) > 0;
    if (has_self) {
        cw_value* p0 = cwmodule_fn_param(decl, 0);
        cw_value* pnm = p0 ? cw_object_get(p0, "name") : NULL;
        has_self = pnm && cw_typeof(pnm) == CW_STRING
            && strcmp(cw_string_cstr(pnm), "self") == 0;
    }
    const bool implicit_self = !is_instance && has_self;
    cw_value* args = cw_object_get(node, "args");
    const size_t na = (args && cw_typeof(args) == CW_ARRAY)
        ? cw_array_size(args) : 0;
    const size_t nparams = decl ? cwmodule_fn_param_count(decl) : 0;
    if (na + ((is_instance || implicit_self) ? 1u : 0u) != nparams) {
        cg_error(g, "method %s argument count mismatch (%zu vs %zu)",
                 mangled,
                 na + ((is_instance || implicit_self) ? 1u : 0u),
                 nparams);
        return (CwExpr){ NULL, NULL };
    }
    LLVMValueRef* argv = (LLVMValueRef*)malloc(
        (nparams ? nparams : 1) * sizeof(LLVMValueRef));
    if (!argv) {
        cg_error(g, "failed to allocate the method argument array");
        return (CwExpr){ NULL, NULL };
    }
    size_t ai = 0;
    if (is_instance || implicit_self) {
        CwExpr recv;
        if (is_instance) {
            recv = cg_expr(g, cw_object_get(callee, "obj"));
        } else {
            CwVar_t* sv = cg_var_find(g, "self");
            if (!sv) {
                cg_error(g,
                    "instance method %s requires a receiver "
                    "(self is unavailable here)", mangled);
                free(argv);
                return (CwExpr){ NULL, NULL };
            }
            recv = cg_var_read(g, sv);
        }
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
        const size_t pi = (is_instance || implicit_self) ? i + 1 : i;
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
    return cg_fixup_call_result(g, h, t, cg_node_ann_type(node));
}

/* Vector 内置方法体 (owner 分派子例程) */
static CwExpr cg_vec_method(
    CwCodegen_t* g, const cw_value*node,
    const cw_value*objv, const char* mname,
    const cw_value*args, size_t nargs,
    LLVMValueRef rec8
) {
    if (strcmp(mname, "push_back") == 0 && nargs == 1) {
        cw_value* arg_val = cw_object_get(cw_array_get(args, 0),
                                          "value");
        CwExpr a = cg_expr(g, arg_val);
        if (g->failed) return (CwExpr){ NULL, NULL };
        a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
        LLVMValueRef er = cg_vector_value_record(
            g, a, cg_node_ann_type(arg_val));
        if (g->failed) return (CwExpr){ NULL, NULL };
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
            cw_value* vv = cw_object_get(cw_array_get(args, 1), "value");
            CwExpr v = cg_expr(g, vv);
            if (g->failed) return (CwExpr){ NULL, NULL };
            v = cg_coerce_scalar(g, v, cg_receiver_arg(g, objv, 0));
            LLVMValueRef er = cg_vector_value_record(
                g, v, cg_node_ann_type(vv));
            if (g->failed) return (CwExpr){ NULL, NULL };
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
        return cg_method_length(g, rec8);
    }
    if (strcmp(mname, "clear") == 0 && nargs == 0) {
        return cg_method_clear(g, rec8, "cwvec_clear");
    }
    if (strcmp(mname, "contains") == 0 && nargs == 1) {
        CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                            "value"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
        return cg_method_contains_rec(g, rec8, a);
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
        cw_value* iv = cw_object_get(cw_array_get(args, 1), "value");
        CwExpr v = cg_expr(g, iv);
        if (g->failed) return (CwExpr){ NULL, NULL };
        v = cg_coerce_scalar(g, v, cg_receiver_arg(g, objv, 0));
        LLVMValueRef er = cg_vector_value_record(
            g, v, cg_node_ann_type(iv));
        if (g->failed) return (CwExpr){ NULL, NULL };
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
    return (CwExpr){ NULL, NULL }; /* 未命中: 交回上层继续匹配 */
}

/* Map 内置方法体 (owner 分派子例程) */
static CwExpr cg_map_method(
    CwCodegen_t* g, const cw_value*node,
    const cw_value*objv, const char* mname,
    const cw_value*args, size_t nargs,
    LLVMValueRef rec8
) {
    if (strcmp(mname, "entry") == 0 && nargs == 0) {
        cg_error_at(g, node,
                    "Map.entry() can only be used as a for-in "
                    "iterable for now");
        return (CwExpr){ NULL, NULL };
    }
    if ((strcmp(mname, "get") == 0 || strcmp(mname, "set") == 0)
        && nargs >= 1) {
        CwExpr k = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                            "value"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        k = cg_coerce_scalar(g, k, cg_receiver_arg(g, objv, 0));
        LLVMValueRef kr = cg_container_value_record(g, k);
        if (g->failed) return (CwExpr){ NULL, NULL };
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
            cw_value* vv = cw_object_get(cw_array_get(args, 1), "value");
            CwExpr v = cg_expr(g, vv);
            if (g->failed) return (CwExpr){ NULL, NULL };
            v = cg_coerce_scalar(g, v, cg_receiver_arg(g, objv, 1));
            LLVMValueRef vr = cg_vector_value_record(
                g, v, cg_node_ann_type(vv));
            if (g->failed) return (CwExpr){ NULL, NULL };
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
        return cg_method_clear(g, rec8, "cwmap_clear");
    }
    if (strcmp(mname, "length") == 0 && nargs == 0) {
        return cg_method_length(g, rec8);
    }
    if (strcmp(mname, "contains") == 0 && nargs == 1) {
        CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                            "value"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        return cg_method_contains_rec(g, rec8, a);
    }
    return (CwExpr){ NULL, NULL }; /* 未命中: 交回上层继续匹配 */
}

/* 容器/String 内置方法分派 (接收者为 Attribute): 按 owner 路由;
 * owner 例程未命中时继续尝试后续分支 (to_string 对所有类型可用) */
static CwExpr cg_container_method(
    CwCodegen_t* g,
    const cw_value*node
) {
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
        CwExpr r = cg_vec_method(g, node, objv, mname, args, nargs, rec8);
        if (r.handle || g->failed) return r;
    } else if (strcmp(owner, "Map") == 0) {
        CwExpr r = cg_map_method(g, node, objv, mname, args, nargs, rec8);
        if (r.handle || g->failed) return r;
    } else if (strcmp(owner, "String") == 0) {
        if (strcmp(mname, "length") == 0 && nargs == 0) {
            return cg_method_length(g, rec8);
        }
        if (strcmp(mname, "contains") == 0 && nargs == 1) {
            CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
            return cg_method_contains_rec(g, rec8, a);
        }
    } else if (strcmp(owner, "Set") == 0) {
        if ((strcmp(mname, "add") == 0 || strcmp(mname, "remove") == 0)
            && nargs == 1) {
            CwExpr a = cg_expr(g, cw_object_get(
                cw_array_get(args, 0), "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
            LLVMValueRef er = cg_container_value_record(g, a);
            if (g->failed) return (CwExpr){ NULL, NULL };
            LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                                cg_rt_i8_ptr(g), "");
            LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
            LLVMValueRef f = cg_rt_declare(
                g, strcmp(mname, "add") == 0
                    ? "cwset_add" : "cwset_remove",
                LLVMInt1TypeInContext(cg_ctx(g)), pt, 2);
            LLVMValueRef av[2] = { rec8, er8 };
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2,
                           "");
            CwExpr none = { cg_null_handle(g), "None" };
            return none;
        }
        if (strcmp(mname, "length") == 0 && nargs == 0) {
            return cg_method_length(g, rec8);
        }
        if (strcmp(mname, "clear") == 0 && nargs == 0) {
            return cg_method_clear(g, rec8, "cwset_clear");
        }
        if (strcmp(mname, "contains") == 0 && nargs == 1) {
            CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            return cg_method_contains_rec(g, rec8, a);
        }
    } else if (strcmp(owner, "Tuple") == 0) {
        if (strcmp(mname, "length") == 0 && nargs == 0) {
            return cg_method_length(g, rec8);
        }
    }
    if (strcmp(mname, "to_string") == 0 && nargs == 0) {
        /* Display::to_string: 任意内置值 -> String (rt 格式化) */
        return cg_call_to_string_owned(g, rec8, "tos.out");
    }
    cg_error_at(g, node, "method not supported yet: %s.%s", owner, mname);
    return (CwExpr){ NULL, NULL };
}

static CwExpr cg_expr_call(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* ann = cw_object_get(node, "ann");
    cw_value* call = ann ? cw_object_get(ann, "call") : NULL;
    if (!call) {
        cg_error_at(g, node,
                    "Call is missing ann.call (the frontend failed to "
                    "resolve this call)");
        return (CwExpr){ NULL, NULL };
    }
    cw_value* ck_v = cw_object_get(call, "callee_kind");
    const char* ck = (ck_v && cw_typeof(ck_v) == CW_STRING)
        ? cw_string_cstr(ck_v) : NULL;
    cw_value* ref_v = cw_object_get(call, "callee_ref");

    if (ck && strcmp(ck, "enum_variant") == 0) {
        return cg_call_enum_variant(g, node);
    }

    if (ck && strcmp(ck, "builtin") == 0) {
        const char* bname = (ref_v && cw_typeof(ref_v) == CW_STRING)
            ? cw_string_cstr(ref_v) : NULL;
        if (bname && strcmp(bname, "from") == 0) {
            return cg_builtin_from(g, node);
        }
        if (bname && strcmp(bname, "into") == 0) {
            return cg_builtin_into(g, node);
        }
        if (bname && strcmp(bname, "print") == 0) {
            return cg_builtin_print(g, node);
        }
        if (bname && strcmp(bname, "type_of") == 0) {
            return cg_builtin_type_of(g, node);
        }
        if (bname && strcmp(bname, "readline") == 0) {
            return cg_builtin_readline(g, node);
        }
        if (bname && strcmp(bname, "exit") == 0) {
            return cg_builtin_exit(g, node);
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
            return cg_builtin_new(g, node);
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
        return cg_call_fn(g, node, ref_v);
    }

    if (ck && strcmp(ck, "indirect") == 0) {
        return cg_call_indirect(g, node);
    }

    if (ck && strcmp(ck, "method") == 0) {
        /* 用户方法: callee_ref 是 bindings 表 id (int); 否则容器方法 */
        if (ref_v && cw_typeof(ref_v) == CW_INT) {
            return cg_call_bound_method(g, node, ref_v);
        }
        return cg_container_method(g, node);
    }

    cg_error(g, "call not supported yet: %s", ck ? ck : "?");
    return (CwExpr){ NULL, NULL };
}

static CwExpr cg_expr_index(
    CwCodegen_t* g,
    const cw_value*node
) {
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

static CwExpr cg_expr_attribute(
    CwCodegen_t* g,
    const cw_value*node
) {
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

static CwExpr cg_expr(
    CwCodegen_t* g,
    const cw_value*node
) {
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
    if (strcmp(kind, "Closure") == 0) {
        /* 非捕获闭包: 生成一个普通 LLVM 函数 + 取其地址。
         * v0 闭包不捕获环境, 与函数指针同 ABI (N 个句柄入参 -> 句柄)。 */
        const char* sig = cg_node_type_name(g, node);
        if (!sig) {
            cg_error_at(g, node, "closure is missing a signature type");
            return (CwExpr){ NULL, NULL };
        }
        cw_value* params = cw_object_get(node, "params");
        const size_t nparams = (params && cw_typeof(params) == CW_ARRAY)
            ? cw_array_size(params) : 0;
        char name[64];
        snprintf(name, sizeof(name), "$closure.%zu", g->closure_count);
        if (g->closure_count == g->closure_cap) {
            const size_t nc = g->closure_cap ? g->closure_cap * 2 : 8;
            CwClosure_t* ni = (CwClosure_t*)realloc(
                g->closures, nc * sizeof(CwClosure_t));
            if (!ni) {
                cg_error(g, "failed to grow the closure table");
                return (CwExpr){ NULL, NULL };
            }
            g->closures = ni;
            g->closure_cap = nc;
        }
        CwClosure_t* c = &g->closures[g->closure_count++];
        memset(c, 0, sizeof(*c));
        c->name = cg_own_name(g, name);
        c->decl = node;
        snprintf(c->symbol, sizeof(c->symbol), "cwind.closure.%zu",
                 g->closure_count - 1);
        if (nparams > 0) {
            LLVMTypeRef pt[8];
            if (nparams > 8) {
                cg_error_at(g, node,
                            "closures support at most 8 parameters");
                return (CwExpr){ NULL, NULL };
            }
            for (size_t i = 0; i < nparams; i++) pt[i] = g->ll->handle_type;
            LLVMTypeRef ft = LLVMFunctionType(
                g->ll->handle_type, pt, (unsigned)nparams, false);
            LLVMAddFunction(g->ll->module, c->symbol, ft);
        } else {
            LLVMTypeRef ft = LLVMFunctionType(
                g->ll->handle_type, NULL, 0, false);
            LLVMAddFunction(g->ll->module, c->symbol, ft);
        }
        return cg_make_scalar(
            g,
            LLVMBuildPtrToInt(
                cg_b(g),
                LLVMGetNamedFunction(g->ll->module, c->symbol),
                LLVMInt64TypeInContext(cg_ctx(g)), "fp"),
            LLVMInt64TypeInContext(cg_ctx(g)),
            sig,
            8);
    }
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

static void cg_stmt(
    CwCodegen_t* g,
    const cw_value*node
);

/* for 迭代变量类型: ForStmt.type 优先, 否则 iterable 的 Vector<T> 实参 */
static const char* cg_elem_type(
    CwCodegen_t* g,
    const cw_value*node
) {
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
        if (tn && strcmp(tn, "Tuple") == 0) return "Tuple";
        cw_value* args = cw_object_get(t, "args");
        if (args && cw_typeof(args) == CW_ARRAY && cw_array_size(args) > 0) {
            const char* n = cg_type_name_of(g, cw_array_get(args, 0));
            if (n) return n;
        }
    }
    return NULL;
}

/* entry() 在 for-in 里是 Map 的“条目迭代标记”:
 * 类型上写成 Tuple<K, V>, 运行时不新建容器, 直接降级成 Map 迭代。 */
static bool cg_is_map_entry_marker(
    const cw_value*iterable
) {
    if (!iterable || strcmp(cg_node_kind(iterable), "Call") != 0) {
        return false;
    }
    cw_value* ann = cw_object_get(iterable, "ann");
    cw_value* call = ann ? cw_object_get(ann, "call") : NULL;
    cw_value* ck = call ? cw_object_get(call, "callee_kind") : NULL;
    cw_value* ref = call ? cw_object_get(call, "callee_ref") : NULL;
    return ck && cw_typeof(ck) == CW_STRING
        && strcmp(cw_string_cstr(ck), "builtin") == 0
        && ref && cw_typeof(ref) == CW_STRING
        && strcmp(cw_string_cstr(ref), "entry") == 0;
}

static void cg_block(
    CwCodegen_t* g,
    const cw_value*block
) {
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

static void cg_stmt_let(
    CwCodegen_t* g,
    const cw_value*node
) {
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

/* 复合赋值算术: 按目标类型浮点性/符号性选择指令; 不支持时按调用方文案
 * 报错并返回 NULL。allow_bits 控制整数移位/位运算 (字段复合赋值不支持)。 */
static LLVMValueRef cg_compound_arith(
    CwCodegen_t* g, const char* op,
    LLVMValueRef a, LLVMValueRef b,
    const char* tn, bool allow_bits,
    const char* float_err, const char* uns_err
) {
    if (strcmp(tn, "Float") == 0 || strcmp(tn, "Float64") == 0) {
        if (strcmp(op, "+=") == 0)
            return LLVMBuildFAdd(cg_b(g), a, b, "acc");
        if (strcmp(op, "-=") == 0)
            return LLVMBuildFSub(cg_b(g), a, b, "acc");
        if (strcmp(op, "*=") == 0)
            return LLVMBuildFMul(cg_b(g), a, b, "acc");
        if (strcmp(op, "/=") == 0)
            return LLVMBuildFDiv(cg_b(g), a, b, "acc");
        cg_error(g, float_err, op);
        return NULL;
    }
    const bool uns = cg_is_unsigned(tn);
    if (strcmp(op, "+=") == 0) return LLVMBuildAdd(cg_b(g), a, b, "acc");
    if (strcmp(op, "-=") == 0) return LLVMBuildSub(cg_b(g), a, b, "acc");
    if (strcmp(op, "*=") == 0) return LLVMBuildMul(cg_b(g), a, b, "acc");
    if (strcmp(op, "/=") == 0)
        return uns ? LLVMBuildUDiv(cg_b(g), a, b, "acc")
                   : LLVMBuildSDiv(cg_b(g), a, b, "acc");
    if (strcmp(op, "%=") == 0)
        return uns ? LLVMBuildURem(cg_b(g), a, b, "acc")
                   : LLVMBuildSRem(cg_b(g), a, b, "acc");
    if (allow_bits && strcmp(op, "<<=") == 0)
        return LLVMBuildShl(cg_b(g), a, b, "acc");
    if (allow_bits && strcmp(op, ">>=") == 0)
        return uns ? LLVMBuildLShr(cg_b(g), a, b, "acc")
                   : LLVMBuildAShr(cg_b(g), a, b, "acc");
    if (allow_bits && strcmp(op, "&=") == 0)
        return LLVMBuildAnd(cg_b(g), a, b, "acc");
    if (allow_bits && strcmp(op, "|=") == 0)
        return LLVMBuildOr(cg_b(g), a, b, "acc");
    if (allow_bits && strcmp(op, "^=") == 0)
        return LLVMBuildXor(cg_b(g), a, b, "acc");
    cg_error(g, uns_err, op);
    return NULL;
}

/* 下标赋值: Vector[i] = v / Map[k] = v */
static void cg_assign_index(
    CwCodegen_t* g, const cw_value*node,
    const cw_value*target
) {
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
        LLVMValueRef er = cg_vector_value_record(
            g, val, cg_node_ann_type(cw_object_get(node, "value")));
        if (g->failed) return;
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
        return;
    }
    if (strcmp(ot, "Map") == 0) {
        CwExpr k = cg_expr(g, cw_object_get(target, "index"));
        if (g->failed) return;
        LLVMValueRef kr = cg_container_value_record(g, k);
        if (g->failed) return;
        LLVMValueRef kr8 = LLVMBuildBitCast(cg_b(g), kr, cg_rt_i8_ptr(g),
                                            "");
        CwExpr val = cg_expr(g, cw_object_get(node, "value"));
        if (g->failed) return;
        LLVMValueRef vr = cg_vector_value_record(
            g, val, cg_node_ann_type(cw_object_get(node, "value")));
        if (g->failed) return;
        LLVMValueRef vr8 = LLVMBuildBitCast(cg_b(g), vr, cg_rt_i8_ptr(g),
                                            "");
        LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                              cg_rt_i8_ptr(g) };
        LLVMValueRef f = cg_rt_declare(g, "cwmap_put",
                                       LLVMInt1TypeInContext(cg_ctx(g)),
                                       pt, 3);
        LLVMValueRef av[3] = { rec8, kr8, vr8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
        return;
    }
    cg_error(g, "only Vector/Map index assignment is supported (got %s)", ot);
}

/* 字段赋值 (Attribute 目标): obj.field = v 或复合赋值 */
static void cg_assign_field(
    CwCodegen_t* g, const cw_value*node,
    const cw_value*target, const char* op
) {
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
    if (strcmp(op, "=") == 0) {
        cg_store_struct_field(g, base, L, fi, val);
        return;
    }
    /* 复合字段赋值: 读字段 -> 运算 -> 写回 */
    const char* ftype = cwtype_name(g->ll->types, L->fields[fi].type);
    LLVMValueRef fslot = cg_struct_slot(g, base, off);
    LLVMValueRef fh = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                     fslot, "fh");
    CwExpr cur = { fh, ftype };
    if (strcmp(op, "+=") == 0 && ftype
        && strcmp(ftype, "String") == 0) {
        CwExpr res = cg_builtin_concat(g, cur, val);
        if (g->failed) return;
        cg_store_struct_field(g, base, L, fi, res);
        return;
    }
    if (!ftype || !cg_is_scalar(ftype)) {
        cg_error(g, "compound field assignment supports scalars only: "
                 "%s =%s", fname, op);
        return;
    }
    size_t fsz = 0;
    LLVMTypeRef fvt = cg_scalar_type(g, ftype, &fsz);
    LLVMValueRef fa = cg_load_value(g, cur, fvt);
    LLVMValueRef fb = cg_load_value(
        g, val, cg_scalar_type(g, val.type_name, NULL));
    fb = cg_convert_scalar(g, fb, val.type_name, ftype);
    if (g->failed) return;
    LLVMValueRef fres = cg_compound_arith(
        g, op, fa, fb, ftype, false,
        "float compound field assignment is not supported: %s",
        "unsupported compound field assignment: %s");
    if (!fres) return;
    CwExpr fnv = cg_make_scalar(g, fres, fvt, ftype, fsz);
    cg_store_struct_field(g, base, L, fi, fnv);
}

/* 静态字段赋值 (Owner::field 两段 Name 目标)。
 * 模式不适用时返回 false, 由调用方落到普通变量赋值路径。 */
static bool cg_assign_static_field(
    CwCodegen_t* g, const cw_value*node,
    const cw_value*target, const char* op
) {
    cw_value* parts = cw_object_get(target, "parts");
    if (!parts || cw_typeof(parts) != CW_ARRAY
        || cw_array_size(parts) != 2) {
        return false;
    }
    cw_value* p0 = cw_array_get(parts, 0);
    cw_value* p1 = cw_array_get(parts, 1);
    const char* owner = (p0 && cw_typeof(p0) == CW_STRING)
        ? cw_string_cstr(p0) : NULL;
    const char* member = (p1 && cw_typeof(p1) == CW_STRING)
        ? cw_string_cstr(p1) : NULL;
    cw_value* ann = cw_object_get(target, "ann");
    cw_value* binding = ann ? cw_object_get(ann, "binding") : NULL;
    const char* bk = binding ? cg_json_kind(binding) : NULL;
    if (!owner || !member || !bk || strcmp(bk, "field") != 0) {
        return false;
    }
    const char* real_owner = cg_static_owner(g, owner);
    cw_value* type_obj = NULL;
    if (!cg_static_field(g, real_owner, member, &type_obj)) {
        cg_error(g, "static field not found: %s::%s",
                 real_owner ? real_owner : owner, member);
        return true;
    }
    const char* t = cg_node_type_name(g, target);
    if (!t) t = type_obj ? cg_type_name_of(g, type_obj) : NULL;
    if (!t) {
        cg_error(g, "static field is missing a type: %s::%s",
                 real_owner ? real_owner : owner, member);
        return true;
    }
    CwExpr val = cg_expr(g, cw_object_get(node, "value"));
    if (g->failed) return true;
    if (strcmp(op, "=") == 0) {
        if (!cg_static_store(g, real_owner, member, val, t, type_obj)) {
            cg_error(g, "cannot assign static field: %s::%s",
                     real_owner ? real_owner : owner, member);
        }
        return true;
    }
    if (!cg_is_scalar(t)) {
        cg_error(g,
            "compound assignment to a static field supports scalars only: %s::%s =%s",
            real_owner ? real_owner : owner, member, op);
        return true;
    }
    LLVMValueRef gv = cg_static_storage(g, real_owner, member, t,
                                        type_obj);
    if (!gv) {
        cg_error(g, "cannot locate static field: %s::%s",
                 real_owner ? real_owner : owner, member);
        return true;
    }
    LLVMTypeRef vt = cg_scalar_type(g, t, NULL);
    LLVMValueRef cur = LLVMBuildLoad2(cg_b(g), vt, gv, "st.cur");
    val = cg_coerce_scalar(g, val, t);
    if (g->failed) return true;
    LLVMValueRef rhs = cg_load_value(g, val, vt);
    LLVMValueRef res = cg_compound_arith(
        g, op, cur, rhs, t, true,
        "float compound assignment to a static field is not supported: %s",
        "unsupported compound assignment to a static field: %s");
    if (!res) return true;
    LLVMBuildStore(cg_b(g), res, gv);
    return true;
}

/* 变量赋值 (单段 Name 目标): = / String += / 标量复合赋值 */
static void cg_assign_var(
    CwCodegen_t* g, const cw_value*node,
    const cw_value*target, const char* op
) {
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
    LLVMTypeRef vt = cg_scalar_type(g, v->type_name, NULL);
    LLVMValueRef cur = LLVMBuildLoad2(cg_b(g), vt, v->storage, "cur");
    LLVMValueRef rhs_src = cg_load_value(
        g, e, cg_scalar_type(g, e.type_name, NULL));
    LLVMValueRef rhs = cg_convert_scalar(g, rhs_src, e.type_name,
                                         v->type_name);
    if (g->failed) return;
    LLVMValueRef res = cg_compound_arith(
        g, op, cur, rhs, v->type_name, true,
        "float compound assignment is not supported: %s",
        "unsupported compound assignment: %s");
    if (!res) return;
    cg_rec_store_value(g, v, res, v->type_name);
}

/* 解引用赋值 (*p = v): 指针是 i64 地址 (标量), 把值直接 store 到该地址 */
static void cg_assign_deref(
    CwCodegen_t* g, const cw_value*node,
    const cw_value*target, const char* op
) {
    cw_value* ptr_node = cw_object_get(target, "operand");
    cw_value* topv = cw_object_get(target, "op");
    const char* deref_op = (topv && cw_typeof(topv) == CW_STRING)
        ? cw_string_cstr(topv) : "";
    if (!ptr_node || strcmp(deref_op, "*") != 0) {
        cg_error(g, "unsupported assignment target");
        return;
    }
    CwExpr ptr = cg_expr(g, ptr_node);
    if (g->failed) return;
    const char* pointee = NULL;
    cw_value* pann = cw_object_get(ptr_node, "ann");
    cw_value* ptype = pann ? cw_object_get(pann, "type") : NULL;
    if (ptype) pointee = cg_type_name_of(g, ptype);
    if (pointee && (strncmp(pointee, "*const ", 7) == 0
                    || strncmp(pointee, "*mut ", 5) == 0)) {
        pointee += (strchr(pointee, ' ') - pointee) + 1;
    }
    if (!pointee) pointee = ptr.type_name;
    if (!cg_is_rawptr(ptr.type_name)) {
        cg_error(g,
            "cannot assign through non-scalar pointer type: %s",
            ptr.type_name ? ptr.type_name : "?");
        return;
    }
    size_t size = 0;
    LLVMTypeRef vt = cg_scalar_type(g, pointee, &size);
    if (!vt) {
        cg_error(g, "unsupported pointer pointee type: %s", pointee);
        return;
    }
    CwExpr val = cg_expr(g, cw_object_get(node, "value"));
    if (g->failed) return;

    LLVMValueRef addr = cg_handle_addr(g, ptr);
    LLVMValueRef dst = LLVMBuildIntToPtr(
        cg_b(g), addr, LLVMPointerType(vt, 0), "deref.dst");

    if (strcmp(op, "=") == 0) {
        size_t src_size = 0;
        LLVMTypeRef src_vt = cg_scalar_type(g, val.type_name, &src_size);
        LLVMValueRef raw = src_vt ? cg_load_value(g, val, src_vt) : NULL;
    if (raw && strcmp(val.type_name, pointee) != 0) {
            raw = cg_convert_scalar(
                g, raw, val.type_name, pointee);
        }
        if (g->failed || !raw) return;
        LLVMBuildStore(cg_b(g), raw, dst);
        return;
    }

    /* 复合解引用赋值: 读 -> 运算 -> 写回 */
    LLVMValueRef cur = LLVMBuildLoad2(cg_b(g), vt, dst, "deref.cur");
    LLVMValueRef rhs_src = cg_load_value(
        g, val, cg_scalar_type(g, val.type_name, NULL));
    LLVMValueRef rhs = cg_convert_scalar(
        g, rhs_src, val.type_name, pointee);
    if (g->failed) return;
    LLVMValueRef res = cg_compound_arith(
        g, op, cur, rhs, pointee, true,
        "float compound pointer write is not supported: %s",
        "unsupported compound pointer write: %s");
    if (!res) return;
    LLVMBuildStore(cg_b(g), res, dst);
}

static void cg_stmt_assign(
    CwCodegen_t* g,
    const cw_value*node
) {
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
        cg_assign_index(g, node, target);
        return;
    }
    if (target && cw_typeof(target) == CW_OBJECT
        && strcmp(cg_node_kind(target), "Attribute") == 0) {
        cg_assign_field(g, node, target, op);
        return;
    }
    if (target && cw_typeof(target) == CW_OBJECT
        && strcmp(cg_node_kind(target), "UnaryOp") == 0) {
        cg_assign_deref(g, node, target, op);
        return;
    }
    if (target && cw_typeof(target) == CW_OBJECT
        && strcmp(cg_node_kind(target), "Name") == 0
        && cg_binding_is(cw_object_get(target, "ann"), "extern_static")) {
        /* todo-56: extern 静态变量赋值 (= / 标量复合赋值, 须 static mut) */
        if (cg_assign_extern_static(g, node, target, op)) return;
    }
    if (target && cw_typeof(target) == CW_OBJECT
        && strcmp(cg_node_kind(target), "Name") == 0
        && cg_assign_static_field(g, node, target, op)) {
        return;
    }
    cg_assign_var(g, node, target, op);
}

static void cg_stmt_return(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* value = cw_object_get(node, "value");
    if (!value || cw_typeof(value) == CW_NULL) {
        LLVMBuildRet(cg_b(g), cg_null_handle(g));
        return;
    }
    CwExpr e = cg_expr(g, value);
    if (g->failed) return;
    if (g->ret_global && cg_is_fnptr(e.type_name)) {
        /* 函数指针值 (8 字节地址) 拷进全局缓冲, 避免返回指向
         * callee 栈帧临时槽的悬垂句柄 */
        LLVMValueRef fp = cg_load_value(
            g, e, LLVMInt64TypeInContext(cg_ctx(g)));
        LLVMBuildStore(cg_b(g), fp, g->ret_global);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), g->ret_global, LLVMInt64TypeInContext(cg_ctx(g)),
            "ret.addr");
        LLVMValueRef h = cg_build_handle(g, cg_i64(g, 0), addr,
                                         cg_i64(g, 8), cg_i64(g, 0));
        LLVMBuildRet(cg_b(g), h);
        return;
    }
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

static void cg_stmt_if(
    CwCodegen_t* g,
    const cw_value*node
) {
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
                : LLVMAppendBasicBlockInContext(cg_ctx(g), g->current_fn,
                                                "elif.end");
            LLVMBuildCondBr(cg_b(g), cg_bool_cond(g, ec), ethen, enext);
            LLVMPositionBuilderAtEnd(cg_b(g), ethen);
            cg_block(g, cw_object_get(elif, "body"));
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

/* 压入循环栈 (break/continue 的跳转目标); 扩容失败报错返回 false */
static bool cg_loop_push(
    CwCodegen_t* g, LLVMBasicBlockRef break_bb,
    LLVMBasicBlockRef continue_bb
) {
    CwLoop_t* nl = (CwLoop_t*)realloc(
        g->loops, (g->loop_count + 1) * sizeof(CwLoop_t));
    if (!nl) {
        cg_error(g, "failed to grow the loop stack");
        return false;
    }
    g->loops = nl;
    g->loops[g->loop_count++] = (CwLoop_t){ break_bb, continue_bb };
    return true;
}

static void cg_stmt_while(
    CwCodegen_t* g,
    const cw_value*node
) {
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
    if (!cg_loop_push(g, end_bb, cond_bb)) return;
    cg_block(g, cw_object_get(node, "body"));
    g->loop_count--;
    if (!g->failed && !cg_block_terminated(g)) {
        LLVMBuildBr(cg_b(g), cond_bb);
    }

    LLVMPositionBuilderAtEnd(cg_b(g), end_bb);
}

static void cg_stmt_for(
    CwCodegen_t* g,
    const cw_value*node
) {
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
    const bool is_entry_marker = it_type
        && strcmp(it_type, "Tuple") == 0
        && cg_is_map_entry_marker(iterable);
    const bool is_map = (it_type && strcmp(it_type, "Map") == 0)
        || is_entry_marker;
    if (!it_type || (strcmp(it_type, "Vector") != 0 && !is_set && !is_map)) {
        cg_error_at(g, node, "only Vector/Set/Map iteration is supported (got %s)",
                    it_type ? it_type : "?");
        return;
    }

    cw_value* iter_recv = iterable;
    if (is_entry_marker) {
        cw_value* attr = cw_object_get(iterable, "callee");
        cw_value* objv = attr ? cw_object_get(attr, "obj") : NULL;
        if (!objv) {
            cg_error_at(g, node, "Map.entry() is missing its receiver");
            return;
        }
        iter_recv = objv;
    }
    LLVMValueRef rec = cg_expr_record(g, iter_recv);
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
    const char* pf = "cwvec";
    if (is_set) pf = "cwset";
    else if (is_map) pf = "cwmap";
    const char* item_kind = "value";
    if (is_set) item_kind = "item";
    else if (is_map) item_kind = "key";
    char bname[64];
    char vname[64];
    char iname[64];
    char nname[64];
    snprintf(bname, sizeof(bname), "%s_iter_begin", pf);
    snprintf(vname, sizeof(vname), "%s_iter_valid", pf);
    snprintf(iname, sizeof(iname), "%s_iter_%s", pf, item_kind);
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
    if (!cg_loop_push(g, end_bb, next_bb)) return;
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
    const cw_value* type_obj;
    LLVMValueRef handle;
} CwPatBind_t;

static void cg_pattern_prepare(
    CwCodegen_t* g, const cw_value*pat, CwExpr subj,
    LLVMBasicBlockRef fail_bb,
    CwPatBind_t** binds, size_t* nb
);

/* 字面量模式比较: 返回 i1, 相等则模式继续 */
static LLVMValueRef cg_pattern_cmp_literal(
    CwCodegen_t* g, CwExpr subj,
    const cw_value*lit
) {
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
static CwExpr cg_pattern_tuple_elem(
    CwCodegen_t* g, CwExpr tup, int64_t idx,
    const char* elem_type
) {
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
static CwExpr cg_pattern_struct_field(
    CwCodegen_t* g, CwExpr obj,
    const CwLayout_t* L,
    const char* fname,
    const char* ftype
) {
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

static void cg_pattern_bind_add(
    CwCodegen_t* g, CwPatBind_t** binds,
    size_t* nb, const char* name,
    const char* type_name, const cw_value* type_obj,
    LLVMValueRef handle
) {
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
/* 绑定模式: 只登记, 等整个模式成功后统一创建变量 */
static void cg_pat_bind(
    CwCodegen_t* g, const cw_value*pat, CwExpr subj,
    CwPatBind_t** binds, size_t* nb
) {
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
}

/* 字面量模式: 比较通过则继续, 失败跳 fail_bb */
static void cg_pat_lit(
    CwCodegen_t* g, const cw_value*pat, CwExpr subj,
    LLVMBasicBlockRef fail_bb
) {
    cw_value* lit = cw_object_get(pat, "value");
    LLVMValueRef c = cg_pattern_cmp_literal(g, subj, lit);
    if (g->failed) return;
    LLVMBasicBlockRef cont = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "pat.cont");
    LLVMBuildCondBr(cg_b(g), c, cont, fail_bb);
    LLVMPositionBuilderAtEnd(cg_b(g), cont);
}

/* 元组模式: 按位置取元素并递归解构 */
static void cg_pat_tuple(
    CwCodegen_t* g, const cw_value*pat, CwExpr subj,
    LLVMBasicBlockRef fail_bb,
    CwPatBind_t** binds, size_t* nb
) {
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
}

/* 结构体模式: 按字段名解构; 无子模式时直接绑定整个字段值 */
static void cg_pat_struct(
    CwCodegen_t* g, const cw_value*pat, CwExpr subj,
    LLVMBasicBlockRef fail_bb,
    CwPatBind_t** binds, size_t* nb
) {
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
}

/* 枚举模式: tag 相等测试通过后逐个解构载荷槽 */
static void cg_pat_enum(
    CwCodegen_t* g, const cw_value*pat, CwExpr subj,
    LLVMBasicBlockRef fail_bb,
    CwPatBind_t** binds, size_t* nb
) {
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
}

/* 在当前块内逐项测试 pattern:
 *  - 字面量不匹配 → 无条件跳 fail_bb;
 *  - 元组/结构体递归解构;
 *  - 绑定只登记到 binds, 等整个模式成功后再统一创建变量。
 */
static void cg_pattern_prepare(
    CwCodegen_t* g, const cw_value*pat, CwExpr subj,
    LLVMBasicBlockRef fail_bb,
    CwPatBind_t** binds, size_t* nb
) {
    const char* kind = cg_node_kind(pat);
    if (!kind) {
        cg_error(g, "pattern is missing kind");
        return;
    }
    if (strcmp(kind, "WildcardPattern") == 0) return;
    if (strcmp(kind, "BindPattern") == 0) {
        cg_pat_bind(g, pat, subj, binds, nb);
        return;
    }
    if (strcmp(kind, "LitPattern") == 0) {
        cg_pat_lit(g, pat, subj, fail_bb);
        return;
    }
    if (strcmp(kind, "TuplePattern") == 0) {
        cg_pat_tuple(g, pat, subj, fail_bb, binds, nb);
        return;
    }
    if (strcmp(kind, "StructPattern") == 0) {
        cg_pat_struct(g, pat, subj, fail_bb, binds, nb);
        return;
    }
    if (strcmp(kind, "EnumPattern") == 0) {
        cg_pat_enum(g, pat, subj, fail_bb, binds, nb);
        return;
    }
    cg_error_at(g, pat, "unsupported pattern: %s", kind);
}


/* 模式成功: 按登记顺序创建变量并写值 (此时所在块 = 模式全部通过) */
static bool cg_pattern_bind_all(
    CwCodegen_t* g, CwPatBind_t* binds,
    size_t nb
) {
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
static CwExpr cg_expr_match(
    CwCodegen_t* g,
    const cw_value*node
) {
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
    cw_value* ann = cw_object_get(node, "ann");
    cw_value* at = ann ? cw_object_get(ann, "type") : NULL;
    const char* rtype = at ? cg_type_name_of(g, at) : NULL;
    if (!rtype) rtype = cg_node_type_name(g, body0);
    if (!rtype) {
        cg_error_at(g, node, "match expression is missing its result type");
        return (CwExpr){ NULL, NULL };
    }
    char vname[64];
    snprintf(vname, sizeof(vname), "$m.%zu", g->var_count);
    const char* stable = cg_own_name(g, vname);
    if (!cg_var_declare(g, stable, rtype, at ? at : cg_node_ann_type(body0))) {
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

static void cg_stmt_match(
    CwCodegen_t* g,
    const cw_value*node
) {
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

static void cg_stmt_if_let(
    CwCodegen_t* g,
    const cw_value*node
) {
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

static void cg_stmt_break(
    CwCodegen_t* g
) {
    if (g->loop_count == 0) {
        cg_error(g, "break outside a loop");
        return;
    }
    LLVMBuildBr(cg_b(g), g->loops[g->loop_count - 1].break_bb);
}

static void cg_stmt_continue(
    CwCodegen_t* g
) {
    if (g->loop_count == 0) {
        cg_error(g, "continue outside a loop");
        return;
    }
    LLVMBuildBr(cg_b(g), g->loops[g->loop_count - 1].continue_bb);
}

static void cg_stmt(
    CwCodegen_t* g,
    const cw_value*node
) {
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

/* 标量/函数指针返回值的全局缓冲: 返回句柄指向它, 跨调用存活 */
static void cg_setup_scalar_ret_global(
    CwCodegen_t* g, const char* ret_name, const char* key
) {
    size_t size = 0;
    LLVMTypeRef vt = cg_is_scalar(ret_name)
        ? cg_scalar_type(g, ret_name, &size)
        : LLVMInt64TypeInContext(cg_ctx(g));
    char gname[128];
    snprintf(gname, sizeof(gname), "fnret.%s", key);
    g->ret_global = LLVMAddGlobal(g->ll->module, vt, gname);
    LLVMSetInitializer(g->ret_global, LLVMConstNull(vt));
}

/* 结构体/枚举返回值的全局 blob 缓冲 (值拷贝语义); 失败报错返回 false */
static bool cg_setup_aggregate_ret_global(
    CwCodegen_t* g, const char* tn,
    const cw_value*rtv, const char* key
) {
    if (cg_is_struct_type(g, tn)) {
        const CwLayout_t* L = cg_struct_layout(g, rtv);
        if (!L) {
            cg_error(g, "unknown return type layout: %s", tn);
            return false;
        }
        g->ret_struct_fields = L->field_count;
        g->ret_struct_layout = L;
        g->ret_struct_size = cg_struct_blob_size(g, L);
    } else if (cg_is_enum_type(g, tn)) {
        g->ret_struct_size = cg_enum_blob_size(g, tn);
    } else {
        return true; /* 其它类型无需聚合缓冲 */
    }
    char gname[128];
    snprintf(gname, sizeof(gname), "fnret.%s", key);
    LLVMTypeRef arr = LLVMArrayType(
        LLVMInt8TypeInContext(cg_ctx(g)),
        (unsigned)g->ret_struct_size);
    g->ret_struct_global = LLVMAddGlobal(g->ll->module, arr, gname);
    LLVMSetInitializer(g->ret_struct_global, LLVMConstNull(arr));
    return true;
}

static void cg_emit_closure_body(
    CwCodegen_t* g,
    const CwClosure_t* c
) {
    /* 注意: 调用方在循环里发射闭包; 发射过程中产生的嵌套闭包会追加到
     * 队列尾部并可能触发 realloc, 因此这里不得持有跨调用的队列指针,
     * 也不能重置 closure_count (那会让外层循环错位)。 */
    const cw_value* node = c->decl;
    LLVMValueRef fn = LLVMGetNamedFunction(g->ll->module, c->symbol);
    if (!node || !fn) {
        cg_error(g, "closure function is not declared: %s", c->symbol);
        return;
    }
    g->current_fn = fn;
    g->current_owner = NULL;
    g->tparam_names = NULL;
    g->targs = NULL;
    g->tcount = 0;
    g->var_count = 0;
    g->scope_depth = 0;
    g->scope_mark_count = 0;
    cg_free_owned_names(g);
    g->loop_count = 0; /* 循环栈 push/pop 平衡, 只需清零计数 */
    g->current_ret_type = NULL;
    g->ret_global = NULL;
    g->ret_struct_global = NULL;
    g->ret_struct_size = 0;
    g->ret_struct_fields = 0;
    g->ret_struct_layout = NULL;
    cw_value* rtv = cw_object_get(node, "return_type");
    const char* ret_name = NULL;
    if (rtv && cw_typeof(rtv) == CW_OBJECT) {
        ret_name = cg_type_name_of(g, rtv);
    } else {
        /* 推断返回类型的闭包没有 return_type 节点:
         * 从 SA 的 fn_return 标注恢复, 保证标量结果同样走全局缓冲,
         * 否则 -O3 会把"写 callee 局部槽 + 返回句柄"优化成空壳 */
        cw_value* ann = cw_object_get(node, "ann");
        cw_value* fr = ann ? cw_object_get(ann, "fn_return") : NULL;
        if (fr && cw_typeof(fr) == CW_OBJECT) {
            ret_name = cg_json_name(fr);
        }
    }
    if (ret_name) {
        g->current_ret_type = ret_name;
        if (cg_is_scalar(ret_name) || cg_is_fnptr(ret_name)) {
            cg_setup_scalar_ret_global(g, ret_name, c->symbol);
        }
    }
    LLVMBasicBlockRef entry = LLVMAppendBasicBlockInContext(
        cg_ctx(g), fn, "entry");
    LLVMPositionBuilderAtEnd(cg_b(g), entry);
    cw_value* params = cw_object_get(node, "params");
    const unsigned nparams = LLVMCountParams(fn);
    for (unsigned i = 0; i < nparams; i++) {
        LLVMValueRef arg = LLVMGetParam(fn, i);
        cw_value* p = (params && cw_typeof(params) == CW_ARRAY)
            ? cw_array_get(params, i) : NULL;
        const char* pname = p ? cg_json_name(p) : NULL;
        cw_value* ptype = p ? cw_object_get(p, "type") : NULL;
        const bool ptype_ok = ptype && cw_typeof(ptype) == CW_OBJECT;
        const char* tname = ptype_ok ? cg_type_name_of(g, ptype)
                                     : cg_node_type_name(g, p);
        if (!pname || !tname || !cg_var_declare(g, pname, tname,
                                                ptype_ok ? ptype
                                                    : cg_node_ann_type(p))) {
            return;
        }
        CwVar_t* v = cg_var_find(g, pname);
        if (!v) return;
        LLVMSetValueName2(arg, pname, (unsigned)strlen(pname));
        CwExpr a = { arg, tname };
        if (!cg_rec_store(g, v, a)) return;
    }
    cg_block(g, cw_object_get(node, "body"));
    if (g->failed) return;
    if (!cg_block_terminated(g)) {
        LLVMBuildRet(cg_b(g), cg_null_handle(g));
    }
}

/* 把 LLVM 形参绑定到变量表:
 * self/&T 按引用传递 (句柄直指调用者 blob, 字段修改可传回),
 * 其余参数做值拷贝。失败返回 false。 */
static bool cg_bind_params(
    CwCodegen_t* g,
    const CwSymEntry_t* e
) {
    LLVMValueRef fn = g->current_fn;
    const unsigned nparams = LLVMCountParams(fn);
    for (unsigned i = 0; i < nparams; i++) {
        LLVMValueRef arg = LLVMGetParam(fn, i);
        cw_value* p = cwmodule_fn_param(e->decl, i);
        const char* pname = p ? cg_json_name(p) : NULL;
        if (!pname) {
            cg_error(g, "function %s parameter %u is missing a name", e->mangled, i);
            return false;
        }
        cw_value* ptype = p ? cw_object_get(p, "type") : NULL;
        const bool ptype_ok = ptype && cw_typeof(ptype) == CW_OBJECT;
        const char* tname = ptype_ok ? cg_type_name_of(g, ptype) : NULL;
        if (!tname) tname = cg_node_type_name(g, p);
        if (!tname) tname = "Any";
        cw_value* ptype_obj = ptype_ok ? ptype : cg_node_ann_type(p);
        if (!cg_var_declare(g, pname, tname, ptype_obj)) return false;
        CwVar_t* v = cg_var_find(g, pname);
        if (!v) return false;
        LLVMSetValueName2(arg, pname, (unsigned)strlen(pname));
        CwExpr a = { arg, tname };
        const bool self_ref = i == 0 && e->owner != NULL
            && (e->kind == CW_SYM_METHOD || e->kind == CW_SYM_INSTANCE)
            && pname && strcmp(pname, "self") == 0
            && cg_type_is_ref(ptype_obj);
        const bool param_ref = self_ref || cg_type_is_ref(ptype_obj);
        if (!param_ref) {
            if (!cg_rec_store(g, v, a)) return false;
            continue;
        }
        /* self / &T 按引用传递: 句柄直接指向调用者的实例 blob,
         * 字段修改才能传回调用者, 不做值拷贝 */
        LLVMValueRef tid = LLVMBuildStructGEP2(
            cg_b(g), g->ll->rec_type, v->record, 0, "tid");
        LLVMBuildStore(cg_b(g),
                       cg_i32(g, (uint32_t)cg_type_id(tname)), tid);
        LLVMValueRef gcnt = LLVMBuildStructGEP2(
            cg_b(g), g->ll->rec_type, v->record, 1, "gc");
        LLVMBuildStore(cg_b(g), cg_i8(g, 0), gcnt);
        LLVMValueRef hptr = LLVMBuildStructGEP2(
            cg_b(g), g->ll->rec_type, v->record, 3, "h");
        LLVMBuildStore(cg_b(g), arg, hptr);
    }
    return true;
}

static void cg_emit_function(
    CwCodegen_t* g,
    const CwSymEntry_t* e
) {
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
    char** tparam_alloc = NULL;
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
            tparam_alloc = (char**)malloc(ntp * sizeof(char*));
            char** names = tparam_alloc;
            if (!names) {
                cg_error(g, "failed to allocate generic parameter names");
                return;
            }
            size_t k = 0;
            for (size_t pass = 0; pass < 2; pass++) {
                cw_value* plist = (pass == 0) ? otp : ftp;
                const size_t np = (pass == 0) ? n_owner : n_fn;
                for (size_t i = 0; i < np; i++) {
                    names[k++] = (char*)cg_json_name(
                        cw_array_get(plist, i));
                }
            }
            g->tparam_names = (const char**)names;
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
        if (g->current_ret_type
            && (cg_is_scalar(g->current_ret_type)
                || cg_is_fnptr(g->current_ret_type))) {
            cg_setup_scalar_ret_global(g, g->current_ret_type, e->mangled);
        } else if (g->current_ret_type) {
            if (!cg_setup_aggregate_ret_global(g, g->current_ret_type,
                                               rtv, e->mangled)) {
                return;
            }
        }
    }
    LLVMBasicBlockRef entry = LLVMAppendBasicBlockInContext(cg_ctx(g), fn,
                                                            "entry");
    LLVMPositionBuilderAtEnd(cg_b(g), entry);

    if (!cg_bind_params(g, e)) return;

    cg_block(g, body);
    if (g->failed) return;
    if (!cg_block_terminated(g)) {
        LLVMBuildRet(cg_b(g), cg_null_handle(g));
    }
    free(tparam_alloc);
    g->tparam_names = NULL;
    g->targs = NULL;
    g->tcount = 0;
}

/* 调用所有静态字段的初始化函数 (main 入口处, 先于用户 main) */
static void cg_emit_const_inits(
    CwCodegen_t* g
) {
    const size_t nsym = cwmodule_symbol_count(g->m);
    for (size_t i = 0; i < nsym && !g->failed; i++) {
        const CwSymbol_t* s = cwmodule_symbol(g->m, i);
        if (!s || strcmp(s->kind, "const") != 0) continue;
        const CwNode_t* decl = cwmodule_node(g->m, s->ref);
        if (!decl) continue;
        cw_value* type_obj = cw_object_get(decl->value, "type");
        cw_value* ann = cw_object_get(decl->value, "ann");
        cw_value* at = ann ? cw_object_get(ann, "type") : NULL;
        if (at && cw_typeof(at) == CW_OBJECT) type_obj = at;
        const char* tname = type_obj
            ? cg_type_name_of(g, type_obj) : NULL;
        if (!tname) {
            cg_error(g, "const '%s' is missing a type", s->name);
            return;
        }
        LLVMValueRef fn = cg_const_init_fn(
            g, s->name, decl->value, tname, type_obj);
        if (!fn || g->failed) return;
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn),
                       fn, NULL, 0, "");
    }
}

static void cg_emit_static_inits(
    CwCodegen_t* g
) {
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

static void cg_emit_main_wrapper(
    CwCodegen_t* g
) {
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

    cg_emit_const_inits(g);
    if (g->failed) return;

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

bool cwcodegen_init(
    CwCodegen_t* g,
    CwLlvm_t* ll,
    const CwModule_t* m
) {
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

void cwcodegen_destroy(
    CwCodegen_t* g
) {
    if (!g) return;
    if (g->builder) LLVMDisposeBuilder(g->builder);
    if (g->alloca_builder) LLVMDisposeBuilder(g->alloca_builder);
    free(g->loops);
    free(g->vars);
    free(g->scope_marks);
    cg_free_owned_names(g);
    free(g->owned_names);
    free(g->closures);
    memset(g, 0, sizeof(*g));
}

bool cwcodegen_emit(
    CwCodegen_t* g
) {
    if (!g || g->failed) return false;
    cg_closure_reset(g);
    /* 主体生成过程中可能新增泛型实例 / 嵌套闭包, 逐轮补齐直到收敛:
     * 函数体注册闭包与实例 -> 闭包体又可能调用泛型函数注册新实例。 */
    size_t sym_i = 0;
    size_t clo_i = 0;
    while (!g->failed) {
        const size_t nsyms = g->ll->syms->count;
        for (; sym_i < nsyms && !g->failed; sym_i++) {
            cg_emit_function(g, &g->ll->syms->items[sym_i]);
        }
        const size_t nclo = g->closure_count;
        for (; clo_i < nclo && !g->failed; clo_i++) {
            /* 拷贝条目: 发射中嵌套闭包可能触发 realloc 使原位失效 */
            CwClosure_t c = g->closures[clo_i];
            cg_emit_closure_body(g, &c);
        }
        if (sym_i >= g->ll->syms->count && clo_i >= g->closure_count) break;
    }
    if (!g->failed) cg_emit_main_wrapper(g);
    return !g->failed;
}

const char* cwcodegen_error(
    const CwCodegen_t* g
) {
    return g ? g->error : "?";
}






