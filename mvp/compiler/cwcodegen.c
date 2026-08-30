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

size_t cwllvm_abisize(const CwLlvm_t* ll, LLVMTypeRef ty);

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
static int cg_ann_arg_tag(
    CwCodegen_t* g, const cw_value* node, size_t idx
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

/* ---- todo-88/89: extern 签名位置的完整类型名渲染 ----
 * cg_type_name_of 只返回基名 (Option<String> -> "Option"), 无法
 * 区分 Option 实例; extern 声明无泛型形参变量, 在签名位置按
 * Base<Arg1, Arg2> 递归渲染 (优先 ann.type 的实参列表)。 */

static bool cg_ext_full_type_name_r(
    CwCodegen_t* g, const cw_value* t, char* out, size_t cap
) {
    if (!t || !cap) return false;
    /* 基名优先取 SA 解析后的类型标注 (bug-29 别名展开同路径) */
    cw_value* resolved = NULL;
    const char* rawname = cg_json_name(t);
    if (rawname && strcmp(rawname, "Self") != 0) {
        resolved = cg_node_ann_type(t);
        if (!(resolved && cw_typeof(resolved) == CW_OBJECT
              && cg_json_name(resolved))) {
            resolved = NULL;
        }
    }
    const cw_value* name_src = resolved ? resolved : t;
    const char* n = cg_json_name(name_src);
    if (!n) return false;
    const int w = snprintf(out, cap, "%s", n);
    if (w < 0 || (size_t)w >= cap) return false;
    size_t off = (size_t)w;
    /* 扁平名字类型 (fn 签名 / 原始指针 / 定长数组) 的名字即完整
     * 类型串, args 数组只是辅助信息, 不参与渲染 */
    if (strncmp(n, "fn(", 3) == 0
        || strncmp(n, "*const ", 7) == 0
        || strncmp(n, "*mut ", 5) == 0
        || n[0] == '[') {
        return true;
    }
    /* 实参列表: 取带非空 args 的一侧 (resolved 缺实参时回退原节点) */
    cw_value* vargs = resolved
        ? cw_object_get(resolved, "args") : NULL;
    cw_value* rargs = cw_object_get((cw_value*)t, "args");
    cw_value* args =
        (vargs && cw_typeof(vargs) == CW_ARRAY
         && cw_array_size(vargs) > 0) ? vargs
        : ((rargs && cw_typeof(rargs) == CW_ARRAY) ? rargs : NULL);
    const size_t nargs = (args && cw_typeof(args) == CW_ARRAY)
        ? cw_array_size(args) : 0;
    if (nargs == 0) return true;
    if (off + 1 >= cap) return false;
    out[off++] = '<';
    for (size_t i = 0; i < nargs; i++) {
        if (i > 0) {
            if (off + 2 >= cap) return false;
            out[off++] = ',';
            out[off++] = ' ';
        }
        if (!cg_ext_full_type_name_r(
                g, cw_array_get(args, i), out + off, cap - off)) {
            return false;
        }
        off += strlen(out + off);
    }
    if (off + 1 >= cap) return false;
    out[off++] = '>';
    out[off] = '\0';
    return true;
}

static bool cg_ext_full_type_name(
    CwCodegen_t* g, const cw_value* t, char* out, size_t cap
) {
    return cg_ext_full_type_name_r(g, t, out, cap);
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

/* Vector 字面量元素类型 tag (ann.element_type); 未知/用户类型 -> 0 */
static int cg_elem_type_id(
    CwCodegen_t* g, const cw_value* node
) {
    cw_value* ann = node ? cw_object_get(node, "ann") : NULL;
    cw_value* et = ann ? cw_object_get(ann, "element_type") : NULL;
    const char* n = et ? cg_type_name_of(g, et) : NULL;
    const int tid = n ? cg_type_id(n) : -1;
    return tid >= 0 ? tid : 0;
}

static bool cg_is_scalar(
    const char* name
) {
    const int id = cg_type_id(name);
    return id == CWInt || id == CWUInt || id == CWInt8 || id == CWUInt8
        || id == CWInt16 || id == CWUInt16
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
        || id == CWInt16 || id == CWUInt16
        || id == CWInt32 || id == CWUInt32 || id == CWInt64
        || id == CWUInt64 || id == CWByte;
}

static bool cg_is_unsigned(
    const char* name
) {
    const int id = cg_type_id(name);
    return id == CWUInt || id == CWUInt8 || id == CWUInt16
        || id == CWUInt32 || id == CWUInt64 || id == CWByte;
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
    case CWInt16:
    case CWUInt16:
        if (size) *size = 2;
        return LLVMInt16TypeInContext(cg_ctx(g));
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

/* ABI v2: 值 = {address, length, cursor} 24B, 无类型头/自指元数据 */
static LLVMValueRef cg_build_value(
    CwCodegen_t* g,
    LLVMValueRef address,
    LLVMValueRef length,
    LLVMValueRef cursor
) {
    LLVMValueRef h = LLVMGetUndef(g->ll->handle_type);
    h = LLVMBuildInsertValue(cg_b(g), h, address, 0, "h.addr");
    h = LLVMBuildInsertValue(cg_b(g), h, length, 1, "h.len");
    h = LLVMBuildInsertValue(cg_b(g), h, cursor, 2, "h.cur");
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
        cg_build_value(g, addr, cg_i64(g, size), cg_i64(g, 0)),
        type_name,
    };
    return e;
}

static CwVar_t* cg_var_find(
    CwCodegen_t* g,
    const char* name
);
static bool cg_is_array_type(
    const char* tname
);
static size_t cg_array_total_bytes(
    const char* tname
);
static bool cg_array_info(
    const char* tname,
    char* elem, size_t elem_cap,
    size_t* out_n
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

/* 聚合传递约定分类 */
typedef enum {
    CG_AGG_NONE = 0, /* 无 C-ABI 映射 */
    CG_AGG_PACK,     /* <=8B 同宽全标量 -> 单整数寄存器 (按位镜像) */
    CG_AGG_REGS,     /* SysV 9~16B -> 一等结构体实参/返回 (寄存器对) */
    CG_AGG_MEM,      /* 内存约定 (Win64 byval / SysV MEMORY sret) */
} CgAggMode;

typedef struct {
    const CwLayout_t* L;
    size_t size;   /* C 视图字节数 */
    size_t align;
    bool nested;   /* 含内嵌结构体字段 */
    CgAggMode mode;
} CgAggInfo;

static bool cg_ext_agg_classify(
    CwCodegen_t* g, const char* tname, CgAggInfo* out
); /* todo-61/65/66 */
static LLVMValueRef cg_ext_agg_image_ptr(
    CwCodegen_t* g, CwExpr e, const CgAggInfo* info
); /* todo-61/66 */
static CwExpr cg_ext_unflatten(
    CwCodegen_t* g, LLVMValueRef src_i8, const char* tname
); /* todo-61/66 */
static size_t cg_ext_pod_region_start(
    CwCodegen_t* g, const CwLayout_t* L
); /* todo-61 */
static LLVMValueRef cg_extern_thunk(
    CwCodegen_t* g, const CwSymEntry_t* sym
); /* todo-54 */

/* ---- 带载荷枚举的 FFI 表示 (todo-89) ----
 * C 视图 = { i32 tag; <载荷字段...> } (字段自然对齐放置):
 *   - 载荷区起点 = align_up(4, 最大字段对齐);
 *   - 全体带载荷变体必须共享同一字段表 (SA 保证), C 侧镜像成
 *     `struct { int32_t tag; <fields>; }` 即可;
 *   - 传递一律走内存约定 (byval 形参 / sret 返回), 与聚合大小无关。 */
#define CG_EXT_ENUM_MAX_FIELDS 16

typedef struct {
    size_t nfields;                     /* 共享载荷字段数 */
    char ftypes[CG_EXT_ENUM_MAX_FIELDS][128];
    size_t foff[CG_EXT_ENUM_MAX_FIELDS]; /* 字段在 C 视图中的偏移 */
    size_t pay_off;                     /* 载荷区起点 (>=4) */
    size_t total;                       /* C 视图总字节数 (含尾补齐) */
    size_t nvariants;                   /* 变体总数 */
    bool pay_mask[64];                  /* 各变体是否带载荷 */
} CgEnumAbi;

static bool cg_ext_enum_abi(
    CwCodegen_t* g, const char* tname, CgEnumAbi* out
); /* todo-89 */
static LLVMTypeRef cg_ext_enum_llvm_type(
    CwCodegen_t* g, const CgEnumAbi* ai
); /* todo-89 */

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

/* ---- 值单元 (ABI v2) ----
 * 容器元素/rt 边界 = 24B CWValue; 异构入口 = 32B CWCell {tag, value}。
 * 类型 tag 由调用点静态提供 (元数据分区存放), 值本身不携带。 */

/* 读 rt 出参值 (出参均为 CWValue*, 直读 24B 值) */
static CwExpr cg_out_value_read(
    CwCodegen_t* g, LLVMValueRef vp,
    const char* type_name
) {
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, vp, "ov");
    return (CwExpr){ h, type_name };
}

/* 24B CWValue alloca (容器元素 cell / rt 出参值) */
static LLVMValueRef cg_cell_alloca(
    CwCodegen_t* g,
    const char* name
) {
    return cg_alloca(g, g->ll->handle_type, name);
}

/* 容器元素 cell: 标量拷进 arena 单元 (循环 push 值语义, 避免复用同一
 * entry alloca 旧元素随变量变化), 引用类型值直存, 用户结构体/枚举
 * blob 深拷进 arena 单元 (cg_enum_payload_handle)。返回 CWValue 指针。 */
static LLVMValueRef cg_value_cell(
    CwCodegen_t* g,
    CwExpr e,
    const cw_value* type_obj
) {
    LLVMValueRef cell = cg_cell_alloca(g, "elem.cell");
    LLVMValueRef handle = NULL;
    if (cg_is_scalar(e.type_name) || cg_is_fnptr(e.type_name)) {
        size_t size = 0;
        LLVMTypeRef vt = cg_scalar_type(g, e.type_name, &size);
        if (vt) {
            LLVMValueRef unit = cg_rt_arena_alloc(g, cg_i64(g, (uint64_t)size));
            LLVMValueRef p = LLVMBuildIntToPtr(cg_b(g), unit, cg_rt_i8_ptr(g),
                                               "elem.unit");
            LLVMValueRef v = cg_load_value(g, e, vt);
            LLVMBuildStore(cg_b(g), v, LLVMBuildBitCast(
                cg_b(g), p, LLVMPointerType(vt, 0), ""));
            LLVMValueRef addr = LLVMBuildPtrToInt(
                cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "elem.addr");
            handle = cg_build_value(g, addr,
                                    cg_i64(g, size), cg_i64(g, 0));
        }
    }
    if (!handle) {
        if (cg_type_id(e.type_name) < 0) {
            /* 用户结构体/枚举: blob 深拷进 arena 单元 (值语义) */
            handle = cg_enum_payload_handle(g, e, type_obj);
            if (g->failed) return NULL;
        } else {
            handle = e.handle; /* String/容器/None: 值直存 (数据已持久) */
        }
    }
    LLVMBuildStore(cg_b(g), handle, cell);
    return cell;
}

static LLVMValueRef cg_handle_addr(
    CwCodegen_t* g,
    CwExpr e
) {
    return LLVMBuildExtractValue(cg_b(g), e.handle, 0, "addr");
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

/* 定长数组动态索引的运行时边界检查 (todo-60):
 * ix >= n 时调用 C abort() 终止; 返回后 builder 位于继续块 */
static void cg_array_bounds_check(
    CwCodegen_t* g, LLVMValueRef ix, size_t n
) {
    LLVMValueRef ok = LLVMBuildICmp(cg_b(g), LLVMIntULT, ix,
                                    cg_i64(g, (uint64_t)n), "arr.chk");
    LLVMBasicBlockRef oob = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "arr.oob");
    LLVMBasicBlockRef cont = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "arr.ok");
    LLVMBuildCondBr(cg_b(g), ok, cont, oob);
    LLVMPositionBuilderAtEnd(cg_b(g), oob);
    LLVMValueRef ab = LLVMGetNamedFunction(g->ll->module, "abort");
    if (!ab) {
        ab = LLVMAddFunction(
            g->ll->module, "abort",
            LLVMFunctionType(LLVMVoidTypeInContext(cg_ctx(g)),
                             NULL, 0, false));
    }
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(ab), ab, NULL, 0, "");
    LLVMBuildUnreachable(cg_b(g));
    LLVMPositionBuilderAtEnd(cg_b(g), cont);
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
    v->is_array = false;
    v->is_value = false;
    v->is_ref_param = false;
    v->is_ref = false;
    v->blob = NULL;
    v->blob_size = 0;
    v->field_count = 0;
    v->layout = NULL;
    size_t size = 0;
    if (type_obj && cg_type_is_ref(type_obj)) {
        /* todo-145: &T/&mut T 引用绑定: 与原始指针同一存储模型 ——
         * slot 存被借用存储的地址 (值 = address 句柄) */
        v->slot = cg_alloca(g, LLVMInt64TypeInContext(cg_ctx(g)), name);
        v->is_ref = true;
        return true;
    }
    if (cg_is_scalar(type_name) || cg_is_fnptr(type_name)
        || cg_is_rawptr(type_name)) {
        /* 标量/函数指针/原始指针: 裸值存储 (ABI v2 无记录) */
        if (cg_is_fnptr(type_name) || cg_is_rawptr(type_name)) {
            v->slot = cg_alloca(g, LLVMInt64TypeInContext(cg_ctx(g)),
                                name);
        } else {
            v->slot = cg_alloca(g, cg_scalar_type(g, type_name, &size),
                                "v.storage");
        }
    } else if (cg_type_id(type_name) == CWString
               || cg_type_id(type_name) == CWVector
               || cg_type_id(type_name) == CWMap
               || cg_type_id(type_name) == CWSet
               || cg_type_id(type_name) == CWTuple
               || cg_type_id(type_name) == CWNone) {
        /* 值类型变量: CWValue alloca (24B 纯数据) */
        v->is_value = true;
        v->slot = cg_alloca(g, g->ll->handle_type, name);
        LLVMBuildStore(cg_b(g), cg_null_handle(g), v->slot);
    } else {
        v->slot = NULL;
    }
    if (type_name && cg_is_array_type(type_name)) {
        /* 定长数组 (todo-60): 纯载荷 blob, 值语义整块拷贝 */
        const size_t total = cg_array_total_bytes(type_name);
        if (total == 0) {
            cg_error(g, "unsupported array element type: %s", type_name);
            return false;
        }
        v->is_array = true;
        v->blob_size = total;
        v->blob = cg_blob_alloc(g, total, "arr.blob");
    }
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

/* 把表达式写进变量: 标量拷值, 值类型拷 CWValue, blob 类型整块 memcpy
 * (C-Like-Layout 无自指句柄, 拷贝即深拷贝) */
static bool cg_var_store(
    CwCodegen_t* g,
    CwVar_t* v,
    CwExpr e
) {
    if (e.type_name && strcmp(e.type_name, "!") == 0) {
        return true; /* never 值: 调用点发散, 存储永远不会执行 */
    }
    if (v->is_ref_param) {
        /* 引用形参直传: 存整句柄 (不可变更, v0 约定) */
        LLVMBuildStore(cg_b(g), e.handle, v->slot);
        return true;
    }
    if (v->is_ref) {
        /* todo-145: 引用绑定: 存被借用存储的地址 */
        LLVMBuildStore(cg_b(g), cg_handle_addr(g, e), v->slot);
        return true;
    }
    if (v->blob) {
        /* 用户结构体/枚举/定长数组: 整块拷贝 (枚举浅拷即深拷:
         * 载荷 cell 已指向 arena/引用型持久值) */
        LLVMValueRef src = cg_expr_blob_i8(g, e);
        LLVMValueRef dst = cg_blob_i8(g, v->blob);
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1,
                        cg_i64(g, (uint64_t)v->blob_size));
        return true;
    }
    if (v->is_value) {
        /* String / 容器 / None: 值直存 (CWValue 拷贝, 数据别名) */
        LLVMBuildStore(cg_b(g), e.handle, v->slot);
        return true;
    }
    if (v->slot) {
        size_t size = 0;
        if (cg_is_rawptr(v->type_name)) {
            /* 原始指针: 地址即值 (值 address 字段就是指针本身) */
            LLVMBuildStore(cg_b(g), cg_handle_addr(g, e), v->slot);
            return true;
        }
        LLVMTypeRef vt = cg_scalar_type(g, v->type_name, &size);
        if (!vt) {
            if (cg_is_fnptr(v->type_name)) {
                vt = LLVMInt64TypeInContext(cg_ctx(g));
                size = 8;
            } else {
                cg_error(g, "unknown scalar type: %s", v->type_name);
                return false;
            }
        }
        if (cg_is_fnptr(v->type_name)) {
            /* 函数指针: 值 = {address -> 存 fn 地址的存储}, 拷地址值 */
            LLVMValueRef fp = cg_load_value(g, e, vt);
            LLVMBuildStore(cg_b(g), fp, v->slot);
            return true;
        }
        /* 先按源类型取值, 再转换到目标类型 (字面量现在是宽类型) */
        size_t esize = 0;
        LLVMTypeRef evt = cg_scalar_type(g, e.type_name, &esize);
        LLVMValueRef src = evt ? cg_load_value(g, e, evt) : cg_handle_addr(g, e);
        LLVMValueRef val = cg_convert_scalar(g, src, e.type_name,
                                             v->type_name);
        if (g->failed) return false;
        LLVMBuildStore(cg_b(g), val, v->slot);
        return true;
    }
    cg_error(g, "variable %s has no storage", v->name ? v->name : "?");
    return false;
}

/* 直接以 LLVM 值写变量 (复合赋值用): 标量存值 */
static bool cg_var_store_value(
    CwCodegen_t* g, CwVar_t* v,
    LLVMValueRef val,
    const char* type_name
) {
    if (!v->slot) {
        cg_error(g, "compound assignment supports scalars only: %s", v->name);
        return false;
    }
    size_t size = 0;
    LLVMTypeRef vt = cg_scalar_type(g, type_name, &size);
    if (!vt) {
        cg_error(g, "unknown scalar type: %s", type_name);
        return false;
    }
    LLVMBuildStore(cg_b(g), val, v->slot);
    return true;
}

static CwExpr cg_var_read(
    CwCodegen_t* g,
    CwVar_t* v
) {
    if (v->is_ref_param) {
        /* self / &T 引用: 值直传 (address 指向调用方 blob/存储) */
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                        v->slot, "vh");
        return (CwExpr){ h, v->type_name };
    }
    if (v->is_ref) {
        /* todo-145: 引用绑定: 读出被借用存储的地址句柄 */
        LLVMValueRef p = LLVMBuildLoad2(
            cg_b(g), LLVMInt64TypeInContext(cg_ctx(g)), v->slot, "ref.addr");
        return (CwExpr){
            cg_build_value(g, p, cg_i64(g, 0), cg_i64(g, 0)),
            v->type_name,
        };
    }
    if (v->blob) {
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), v->blob, LLVMInt64TypeInContext(cg_ctx(g)), "addr");
        if (v->is_array) {
            char elem[128];
            size_t n = 0;
            if (!cg_array_info(v->type_name, elem, sizeof(elem), &n)) {
                cg_error(g, "array type lost its length: %s", v->type_name);
                return (CwExpr){ NULL, NULL };
            }
            return (CwExpr){
                cg_build_value(g, addr, cg_i64(g, n), cg_i64(g, 0)),
                v->type_name,
            };
        }
        return (CwExpr){
            cg_build_value(g, addr, cg_i64(g, v->blob_size), cg_i64(g, 0)),
            v->type_name,
        };
    }
    if (v->is_value) {
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                        v->slot, "vv");
        return (CwExpr){ h, v->type_name };
    }
    if (v->slot) {
        if (cg_is_rawptr(v->type_name)) {
            /* 原始指针: 从槽里加载地址值, 值的 address 就是地址本身 */
            LLVMValueRef p = LLVMBuildLoad2(
                cg_b(g), LLVMInt64TypeInContext(cg_ctx(g)), v->slot, "rp");
            return (CwExpr){
                cg_build_value(g, p, cg_i64(g, 0), cg_i64(g, 0)),
                v->type_name,
            };
        }
        size_t size = 0;
        if (cg_is_fnptr(v->type_name)) {
            size = 8;
        } else {
            cg_scalar_type(g, v->type_name, &size);
        }
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), v->slot, LLVMInt64TypeInContext(cg_ctx(g)), "addr");
        return (CwExpr){
            cg_build_value(g, addr, cg_i64(g, size), cg_i64(g, 0)),
            v->type_name,
        };
    }
    cg_error(g, "variable %s has no storage", v->name ? v->name : "?");
    return (CwExpr){ NULL, NULL };
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
    LLVMValueRef a = cg_alloca(g, arr, name);
    /* 16 字节对齐: FFI 按真实 C 布局取载荷区地址时保证对齐安全 */
    LLVMSetAlignment(a, 16);
    return a;
}

static LLVMValueRef cg_blob_i8(
    CwCodegen_t* g,
    LLVMValueRef blob
) {
    return LLVMBuildBitCast(cg_b(g), blob, cg_rt_i8_ptr(g), "");
}

/* 引用型字段的 CWValue cell 指针: base 为 blob 字节指针,
 * offset 为 C-Like 布局偏移 (无头, 直接偏移) */
static LLVMValueRef cg_struct_slot(
    CwCodegen_t* g, LLVMValueRef base,
    size_t offset
) {
    LLVMValueRef idx[1] = { cg_i64(g, (uint64_t)offset) };
    LLVMValueRef p = LLVMBuildGEP2(cg_b(g),
                                   LLVMInt8TypeInContext(cg_ctx(g)),
                                   base, idx, 1, "f.slot");
    return LLVMBuildBitCast(cg_b(g), p,
                            LLVMPointerType(g->ll->handle_type, 0), "");
}

/* 内联字段的字节指针 (标量/数组/嵌套结构体/指针) */
static LLVMValueRef cg_field_ptr(
    CwCodegen_t* g, LLVMValueRef base,
    size_t offset
) {
    LLVMValueRef idx[1] = { cg_i64(g, (uint64_t)offset) };
    return LLVMBuildGEP2(cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)),
                         base, idx, 1, "f.ptr");
}

static LLVMValueRef cg_struct_handle(
    CwCodegen_t* g, LLVMValueRef blob,
    size_t size
) {
    LLVMValueRef addr = LLVMBuildPtrToInt(cg_b(g), blob,
                                          LLVMInt64TypeInContext(cg_ctx(g)),
                                          "st.addr");
    return cg_build_value(g, addr, cg_i64(g, size),
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
    if (strcmp(name, "Int") == 0 || strcmp(name, "UInt") == 0
        || strcmp(name, "Int16") == 0 || strcmp(name, "UInt16") == 0) {
        return 2;
    }
    if (strcmp(name, "Int8") == 0 || strcmp(name, "UInt8") == 0
        || strcmp(name, "Byte") == 0 || strcmp(name, "Bool") == 0) return 1;
    if (strcmp(name, "Int32") == 0 || strcmp(name, "UInt32") == 0) return 4;
    if (strcmp(name, "Int64") == 0 || strcmp(name, "UInt64") == 0
        || strcmp(name, "Float64") == 0) return 8;
    if (strcmp(name, "Float") == 0) return 4;
    return 0;
}

/* ---- 定长数组 [T; N] (todo-60) ----
 * 类型名整体扁平编码 ("[Byte; 4]"), 元素必须是定宽标量;
 * 值 = 句柄 {address -> 内联数据, length -> N}, 数据随 blob
 * 整块 memcpy, 复制即深拷贝 (纯标量载荷无内嵌指针)。 */

/* 解析 "[Elem; N]" -> elem 缓冲 + 元素数; 非数组名返回 false */
static bool cg_array_info(
    const char* tname,
    char* elem, size_t elem_cap,
    size_t* out_n
) {
    if (!tname || tname[0] != '[') return false;
    const char* semi = strrchr(tname, ';');
    const size_t len = strlen(tname);
    if (!semi || len < 3 || tname[len - 1] != ']') return false;
    /* 元素名: '[' 与 ';' 之间, trim 首尾空格 */
    size_t lo = 1;
    size_t hi = (size_t)(semi - tname);
    while (lo < hi && (tname[lo] == ' ' || tname[lo] == '\t')) lo++;
    while (hi > lo && (tname[hi - 1] == ' ' || tname[hi - 1] == '\t')) hi--;
    if (lo >= hi || hi - lo + 1 >= elem_cap) return false;
    memcpy(elem, tname + lo, hi - lo);
    elem[hi - lo] = '\0';
    /* 长度: "; N]" */
    const char* p = semi + 1;
    while (*p == ' ' || *p == '\t') p++;
    char* end = NULL;
    const unsigned long long v = strtoull(p, &end, 10);
    if (!end || end == p) return false;
    while (*end == ' ' || *end == '\t') end++;
    if (*end != ']') return false;
    if (v == 0 || v > 65536) return false; /* 上限防栈溢出滥用 */
    if (out_n) *out_n = (size_t)v;
    return true;
}

static bool cg_is_array_type(
    const char* tname
) {
    char elem[128];
    return cg_array_info(tname, elem, sizeof(elem), NULL);
}

/* 数组总字节数 (元素必须是定宽标量); 非数组/未知元素返回 0 */
static size_t cg_array_total_bytes(
    const char* tname
) {
    char elem[128];
    size_t n = 0;
    if (!cg_array_info(tname, elem, sizeof(elem), &n)) return 0;
    const size_t esz = cg_scalar_bytes(elem);
    if (esz == 0) return 0;
    return esz * n;
}

/* 二的幂对齐上取整 */
static size_t cg_align_up(
    size_t off, size_t align
) {
    return (off + align - 1) & ~(align - 1);
}

/* 整数宽度档: 同宽类型共享 rank (与前端 _INT_RANK 一致) */
static int cg_int_rank(
    const char* n
) {
    if (!n) return -1;
    if (strcmp(n, "Int8") == 0 || strcmp(n, "UInt8") == 0
        || strcmp(n, "Byte") == 0) return 1;
    if (strcmp(n, "Int") == 0 || strcmp(n, "UInt") == 0
        || strcmp(n, "Int16") == 0 || strcmp(n, "UInt16") == 0) return 2;
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

/* todo-151: 布局字段的泛型实参类型名 (类型表携带实例化后的实参)。
 * Map<Int,Int> 字段 -> arg0="Int" / arg1="Int"; 标量返回名字,
 * 泛型 opaque 叶子或未知返回 NULL (调用方自行兜底)。 */
static const char* cg_field_arg_type_name(
    CwCodegen_t* g, const CwLayout_t* L,
    size_t fi, size_t idx
) {
    const CwType_t* t = cwtype_get(g->ll->types, L->fields[fi].type);
    if (!t || !t->name || !t->args || t->arg_count <= idx) return NULL;
    const CwType_t* a = cwtype_get(g->ll->types, t->args[idx]);
    if (!a || !a->name || a->opaque) return NULL;
    return a->name;
}

/* todo-151: 字段/下标赋值路径的 `T::new()` 静态构造期望 tag ——
 * 与 cg_stmt_let/cg_assign_var 同纪律 (bug-47 绑定语义): 从目标
 * 容器 (或持有容器的字段/被下标容器) 的类型上下文取泛型实参 tag。
 * 已有更内层上下文 (has_exp_tags) 时不覆盖。 */
static void cg_push_expected_tags_from_ann(
    CwCodegen_t* g, const cw_value* type_node
) {
    if (g->has_exp_tags || !type_node) return;
    const int t0 = cg_ann_arg_tag(g, type_node, 0);
    const int t1 = cg_ann_arg_tag(g, type_node, 1);
    if (t0 == 0 && t1 == 0) return;
    g->exp_tags[0] = t0;
    g->exp_tags[1] = t1;
    g->has_exp_tags = true;
}

/* 结构体 blob 总字节数: C-Like 布局已由 cwlayout 缓存算好 (含尾补齐) */
static size_t cg_struct_blob_size(
    CwCodegen_t* g,
    const CwLayout_t* L
) {
    (void)g;
    return L ? L->size : 0;
}

/* 字段类别 (C-Like-Layout, 与 cwlayout 的尺寸/对齐规则对应) */
typedef enum {
    CG_FK_SCALAR = 0, /* 内联标量 */
    CG_FK_ARRAY,      /* 内联定长标量数组 */
    CG_FK_PTR,        /* 内联 8B 地址 (rawptr: 地址即值) */
    CG_FK_FNPTR,      /* 内联 8B 函数地址 (值指向其存储) */
    CG_FK_STRUCT,     /* 内联嵌套结构体 */
    CG_FK_CELL,       /* 24B CWValue cell (String/容器/枚举/泛型遗留) */
} CgFieldKind;

static CgFieldKind cg_field_kind(
    CwCodegen_t* g, const CwLayout_t* L, size_t i
) {
    const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
    if (cg_scalar_bytes(ft) > 0) return CG_FK_SCALAR;
    char elem[128];
    if (cg_array_info(ft, elem, sizeof(elem), NULL)) return CG_FK_ARRAY;
    if (cg_is_rawptr(ft)) return CG_FK_PTR;
    if (cg_is_fnptr(ft)) return CG_FK_FNPTR;
    if (cg_is_struct_type(g, ft)) return CG_FK_STRUCT;
    return CG_FK_CELL;
}

/* 内联嵌套结构体字段的内部布局 (泛型实参经类型表解析) */
static const CwLayout_t* cg_field_inner_layout(
    CwCodegen_t* g, const CwLayout_t* L, size_t i
) {
    const CwTypeId tid = L->fields[i].type;
    const CwType_t* t = cwtype_get(g->ll->types, tid);
    if (!t || !t->name) return NULL;
    const CwNode_t* decl = cg_struct_decl(g, t->name);
    if (!decl) return NULL;
    return cwlayout_get(g->ll->layouts, g->m, decl,
                        t->args, t->arg_count);
}

/* 读一个字段 (C-Like-Layout): 内联字段值指向 blob 内偏移 (零拷贝),
 * 引用型字段加载 24B CWValue cell。base 为 blob 字节指针。 */
static CwExpr cg_read_struct_field(
    CwCodegen_t* g, LLVMValueRef base,
    const CwLayout_t* L, size_t fi,
    const char* t
) {
    const size_t off = L->fields[fi].offset;
    CwExpr e = { NULL, t ? t : "Any" };
    switch (cg_field_kind(g, L, fi)) {
    case CG_FK_SCALAR: {
        const char* ft = cwtype_name(g->ll->types, L->fields[fi].type);
        size_t wsz = 0;
        cg_scalar_type(g, ft, &wsz);
        LLVMValueRef p = cg_field_ptr(g, base, off);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "f.addr");
        e.handle = cg_build_value(g, addr, cg_i64(g, wsz), cg_i64(g, 0));
        return e;
    }
    case CG_FK_ARRAY: {
        char elem[128];
        size_t n = 0;
        cg_array_info(cwtype_name(g->ll->types, L->fields[fi].type),
                      elem, sizeof(elem), &n);
        LLVMValueRef p = cg_field_ptr(g, base, off);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "f.addr");
        e.handle = cg_build_value(g, addr, cg_i64(g, n), cg_i64(g, 0));
        return e;
    }
    case CG_FK_PTR: {
        /* 原始指针: 从 8B 槽加载地址值 (地址即值) */
        LLVMValueRef p = LLVMBuildBitCast(
            cg_b(g), cg_field_ptr(g, base, off),
            LLVMPointerType(LLVMInt64TypeInContext(cg_ctx(g)), 0), "");
        LLVMValueRef a = LLVMBuildLoad2(
            cg_b(g), LLVMInt64TypeInContext(cg_ctx(g)), p, "f.addr");
        e.handle = cg_build_value(g, a, cg_i64(g, 0), cg_i64(g, 0));
        return e;
    }
    case CG_FK_FNPTR: {
        /* 函数指针: 值指向存 fn 地址的 8B 槽 */
        LLVMValueRef p = cg_field_ptr(g, base, off);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "f.addr");
        e.handle = cg_build_value(g, addr, cg_i64(g, 8), cg_i64(g, 0));
        return e;
    }
    case CG_FK_STRUCT: {
        /* 内联嵌套结构体: 值指向 blob 内子 blob */
        LLVMValueRef p = cg_field_ptr(g, base, off);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "f.addr");
        e.handle = cg_build_value(g, addr,
                                  cg_i64(g, L->fields[fi].size),
                                  cg_i64(g, 0));
        return e;
    }
    case CG_FK_CELL:
    default: {
        LLVMValueRef slot = cg_struct_slot(g, base, off);
        e.handle = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, slot, "fh");
        return e;
    }
    }
}

/* 写一个字段 (C-Like-Layout): 内联字段直接落在偏移上,
 * 引用型字段存 24B CWValue cell。无自指句柄, 拷贝即深拷贝。 */
static void cg_store_struct_field(
    CwCodegen_t* g, LLVMValueRef base,
    const CwLayout_t* L, size_t i,
    CwExpr val
) {
    const size_t off = L->fields[i].offset;
    switch (cg_field_kind(g, L, i)) {
    case CG_FK_SCALAR: {
        const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
        /* bug-55: RHS 标量必须先转到字段宽度 —— 字面量等窄句柄
         * 直接按字段宽度 load 会读进相邻栈内存 (如 Int 字面量的
         * 2B 槽被按 usize 8B 读, 高位全是垃圾) */
        val = cg_coerce_scalar(g, val, ft);
        LLVMTypeRef vt = cg_scalar_type(g, ft, NULL);
        LLVMValueRef v = cg_load_value(g, val, vt);
        LLVMBuildStore(cg_b(g), v, LLVMBuildBitCast(
            cg_b(g), cg_field_ptr(g, base, off),
            LLVMPointerType(vt, 0), ""));
        return;
    }
    case CG_FK_ARRAY: {
        const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
        const size_t total = cg_array_total_bytes(ft);
        LLVMValueRef src = LLVMBuildIntToPtr(cg_b(g),
                                             cg_handle_addr(g, val),
                                             cg_rt_i8_ptr(g), "f.asrc");
        LLVMBuildMemCpy(cg_b(g), cg_field_ptr(g, base, off), 1, src, 1,
                        cg_i64(g, (uint64_t)total));
        return;
    }
    case CG_FK_PTR: {
        /* 原始指针: 8B 地址直存 (地址即值) */
        LLVMValueRef p = LLVMBuildBitCast(
            cg_b(g), cg_field_ptr(g, base, off),
            LLVMPointerType(LLVMInt64TypeInContext(cg_ctx(g)), 0), "");
        LLVMBuildStore(cg_b(g), cg_handle_addr(g, val), p);
        return;
    }
    case CG_FK_FNPTR: {
        /* 函数指针: 8B 函数地址直存 (值指向存储, 拷地址值) */
        LLVMTypeRef i64t = LLVMInt64TypeInContext(cg_ctx(g));
        LLVMValueRef v = cg_load_value(g, val, i64t);
        LLVMBuildStore(cg_b(g), v, LLVMBuildBitCast(
            cg_b(g), cg_field_ptr(g, base, off),
            LLVMPointerType(i64t, 0), ""));
        return;
    }
    case CG_FK_STRUCT: {
        /* 内联嵌套结构体: 子 blob 整块拷入偏移处 (值语义) */
        const CwLayout_t* inner = cg_field_inner_layout(g, L, i);
        if (!inner) {
            cg_error(g, "inline struct field has no layout");
            return;
        }
        LLVMValueRef src = cg_expr_blob_i8(g, val);
        LLVMBuildMemCpy(cg_b(g), cg_field_ptr(g, base, off), 1, src, 1,
                        cg_i64(g, (uint64_t)inner->size));
        return;
    }
    case CG_FK_CELL:
    default: {
        LLVMValueRef slot = cg_struct_slot(g, base, off);
        LLVMBuildStore(cg_b(g), val.handle, slot);
        return;
    }
    }
}

/* ---- 枚举 (Rust 风格带值 enum) ----
 * 实例 blob 布局 (对所有变体统一):
 *   8B 头 + i32 tag (偏移 8) + 4B pad + max_payloads × 32B 句柄槽;
 * 标量/结构体载荷拷进进程期 arena (跨函数存活), 复制只需浅拷 blob;
 * String/容器/嵌套枚举载荷直接存句柄 (其内容已持久)。
 */

#define CWENUM_TAG_OFFSET 0
#define CWENUM_SLOTS_OFFSET 8

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
        + cg_enum_max_payloads(decl) * CWIND_VALUE_SIZE;
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
        cg_i64(g, CWENUM_SLOTS_OFFSET + i * CWIND_VALUE_SIZE)
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
    return cg_build_value(g, addr,
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
        return cg_build_value(g, addr,
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
        /* ABI v2: blob 无自指句柄, 拷贝即完整 */
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), dst, LLVMInt64TypeInContext(cg_ctx(g)), "en.cell.addr");
        return cg_build_value(g, addr,
                               cg_i64(g, size), cg_i64(g, 0));
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
        return cg_build_value(g, addr,
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
    if (cg_is_enum_type(g, type_name)) {
        snprintf(gname, sizeof(gname), "cwind.static.%s.%s.blob",
                 owner, fname);
        LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
        if (gv) return gv;
        LLVMTypeRef arr = LLVMArrayType(
            LLVMInt8TypeInContext(cg_ctx(g)),
            (unsigned)cg_enum_blob_size(g, type_name));
        gv = LLVMAddGlobal(g->ll->module, arr, gname);
        LLVMSetInitializer(gv, LLVMConstNull(arr));
        return gv;
    }
    snprintf(gname, sizeof(gname), "cwind.static.%s.%s.val",
             owner, fname);
    LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
    if (gv) return gv;
    /* 值类型 (String/容器/None): CWValue 全局 (值本体) */
    gv = LLVMAddGlobal(g->ll->module, g->ll->handle_type, gname);
    LLVMSetInitializer(gv, LLVMConstNull(g->ll->handle_type));
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
    if (cg_is_struct_type(g, type_name) || cg_is_enum_type(g, type_name)) {
        const CwLayout_t* L = cg_is_struct_type(g, type_name)
            ? cg_struct_layout(g, type_obj) : NULL;
        LLVMValueRef gb = cg_static_storage(
            g, owner, fname, type_name, type_obj);
        if ((cg_is_struct_type(g, type_name) && !L) || !gb) return false;
        LLVMValueRef src = cg_expr_blob_i8(g, e);
        LLVMValueRef dst = cg_blob_i8(g, gb);
        const size_t bsz = cg_is_struct_type(g, type_name)
            ? cg_struct_blob_size(g, L) : cg_enum_blob_size(g, type_name);
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1,
                        cg_i64(g, (uint64_t)bsz));
        return true;
    }
    /* 值类型: CWValue 直存 */
    LLVMValueRef gr = cg_static_storage(
        g, owner, fname, type_name, type_obj);
    if (!gr) return false;
    LLVMBuildStore(cg_b(g), e.handle, gr);
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
            cg_build_value(g, addr,
                            cg_i64(g, size), cg_i64(g, 0)),
            type_name,
        };
    }
    if (cg_is_struct_type(g, type_name) || cg_is_enum_type(g, type_name)) {
        const CwLayout_t* L = cg_is_struct_type(g, type_name)
            ? cg_struct_layout(g, type_obj) : NULL;
        LLVMValueRef gb = cg_static_storage(
            g, owner, fname, type_name, type_obj);
        if ((cg_is_struct_type(g, type_name) && !L) || !gb) {
            return (CwExpr){ NULL, NULL };
        }
        const size_t bsz = cg_is_struct_type(g, type_name)
            ? cg_struct_blob_size(g, L) : cg_enum_blob_size(g, type_name);
        return (CwExpr){ cg_struct_handle(g, gb, bsz), type_name };
    }
    /* 值类型: CWValue 全局直读 */
    LLVMValueRef gr = cg_static_storage(
        g, owner, fname, type_name, type_obj);
    if (!gr) return (CwExpr){ NULL, NULL };
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                    gr, "st.vh");
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
    if (cg_is_enum_type(g, type_name)) {
        snprintf(gname, sizeof(gname), "cwind.const.%s.blob", name);
        LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
        if (gv) return gv;
        LLVMTypeRef arr = LLVMArrayType(
            LLVMInt8TypeInContext(cg_ctx(g)),
            (unsigned)cg_enum_blob_size(g, type_name));
        gv = LLVMAddGlobal(g->ll->module, arr, gname);
        LLVMSetInitializer(gv, LLVMConstNull(arr));
        return gv;
    }
    snprintf(gname, sizeof(gname), "cwind.const.%s.val", name);
    LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
    if (gv) return gv;
    gv = LLVMAddGlobal(g->ll->module, g->ll->handle_type, gname);
    LLVMSetInitializer(gv, LLVMConstNull(g->ll->handle_type));
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
    if (cg_is_struct_type(g, type_name) || cg_is_enum_type(g, type_name)) {
        const CwLayout_t* L = cg_is_struct_type(g, type_name)
            ? cg_struct_layout(g, type_obj) : NULL;
        LLVMValueRef gb = cg_const_storage(
            g, name, type_name, type_obj);
        if ((cg_is_struct_type(g, type_name) && !L) || !gb) return false;
        LLVMValueRef src = cg_expr_blob_i8(g, e);
        LLVMValueRef dst = cg_blob_i8(g, gb);
        const size_t bsz = cg_is_struct_type(g, type_name)
            ? cg_struct_blob_size(g, L) : cg_enum_blob_size(g, type_name);
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1,
                        cg_i64(g, (uint64_t)bsz));
        return true;
    }
    LLVMValueRef gr = cg_const_storage(
        g, name, type_name, type_obj);
    if (!gr) return false;
    LLVMBuildStore(cg_b(g), e.handle, gr);
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
            cg_build_value(g, addr,
                            cg_i64(g, size), cg_i64(g, 0)),
            type_name,
        };
    }
    if (cg_is_struct_type(g, type_name) || cg_is_enum_type(g, type_name)) {
        const CwLayout_t* L = cg_is_struct_type(g, type_name)
            ? cg_struct_layout(g, type_obj) : NULL;
        LLVMValueRef gb = cg_const_storage(
            g, name, type_name, type_obj);
        if ((cg_is_struct_type(g, type_name) && !L) || !gb) {
            return (CwExpr){ NULL, NULL };
        }
        const size_t bsz = cg_is_struct_type(g, type_name)
            ? cg_struct_blob_size(g, L) : cg_enum_blob_size(g, type_name);
        return (CwExpr){ cg_struct_handle(g, gb, bsz), type_name };
    }
    LLVMValueRef gr = cg_const_storage(
        g, name, type_name, type_obj);
    if (!gr) return (CwExpr){ NULL, NULL };
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                    gr, "c.vh");
    return (CwExpr){ h, type_name };
}

/* ---- 表达式 ---- */

/* todo-122: 关联常量 (extra 块内的 const)。
 * 在节点池里按 kind 扫描 ExtraDecl, 目标类型与 owner 匹配后再按名
 * 查 consts; 返回 ConstDecl 节点对象 (type/ann.type + value)。 */
static cw_value* cg_extra_const(
    const CwCodegen_t* g, const char* owner,
    const char* cname,
    cw_value** type_out
) {
    if (!owner || !cname) return NULL;
    const size_t n = cwmodule_node_count(g->m);
    for (size_t i = 0; i < n; i++) {
        const CwNode_t* nd = cwmodule_node_at(g->m, i);
        if (!nd || strcmp(nd->kind, "ExtraDecl") != 0) continue;
        cw_value* st = cw_object_get(nd->value, "struct");
        const char* sname = cg_json_name(st);
        if (!sname || strcmp(sname, owner) != 0) continue;
        cw_value* consts = cw_object_get(nd->value, "consts");
        if (!consts || cw_typeof(consts) != CW_ARRAY) continue;
        const size_t nc = cw_array_size(consts);
        for (size_t j = 0; j < nc; j++) {
            cw_value* c = cw_array_get(consts, j);
            const char* cn = cg_json_name(c);
            if (!cn || strcmp(cn, cname) != 0) continue;
            cw_value* t = cw_object_get(c, "type");
            cw_value* ann = cw_object_get(c, "ann");
            cw_value* at = ann ? cw_object_get(ann, "type") : NULL;
            if (at && cw_typeof(at) == CW_OBJECT) t = at;
            if (type_out) *type_out = t;
            return c;
        }
    }
    return NULL;
}

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
        cg_build_value(g, addr, cg_i64(g, len),
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
            /* todo-85: 十六进制字面量的 raw 带 0x/0X 前缀 (u64 语义) */
            const int base =
                (rs[0] == '0' && (rs[1] == 'x' || rs[1] == 'X')) ? 16 : 10;
            uv = strtoull(rs, NULL, base);
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
    /* cwvec_init 要求值地址为 0, 先清零 CWValue */
    LLVMValueRef val = cg_cell_alloca(g, "vec.val");
    LLVMBuildStore(cg_b(g), cg_null_handle(g), val);
    LLVMValueRef val8 = LLVMBuildBitCast(cg_b(g), val, cg_rt_i8_ptr(g),
                                         "");
    const int elem_tid = cg_elem_type_id(g, node);
    LLVMTypeRef pr_init[3] = { cg_rt_i8_ptr(g),
                               LLVMInt32TypeInContext(cg_ctx(g)),
                               LLVMInt64TypeInContext(cg_ctx(g)) };
    LLVMValueRef init = cg_rt_declare(g, "cwvec_init",
                                      LLVMInt1TypeInContext(cg_ctx(g)),
                                      pr_init, 3);
    LLVMValueRef init_args[3] = { val8, cg_i32(g, (uint32_t)elem_tid),
                                  cg_i64(g, 0) };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(init), init,
                   init_args, 3, "");
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
        LLVMValueRef er = cg_value_cell(g, e, cg_node_ann_type(elem));
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                            cg_rt_i8_ptr(g), "");
        LLVMValueRef pargs[2] = { val8, er8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(push), push,
                       pargs, 2, "");
    }
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, val,
                                    "vh");
    CwExpr e = { h, "Vector" };
    return e;
}

static CwExpr cg_lit_map(
    CwCodegen_t* g,
    const cw_value*node
) {
    /* cwmap_init 要求值地址为 0, 先清零 CWValue */
    LLVMValueRef val = cg_cell_alloca(g, "map.val");
    LLVMBuildStore(cg_b(g), cg_null_handle(g), val);
    LLVMValueRef val8 = LLVMBuildBitCast(cg_b(g), val, cg_rt_i8_ptr(g),
                                         "");
    cw_value* mann = cw_object_get(node, "ann");
    cw_value* mkt = mann ? cw_object_get(mann, "key_type") : NULL;
    cw_value* mvt = mann ? cw_object_get(mann, "value_type") : NULL;
    /* key/value tag: 显式注解优先, 否则取 ann.type 泛型实参 */
    const char* mk = mkt ? cg_type_name_of(g, mkt) : NULL;
    const char* mv = mvt ? cg_type_name_of(g, mvt) : NULL;
    const int ktid0 = mk ? cg_type_id(mk)
                         : cg_ann_arg_tag(g, node, 0);
    const int vtid0 = mv ? cg_type_id(mv)
                         : cg_ann_arg_tag(g, node, 1);
    LLVMTypeRef pr_init[3] = { cg_rt_i8_ptr(g),
                               LLVMInt32TypeInContext(cg_ctx(g)),
                               LLVMInt32TypeInContext(cg_ctx(g)) };
    LLVMValueRef init = cg_rt_declare(g, "cwmap_init",
                                      LLVMInt1TypeInContext(cg_ctx(g)),
                                      pr_init, 3);
    LLVMValueRef init_args[3] = {
        val8,
        cg_i32(g, (uint32_t)(ktid0 >= 0 ? ktid0 : 0)),
        cg_i32(g, (uint32_t)(vtid0 >= 0 ? vtid0 : 0)),
    };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(init), init,
                   init_args, 3, "");
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
        LLVMValueRef kr = cg_value_cell(g, k, NULL);
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef vr = cg_value_cell(
            g, v, cg_node_ann_type(cw_object_get(entry, "value")));
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef kr8 = LLVMBuildBitCast(cg_b(g), kr,
                                            cg_rt_i8_ptr(g), "");
        LLVMValueRef vr8 = LLVMBuildBitCast(cg_b(g), vr,
                                            cg_rt_i8_ptr(g), "");
        LLVMValueRef pargs[3] = { val8, kr8, vr8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(put), put,
                       pargs, 3, "");
    }
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, val,
                                    "vh");
    CwExpr e = { h, "Map" };
    return e;
}

static CwExpr cg_lit_tuple(
    CwCodegen_t* g,
    const cw_value*node
) {
    /* cwtuple_init 要求值地址为 0, 先清零 CWValue */
    LLVMValueRef val = cg_cell_alloca(g, "tup.val");
    LLVMBuildStore(cg_b(g), cg_null_handle(g), val);
    LLVMValueRef val8 = LLVMBuildBitCast(cg_b(g), val, cg_rt_i8_ptr(g),
                                         "");
    cw_value* elems = cw_object_get(node, "elems");
    const size_t ne = (elems && cw_typeof(elems) == CW_ARRAY)
        ? cw_array_size(elems) : 0;
    cw_value* ann = cw_object_get(node, "ann");
    cw_value* ets = ann ? cw_object_get(ann, "element_types") : NULL;
    /* 元素 cell 数组 + 元素类型表 (Tuple 异构, 类型存 data 头) */
    LLVMValueRef arr = ne > 0
        ? cg_alloca(g, LLVMArrayType(g->ll->handle_type, (unsigned)ne),
                    "tup.elems")
        : NULL;
    LLVMValueRef tarr = ne > 0
        ? cg_alloca(g, LLVMArrayType(
                        LLVMInt32TypeInContext(cg_ctx(g)), (unsigned)ne),
                    "tup.types")
        : NULL;
    LLVMValueRef arr8 = arr ? LLVMBuildBitCast(cg_b(g), arr,
                                               cg_rt_i8_ptr(g), "") : NULL;
    for (size_t i = 0; i < ne && !g->failed; i++) {
        const char* en = NULL;
        if (ets && cw_typeof(ets) == CW_ARRAY
            && i < cw_array_size(ets)) {
            en = cg_type_name_of(g, cw_array_get(ets, i));
        }
        CwExpr e = cg_expr(g, cw_array_get(elems, i));
        if (g->failed) return (CwExpr){ NULL, NULL };
        e = cg_coerce_scalar(g, e, en);
        LLVMValueRef er = cg_value_cell(
            g, e, cg_node_ann_type(cw_array_get(elems, i)));
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef slot_idx[1] = { cg_i64(g, (uint64_t)i) };
        LLVMValueRef slot = LLVMBuildGEP2(
            cg_b(g), g->ll->handle_type, arr, slot_idx, 1, "tup.slot");
        LLVMBuildStore(cg_b(g), e.handle, slot);
        const int etid = en ? cg_type_id(en) : -1;
        LLVMValueRef tslot = LLVMBuildGEP2(
            cg_b(g), LLVMInt32TypeInContext(cg_ctx(g)), tarr,
            slot_idx, 1, "tup.type");
        LLVMBuildStore(cg_b(g),
                       cg_i32(g, (uint32_t)(etid >= 0 ? etid : 0)), tslot);
        (void)arr8;
    }
    LLVMTypeRef pr_init[4] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                               cg_rt_i8_ptr(g),
                               LLVMInt64TypeInContext(cg_ctx(g)) };
    LLVMValueRef init = cg_rt_declare(
        g, "cwtuple_init", LLVMInt1TypeInContext(cg_ctx(g)), pr_init, 4);
    LLVMValueRef init_args[4] = {
        val8,
        tarr ? LLVMBuildBitCast(cg_b(g), tarr, cg_rt_i8_ptr(g), "")
             : LLVMConstPointerNull(cg_rt_i8_ptr(g)),
        arr8 ? arr8 : LLVMConstPointerNull(cg_rt_i8_ptr(g)),
        cg_i64(g, ne)
    };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(init), init,
                   init_args, 4, "");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, val,
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

/* 定长数组字面量 (todo-60): `[a, b, c]` 在期望类型为 `[T; N]` 时
 * 按数组构造: 元素逐个内联进载荷 blob, 值 = {address -> blob,
 * length -> N}。元素必须为定宽标量 (SA 保证)。 */
static CwExpr cg_lit_array(
    CwCodegen_t* g,
    const cw_value*node
) {
    const char* tname = cg_node_type_name(g, node);
    char elem[128];
    size_t n = 0;
    if (!tname || !cg_array_info(tname, elem, sizeof(elem), &n)) {
        cg_error(g, "array literal is missing its array type");
        return (CwExpr){ NULL, NULL };
    }
    const size_t esz = cg_scalar_bytes(elem);
    const size_t total = esz * n;
    LLVMTypeRef evt = cg_scalar_type(g, elem, NULL);
    LLVMValueRef blob = cg_blob_alloc(g, total, "arr.lit");
    LLVMValueRef base = cg_blob_i8(g, blob);
    cw_value* elems = cw_object_get(node, "elems");
    const size_t ne = (elems && cw_typeof(elems) == CW_ARRAY)
        ? cw_array_size(elems) : 0;
    /* bug-35: `[x; N]` 重复字面量, 单个元素重复 N 次
     * (SA 已把 ann.repeat 写进结点, 并保证目标类型是定长数组) */
    cw_value* ann = cw_object_get(node, "ann");
    cw_value* repv = (ann && cw_typeof(ann) == CW_OBJECT)
        ? cw_object_get(ann, "repeat") : NULL;
    int64_t rep = 0;
    if (repv && cw_typeof(repv) == CW_INT
        && cw_as_int(repv, &rep) == CW_OK && rep > 0) {
        if (ne != 1) {
            cg_error_at(g, node,
                        "repeat array literal requires exactly one element");
            return (CwExpr){ NULL, NULL };
        }
        CwExpr e = cg_expr(g, cw_array_get(elems, 0));
        if (g->failed) return (CwExpr){ NULL, NULL };
        e = cg_coerce_scalar(g, e, elem);
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef v = cg_load_value(g, e, evt);
        for (int64_t i = 0; i < rep && !g->failed; i++) {
            LLVMValueRef off[1] = { cg_i64(g, (uint64_t)((size_t)i * esz)) };
            LLVMValueRef p = LLVMBuildGEP2(cg_b(g),
                                           LLVMInt8TypeInContext(cg_ctx(g)),
                                           base, off, 1, "arr.slot");
            LLVMBuildStore(cg_b(g), v, LLVMBuildBitCast(
                cg_b(g), p, LLVMPointerType(evt, 0), ""));
        }
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef addr = LLVMBuildPtrToInt(cg_b(g), blob,
                                              LLVMInt64TypeInContext(cg_ctx(g)),
                                              "arr.addr");
        return (CwExpr){
            cg_build_value(g, addr, cg_i64(g, n),
                            cg_i64(g, 0)),
            tname,
        };
    }
    for (size_t i = 0; i < ne && !g->failed; i++) {
        CwExpr e = cg_expr(g, cw_array_get(elems, i));
        if (g->failed) return (CwExpr){ NULL, NULL };
        e = cg_coerce_scalar(g, e, elem);
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef v = cg_load_value(g, e, evt);
        LLVMValueRef off[1] = { cg_i64(g, (uint64_t)(i * esz)) };
        LLVMValueRef p = LLVMBuildGEP2(cg_b(g),
                                       LLVMInt8TypeInContext(cg_ctx(g)),
                                       base, off, 1, "arr.slot");
        LLVMBuildStore(cg_b(g), v, LLVMBuildBitCast(
            cg_b(g), p, LLVMPointerType(evt, 0), ""));
    }
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef addr = LLVMBuildPtrToInt(cg_b(g), blob,
                                          LLVMInt64TypeInContext(cg_ctx(g)),
                                          "arr.addr");
    return (CwExpr){
        cg_build_value(g, addr, cg_i64(g, n),
                        cg_i64(g, 0)),
        tname,
    };
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
    if (strcmp(kind, "VectorLit") == 0) {
        /* todo-60: 期望类型为定长数组时按数组构造而非堆容器 */
        if (cg_is_array_type(cg_node_type_name(g, node))) {
            return cg_lit_array(g, node);
        }
        return cg_lit_vector(g, node);
    }
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
        if (bk && strcmp(bk, "assoc_const") == 0) {
            /* todo-122: 关联常量读取。存储复用顶层 const 的全局槽位
             * (键 = "Owner.member", 与裸名 const / 静态字段不冲突),
             * 初始化由 main 包装里的 cg_emit_const_inits 完成。 */
            cw_value* type_obj = NULL;
            const char* real_owner = cg_static_owner(g, owner);
            cw_value* decl = cg_extra_const(
                g, real_owner, member, &type_obj);
            if (!decl) {
                cg_error(g, "associated const not found: %s::%s",
                         real_owner ? real_owner : owner, member);
                return (CwExpr){ NULL, NULL };
            }
            const char* t = cg_node_type_name(g, node);
            if (!t) t = type_obj ? cg_type_name_of(g, type_obj) : NULL;
            if (!t) {
                cg_error(g, "associated const is missing a type: %s::%s",
                         real_owner ? real_owner : owner, member);
                return (CwExpr){ NULL, NULL };
            }
            char key[256];
            snprintf(key, sizeof(key), "%s.%s", real_owner, member);
            return cg_const_read(g, key, t, type_obj);
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
    LLVMValueRef lp = cg_cell_alloca(g, "cat.l");
    LLVMValueRef rp = cg_cell_alloca(g, "cat.r");
    LLVMValueRef out = cg_cell_alloca(g, "cat.out");
    LLVMBuildStore(cg_b(g), l.handle, lp);
    LLVMBuildStore(cg_b(g), r.handle, rp);
    LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                          cg_rt_i8_ptr(g) };
    LLVMValueRef f = cg_rt_declare(g, "cw_builtin_concat",
                                   LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
    LLVMValueRef av[3] = {
        LLVMBuildBitCast(cg_b(g), lp, cg_rt_i8_ptr(g), ""),
        LLVMBuildBitCast(cg_b(g), rp, cg_rt_i8_ptr(g), ""),
        LLVMBuildBitCast(cg_b(g), out, cg_rt_i8_ptr(g), ""),
    };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
    CwExpr e = cg_out_value_read(g, out, "String");
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

    LLVMValueRef self_cell = cg_cell_alloca(g, "fmt.self");
    LLVMBuildStore(cg_b(g), self.handle, self_cell);
    LLVMValueRef self8 = LLVMBuildBitCast(cg_b(g), self_cell,
                                          cg_rt_i8_ptr(g), "");

    cw_value* args = cw_object_get(node, "args");
    const size_t na = (args && cw_typeof(args) == CW_ARRAY)
        ? cw_array_size(args) : 0;
    LLVMValueRef arr8 = NULL;
    if (na > 0) {
        /* rt 约定: args 是 CWCell 数组 (tag + 值, 异构边界) */
        LLVMValueRef arr = cg_alloca(
            g, LLVMArrayType(g->ll->cell_type, (unsigned)na), "fmt.args");
        arr8 = LLVMBuildBitCast(cg_b(g), arr, cg_rt_i8_ptr(g), "");
        for (size_t i = 0; i < na && !g->failed; i++) {
            cw_value* arg = cw_array_get(args, i);
            CwExpr a = cg_expr(g, cw_object_get(arg, "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            if (a.type_name && cg_is_array_type(a.type_name)) {
                /* todo-60: 定长数组尚无运行时 Display,
                 * 以编译期占位串 "<元素, 长度>" 参与格式化 */
                char elem[128];
                size_t n = 0;
                char ph[192];
                if (cg_array_info(a.type_name, elem, sizeof(elem), &n)) {
                    snprintf(ph, sizeof(ph), "<%s, %llu>",
                             elem, (unsigned long long)n);
                } else {
                    snprintf(ph, sizeof(ph), "<array>");
                }
                a = cg_string_lit(g, ph, strlen(ph));
            }
            /* 单元逐字段写入 (alloca 数组不能按聚合 store) */
            LLVMValueRef idx[1] = { cg_i64(g, (uint64_t)i) };
            LLVMValueRef slot = LLVMBuildGEP2(
                cg_b(g), g->ll->cell_type, arr, idx, 1, "fmt.slot");
            LLVMValueRef tp = LLVMBuildStructGEP2(cg_b(g),
                g->ll->cell_type, slot, 0, "fmt.tag");
            LLVMValueRef ap = LLVMBuildStructGEP2(cg_b(g),
                g->ll->cell_type, slot, 2, "fmt.a");
            LLVMValueRef lp = LLVMBuildStructGEP2(cg_b(g),
                g->ll->cell_type, slot, 3, "fmt.l");
            LLVMValueRef cp = LLVMBuildStructGEP2(cg_b(g),
                g->ll->cell_type, slot, 4, "fmt.c");
            const int tid = cg_type_id(a.type_name);
            LLVMBuildStore(cg_b(g),
                           cg_i32(g, (uint32_t)(tid >= 0 ? tid : 0)), tp);
            LLVMBuildStore(cg_b(g),
                LLVMBuildExtractValue(cg_b(g), a.handle, 0, "v.a"), ap);
            LLVMBuildStore(cg_b(g),
                LLVMBuildExtractValue(cg_b(g), a.handle, 1, "v.l"), lp);
            LLVMBuildStore(cg_b(g),
                LLVMBuildExtractValue(cg_b(g), a.handle, 2, "v.c"), cp);
        }
    }

    LLVMValueRef out = cg_cell_alloca(g, "fmt.out");
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
    return cg_out_value_read(g, out, "String");
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
            return (CwExpr){ cg_struct_handle(g, blob, L->size), t };
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

    /* todo-88: 原始指针判等 (同型指针比较 address 字段;
     * const/mut 视图差异由前端负责) */
    if ((strcmp(op, "==") == 0 || strcmp(op, "!=") == 0)
        && l.type_name && r.type_name
        && cg_is_rawptr(l.type_name)
        && strcmp(l.type_name, r.type_name) == 0) {
        LLVMValueRef c = LLVMBuildICmp(cg_b(g),
            strcmp(op, "==") == 0 ? LLVMIntEQ : LLVMIntNE,
            cg_handle_addr(g, l), cg_handle_addr(g, r), "p.cmp");
        LLVMValueRef z = LLVMBuildZExt(cg_b(g), c,
            LLVMInt8TypeInContext(cg_ctx(g)), "p.z");
        return cg_make_scalar(g, z,
                              LLVMInt8TypeInContext(cg_ctx(g)),
                              "Bool", 1);
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
        /* todo-74: 整数操作数 = Rust 风格按位取反 (同宽结果);
         * Bool 操作数维持 i8 布尔翻转。 */
        if (e.type_name && strcmp(e.type_name, "Bool") != 0
            && cg_is_int(e.type_name)) {
            size_t sz = 0;
            LLVMTypeRef it = cg_scalar_type(g, e.type_name, &sz);
            LLVMValueRef v = cg_load_value(g, e, it);
            return cg_make_scalar(g, LLVMBuildNot(cg_b(g), v, "bnot"),
                                  it, e.type_name, sz);
        }
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
         * const/mut 约束由前端 SA 负责。todo-145: &T/&mut T 引用与
         * 裸指针同一 load/store 路径 (句柄 address 指向被借用存储)。 */
        const char* pointee = NULL;
        cw_value* opnd = cw_object_get(node, "operand");
        cw_value* oann = opnd ? cw_object_get(opnd, "ann") : NULL;
        cw_value* oty = oann ? cw_object_get(oann, "type") : NULL;
        const bool via_ref = oty && cg_type_is_ref(oty);
        if (cg_is_rawptr(e.type_name)) {
            pointee = strchr(e.type_name, ' ') + 1;
        } else if (via_ref) {
            pointee = cg_type_name_of(g, oty);
        }
        if (!pointee) {
            cg_error_at(g, node,
                        "cannot dereference non-scalar pointer type: %s",
                        e.type_name ? e.type_name : "?");
            return (CwExpr){ NULL, NULL };
        }
        size_t size = 0;
        LLVMValueRef addr = cg_handle_addr(g, e);

        if (cg_is_struct_type(g, pointee)) {
            /* todo-120: 结构体指针解引用必须区分两种 pointee 布局 ——
             *  `&`-借用 (cursor==0): 地址即 CWind blob 起始, 直接构造句柄;
             *  FFI 返回 (cursor==1, C 布局): 地址指向 C 内存, 经
             *    cg_ext_unflatten 重建为 CWind 实例 (否则按 CWind-blob
             *    布局读槽区/载荷会读到垃圾)。 */
            LLVMValueRef tag = LLVMBuildExtractValue(
                cg_b(g), e.handle, 2, "deref.tag");
            LLVMValueRef base = LLVMBuildIntToPtr(
                cg_b(g), addr, cg_rt_i8_ptr(g), "deref.st");
            LLVMValueRef tagcw = LLVMBuildICmp(
                cg_b(g), LLVMIntEQ, tag, cg_i64(g, 0), "deref.cw");

            LLVMBasicBlockRef cw_bb = LLVMAppendBasicBlockInContext(
                cg_ctx(g), g->current_fn, "deref.cw");
            LLVMBasicBlockRef c_bb = LLVMAppendBasicBlockInContext(
                cg_ctx(g), g->current_fn, "deref.c");
            LLVMBasicBlockRef m_bb = LLVMAppendBasicBlockInContext(
                cg_ctx(g), g->current_fn, "deref.m");
            LLVMBuildCondBr(cg_b(g), tagcw, cw_bb, c_bb);

            LLVMPositionBuilderAtEnd(cg_b(g), cw_bb);
            LLVMValueRef cw_h = cg_struct_handle(g, base, 0);
            LLVMBuildBr(cg_b(g), m_bb);

            LLVMPositionBuilderAtEnd(cg_b(g), c_bb);
            CwExpr u = cg_ext_unflatten(g, base, pointee);
            if (g->failed) return (CwExpr){ NULL, NULL };
            LLVMValueRef c_h = u.handle;
            LLVMBuildBr(cg_b(g), m_bb);

            LLVMPositionBuilderAtEnd(cg_b(g), m_bb);
            LLVMValueRef phi = LLVMBuildPhi(
                cg_b(g), g->ll->handle_type, "deref.h");
            LLVMAddIncoming(phi, &cw_h, &cw_bb, 1);
            LLVMAddIncoming(phi, &c_h, &c_bb, 1);
            return (CwExpr){ phi, pointee };
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
    LLVMValueRef rec8,
    int tid
) {
    LLVMValueRef slot = cg_alloca(
        g, LLVMInt64TypeInContext(cg_ctx(g)), "len");
    LLVMTypeRef pt[3] = { LLVMInt32TypeInContext(cg_ctx(g)),
                          cg_rt_i8_ptr(g),
                          LLVMPointerType(
                              LLVMVoidTypeInContext(cg_ctx(g)), 0) };
    LLVMValueRef f = cg_rt_declare(
        g, "cw_builtin_length", LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
    LLVMValueRef av[3] = { cg_i32(g, (uint32_t)tid), rec8, slot };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
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
    int ctid,
    CwExpr a
) {
    LLVMValueRef er = cg_cell_alloca(g, "cont.item");
    LLVMBuildStore(cg_b(g), a.handle, er);
    LLVMValueRef er8 = LLVMBuildBitCast(cg_b(g), er,
                                        cg_rt_i8_ptr(g), "");
    const int itid = cg_type_id(a.type_name);
    LLVMValueRef slot = cg_alloca(
        g, LLVMInt8TypeInContext(cg_ctx(g)), "found");
    LLVMTypeRef pt[5] = { LLVMInt32TypeInContext(cg_ctx(g)),
                          cg_rt_i8_ptr(g),
                          LLVMInt32TypeInContext(cg_ctx(g)),
                          cg_rt_i8_ptr(g),
                          LLVMPointerType(
                              LLVMVoidTypeInContext(cg_ctx(g)), 0) };
    LLVMValueRef f = cg_rt_declare(
        g, "cw_builtin_contains",
        LLVMInt1TypeInContext(cg_ctx(g)), pt, 5);
    LLVMValueRef av[5] = { cg_i32(g, (uint32_t)ctid), rec8,
                           cg_i32(g, (uint32_t)(itid >= 0 ? itid : 0)),
                           er8, slot };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 5, "");
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

/* 调 rt cw_builtin_parse_owned: String 值 -> 目标标量值 (out-cell 读回) */
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
    return LLVMBuildLoad2(cg_b(g), g->ll->handle_type, out, "ph");
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
    LLVMValueRef ar = cg_cell_alloca(g, "from.src");
    LLVMBuildStore(cg_b(g), a.handle, ar);
    LLVMValueRef ar8 = LLVMBuildBitCast(cg_b(g), ar, cg_rt_i8_ptr(g), "");
    LLVMValueRef out = cg_cell_alloca(g, "parse.out");
    LLVMValueRef h = cg_parse_owned_handle(g, ar8, tid, out);
    return (CwExpr){ h, target };
}

/* 调 rt cw_builtin_to_string_owned: (tag, 值) -> 新 String 值 */
static CwExpr cg_call_to_string_owned(
    CwCodegen_t* g, int tid, LLVMValueRef val8
) {
    LLVMValueRef out = cg_cell_alloca(g, "tos.out");
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                         cg_rt_i8_ptr(g), "");
    LLVMTypeRef pt[3] = { LLVMInt32TypeInContext(cg_ctx(g)),
                          cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
    LLVMValueRef f = cg_rt_declare(
        g, "cw_builtin_to_string_owned",
        LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
    LLVMValueRef av[3] = { cg_i32(g, (uint32_t)tid), val8, out8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
    return cg_out_value_read(g, out, "String");
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
    LLVMValueRef ar = cg_cell_alloca(g, "into.src");
    LLVMBuildStore(cg_b(g), a.handle, ar);
    LLVMValueRef ar8 = LLVMBuildBitCast(cg_b(g), ar, cg_rt_i8_ptr(g), "");
    const int rtid = cg_type_id(rt);
    if (strcmp(rt, "String") == 0) {
        const int tid = cg_type_id(tt);
        if (!tt || tid < 0 || !cg_is_scalar(tt)
            || strcmp(tt, "Bool") == 0) {
            cg_error_at(g, node,
                        "unsupported String conversion target: %s",
                        tt);
            return (CwExpr){ NULL, NULL };
        }
        LLVMValueRef out = cg_cell_alloca(g, "conv.out");
        LLVMValueRef h = cg_parse_owned_handle(g, ar8, tid, out);
        return (CwExpr){ h, tt };
    }
    if (strcmp(tt, "String") == 0) {
        return cg_call_to_string_owned(
            g, rtid >= 0 ? rtid : 0, ar8);
    }
    cg_error_at(g, node, "unsupported into() conversion: %s -> %s",
                rt, tt);
    return (CwExpr){ NULL, NULL };
}

/* builtins::print(value): 物化 CWCell (tag + 值) 后调 rt 打印 */
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
    /* 元数据分区: 类型 tag 由调用点静态提供 */
    const int tid = cg_type_id(a.type_name);
    LLVMValueRef vp = cg_cell_alloca(g, "print.val");
    LLVMBuildStore(cg_b(g), a.handle, vp);
    LLVMTypeRef pr[2] = { LLVMInt32TypeInContext(cg_ctx(g)),
                          cg_rt_i8_ptr(g) };
    LLVMValueRef fn = cg_rt_declare(
        g, "cw_builtin_print", LLVMInt1TypeInContext(cg_ctx(g)), pr, 2);
    LLVMValueRef argsv[2] = {
        cg_i32(g, (uint32_t)(tid >= 0 ? tid : 0)),
        LLVMBuildBitCast(cg_b(g), vp, cg_rt_i8_ptr(g), ""),
    };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, argsv, 2, "");
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
    const int tid = cg_type_id(a.type_name);
    LLVMValueRef cell = cg_cell_alloca(g, "type.src");
    LLVMBuildStore(cg_b(g), a.handle, cell);
    LLVMValueRef cell8 = LLVMBuildBitCast(cg_b(g), cell,
                                          cg_rt_i8_ptr(g), "");
    LLVMValueRef out = cg_cell_alloca(g, "type.out");
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                         cg_rt_i8_ptr(g), "");
    LLVMTypeRef pr[3] = { LLVMInt32TypeInContext(cg_ctx(g)),
                          cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
    LLVMValueRef fn = cg_rt_declare(
        g, "cw_builtin_type_of_owned",
        LLVMInt1TypeInContext(cg_ctx(g)), pr, 3);
    LLVMValueRef av[3] = { cg_i32(g, (uint32_t)(tid >= 0 ? tid : 0)),
                           cell8, out8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 3, "");
    return cg_out_value_read(g, out, "String");
}

/* builtins::readline(): 预置空 String 值兜底后调 rt 读一行 */
static CwExpr cg_builtin_readline(
    CwCodegen_t* g,
    const cw_value*node
) {
    (void)node;
    /* 失败兜底: 预置成空 String 值, rt 无论成败 out 都有效 */
    LLVMValueRef out = cg_cell_alloca(g, "read.out");
    LLVMBuildStore(cg_b(g), cg_null_handle(g), out);
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                         cg_rt_i8_ptr(g), "");
    LLVMTypeRef pr[1] = { cg_rt_i8_ptr(g) };
    LLVMValueRef fn = cg_rt_declare(
        g, "cw_builtin_readline", LLVMInt1TypeInContext(cg_ctx(g)),
        pr, 1);
    LLVMValueRef av[1] = { out8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 1, "");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, out,
                                    "vh");
    CwExpr e = { h, "String" };
    return e;
}

/* builtins::gc_collect(): 强制一整轮标记-清扫 (todo-35 调试/压测入口) */
static CwExpr cg_builtin_gc_collect(
    CwCodegen_t* g,
    const cw_value*node
) {
    (void)node;
    LLVMValueRef fn = cg_rt_declare(
        g, "cwgc_collect", LLVMVoidTypeInContext(cg_ctx(g)), NULL, 0);
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, NULL, 0, "");
    CwExpr none = { cg_null_handle(g), "None" };
    return none;
}

/* builtins::gc_alloc_bytes/gc_live_bytes/gc_pause_ns(): 零参观测,
 * rt 返回 u64, 按标量 ABI 装进句柄 */
static CwExpr cg_builtin_gc_u64(
    CwCodegen_t* g,
    const char* rt_name
) {
    LLVMTypeRef rt = LLVMInt64TypeInContext(cg_ctx(g));
    LLVMValueRef fn = cg_rt_declare(g, rt_name, rt, NULL, 0);
    LLVMValueRef v = LLVMBuildCall2(
        cg_b(g), LLVMGlobalGetValueType(fn), fn, NULL, 0, "gc.stat");
    return cg_make_scalar(g, v, LLVMInt64TypeInContext(cg_ctx(g)),
                          "UInt64", 8);
}

/* builtins::gc_enable(on): 运行时启停 GC (CWGC_DISABLE 的进程内副本) */
static CwExpr cg_builtin_gc_enable(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* arg0 = cg_call_arg0(node);
    if (!arg0) {
        cg_error(g, "gc_enable expects 1 argument");
        return (CwExpr){ NULL, NULL };
    }
    CwExpr a = cg_expr(g, cw_object_get(arg0, "value"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef v = cg_load_value(g, a,
                                   LLVMInt8TypeInContext(cg_ctx(g)));
    LLVMTypeRef pr[1] = { LLVMInt8TypeInContext(cg_ctx(g)) };
    LLVMValueRef fn = cg_rt_declare(
        g, "cwgc_set_enabled", LLVMVoidTypeInContext(cg_ctx(g)), pr, 1);
    LLVMValueRef av[1] = { v };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 1, "");
    CwExpr none = { cg_null_handle(g), "None" };
    return none;
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

/* 从调用点 ann.type 的泛型实参里取类型 tag (Vector<T>/Map<K,V>/Set<T>);
 * 注解缺失或非内置类型返回 0 (OPAQUE) */
static int cg_ann_arg_tag(
    CwCodegen_t* g, const cw_value* node, size_t idx
) {
    cw_value* t = cg_node_ann_type(node);
    cw_value* args = t ? cw_object_get(t, "args") : NULL;
    if (!args || cw_typeof(args) != CW_ARRAY
        || cw_array_size(args) <= idx) {
        return 0;
    }
    const char* n = cg_type_name_of(g, cw_array_get(args, idx));
    const int tid = n ? cg_type_id(n) : -1;
    return tid >= 0 ? tid : 0;
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
    /* ABI v2: init 收 CWValue + 元素类型 tag (元数据分区: data 头) */
    LLVMValueRef val = cg_cell_alloca(g, "new.val");
    LLVMBuildStore(cg_b(g), cg_null_handle(g), val);
    LLVMValueRef val8 = LLVMBuildBitCast(cg_b(g), val,
                                         cg_rt_i8_ptr(g), "");
    /* tag 优先级: let/assign 绑定类型上下文 > 调用点 ann.args > 0 */
    const int t0 = g->has_exp_tags
        ? g->exp_tags[0] : cg_ann_arg_tag(g, node, 0);
    const int t1 = g->has_exp_tags
        ? g->exp_tags[1] : cg_ann_arg_tag(g, node, 1);
    LLVMValueRef h = NULL;
    if (strcmp(init_fn, "cwvec_init") == 0) {
        LLVMTypeRef pr[3] = { cg_rt_i8_ptr(g),
                              LLVMInt32TypeInContext(cg_ctx(g)),
                              LLVMInt64TypeInContext(cg_ctx(g)) };
        LLVMValueRef fn = cg_rt_declare(
            g, init_fn, LLVMInt1TypeInContext(cg_ctx(g)), pr, 3);
        LLVMValueRef av[3] = {
            val8, cg_i32(g, (uint32_t)t0), cg_i64(g, 0),
        };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 3, "");
    } else if (strcmp(init_fn, "cwmap_init") == 0) {
        LLVMTypeRef pr[3] = { cg_rt_i8_ptr(g),
                              LLVMInt32TypeInContext(cg_ctx(g)),
                              LLVMInt32TypeInContext(cg_ctx(g)) };
        LLVMValueRef fn = cg_rt_declare(
            g, init_fn, LLVMInt1TypeInContext(cg_ctx(g)), pr, 3);
        LLVMValueRef av[3] = {
            val8, cg_i32(g, (uint32_t)t0), cg_i32(g, (uint32_t)t1),
        };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 3, "");
    } else {
        LLVMTypeRef pr[2] = { cg_rt_i8_ptr(g),
                              LLVMInt32TypeInContext(cg_ctx(g)) };
        LLVMValueRef fn = cg_rt_declare(
            g, init_fn, LLVMInt1TypeInContext(cg_ctx(g)), pr, 2);
        LLVMValueRef av[2] = { val8, cg_i32(g, (uint32_t)t0) };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn), fn, av, 2, "");
    }
    h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, val, "vh");
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

/* 无载荷枚举 (所有变体都不带字段) */
static bool cg_ext_is_fieldless_enum(
    CwCodegen_t* g, const char* tname
) {
    const CwNode_t* d = cg_enum_decl(g, tname);
    return d && cg_enum_max_payloads(d) == 0;
}

/* CWind 结构体句柄 -> C 值 (todo-52):
 * <=8 字节的打包整数按位镜像即 C 寄存器内容, 直接从实例 blob 的
 * C 视图区按目标宽度整块载入 (小端布局与 clang/gcc 的降级一致)。 */

/* 无载荷枚举句柄 -> i32 判别值 (tag = 变体序号) */
static LLVMValueRef cg_ext_enum_to_c(
    CwCodegen_t* g, CwExpr e
) {
    LLVMValueRef base = LLVMBuildIntToPtr(
        cg_b(g), cg_handle_addr(g, e), cg_rt_i8_ptr(g), "en.base");
    return LLVMBuildLoad2(cg_b(g), LLVMInt32TypeInContext(cg_ctx(g)),
                          cg_enum_tag_ptr(g, base), "en.tag");
}

/* 打包整数 -> CWind 结构体实例: 收到按位镜像后经连续镜像
 * 快照路径重组 blob (与 REGS/MEM 返回共用 unflatten 机制)。 */
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

/* ---- 真实 C 布局聚合 (todo-61/65/66) ----
 * 纯内联非泛型结构体 (字段 = 定宽标量 / 定长标量数组 / 内嵌纯内联
 * 结构体) 映射为真 LLVM 结构体类型, 传递约定按目标 ABI 分三档:
 *   - PACK: <=8 字节同宽全标量 -> 打包成单个整数按寄存器传递;
 *   - REGS: SysV 下 9~16 字节小聚合 -> 一等结构体实参/返回, LLVM 按
 *     psABI 拆寄存器对 (整数对走 RAX:RDX / SSE 混合自动分类),
 *     解除 todo-61 内存约定的平台差异限制 (todo-65);
 *   - MEM : 其余 (Win64 >8B / SysV >16B) -> byval 内存约定传指针,
 *     返回经 sret 隐式首参写回。
 * CWind 侧实例仍是 blob; 平铺聚合的载荷区即 C 视图的连续内存镜像
 * (字段声明序 + 自然对齐, 尾部补齐); 含嵌套字段 (todo-66) 时数据
 * 散落在子 blob 中, 跨边界前先扁平化到连续缓冲, 返回时再逆扁平化
 * 重建 blob 树。 */

/* 目标 ABI: cwindc 只为本机编译, 以编译期宏区分 Win64 / SysV */
static bool cg_ext_abi_sysv(
    void
) {
#if defined(_WIN32)
    return false;
#else
    return true;
#endif
}

#define CG_EXT_MAX_NEST 4

/* 纯内联结构体布局判定 (递归, todo-66):
 * 非泛型且全部字段为定宽标量 / 定长标量数组 / 内嵌纯内联结构体;
 * 命中返回其布局。depth 防御超深嵌套。 */
static bool cg_ext_pod_layout_d(
    CwCodegen_t* g, const char* tname,
    int depth, const CwLayout_t** out_L
) {
    if (!tname || strchr(tname, '<')) return false; /* 泛型实例 v0 拒绝 */
    if (depth > CG_EXT_MAX_NEST) return false;
    const CwNode_t* decl = cg_struct_decl(g, tname);
    if (!decl) return false;
    cw_value* tp = cw_object_get(decl->value, "params");
    if (tp && cw_typeof(tp) == CW_ARRAY && cw_array_size(tp) > 0) {
        return false;
    }
    const CwLayout_t* L = cwlayout_get(
        g->ll->layouts, g->m, decl, NULL, 0);
    if (!L || L->field_count == 0 || L->field_count > CG_EXT_MAX_FIELDS) {
        return false;
    }
    for (size_t i = 0; i < L->field_count; i++) {
        const char* ft = cg_ext_field_type_name(g, L, i);
        if (!ft) return false;
        if (cg_scalar_bytes(ft) > 0) continue;
        char elem[128];
        size_t n = 0;
        if (cg_array_info(ft, elem, sizeof(elem), &n)) {
            if (cg_scalar_bytes(elem) == 0) return false;
            continue;
        }
        /* 内嵌结构体: 必须同为纯内联 (递归判定) */
        if (!cg_is_struct_type(g, ft)) return false;
        if (!cg_ext_pod_layout_d(g, ft, depth + 1, NULL)) return false;
    }
    if (out_L) *out_L = L;
    return true;
}

static bool cg_ext_pod_layout(
    CwCodegen_t* g, const char* tname,
    const CwLayout_t** out_L
) {
    return cg_ext_pod_layout_d(g, tname, 0, out_L);
}

/* 是否含内嵌结构体字段 (决定跨边界时要不要扁平化拷贝) */
static bool cg_ext_pod_has_nested_d(
    CwCodegen_t* g, const CwLayout_t* L, int depth
) {
    for (size_t i = 0; i < L->field_count; i++) {
        const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
        if (!ft || cg_scalar_bytes(ft) > 0) continue;
        char elem[128];
        size_t n = 0;
        if (cg_array_info(ft, elem, sizeof(elem), &n)) continue;
        return true;
    }
    return false;
}

/* 单个字段在 C 视图中的对齐字节数: 标量取宽度, 数组取元素宽度,
 * 内嵌结构体取其自身对齐 (C 规则) */
static size_t cg_ext_leaf_align_d(
    CwCodegen_t* g, const char* ft, int depth
);

static size_t cg_ext_pod_align_d(
    CwCodegen_t* g, const CwLayout_t* L, int depth
) {
    size_t align = 1;
    for (size_t i = 0; i < L->field_count; i++) {
        const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
        const size_t a = cg_ext_leaf_align_d(g, ft, depth + 1);
        if (a > align) align = a;
    }
    return align;
}

static size_t cg_ext_leaf_align_d(
    CwCodegen_t* g, const char* ft, int depth
) {
    const size_t sz = cg_scalar_bytes(ft);
    if (sz > 0) return sz;
    char elem[128];
    size_t n = 0;
    if (cg_array_info(ft, elem, sizeof(elem), &n)) {
        const size_t esz = cg_scalar_bytes(elem);
        if (esz > 0) return esz;
    }
    const CwLayout_t* CL = NULL;
    if (depth <= CG_EXT_MAX_NEST
        && cg_ext_pod_layout_d(g, ft, depth, &CL)) {
        return cg_ext_pod_align_d(g, CL, depth);
    }
    return 1;
}

/* 单个字段在 C 视图中的尺寸: 标量宽度 / 数组总字节 /
 * 内嵌结构体的完整 C 布局大小 (含尾部补齐) */
static size_t cg_ext_leaf_size_d(
    CwCodegen_t* g, const char* ft, int depth
);

static size_t cg_ext_pod_size_d(
    CwCodegen_t* g, const CwLayout_t* L, int depth
) {
    size_t off = 0;
    for (size_t i = 0; i < L->field_count; i++) {
        const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
        off = cg_align_up(off, cg_ext_leaf_align_d(g, ft, depth + 1));
        off += cg_ext_leaf_size_d(g, ft, depth + 1);
    }
    return cg_align_up(off, cg_ext_pod_align_d(g, L, depth));
}

static size_t cg_ext_leaf_size_d(
    CwCodegen_t* g, const char* ft, int depth
) {
    const size_t sz = cg_scalar_bytes(ft);
    if (sz > 0) return sz;
    const size_t total = cg_array_total_bytes(ft);
    if (total > 0) return total;
    const CwLayout_t* CL = NULL;
    if (depth <= CG_EXT_MAX_NEST
        && cg_ext_pod_layout_d(g, ft, depth, &CL)) {
        return cg_ext_pod_size_d(g, CL, depth);
    }
    return 0;
}

/* blob 内 C 视图区的起始偏移 — ABI v2 (todo-50): 实例 blob 本身就是
 * C-Like 布局, C 视图区从偏移 0 开始 (槽位区已拆除) */
static size_t cg_ext_pod_region_start(
    CwCodegen_t* g, const CwLayout_t* L
) {
    (void)g;
    (void)L;
    return 0;
}

/* C 视图区内各字段偏移与总大小 (C 自然布局: 字段按自身对齐放置,
 * 尾部补齐到结构体对齐; 偏移相对视图区起点)。含嵌套时内嵌结构体
 * 字段占其完整 C 布局区间。 */
static void cg_ext_pod_geometry_d(
    CwCodegen_t* g, const CwLayout_t* L, int depth,
    size_t field_off[], size_t* out_size
) {
    size_t off = 0;
    for (size_t i = 0; i < L->field_count; i++) {
        const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
        off = cg_align_up(off, cg_ext_leaf_align_d(g, ft, depth + 1));
        if (field_off) field_off[i] = off;
        off += cg_ext_leaf_size_d(g, ft, depth + 1);
    }
    if (out_size) {
        *out_size = cg_align_up(off, cg_ext_pod_align_d(g, L, depth));
    }
}

/* POD 的 LLVM 结构体类型 (字段序 = 声明序, 内嵌结构体递归展开) */
static LLVMTypeRef cg_ext_pod_llvm_type_d(
    CwCodegen_t* g, const CwLayout_t* L, int depth
) {
    LLVMTypeRef elems[CG_EXT_MAX_FIELDS];
    for (size_t i = 0; i < L->field_count; i++) {
        const char* ft = cwtype_name(g->ll->types, L->fields[i].type);
        char elem[128];
        size_t n = 0;
        if (cg_scalar_bytes(ft) == 0 && cg_array_info(ft, elem,
                                                      sizeof(elem), &n)) {
            elems[i] = LLVMArrayType(
                cg_scalar_type(g, elem, NULL), (unsigned)n);
            continue;
        }
        if (cg_scalar_bytes(ft) == 0 && depth <= CG_EXT_MAX_NEST) {
            const CwLayout_t* CL = NULL;
            if (cg_ext_pod_layout_d(g, ft, depth + 1, &CL)) {
                elems[i] = cg_ext_pod_llvm_type_d(g, CL, depth + 1);
                continue;
            }
        }
        elems[i] = cg_scalar_type(g, ft, NULL);
    }
    return LLVMStructTypeInContext(cg_ctx(g), elems,
                                   (unsigned)L->field_count, false);
}

static LLVMTypeRef cg_ext_pod_llvm_type(
    CwCodegen_t* g, const CwLayout_t* L
) {
    return cg_ext_pod_llvm_type_d(g, L, 0);
}

/* 结构体实参/返回的传递约定判定:
 *  - Win64: 1/2/4/8 字节聚合 -> PACK (按位镜像进整数寄存器,
 *    与 clang 对小结构体的降级一致); 其余 -> MEM (byval/sret);
 *  - SysV : <=16 字节 -> REGS (一等结构体实参/返回, psABI 寄存器对,
 *    LLVM 按八字节组自动做 INTEGER/SSE 分类); 其余 -> MEM。
 * 非纯内联结构体返回 NONE。 */
static bool cg_ext_agg_classify(
    CwCodegen_t* g, const char* tname, CgAggInfo* out
) {
    memset(out, 0, sizeof(*out));
    out->mode = CG_AGG_NONE;
    const CwLayout_t* L = NULL;
    if (!cg_ext_pod_layout(g, tname, &L)) return false;
    out->L = L;
    out->align = cg_ext_pod_align_d(g, L, 0);
    out->size = cg_ext_pod_size_d(g, L, 0);
    out->nested = cg_ext_pod_has_nested_d(g, L, 0);
    if (cg_ext_abi_sysv()) {
        out->mode = (out->size > 0 && out->size <= 16)
            ? CG_AGG_REGS : CG_AGG_MEM;
    } else {
        out->mode = (out->size == 1 || out->size == 2
                     || out->size == 4 || out->size == 8)
            ? CG_AGG_PACK : CG_AGG_MEM;
    }
    return true;
}

/* PACK 聚合承载自身的 LLVM 整数类型 (按位镜像宽度) */
static LLVMTypeRef cg_ext_pack_int_ty(
    CwCodegen_t* g, const CgAggInfo* info
) {
    return LLVMIntTypeInContext(cg_ctx(g),
                                (unsigned)(info->size * 8));
}

/* 实参的连续 C 镜像指针: 平铺聚合直接指进 blob 的 C 视图区
 * (零拷贝), 含嵌套字段时先把散落的子 blob 数据扁平化进栈缓冲。
 * 返回 i8*。失败时上报错误并返回 NULL。 */
static LLVMValueRef cg_ext_agg_image_ptr(
    CwCodegen_t* g, CwExpr e, const CgAggInfo* info
);

/* 连续 C 镜像 -> 实例 blob (含嵌套子 blob 重建):
 * 逐字段把镜像数据写进标准 blob 的通用载荷区 (标量/数组生成自指
 * 句柄, 内嵌结构体字段递归构造子 blob 后存其句柄)。产物与普通
 * 结构体字面量构造的实例布局完全一致, 从而兼容深拷贝后的
 * 句柄重定向 (cg_rebase_struct_fields)。 */
static CwExpr cg_ext_unflatten_d(
    CwCodegen_t* g, LLVMValueRef src_i8, const char* tname,
    int depth
) {
    (void)depth;
    const CwLayout_t* L = NULL;
    if (!cg_ext_pod_layout_d(g, tname, 0, &L)) {
        cg_error(g, "extern aggregate has an unsupported layout: %s",
                 tname ? tname : "?");
        return (CwExpr){ NULL, NULL };
    }
    /* ABI v2 (todo-50): 实例 blob 即 C-Like 布局, 整块镜像 */
    const size_t blob_size = L->size;
    LLVMValueRef blob = cg_blob_alloc(g, blob_size, "ext.img");
    LLVMValueRef base = cg_blob_i8(g, blob);
    LLVMBuildMemCpy(cg_b(g), base, 1, src_i8, 1,
                    cg_i64(g, (uint64_t)blob_size));
    return (CwExpr){ cg_struct_handle(g, blob, L->size), tname };
}

static CwExpr cg_ext_unflatten(
    CwCodegen_t* g, LLVMValueRef src_i8, const char* tname
) {
    return cg_ext_unflatten_d(g, src_i8, tname, 0);
}

/* 句柄值 (LLVMValueRef) 的 address 字段 */
static LLVMValueRef cg_ext_haddr(
    CwCodegen_t* g, LLVMValueRef h
) {
    return LLVMBuildExtractValue(cg_b(g), h, 0, "h.addr");
}

/* 把实例 blob 的载荷按 C 布局写进 dst (dst 指向镜像区起点)。
 * 标量/数组直接读槽位句柄指向的载荷数据, 内嵌结构体字段
 * 顺着子 blob 句柄递归展开。 */
static void cg_ext_flatten_fields(
    CwCodegen_t* g, LLVMValueRef src_base_i8,
    const CwLayout_t* L, int depth,
    LLVMValueRef dst_i8
) {
    (void)depth;
    /* ABI v2 (todo-50): 实例 blob 即 C-Like 布局, 整块镜像 */
    LLVMBuildMemCpy(cg_b(g), dst_i8, 1, src_base_i8, 1,
                    cg_i64(g, (uint64_t)L->size));
}


/* 实参的连续 C 镜像指针 */
static LLVMValueRef cg_ext_agg_image_ptr(
    CwCodegen_t* g, CwExpr e, const CgAggInfo* info
) {
    LLVMValueRef base = LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, e),
                                          cg_rt_i8_ptr(g), "agg.base");
    if (!info->nested) {
        LLVMValueRef off[1] = {
            cg_i64(g, (uint64_t)cg_ext_pod_region_start(g, info->L))
        };
        return LLVMBuildGEP2(cg_b(g), LLVMInt8TypeInContext(cg_ctx(g)),
                             base, off, 1, "agg.img");
    }
    /* 含嵌套: 先扁平化到对齐栈缓冲 */
    LLVMValueRef buf = cg_alloca(g, LLVMArrayType(
        LLVMInt8TypeInContext(cg_ctx(g)),
        (unsigned)(info->size ? info->size : 1)), "agg.flat");
    LLVMValueRef b8 = cg_blob_i8(g, buf);
    cg_ext_flatten_fields(g, base, info->L, 0, b8);
    return b8;
}

/* ---- todo-59: 结构体指针参数 (*const S / *mut S) ----
 * 被指类型为纯内联结构体时按真实地址跨越边界: 调用前把实例 blob
 * 扁平化进按 C 布局对齐的临时缓冲 (解除按值 ≤8B/16B 限制),
 * *mut 方向在调用后把 C 的写回内容拷回实例 (accept 出参模式)。 */

/* 提取原始指针的被指类型; out_mut 报告是否 *mut */
static bool cg_ext_pointee_of(
    const char* tname, char* buf, size_t cap, bool* out_mut
) {
    if (!tname) return false;
    const char* rest = NULL;
    if (strncmp(tname, "*const ", 7) == 0) {
        rest = tname + 7;
        if (out_mut) *out_mut = false;
    } else if (strncmp(tname, "*mut ", 5) == 0) {
        rest = tname + 5;
        if (out_mut) *out_mut = true;
    } else {
        return false;
    }
    if (strlen(rest) >= cap) return false;
    memcpy(buf, rest, strlen(rest) + 1);
    return true;
}

/* 被指类型是纯内联结构体的指针 (todo-59) */
static bool cg_ext_is_aggptr(
    CwCodegen_t* g, const char* tname
) {
    char pt[128];
    if (!cg_ext_pointee_of(tname, pt, sizeof(pt), NULL)) return false;
    return cg_is_struct_type(g, pt) && cg_ext_pod_layout(g, pt, NULL);
}

/* 按字节大小分配 8 字节对齐的栈缓冲, 返回 i8* */
static LLVMValueRef cg_ext_aligned_buf(
    CwCodegen_t* g, size_t bytes
) {
    const size_t nq = (bytes + 7) / 8;
    LLVMValueRef buf = cg_alloca(g,
        LLVMArrayType(LLVMInt64TypeInContext(cg_ctx(g)),
                      (unsigned)(nq ? nq : 1)), "ext.buf");
    return cg_blob_i8(g, buf);
}

/* ---- todo-89: 带载荷枚举的 FFI 表示 ---- */

/* 解析枚举的共享载荷表并计算 C 视图几何; 仅带载荷变体存在时成功 */
static bool cg_ext_enum_abi(
    CwCodegen_t* g, const char* tname, CgEnumAbi* out
) {
    memset(out, 0, sizeof(*out));
    if (!tname || strchr(tname, '<')) return false; /* 泛型拒绝 */
    const CwNode_t* decl = cg_enum_decl(g, tname);
    if (!decl || cg_enum_max_payloads(decl) == 0) return false;
    cw_value* variants = cw_object_get(decl->value, "variants");
    if (!variants || cw_typeof(variants) != CW_ARRAY) return false;
    out->nvariants = cw_array_size(variants);
    /* 取第一个带载荷变体的字段表 (SA 已验证全体一致) */
    for (size_t i = 0; i < cw_array_size(variants); i++) {
        cw_value* v = cw_array_get(variants, i);
        cw_value* fields = v ? cw_object_get(v, "fields") : NULL;
        const size_t nf = (fields && cw_typeof(fields) == CW_ARRAY)
            ? cw_array_size(fields) : 0;
        if (i < 64 && nf > 0) out->pay_mask[i] = true;
        if (nf == 0) continue;
        if (nf > CG_EXT_ENUM_MAX_FIELDS) return false;
        if (out->nfields > 0) continue; /* 字段表只取第一份 */
        out->nfields = nf;
        size_t align = 4;
        for (size_t j = 0; j < nf; j++) {
            cw_value* ftv = cw_array_get(fields, j);
            const char* ft = (ftv && cw_typeof(ftv) == CW_OBJECT)
                ? cg_type_name_of(g, ftv) : NULL;
            if (!ft || strlen(ft) >= 128) return false;
            snprintf(out->ftypes[j], 128, "%s", ft);
            const size_t fa = cg_ext_leaf_align_d(g, ft, 1);
            if (fa > align) align = fa;
        }
        size_t off = cg_align_up(4, align);
        out->pay_off = off;
        for (size_t j = 0; j < nf; j++) {
            off = cg_align_up(off, cg_ext_leaf_align_d(g,
                                                       out->ftypes[j], 1));
            out->foff[j] = off;
            off += cg_ext_leaf_size_d(g, out->ftypes[j], 1);
        }
        out->total = cg_align_up(off, align);
    }
    return out->nfields > 0;
}

/* tag 是否指向带载荷变体 (运行时 OR 链); 无掩码信息时恒真 */
static LLVMValueRef cg_ext_enum_tag_has_payload(
    CwCodegen_t* g, LLVMValueRef tag, const CgEnumAbi* ai
) {
    LLVMTypeRef i32t = LLVMInt32TypeInContext(cg_ctx(g));
    LLVMValueRef cond = NULL;
    for (size_t i = 0; i < ai->nvariants && i < 64; i++) {
        if (!ai->pay_mask[i]) continue;
        LLVMValueRef m = LLVMBuildICmp(cg_b(g), LLVMIntEQ, tag,
                                       LLVMConstInt(i32t, i, false),
                                       "en.pay.m");
        cond = cond ? LLVMBuildOr(cg_b(g), cond, m, "en.pay.or") : m;
    }
    if (!cond) return LLVMConstInt(LLVMInt1TypeInContext(cg_ctx(g)),
                                   1, false);
    return cond;
}

/* 枚举 C 视图的 LLVM 结构体类型: tag + 显式补齐 + 字段叶类型,
 * 与 C 镜像声明逐字节一致 */
static LLVMTypeRef cg_ext_enum_llvm_type(
    CwCodegen_t* g, const CgEnumAbi* ai
) {
    LLVMTypeRef elems[CG_EXT_ENUM_MAX_FIELDS * 2 + 3];
    size_t n = 0;
    elems[n++] = LLVMInt32TypeInContext(cg_ctx(g));
    size_t prev_end = 4;
    for (size_t i = 0; i < ai->nfields; i++) {
        const char* ft = ai->ftypes[i];
        if (ai->foff[i] > prev_end) {
            elems[n++] = LLVMArrayType(
                LLVMInt8TypeInContext(cg_ctx(g)),
                (unsigned)(ai->foff[i] - prev_end));
        }
        char elem[128];
        size_t an = 0;
        if (cg_scalar_bytes(ft) > 0) {
            elems[n++] = cg_scalar_type(g, ft, NULL);
        } else if (cg_array_info(ft, elem, sizeof(elem), &an)) {
            elems[n++] = LLVMArrayType(
                cg_scalar_type(g, elem, NULL), (unsigned)an);
        } else {
            const CwLayout_t* CL = NULL;
            if (!cg_ext_pod_layout_d(g, ft, 1, &CL)) return NULL;
            elems[n++] = cg_ext_pod_llvm_type_d(g, CL, 1);
        }
        prev_end = ai->foff[i] + cg_ext_leaf_size_d(g, ft, 1);
    }
    if (ai->total > prev_end) {
        elems[n++] = LLVMArrayType(
            LLVMInt8TypeInContext(cg_ctx(g)),
            (unsigned)(ai->total - prev_end));
    }
    return LLVMStructTypeInContext(cg_ctx(g), elems,
                                   (unsigned)n, false);
}

/* 实例句柄 -> C 视图 (tag + 载荷字段逐个从槽位句柄搬运;
 * 缓冲先清零, 无载荷变体只搬 tag, 避免读未初始化槽位) */
static void cg_ext_enum_to_c_view(
    CwCodegen_t* g, CwExpr e, const CgEnumAbi* ai, LLVMValueRef dst8
) {
    LLVMBuildMemSet(cg_b(g), dst8, cg_i8(g, 0),
                    cg_i64(g, (uint64_t)(ai->total ? ai->total : 1)), 1);
    LLVMValueRef base = LLVMBuildIntToPtr(
        cg_b(g), cg_handle_addr(g, e), cg_rt_i8_ptr(g), "en.base");
    LLVMValueRef tag = LLVMBuildLoad2(cg_b(g),
        LLVMInt32TypeInContext(cg_ctx(g)),
        cg_enum_tag_ptr(g, base), "en.tag");
    LLVMBuildStore(cg_b(g), tag, LLVMBuildBitCast(cg_b(g), dst8,
        LLVMPointerType(LLVMInt32TypeInContext(cg_ctx(g)), 0), ""));
    LLVMBasicBlockRef saved_block = LLVMGetInsertBlock(cg_b(g));
    LLVMBasicBlockRef pay_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "en.pay");
    LLVMBasicBlockRef done_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "en.done");
    LLVMBuildCondBr(cg_b(g),
                    cg_ext_enum_tag_has_payload(g, tag, ai),
                    pay_bb, done_bb);
    LLVMPositionBuilderAtEnd(cg_b(g), pay_bb);
    for (size_t i = 0; i < ai->nfields; i++) {
        const char* ft = ai->ftypes[i];
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                        cg_enum_slot(g, base, i), "en.h");
        LLVMValueRef src = LLVMBuildIntToPtr(cg_b(g),
            LLVMBuildExtractValue(cg_b(g), h, 0, "en.addr"),
            cg_rt_i8_ptr(g), "en.src");
        LLVMValueRef off[1] = { cg_i64(g, (uint64_t)ai->foff[i]) };
        LLVMValueRef dp = LLVMBuildGEP2(cg_b(g),
            LLVMInt8TypeInContext(cg_ctx(g)), dst8, off, 1, "en.d");
        char elem[128];
        size_t an = 0;
        if (cg_scalar_bytes(ft) > 0) {
            LLVMBuildMemCpy(cg_b(g), dp, 1, src, 1,
                            cg_i64(g, (uint64_t)cg_scalar_bytes(ft)));
            continue;
        }
        if (cg_array_info(ft, elem, sizeof(elem), &an)) {
            LLVMBuildMemCpy(cg_b(g), dp, 1, src, 1,
                            cg_i64(g,
                                   (uint64_t)cg_array_total_bytes(ft)));
            continue;
        }
        const CwLayout_t* CL = NULL;
        if (!cg_ext_pod_layout_d(g, ft, 0, &CL)) {
            cg_error(g, "extern enum payload struct is unsupported: %s",
                     ft);
            return;
        }
        cg_ext_flatten_fields(g, src, CL, 0, dp);
    }
    LLVMBuildBr(cg_b(g), done_bb);
    LLVMPositionBuilderAtEnd(cg_b(g), done_bb);
}

/* C 视图 -> 枚举实例 (载荷标量/数组进 arena 单元, 结构体重建子 blob;
 * 无载荷变体的槽位保持清零) */
static CwExpr cg_ext_enum_from_c_view(
    CwCodegen_t* g, LLVMValueRef src8, const char* ename,
    const CgEnumAbi* ai
) {
    const size_t bsz = cg_enum_blob_size(g, ename);
    LLVMValueRef blob = cg_blob_alloc(g, bsz, "ext.en.blob");
    LLVMValueRef base = cg_blob_i8(g, blob);
    LLVMBuildMemSet(cg_b(g), base, cg_i8(g, 0),
                    cg_i64(g, (uint64_t)bsz), 1);
    LLVMValueRef tag = LLVMBuildLoad2(cg_b(g),
        LLVMInt32TypeInContext(cg_ctx(g)),
        LLVMBuildBitCast(cg_b(g), src8,
            LLVMPointerType(LLVMInt32TypeInContext(cg_ctx(g)), 0), ""),
        "en.tag");
    LLVMBuildStore(cg_b(g), tag, cg_enum_tag_ptr(g, base));
    LLVMBasicBlockRef saved_block = LLVMGetInsertBlock(cg_b(g));
    LLVMBasicBlockRef pay_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "en.pay");
    LLVMBasicBlockRef done_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "en.done");
    LLVMBuildCondBr(cg_b(g),
                    cg_ext_enum_tag_has_payload(g, tag, ai),
                    pay_bb, done_bb);
    LLVMPositionBuilderAtEnd(cg_b(g), pay_bb);
    for (size_t i = 0; i < ai->nfields; i++) {
        const char* ft = ai->ftypes[i];
        LLVMValueRef off[1] = { cg_i64(g, (uint64_t)ai->foff[i]) };
        LLVMValueRef sp = LLVMBuildGEP2(cg_b(g),
            LLVMInt8TypeInContext(cg_ctx(g)), src8, off, 1, "en.s");
        LLVMValueRef h = NULL;
        char elem[128];
        size_t an = 0;
        const size_t w = cg_scalar_bytes(ft);
        if (w > 0) {
            LLVMTypeRef vt = cg_scalar_type(g, ft, NULL);
            LLVMValueRef cell = cg_rt_arena_alloc(g,
                                                  cg_i64(g, (uint64_t)w));
            LLVMValueRef cp = LLVMBuildIntToPtr(cg_b(g), cell,
                                                cg_rt_i8_ptr(g), "en.cp");
            LLVMValueRef v = LLVMBuildLoad2(cg_b(g), vt,
                LLVMBuildBitCast(cg_b(g), sp, LLVMPointerType(vt, 0),
                                 ""), "en.v");
            LLVMBuildStore(cg_b(g), v, LLVMBuildBitCast(cg_b(g), cp,
                                LLVMPointerType(vt, 0), ""));
            h = cg_build_value(g, LLVMBuildPtrToInt(
                                    cg_b(g), cp, LLVMInt64TypeInContext(
                                        cg_ctx(g)), "en.cell.addr"),
                                cg_i64(g, (uint64_t)w), cg_i64(g, 0));
        } else if (cg_array_info(ft, elem, sizeof(elem), &an)) {
            const size_t tb = cg_array_total_bytes(ft);
            LLVMValueRef cell = cg_rt_arena_alloc(g,
                                                  cg_i64(g,
                                                         (uint64_t)tb));
            LLVMValueRef cp = LLVMBuildIntToPtr(cg_b(g), cell,
                                                cg_rt_i8_ptr(g), "en.cp");
            LLVMBuildMemCpy(cg_b(g), cp, 1, sp, 1,
                            cg_i64(g, (uint64_t)tb));
            h = cg_build_value(g, LLVMBuildPtrToInt(
                                    cg_b(g), cp, LLVMInt64TypeInContext(
                                        cg_ctx(g)), "en.cell.addr"),
                                cg_i64(g, (uint64_t)an), cg_i64(g, 0));
        } else {
            const CwLayout_t* CL = NULL;
            if (!cg_ext_pod_layout_d(g, ft, 0, &CL)) {
                cg_error(g,
                         "extern enum payload struct is unsupported: %s",
                         ft);
                return (CwExpr){ NULL, NULL };
            }
            CwExpr child = cg_ext_unflatten_d(g, sp, ft, 0);
            if (g->failed) return (CwExpr){ NULL, NULL };
            const size_t cbsz = cg_struct_blob_size(g, CL);
            LLVMValueRef cell = cg_rt_arena_alloc(g,
                                                  cg_i64(g,
                                                         (uint64_t)cbsz));
            LLVMValueRef cp = LLVMBuildIntToPtr(cg_b(g), cell,
                                                cg_rt_i8_ptr(g), "en.cp");
            LLVMBuildMemCpy(cg_b(g), cp, 1, cg_expr_blob_i8(g, child),
                            1, cg_i64(g, (uint64_t)cbsz));
            h = cg_build_value(g, LLVMBuildPtrToInt(
                                    cg_b(g), cp, LLVMInt64TypeInContext(
                                        cg_ctx(g)), "en.cell.addr"),
                                cg_i64(g, (uint64_t)cbsz),
                                cg_i64(g, 0));
        }
        LLVMBuildStore(cg_b(g), h, cg_enum_slot(g, base, i));
    }
    LLVMBuildBr(cg_b(g), done_bb);
    LLVMPositionBuilderAtEnd(cg_b(g), done_bb);
    return (CwExpr){ cg_enum_handle(g, blob, ename), ename };
}

/* todo-59 写回: 把 C 视图缓冲的数据拷回实例 blob (含嵌套子 blob 递归)。
 * 标量/数组直接覆盖载荷区字节 (自指槽位句柄地址不变); 内嵌结构体
 * 字段顺着槽位句柄找到子 blob 后递归。 */
static void cg_ext_copyback_fields(
    CwCodegen_t* g, LLVMValueRef src8,
    LLVMValueRef dst_base8,
    const CwLayout_t* L, int depth
) {
    (void)depth;
    /* ABI v2 (todo-50): blob 即 C 布局, 写回 = 整块镜像 */
    LLVMBuildMemCpy(cg_b(g), dst_base8, 1, src8, 1,
                    cg_i64(g, (uint64_t)L->size));
}


/* todo-88: Option<String> 判定 (仅精确形态 Option< String >) */
static bool cg_ext_is_optstring(const char* tname) {
    if (!tname || strncmp(tname, "Option<", 7) != 0) return false;
    const size_t len = strlen(tname);
    if (len < 8 || tname[len - 1] != '>') return false;
    const char* inner = tname + 7;
    while (*inner == ' ') inner++;
    const char* end = tname + len - 1;
    while (end > inner && *(end - 1) == ' ') end--;
    return (size_t)(end - inner) == 6
        && strncmp(inner, "String", 6) == 0;
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
    /* todo-88: Option<String> 返回映射到可空 char*
     * (NULL -> None, 否则 Some(String)) */
    if (cg_ext_is_optstring(tname)) {
        return LLVMPointerType(LLVMInt8TypeInContext(cg_ctx(g)), 0);
    }
    /* todo-67: [T; N] 作形参时遵循 C 数组退化语义 -> T* 指针 */
    char decay_elem[128];
    size_t decay_n = 0;
    if (cg_array_info(tname, decay_elem, sizeof(decay_elem), &decay_n)) {
        LLVMTypeRef et = cg_scalar_type(g, decay_elem, NULL);
        return et ? LLVMPointerType(et, 0) : NULL;
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
        /* 聚合: PACK 打包整数 / REGS+MEM 真 C 布局结构体 */
        CgAggInfo ai;
        if (cg_ext_agg_classify(g, tname, &ai)) {
            if (ai.mode == CG_AGG_PACK) return cg_ext_pack_int_ty(g, &ai);
            return cg_ext_pod_llvm_type(g, ai.L);
        }
        return NULL;
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
 *  - 读 = load 后按类型包装: 标量直接成值; *const / *mut 与 String
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

/* 解析 ExternStatic 节点的符号名与声明类型;
 * todo-62: 节点带 "link_name" 时 C 符号名以它为准 */
static bool cg_extern_static_info(
    CwCodegen_t* g, const CwNode_t* n,
    const char** out_name, const char** out_type, bool* out_mutable
) {
    cw_value* nv = cw_object_get(n->value, "link_name");
    if (!nv || cw_typeof(nv) != CW_STRING) {
        nv = cw_object_get(n->value, "name");
    }
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
    /* todo-61/65/66: 纯内联聚合 -> 快照成 CWind 实例
     * (全局内存即 C 视图镜像; 含嵌套字段时逆扁平化重建子 blob) */
    CgAggInfo ai;
    if (cg_is_struct_type(g, tname)
        && cg_ext_agg_classify(g, tname, &ai)) {
        if (ai.mode == CG_AGG_PACK) {
            /* 打包整数全局: 载入后经镜像快照重组 blob */
            LLVMTypeRef ity = cg_ext_pack_int_ty(g, &ai);
            LLVMValueRef v = LLVMBuildLoad2(cg_b(g), ity, gv,
                                            "ext.stat.pack");
            LLVMValueRef slot = cg_alloca(g, ity, "ext.stat.pslot");
            LLVMBuildStore(cg_b(g), v, slot);
            LLVMValueRef src = cg_blob_i8(g, slot);
            return cg_ext_unflatten(g, src, tname);
        }
        LLVMValueRef src = LLVMBuildBitCast(cg_b(g), gv,
                                            cg_rt_i8_ptr(g),
                                            "ext.stat.pod");
        return cg_ext_unflatten(g, src, tname);
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
            cg_build_value(g, addr, len, cg_i64(g, 0)),
            "String",
        };
    }
    return (CwExpr){
        cg_build_value(g, addr, cg_i64(g, 0),
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
    /* todo-61/65/66: 纯内联聚合写回 (整块镜像拷贝 / 嵌套扁平化) */
    CgAggInfo ai;
    if (cg_is_struct_type(g, tname)
        && cg_ext_agg_classify(g, tname, &ai) && strcmp(op, "=") == 0) {
        LLVMValueRef base = LLVMBuildIntToPtr(cg_b(g),
                                              cg_handle_addr(g, val),
                                              cg_rt_i8_ptr(g), "pod.base");
        if (ai.mode == CG_AGG_PACK) {
            /* 打包整数全局: 镜像进栈缓冲后按位存回 */
            LLVMTypeRef ity = cg_ext_pack_int_ty(g, &ai);
            LLVMValueRef slot = cg_alloca(g, ity, "ext.stat.pslot");
            LLVMValueRef s8 = cg_blob_i8(g, slot);
            if (ai.nested) {
                cg_ext_flatten_fields(g, base, ai.L, 0, s8);
            } else {
                LLVMValueRef off[1] = { cg_i64(g,
                    (uint64_t)cg_ext_pod_region_start(g, ai.L)) };
                LLVMValueRef src = LLVMBuildGEP2(cg_b(g),
                    LLVMInt8TypeInContext(cg_ctx(g)), base, off, 1,
                    "pod.src");
                LLVMBuildMemCpy(cg_b(g), s8, 1, src, 1,
                                cg_i64(g, (uint64_t)ai.size));
            }
            LLVMValueRef v = LLVMBuildLoad2(cg_b(g), ity, slot,
                                            "ext.stat.pack");
            LLVMBuildStore(cg_b(g), v, gv);
            return true;
        }
        if (!ai.nested) {
            size_t foff[CG_EXT_MAX_FIELDS];
            size_t size = 0;
            const size_t start = cg_ext_pod_region_start(g, ai.L);
            cg_ext_pod_geometry_d(g, ai.L, 0, foff, &size);
            LLVMValueRef off[1] = { cg_i64(g, (uint64_t)start) };
            LLVMValueRef src = LLVMBuildGEP2(cg_b(g),
                                             LLVMInt8TypeInContext(cg_ctx(g)),
                                             base, off, 1, "pod.src");
            LLVMBuildMemCpy(cg_b(g), gv, 1, src, 1,
                            cg_i64(g, (uint64_t)size));
            return true;
        }
        /* todo-66: 含嵌套字段 -> 扁平化子 blob 树到全局内存 */
        cg_ext_flatten_fields(g, base, ai.L, 0, LLVMBuildBitCast(
            cg_b(g), gv, cg_rt_i8_ptr(g), "ext.stat.gv"));
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

/* C 值 -> 句柄; 标量结果经全局缓冲中转 (buf_name),
 * 与普通调用返回同一纪律: 调用点立即 fixup 拷出。
 * (聚合返回已由 thunk 的镜像快照路径处理, 不会走到这里) */
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
            cg_build_value(g, addr, len, cg_i64(g, 0)),
            "String",
        };
    }
    if (cg_is_rawptr(tname)) {
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), v, LLVMInt64TypeInContext(cg_ctx(g)), "th.addr");
        return (CwExpr){
            cg_build_value(g, addr, cg_i64(g, 0),
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
        cg_build_value(g, addr, cg_i64(g, sz),
                        cg_i64(g, 0)),
        tname,
    };
}

/* todo-61: 构造类型属性 (byval/sret 等带类型的 ABI 标记) */
static LLVMAttributeRef cg_ext_type_attr(
    CwCodegen_t* g, const char* kind, LLVMTypeRef ty
) {
    const unsigned id = LLVMGetEnumAttributeKindForName(
        kind, strlen(kind));
    return LLVMCreateTypeAttribute(cg_ctx(g), id, ty);
}

/* todo-61/65/67/88/89 + todo-59: extern 函数完整 C 签名构造
 * (cg_call_extern 与 cg_extern_ensure_declared 共用, 保证两处 ABI 一致):
 *  - PACK 聚合 (<=8B 同宽全标量): 打包整数按寄存器传递;
 *  - REGS 聚合 (SysV 9~16B): 一等结构体实参 / 返回值;
 *  - MEM 聚合: byval 内存约定传指针, 返回经 sret 隐式首参写回
 *    (LLVM 17+ 不透明指针下形参声明为 ptr, 类型由属性承载);
 *  - [T; N] 形参 (todo-67): C 数组退化语义 -> T* 指针;
 *  - *const S / *mut S 结构体指针形参 (todo-59): 按地址传递
 *    (ptragg[i] 标记, 调用点负责临时缓冲扁平化与 *mut 写回);
 *  - 带载荷枚举 (todo-89): 一律内存约定, byval 形参 / sret 返回
 *    (enump[i] 与 out_ret_penum/out_ret_enum_ai 标记);
 *  - Option<String> 返回 (todo-88): 可空 char*, 无特殊标记。
 * want[] 为各参数的 CWind 类型名; pt[] 输出最终形参类型;
 * byval[i]/regs[i]/decay[i]/ptragg[i]/ptrwback[i]/enump[i] 输出
 * 第 i 个参数的传递方式; podL[i] 输出 MEM 参数的布局;
 * enumL[i] 输出带载荷枚举参数的 ABI 信息; out_sret_ty 输出 sret
 * 结构体类型 (无则 NULL); out_ret_pod/out_ret_regs/out_ret_penum
 * 输出返回值聚合信息。 */
static bool cg_ext_build_signature(
    CwCodegen_t* g,
    const char* mangled,
    const char* const* want, size_t n,
    const char* ret_name, bool ret_void,
    bool* byval, const CwLayout_t** podL,
    bool* regs, bool* decay,
    bool* ptragg, bool* ptrwback,
    bool* enump, CgEnumAbi** enumL,
    LLVMTypeRef* pt,
    LLVMTypeRef* out_sret_ty,
    const CwLayout_t** out_ret_pod,
    bool* out_ret_regs,
    bool* out_ret_penum, CgEnumAbi* out_ret_enum_ai,
    bool is_var_arg,
    LLVMTypeRef* out_fty
) {
    if (out_sret_ty) *out_sret_ty = NULL;
    if (out_ret_pod) *out_ret_pod = NULL;
    if (out_ret_regs) *out_ret_regs = false;
    if (out_ret_penum) *out_ret_penum = false;
    /* 返回值分类 */
    LLVMTypeRef ret_agg_ty = NULL;
    const CwLayout_t* ret_pod = NULL;
    bool ret_is_regs = false;
    bool ret_is_penum = false;
    CgEnumAbi ret_enum_ai;
    memset(&ret_enum_ai, 0, sizeof(ret_enum_ai));
    const bool ret_is_array = !ret_void && ret_name
        && cg_is_array_type(ret_name);
    if (!ret_void && ret_name && !ret_is_array) {
        /* todo-89: 带载荷枚举返回 -> sret 内存约定 */
        if (cg_is_enum_type(g, ret_name)
            && cg_ext_enum_abi(g, ret_name, &ret_enum_ai)) {
            ret_is_penum = true;
        } else {
            CgAggInfo ai;
            if (cg_ext_agg_classify(g, ret_name, &ai)) {
                if (ai.mode == CG_AGG_REGS) {
                    ret_agg_ty = cg_ext_pod_llvm_type(g, ai.L);
                    ret_is_regs = true;
                } else if (ai.mode == CG_AGG_MEM) {
                    ret_pod = ai.L;
                }
                /* PACK 返回走普通寄存器返回值, 无需特殊标记 */
            } else if (!cg_extern_llvm_type(g, ret_name)) {
                cg_error(g, "extern function %s has an unsupported return type: %s",
                         mangled, ret_name);
                return false;
            }
        }
    }
    if (ret_is_array) {
        /* todo-67: C 中数组不能按值返回, CWind 同样拒绝 */
        cg_error(g, "extern function %s cannot return an array "
                    "(C decay applies to parameters only)", mangled);
        return false;
    }
    size_t pc = n;
    if (ret_pod || ret_is_penum) pc = n + 1;
    for (size_t i = 0; i < n; i++) {
        byval[i] = false;
        podL[i] = NULL;
        regs[i] = false;
        decay[i] = false;
        if (ptragg) ptragg[i] = false;
        if (ptrwback) ptrwback[i] = false;
        if (enump) enump[i] = false;
        if (enumL) enumL[i] = NULL;
        /* todo-67: [T; N] 形参退化为 T* 指针 */
        char elem[128];
        size_t an = 0;
        if (want[i] && cg_array_info(want[i], elem, sizeof(elem), &an)) {
            LLVMTypeRef et = cg_scalar_type(g, elem, NULL);
            if (!et) {
                cg_error(g, "extern function %s has an unsupported array "
                            "parameter type: %s", mangled, want[i]);
                return false;
            }
            decay[i] = true;
            pt[i] = LLVMPointerType(et, 0);
            continue;
        }
        /* todo-59: *const S / *mut S -> 真实地址传递 */
        if (want[i] && cg_ext_is_aggptr(g, want[i])) {
            ptragg[i] = true;
            if (ptrwback) {
                bool m = false;
                cg_ext_pointee_of(want[i], elem, sizeof(elem), &m);
                ptrwback[i] = m;
            }
            pt[i] = LLVMPointerType(LLVMInt8TypeInContext(cg_ctx(g)), 0);
            continue;
        }
        /* todo-89: 带载荷枚举形参 -> byval 内存约定 */
        if (want[i] && cg_is_enum_type(g, want[i])) {
            CgEnumAbi* ai = (CgEnumAbi*)malloc(sizeof(CgEnumAbi));
            if (!ai) {
                cg_error(g, "failed to allocate the extern enum abi");
                return false;
            }
            if (!cg_ext_enum_abi(g, want[i], ai)) {
                free(ai);
            } else {
                byval[i] = true;
                if (enumL) enumL[i] = ai; else free(ai);
                pt[i] = LLVMPointerType(
                    LLVMInt8TypeInContext(cg_ctx(g)), 0);
                continue;
            }
        }
        CgAggInfo ai2;
        if (want[i] && cg_ext_agg_classify(g, want[i], &ai2)
            && ai2.mode != CG_AGG_NONE && ai2.mode != CG_AGG_PACK) {
            if (ai2.mode == CG_AGG_REGS) {
                /* SysV 寄存器对: 一等结构体实参, 无属性 */
                regs[i] = true;
                pt[i] = cg_ext_pod_llvm_type(g, ai2.L);
            } else {
                /* 内存约定: 按指针传; 聚合类型由 byval 属性承载 */
                byval[i] = true;
                podL[i] = ai2.L;
                pt[i] = LLVMPointerType(LLVMInt8TypeInContext(cg_ctx(g)), 0);
            }
            continue;
        }
        pt[i] = want[i] ? cg_extern_llvm_type(g, want[i]) : NULL;
        if (!pt[i]) {
            cg_error(g, "extern function %s has an unsupported parameter "
                        "type: %s", mangled, want[i] ? want[i] : "?");
            return false;
        }
    }
    /* sret 返回: 从后向前平移腾出首参位 (避免覆写参数 0 的类型),
     * 首参声明为 ptr 并由 cg_ext_apply_attrs 挂 sret 类型属性 */
    if (ret_pod || ret_is_penum) {
        for (size_t i = n; i > 0; i--) {
            pt[i] = pt[i - 1];
        }
        pt[0] = LLVMPointerType(LLVMInt8TypeInContext(cg_ctx(g)), 0);
        if (out_sret_ty) {
            *out_sret_ty = ret_is_penum
                ? cg_ext_enum_llvm_type(g, &ret_enum_ai)
                : cg_ext_pod_llvm_type(g, ret_pod);
        }
        if (out_ret_pod) *out_ret_pod = ret_pod;
        if (out_ret_regs) *out_ret_regs = false;
        if (out_ret_penum) *out_ret_penum = ret_is_penum;
        if (out_ret_enum_ai) *out_ret_enum_ai = ret_enum_ai;
    } else if (out_ret_regs && ret_is_regs) {
        *out_ret_regs = true;
    }
    LLVMTypeRef rt = ret_void || ret_pod || ret_is_penum
        ? LLVMVoidTypeInContext(cg_ctx(g))
        : (ret_agg_ty ? ret_agg_ty
                      : cg_extern_llvm_type(g, ret_name));
    if (!ret_void && !rt) {
        cg_error(g, "extern function %s has an unsupported return type: %s",
                 mangled, ret_name);
        return false;
    }
    *out_fty = LLVMFunctionType(rt, pt, (unsigned)pc, is_var_arg);
    return true;
}

/* 在函数/调用点上登记 sret/byval 属性 (index 按 LLVM 约定:
 * 形参从 1 开始)。enumL[i] 非 NULL 时该形参的 byval 类型属性取
 * 枚举 C 视图结构体 (todo-89), 否则查 podL[i]。 */
static void cg_ext_apply_attrs(
    CwCodegen_t* g, LLVMValueRef fn_or_call,
    bool is_call_site,
    size_t n,
    const bool* byval, const CwLayout_t* const* podL,
    const CgEnumAbi* const* enumL,
    LLVMTypeRef sret_ty
) {
    if (sret_ty) {
        LLVMAttributeRef a = cg_ext_type_attr(g, "sret", sret_ty);
        if (is_call_site) {
            LLVMAddCallSiteAttribute(fn_or_call, 1, a);
        } else {
            LLVMAddAttributeAtIndex(fn_or_call, 1, a);
        }
    }
    for (size_t i = 0; i < n; i++) {
        if (!byval[i]) continue;
        LLVMAttributeRef a = NULL;
        if (enumL && enumL[i]) {
            a = cg_ext_type_attr(g, "byval",
                                 cg_ext_enum_llvm_type(g, enumL[i]));
        } else if (podL[i]) {
            a = cg_ext_type_attr(
                g, "byval", cg_ext_pod_llvm_type(g, podL[i]));
        }
        if (!a) continue;
        /* 有 sret 时形参整体后移一位 */
        const unsigned idx = (unsigned)(i + 1 + (sret_ty ? 1 : 0));
        if (is_call_site) {
            LLVMAddCallSiteAttribute(fn_or_call, idx, a);
        } else {
            LLVMAddAttributeAtIndex(fn_or_call, idx, a);
        }
    }
}

/* todo-87: FnDecl 节点的 variadic 标记 (extern 块 ``...`` 形参) */
static bool cg_decl_is_variadic(
    const CwNode_t* decl
) {
    bool v = false;
    if (!decl) return false;
    cg_json_bool(cwmodule_node_field(decl, "variadic"), &v);
    return v;
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
    const size_t n0 = cwmodule_fn_param_count(decl);
    /* todo-88/89: 签名位置类型名带泛型实参渲染 */
    char* namepool = (char*)malloc(((n0 ? n0 : 1) + 1) * 192);
    if (!namepool) {
        cg_error(g, "failed to allocate the extern name buffers");
        return NULL;
    }
    cw_value* rtv = cwmodule_fn_return_type(decl);
    char* ret_name = namepool + n0 * 192;
    if (!rtv || !cg_ext_full_type_name(g, rtv, ret_name, 192)) {
        snprintf(ret_name, 192, "None");
    }
    /* bug-37: never (`!`) 返回映射 C void (noreturn, 如 C 的 exit) */
    const bool ret_void = !ret_name || strcmp(ret_name, "None") == 0
        || strcmp(ret_name, "!") == 0;
    const size_t n = n0;
    /* +1: sret 隐式首参 */
    LLVMTypeRef* pt = (LLVMTypeRef*)malloc((n + 1) * sizeof(LLVMTypeRef));
    bool* byval = (bool*)malloc((n ? n : 1) * sizeof(bool));
    bool* regs = (bool*)malloc((n ? n : 1) * sizeof(bool));
    bool* decay = (bool*)malloc((n ? n : 1) * sizeof(bool));
    const CwLayout_t** podL = (const CwLayout_t**)malloc(
        (n ? n : 1) * sizeof(CwLayout_t*));
    CgEnumAbi** enumL = (CgEnumAbi**)malloc((n ? n : 1)
                                            * sizeof(CgEnumAbi*));
    if (!pt || !byval || !regs || !decay || !podL || !enumL) {
        free(pt); free(byval); free(regs); free(decay); free(podL);
        free(enumL);
        free(namepool);
        cg_error(g, "failed to allocate the extern declaration buffers");
        return NULL;
    }
    const char** want = (const char**)malloc((n ? n : 1)
                                              * sizeof(const char*));
    if (!want) {
        free(pt); free(byval); free(regs); free(decay); free(podL);
        free(enumL);
        free(namepool);
        cg_error(g, "failed to allocate the extern declaration buffers");
        return NULL;
    }
    for (size_t i = 0; i < n; i++) {
        cw_value* p = cwmodule_fn_param(decl, i);
        cw_value* ptype = p ? cw_object_get(p, "type") : NULL;
        want[i] = (ptype && cw_typeof(ptype) == CW_OBJECT
                   && cg_ext_full_type_name(g, ptype,
                                            namepool + i * 192, 192))
            ? namepool + i * 192 : NULL;
        enumL[i] = NULL;
    }
    LLVMTypeRef fty = NULL;
    const bool ok = cg_ext_build_signature(
        g, sym->mangled, want, n,
        ret_void ? NULL : ret_name, ret_void,
        byval, podL, regs, decay, NULL, NULL, NULL, enumL,
        pt, NULL, NULL, NULL, NULL, NULL, cg_decl_is_variadic(decl), &fty);
    if (ok) {
        ef = LLVMAddFunction(g->ll->module, sym->mangled, fty);
        cg_ext_apply_attrs(g, ef, false, n,
                           byval, podL, (const CgEnumAbi* const*)enumL,
                           NULL);
        for (size_t i = 0; i < n; i++) free(enumL[i]);
    }
    free(want);
    free(pt); free(byval); free(regs); free(decay); free(podL);
    free(enumL);
    free(namepool);
    return ok ? ef : NULL;
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

    const size_t n0 = sym->decl
        ? cwmodule_fn_param_count(sym->decl) : 0;
    if (n0 > CG_FN_SIG_MAX) {
        cg_error(g, "extern callbacks support at most %d parameters",
                 CG_FN_SIG_MAX);
        return NULL;
    }
    /* todo-87: 变参 extern 函数不能作为 fn 值 (C 侧无法约定实参个数) */
    if (cg_decl_is_variadic(sym->decl)) {
        cg_error(g, "variadic extern function '%s' cannot be used as a "
                    "function value; call it directly instead",
                 sym->name);
        return NULL;
    }
    /* todo-88/89: 签名位置类型名带泛型实参渲染 */
    char* namepool = (char*)malloc(((n0 ? n0 : 1) + 1) * 192);
    if (!namepool) {
        cg_error(g, "failed to allocate the extern name buffers");
        return NULL;
    }
    cw_value* rtv = sym->decl
        ? cwmodule_fn_return_type(sym->decl) : NULL;
    char* ret_name = namepool + n0 * 192;
    if (!rtv || !cg_ext_full_type_name(g, rtv, ret_name, 192)) {
        snprintf(ret_name, 192, "None");
    }
    const bool ret_void = !ret_name || strcmp(ret_name, "None") == 0;
    /* 下游以 NULL 表示无返回值 (ret_name 缓冲始终非空) */
    const char* rn = ret_void ? NULL : (const char*)ret_name;

    const size_t n = n0;
    LLVMValueRef ef = cg_extern_ensure_declared(g, sym);
    if (!ef) {
        free(namepool);
        return NULL;
    }

    /* 重新收集参数类型名并构造内层 C 调用的完整签名信息
     * (todo-68: 聚合形参/返回按 PACK/REGS/MEM 统一分派;
     *  todo-59/89: 结构体指针与带载荷枚举形参) */
    const char** want = (const char**)malloc((n ? n : 1)
                                              * sizeof(const char*));
    /* +1: sret 隐式首参 */
    LLVMTypeRef* pt = (LLVMTypeRef*)malloc((n + 1) * sizeof(LLVMTypeRef));
    bool* byval = (bool*)malloc((n ? n : 1) * sizeof(bool));
    bool* regs = (bool*)malloc((n ? n : 1) * sizeof(bool));
    bool* decay = (bool*)malloc((n ? n : 1) * sizeof(bool));
    bool* ptragg = (bool*)malloc((n ? n : 1) * sizeof(bool));
    bool* ptrwback = (bool*)malloc((n ? n : 1) * sizeof(bool));
    const CwLayout_t** podL = (const CwLayout_t**)malloc(
        (n ? n : 1) * sizeof(CwLayout_t*));
    CgEnumAbi** enumL = (CgEnumAbi**)calloc(n ? n : 1,
                                            sizeof(CgEnumAbi*));
    LLVMTypeRef hts[CG_FN_SIG_MAX];
    for (size_t i = 0; i < n; i++) {
        cw_value* p = cwmodule_fn_param(sym->decl, i);
        cw_value* ptype = p ? cw_object_get(p, "type") : NULL;
        want[i] = (ptype && cw_typeof(ptype) == CW_OBJECT
                   && cg_ext_full_type_name(g, ptype,
                                            namepool + i * 192, 192))
            ? namepool + i * 192 : NULL;
        hts[i] = g->ll->handle_type;
    }
    LLVMTypeRef sret_ty = NULL;
    const CwLayout_t* ret_pod = NULL;
    bool ret_regs = false;
    bool ret_penum = false;
    CgEnumAbi ret_enum_ai;
    memset(&ret_enum_ai, 0, sizeof(ret_enum_ai));
    LLVMTypeRef fty = NULL;
    const bool ok = cg_ext_build_signature(
        g, sym->mangled, want, n,
        ret_void ? NULL : ret_name, ret_void,
        byval, podL, regs, decay, ptragg, ptrwback, NULL, enumL,
        pt, &sret_ty, &ret_pod, &ret_regs, &ret_penum, &ret_enum_ai,
        false, &fty);
    if (!ok || !ptragg || !ptrwback || !enumL
        || !decay || !byval || !regs || !podL) {
        if (enumL) for (size_t i = 0; i < n; i++) free(enumL[i]);
        free(want); free(pt); free(byval); free(regs); free(decay);
        free(ptragg); free(ptrwback); free(podL); free(enumL);
        free(namepool);
        return NULL;
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

    /* 句柄 -> C 实参 (含 sret 平移) */
    size_t k = 0;
    LLVMValueRef argv[CG_FN_SIG_MAX + 1];
    LLVMValueRef sret_buf = NULL;
    const bool ret_sret = sret_ty && (ret_pod || ret_penum);
    if (sret_ty && ret_sret) {
        sret_buf = cg_alloca(g, sret_ty, "th.sret");
        argv[k++] = sret_buf;
    }
    /* todo-59: *mut S 写回登记 */
    struct { LLVMValueRef img; LLVMValueRef base; const CwLayout_t* L; }
        wb[CG_FN_SIG_MAX];
    size_t nwb = 0;
    for (size_t i = 0; i < n; i++) {
        CwExpr a = { LLVMGetParam(th, (unsigned)i), want[i] };
        CgAggInfo ai2;
        char ptee[128];
        if (decay[i]) {
            argv[k++] = LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, a),
                                          pt[i], "th.arr");
        } else if (ptragg && ptragg[i]
                   && cg_ext_pointee_of(want[i], ptee,
                                        sizeof(ptee), NULL)) {
            /* todo-59: 实例 blob -> 对齐临时 C 缓冲, 按地址传递;
             * *mut 方向调用后写回实例 */
            CgAggInfo pai;
            cg_ext_agg_classify(g, ptee, &pai);
            LLVMValueRef img = cg_ext_aligned_buf(g, pai.size);
            cg_ext_flatten_fields(g,
                LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, a),
                                  cg_rt_i8_ptr(g), "th.pb"), pai.L, 0,
                img);
            if (ptrwback && ptrwback[i] && nwb < CG_FN_SIG_MAX) {
                wb[nwb].img = img;
                wb[nwb].base = LLVMBuildIntToPtr(cg_b(g),
                    cg_handle_addr(g, a), cg_rt_i8_ptr(g), "th.pwb");
                wb[nwb].L = pai.L;
                nwb++;
            }
            argv[k++] = LLVMBuildBitCast(cg_b(g), img, pt[i], "");
        } else if (byval[i] && enumL && enumL[i]) {
            /* todo-89: 带载荷枚举 -> C 视图临时缓冲 */
            LLVMValueRef img = cg_ext_aligned_buf(g, enumL[i]->total);
            cg_ext_enum_to_c_view(g, a, enumL[i], img);
            argv[k++] = LLVMBuildBitCast(cg_b(g), img, pt[i], "");
        } else if (byval[i] && podL[i]) {
            cg_ext_agg_classify(g, want[i], &ai2);
            argv[k++] = LLVMBuildBitCast(cg_b(g),
                                         cg_ext_agg_image_ptr(g, a, &ai2),
                                         pt[i], "");
        } else if (regs[i]) {
            cg_ext_agg_classify(g, want[i], &ai2);
            LLVMTypeRef sty = cg_ext_pod_llvm_type(g, ai2.L);
            LLVMValueRef img = cg_ext_agg_image_ptr(g, a, &ai2);
            argv[k++] = LLVMBuildLoad2(cg_b(g), sty,
                                       LLVMBuildBitCast(cg_b(g), img,
                                                        LLVMPointerType(sty, 0),
                                                        ""), "th.regs");
        } else if (cg_is_struct_type(g, want[i])) {
            cg_ext_agg_classify(g, want[i], &ai2);
            LLVMTypeRef ity = cg_ext_pack_int_ty(g, &ai2);
            LLVMValueRef img = cg_ext_agg_image_ptr(g, a, &ai2);
            LLVMValueRef slot = cg_alloca(g, ity, "th.pack");
            LLVMBuildMemCpy(cg_b(g), slot, 1, img, 1,
                            cg_i64(g, (uint64_t)ai2.size));
            argv[k++] = LLVMBuildLoad2(cg_b(g), ity, slot, "th.packed");
        } else if (cg_is_rawptr(want[i])) {
            argv[k++] = LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, a),
                                          pt[i], "th.ptr");
        } else if (strcmp(want[i], "String") == 0) {
            argv[k++] = LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, a),
                                          pt[i], "th.str");
        } else if (cg_is_enum_type(g, want[i])) {
            argv[k++] = cg_ext_enum_to_c(g, a);
        } else {
            argv[k++] = cg_load_value(
                g, a, cg_scalar_type(g, want[i], NULL));
        }
    }
    LLVMValueRef res = LLVMBuildCall2(cg_b(g),
                                      LLVMGlobalGetValueType(ef), ef, argv,
                                      (unsigned)k,
                                      (ret_void || ret_sret) ? "" : "th.call");
    /* todo-59: C 写回内容拷回实例 blob */
    for (size_t w = 0; w < nwb; w++) {
        cg_ext_copyback_fields(g, wb[w].img, wb[w].base, wb[w].L, 0);
    }

    /* C 结果 -> 句柄; 聚合经镜像快照重组 blob 后必须拷入
     * fnret.thunk.<mangled> 全局缓冲再返回 —— 裸 ret 指向入口
     * alloca 的句柄会被 -O3 优化成空壳 (与普通函数返回同纪律) */
    CwExpr out;
    const CwLayout_t* out_L = NULL;
    bool out_is_enum = false;
    size_t out_slots = 0;
    if (ret_penum && sret_buf) {
        /* todo-89: sret 缓冲 -> 枚举实例 (载荷句柄均指向持久存储) */
        out_is_enum = true;
        out_slots = 1 + ret_enum_ai.nfields;
        out = cg_ext_enum_from_c_view(g, cg_blob_i8(g, sret_buf),
                                      rn ? rn : "",
                                      &ret_enum_ai);
    } else if (ret_pod && sret_buf) {
        out = cg_ext_unflatten(g, cg_blob_i8(g, sret_buf),
                               rn ? rn : "");
        out_L = ret_pod;
    } else if (ret_regs && !ret_void) {
        CgAggInfo ai2;
        cg_ext_agg_classify(g, ret_name, &ai2);
        LLVMTypeRef sty = cg_ext_pod_llvm_type(g, ai2.L);
        LLVMValueRef buf = cg_alloca(g, sty, "th.regs.ret");
        LLVMBuildStore(cg_b(g), res, buf);
        out = cg_ext_unflatten(g, cg_blob_i8(g, buf),
                               rn ? rn : "");
        out_L = ai2.L;
    } else if (!ret_void && cg_is_struct_type(g, ret_name)) {
        CgAggInfo ai2;
        cg_ext_agg_classify(g, ret_name, &ai2);
        LLVMTypeRef ity = cg_ext_pack_int_ty(g, &ai2);
        LLVMValueRef buf = cg_alloca(g, ity, "th.pack.ret");
        LLVMBuildStore(cg_b(g), res, buf);
        out = cg_ext_unflatten(g, cg_blob_i8(g, buf), ret_name);
        out_L = ai2.L;
    } else if (rn) {
        char gname[192];
        snprintf(gname, sizeof(gname), "fnret.thunk.%s", sym->mangled);
        out = cg_ext_c_to_handle(g, res, rn, gname);
    } else {
        out = (CwExpr){ cg_null_handle(g), "None" };
    }
    if (!g->failed && (out_L || out_is_enum)) {
        const size_t bsz = out_is_enum
            ? cg_enum_blob_size(g, rn ? rn : "")
            : cg_struct_blob_size(g, out_L);
        char gname[192];
        snprintf(gname, sizeof(gname), "fnret.thunk.%s", sym->mangled);
        LLVMTypeRef arr = LLVMArrayType(
            LLVMInt8TypeInContext(cg_ctx(g)), (unsigned)bsz);
        LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
        if (!gv) {
            gv = LLVMAddGlobal(g->ll->module, arr, gname);
            LLVMSetInitializer(gv, LLVMConstNull(arr));
        }
        LLVMValueRef src = cg_expr_blob_i8(g, out);
        LLVMValueRef dst = LLVMBuildBitCast(cg_b(g), gv,
                                            cg_rt_i8_ptr(g), "");
        LLVMBuildMemCpy(cg_b(g), dst, 1, src, 1,
                        cg_i64(g, (uint64_t)bsz));
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), gv, LLVMInt64TypeInContext(cg_ctx(g)),
            "th.ret.addr");
        if (out_is_enum) {
            /* 载荷句柄指向 arena 单元/C 内存, 浅拷即完整语义 */
            out.handle = cg_build_value(g, addr,
                                         cg_i64(g, (uint64_t)out_slots),
                                         cg_i64(g, 0));
        } else {
            /* ABI v2: blob 无自指句柄, 全局副本即完整语义 */
            out.handle = cg_build_value(g, addr,
                                         cg_i64(g, out_L->size),
                                         cg_i64(g, 0));
        }
    }
    LLVMBuildRet(cg_b(g), out.handle);

    g->current_fn = saved_fn;
    if (saved_block) LLVMPositionBuilderAtEnd(cg_b(g), saved_block);
    for (size_t i = 0; i < n; i++) free(enumL[i]);
    free(want); free(pt); free(byval); free(regs); free(decay);
    free(ptragg); free(ptrwback); free(podL); free(enumL);
    free(namepool);
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

/* CWind 函数的 C-ABI 适配器 (todo-54/68): 让 CWind 函数能当回调传给 C。
 * 以声明的回调签名生成 C-ABI 函数: 参数包成句柄 -> 调 CWind 函数 ->
 * 结果解包为 C 类型。纯数据聚合 (repr(C) 形态) 可自由出现在形参与
 * 返回位: 入参经 unflatten 包装成实例句柄, 返回经镜像写回
 * (sret 首参 / 打包整数 / 寄存器对)。返回适配器函数值, 失败 NULL。 */
static LLVMValueRef cg_callback_adapter(
    CwCodegen_t* g, const CwSymEntry_t* target, const char* sig
) {
    char sbuf[128];
    cg_sanitize_sig(sig, sbuf, sizeof(sbuf));
    char aname[384];
    snprintf(aname, sizeof(aname), "cwind.cb.%s.%s", target->name, sbuf);
    LLVMValueRef existing = LLVMGetNamedFunction(g->ll->module, aname);
    if (existing) return existing;

    /* 拆签名串为类型段, 再统一走 build_signature 构造 C-ABI 类型 */
    char buf[256];
    if (strlen(sig) >= sizeof(buf)) {
        cg_error(g, "callback signature too long: %s", sig);
        return NULL;
    }
    memcpy(buf, sig, strlen(sig) + 1);
    const char* params[CG_FN_SIG_MAX];
    const char* ret = NULL;
    const size_t n = cg_fn_sig_split(buf, params, CG_FN_SIG_MAX, &ret);
    if (n == SIZE_MAX) {
        cg_error(g, "unsupported callback signature: %s", sig);
        return NULL;
    }
    if (ret && strcmp(ret, "None") == 0) ret = NULL;
    const bool ret_void = ret == NULL;

    const char** want = (const char**)malloc((n ? n : 1)
                                              * sizeof(const char*));
    for (size_t i = 0; i < n; i++) want[i] = params[i];
    /* +1: sret 隐式首参 */
    LLVMTypeRef* pt = (LLVMTypeRef*)malloc((n + 1) * sizeof(LLVMTypeRef));
    bool* byval = (bool*)malloc((n ? n : 1) * sizeof(bool));
    bool* regs = (bool*)malloc((n ? n : 1) * sizeof(bool));
    bool* decay = (bool*)malloc((n ? n : 1) * sizeof(bool));
    const CwLayout_t** podL = (const CwLayout_t**)malloc(
        (n ? n : 1) * sizeof(CwLayout_t*));
    CgEnumAbi** enumL = (CgEnumAbi**)calloc(n ? n : 1,
                                            sizeof(CgEnumAbi*));
    LLVMTypeRef sret_ty = NULL;
    const CwLayout_t* ret_pod = NULL;
    bool ret_regs = false;
    bool ret_penum = false;
    CgEnumAbi ret_enum_ai;
    memset(&ret_enum_ai, 0, sizeof(ret_enum_ai));
    LLVMTypeRef fty = NULL;
    const bool ok = cg_ext_build_signature(
        g, aname, want, n, ret_void ? NULL : ret, ret_void,
        byval, podL, regs, decay, NULL, NULL, NULL, enumL,
        pt, &sret_ty, &ret_pod, &ret_regs, &ret_penum, &ret_enum_ai,
        false, &fty);
    free(decay);
    if (!ok) {
        if (enumL) for (size_t i = 0; i < n; i++) free(enumL[i]);
        free(want); free(pt); free(byval); free(regs); free(podL);
        free(enumL);
        return NULL;
    }
    /* 返回聚合的分类信息 (写回时需要布局) */
    CgAggInfo rai;
    const bool ret_agg = !ret_void && ret
        && cg_ext_agg_classify(g, ret, &rai);

    LLVMValueRef tfn = LLVMGetNamedFunction(g->ll->module, target->mangled);
    if (!tfn) {
        cg_error(g, "callback target is not declared: %s", target->mangled);
        for (size_t i = 0; i < n; i++) free(enumL[i]);
        free(want); free(pt); free(byval); free(regs); free(podL);
        free(enumL);
        return NULL;
    }
    LLVMValueRef ad = LLVMAddFunction(g->ll->module, aname, fty);
    cg_ext_apply_attrs(g, ad, false, n, byval, podL,
                       (const CgEnumAbi* const*)enumL, sret_ty);

    LLVMBasicBlockRef saved_block = LLVMGetInsertBlock(g->builder);
    LLVMValueRef saved_fn = g->current_fn;
    g->current_fn = ad;
    LLVMBasicBlockRef entry = LLVMAppendBasicBlockInContext(
        cg_ctx(g), ad, "entry");
    LLVMPositionBuilderAtEnd(cg_b(g), entry);

    /* C 实参 -> CWind 句柄 (有 sret 时形参整体后移一位) */
    const unsigned shift = (sret_ty && (ret_pod || ret_penum)) ? 1u : 0u;
    LLVMValueRef argv[CG_FN_SIG_MAX];
    for (size_t i = 0; i < n; i++) {
        LLVMValueRef p = LLVMGetParam(ad, (unsigned)(i + shift));
        CgAggInfo ai;
        /* todo-89: 带载荷枚举形参 -> C 视图重建枚举实例 */
        if (byval[i] && enumL && enumL[i]) {
            CwExpr w = cg_ext_enum_from_c_view(g,
                LLVMBuildBitCast(cg_b(g), p, cg_rt_i8_ptr(g), ""),
                want[i], enumL[i]);
            argv[i] = w.handle;
            continue;
        }
        if (byval[i] || regs[i]
            || (cg_is_struct_type(g, want[i])
                && cg_ext_agg_classify(g, want[i], &ai))) {
            /* todo-68: 聚合入参 -> 镜像快照重组实例句柄 */
            cg_ext_agg_classify(g, want[i], &ai);
            CwExpr w;
            if (ai.mode == CG_AGG_MEM) {
                /* byval 指针即调用方拷贝的 C 视图镜像 */
                w = cg_ext_unflatten(g,
                                     LLVMBuildBitCast(cg_b(g), p,
                                                      cg_rt_i8_ptr(g), ""),
                                     want[i]);
            } else {
                LLVMTypeRef vt = ai.mode == CG_AGG_REGS
                    ? cg_ext_pod_llvm_type(g, ai.L)
                    : cg_ext_pack_int_ty(g, &ai);
                LLVMValueRef slot = cg_alloca(g, vt, "cb.agg");
                LLVMBuildStore(cg_b(g), p, slot);
                w = cg_ext_unflatten(g, cg_blob_i8(g, slot), want[i]);
            }
            argv[i] = w.handle;
            continue;
        }
        if (cg_is_scalar(want[i])) {
            /* C 标量 -> 槽位句柄 (alloca 在入口块, 存活整个适配器) */
            size_t sz = 0;
            LLVMTypeRef vt = cg_scalar_type(g, want[i], &sz);
            LLVMValueRef slot = cg_alloca(g, vt, "cb.slot");
            LLVMBuildStore(cg_b(g), p, slot);
            LLVMValueRef addr = LLVMBuildPtrToInt(
                cg_b(g), slot, LLVMInt64TypeInContext(cg_ctx(g)),
                "cb.slot.addr");
            argv[i] = cg_build_value(g, addr,
                                      cg_i64(g, sz), cg_i64(g, 0));
        } else if (strcmp(want[i], "String") == 0) {
            /* char* -> String 句柄: strlen 取长 (NUL 约定) */
            LLVMValueRef sl = cg_extern_declare_strlen(g);
            LLVMValueRef len = LLVMBuildCall2(
                cg_b(g), LLVMGlobalGetValueType(sl), sl, &p, 1,
                "cb.strlen");
            LLVMValueRef addr = LLVMBuildPtrToInt(
                cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "cb.addr");
            argv[i] = cg_build_value(g, addr, len,
                                      cg_i64(g, 0));
        } else if (cg_is_array_type(want[i])) {
            /* todo-67: 退化数组形参 -> {address -> 数据, length -> N}
             * 句柄 (C 传入的就是元素指针) */
            char elem[128];
            size_t an = 0;
            if (!cg_array_info(want[i], elem, sizeof(elem), &an)) {
                cg_error(g, "unsupported array parameter in callback "
                            "signature: %s", want[i]);
                g->current_fn = saved_fn;
                if (saved_block) {
                    LLVMPositionBuilderAtEnd(cg_b(g), saved_block);
                }
                free(want); free(pt); free(byval); free(regs); free(podL);
                return NULL;
            }
            LLVMValueRef addr = LLVMBuildPtrToInt(
                cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)),
                "cb.arr.addr");
            argv[i] = cg_build_value(g, addr,
                                      cg_i64(g, an), cg_i64(g, 0));
        } else {
            /* *const T / *mut T: 地址即值 */
            LLVMValueRef addr = LLVMBuildPtrToInt(
                cg_b(g), p, LLVMInt64TypeInContext(cg_ctx(g)), "cb.addr");
            argv[i] = cg_build_value(g, addr,
                                      cg_i64(g, 0), cg_i64(g, 0));
        }
    }
    LLVMValueRef res = LLVMBuildCall2(cg_b(g),
                                      LLVMGlobalGetValueType(tfn), tfn,
                                      argv, (unsigned)n, "cb.call");
    if (ret_penum) {
        /* todo-89: 枚举实例 -> C 视图写回 sret 首参 */
        CwExpr rh = { res, ret };
        CgEnumAbi* ai = (CgEnumAbi*)malloc(sizeof(CgEnumAbi));
        if (!ai || !cg_ext_enum_abi(g, ret, ai)) {
            free(ai);
            cg_error(g, "callback enum return is unsupported: %s",
                     ret ? ret : "?");
            g->current_fn = saved_fn;
            if (saved_block) {
                LLVMPositionBuilderAtEnd(cg_b(g), saved_block);
            }
            for (size_t i = 0; i < n; i++) free(enumL[i]);
            free(want); free(pt); free(byval); free(regs); free(podL);
            free(enumL);
            return NULL;
        }
        LLVMValueRef dst8 = cg_ext_aligned_buf(g, ai->total);
        cg_ext_enum_to_c_view(g, rh, ai, dst8);
        LLVMValueRef dstp = LLVMGetParam(ad, 0);
        LLVMBuildMemCpy(cg_b(g),
                        LLVMBuildBitCast(cg_b(g), dstp,
                                         cg_rt_i8_ptr(g), ""), 1,
                        dst8, 1, cg_i64(g, (uint64_t)ai->total));
        free(ai);
        LLVMBuildRetVoid(cg_b(g));
    } else if (ret_agg) {
        /* todo-68: 聚合返回 -> 取实例镜像写回 C 约定位置 */
        CwExpr rh = { res, ret };
        LLVMValueRef img = cg_ext_agg_image_ptr(g, rh, &rai);
        if (ret_pod) {
            /* sret 首参: 把镜像整块拷回调用方缓冲 */
            LLVMValueRef dst = LLVMGetParam(ad, 0);
            LLVMBuildMemCpy(cg_b(g),
                            LLVMBuildBitCast(cg_b(g), dst,
                                             cg_rt_i8_ptr(g), ""), 1,
                            img, 1, cg_i64(g, (uint64_t)rai.size));
            LLVMBuildRetVoid(cg_b(g));
        } else if (rai.mode == CG_AGG_REGS) {
            LLVMTypeRef sty = cg_ext_pod_llvm_type(g, rai.L);
            LLVMValueRef v = LLVMBuildLoad2(cg_b(g), sty,
                                            LLVMBuildBitCast(cg_b(g), img,
                                                             LLVMPointerType(sty, 0),
                                                             ""), "cb.ret.regs");
            LLVMBuildRet(cg_b(g), v);
        } else {
            /* PACK: 打包整数按位镜像返回 */
            LLVMTypeRef ity = cg_ext_pack_int_ty(g, &rai);
            LLVMValueRef slot = cg_alloca(g, ity, "cb.ret.pack");
            LLVMBuildMemCpy(cg_b(g), slot, 1, img, 1,
                            cg_i64(g, (uint64_t)rai.size));
            LLVMBuildRet(cg_b(g),
                         LLVMBuildLoad2(cg_b(g), ity, slot, "cb.ret.v"));
        }
    } else if (!ret_void) {
        /* 解包句柄结果为 C 值 (ABI v2: address 在索引 0) */
        LLVMValueRef addr = LLVMBuildExtractValue(
            cg_b(g), res, 0, "cb.ret.addr");
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
    for (size_t i = 0; i < n; i++) free(enumL[i]);
    free(want); free(pt); free(byval); free(regs); free(podL);
    free(enumL);
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

/* todo-88: 可空 char* -> Option<String> 实例。
 * 直接在 fnret 全局缓冲里分支构造 (NULL -> None 变体, 否则 Some +
 * strlen 取长的 String 句柄), 返回指向全局缓冲的句柄 (-O3 纪律)。 */
static CwExpr cg_ext_opt_string_result(
    CwCodegen_t* g, LLVMValueRef p,
    const char* buf_name
) {
    const char* ename = "Option";
    const CwNode_t* ed = cg_enum_decl(g, ename);
    size_t none_idx = 0;
    size_t some_idx = 1;
    if (!ed || !cg_enum_variant_index(g, ed, "None", &none_idx)
        || !cg_enum_variant_index(g, ed, "Some", &some_idx)) {
        cg_error(g, "extern Option<String> return requires the std "
                    "'Option' enum (None/Some variants)");
        return (CwExpr){ NULL, NULL };
    }
    const size_t bsz = cg_enum_blob_size(g, ename);
    if (bsz == 0) {
        cg_error(g, "extern Option<String> return has no runtime "
                    "layout for 'Option'");
        return (CwExpr){ NULL, NULL };
    }
    LLVMTypeRef arr = LLVMArrayType(
        LLVMInt8TypeInContext(cg_ctx(g)), (unsigned)bsz);
    LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, buf_name);
    if (!gv) {
        gv = LLVMAddGlobal(g->ll->module, arr, buf_name);
        LLVMSetInitializer(gv, LLVMConstNull(arr));
    }
    LLVMValueRef dst8 = LLVMBuildBitCast(cg_b(g), gv,
                                         cg_rt_i8_ptr(g), "");

    LLVMBasicBlockRef cur = LLVMGetInsertBlock(cg_b(g));
    LLVMBasicBlockRef some_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "opt.some");
    LLVMBasicBlockRef none_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "opt.none");
    LLVMBasicBlockRef merge_bb = LLVMAppendBasicBlockInContext(
        cg_ctx(g), g->current_fn, "opt.merge");
    LLVMValueRef is_null = LLVMBuildICmp(cg_b(g), LLVMIntEQ, p,
        LLVMConstNull(LLVMPointerType(
            LLVMInt8TypeInContext(cg_ctx(g)), 0)), "opt.null");
    LLVMBuildCondBr(cg_b(g), is_null, none_bb, some_bb);

    LLVMPositionBuilderAtEnd(cg_b(g), none_bb);
    LLVMBuildStore(cg_b(g), cg_i32(g, (uint32_t)none_idx),
                   cg_enum_tag_ptr(g, dst8));
    LLVMBuildBr(cg_b(g), merge_bb);

    LLVMPositionBuilderAtEnd(cg_b(g), some_bb);
    LLVMValueRef sl = cg_extern_declare_strlen(g);
    LLVMValueRef len = LLVMBuildCall2(cg_b(g),
        LLVMGlobalGetValueType(sl), sl, &p, 1, "opt.len");
    LLVMBuildStore(cg_b(g), cg_i32(g, (uint32_t)some_idx),
                   cg_enum_tag_ptr(g, dst8));
    LLVMValueRef addr = LLVMBuildPtrToInt(cg_b(g), p,
        LLVMInt64TypeInContext(cg_ctx(g)), "opt.addr");
    LLVMValueRef h = cg_build_value(g, addr, len,
                                     cg_i64(g, 0));
    LLVMBuildStore(cg_b(g), h, cg_enum_slot(g, dst8, 0));
    LLVMBuildBr(cg_b(g), merge_bb);

    LLVMPositionBuilderAtEnd(cg_b(g), merge_bb);
    LLVMValueRef gaddr = LLVMBuildPtrToInt(cg_b(g), gv,
        LLVMInt64TypeInContext(cg_ctx(g)), "opt.gaddr");
    return (CwExpr){
        cg_build_value(g, gaddr,
                        cg_i64(g, cg_enum_slot_count(g, ename)),
                        cg_i64(g, 0)),
        "Option<String>",
    };
}

/* extern 函数调用:
 * CWind 值是句柄, extern 函数吃原生 C 类型, 调用点做双向转换:
 *  - 标量实参: 从句柄地址 load 出原始值 (先按形参宽度 coerce);
 *  - 指针实参: 句柄 address 字段即指针值, inttoptr 后直传;
 *  - String 实参 (todo-51): 句柄 address 即字节指针 (字面量与拼接
 *    结果都以 NUL 结尾), 按 char* 直传;
 *  - [T; N] 实参 (todo-67): C 数组退化语义, 数据地址按 T* 直传;
 *  - 聚合实参 (todo-52/61/65/66): PACK 打包整数 / REGS 一等结构体
 *    (SysV 寄存器对) / MEM byval 指针, 含嵌套字段先扁平化;
 *  - *const S / *mut S 实参 (todo-59): 实例扁平化进对齐临时缓冲后
 *    按地址传递, *mut 方向调用后把 C 写回内容拷回实例 (出参模式);
 *  - 带载荷枚举实参 (todo-89): C 视图临时缓冲按 byval 指针传递;
 *  - None 返回: void 函数, 结果为空句柄;
 *  - 标量返回: 写进 fnret.ext.<name> 专用全局缓冲再按标量语义取回
 *    (cg_fixup_call_result 立即拷进调用方本地临时, -O3 安全;
 *    不复用 g->ret_global —— 那是外层函数自身 return 语句的缓冲);
 *  - 指针返回: 地址即值, 句柄 address 直接承载;
 *  - String 返回 (todo-51): C 返回 char*, strlen 取长后构造成
 *    String 句柄 (内存归 C 所有, 只读使用);
 *  - Option<String> 返回 (todo-88): NULL -> None, 否则 Some(String);
 *  - 聚合返回: PACK 整数 / REGS 结构体值 / sret 缓冲统一经连续镜像
 *    快照重组实例 blob (含嵌套时重建子 blob 树);
 *  - 带载荷枚举返回 (todo-89): sret 缓冲重建枚举实例。 */
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
    /* todo-88/89: 签名位置类型名带泛型实参渲染 */
    const size_t npn = cwmodule_fn_param_count(decl);
    char* namepool = (char*)malloc(((npn ? npn : 1) + 1) * 192);
    if (!namepool) {
        cg_error(g, "failed to allocate the extern name buffers");
        return (CwExpr){ NULL, NULL };
    }
    cw_value* rtv = cwmodule_fn_return_type(decl);
    char* ret_name = namepool + npn * 192;
    if (!rtv || !cg_ext_full_type_name(g, rtv, ret_name, 192)) {
        snprintf(ret_name, 192, "None");
    }
    /* bug-37: never (`!`) 返回映射 C void (noreturn, 如 C 的 exit) */
    const bool ret_void = !ret_name || strcmp(ret_name, "None") == 0
        || strcmp(ret_name, "!") == 0;
    const bool ret_optstr =
        !ret_void && cg_ext_is_optstring(ret_name);
#define CG_EXT_FREE_ALL() \
    do { \
        for (size_t z = 0; z < n; z++) free(enumL[z]); \
        free(wb); \
        free(pt); free(want); free(argv); free(byval); \
        free(regs); free(decay); free(ptragg); free(ptrwback); \
        free(podL); free(enumL); \
        free(namepool); \
    } while (0)
    if (!ret_void && strncmp(ret_name, "fn(", 3) == 0) {
        cg_error(g,
                 "extern function %s cannot return a function pointer "
                 "(callbacks are parameter-only)", sym->mangled);
        return (CwExpr){ NULL, NULL };
    }
    if (!ret_void && cg_is_array_type(ret_name)) {
        /* todo-67: 数组只能以退化指针作形参, 不能作返回值 */
        cg_error(g, "extern function %s cannot return an array "
                    "(C decay applies to parameters only)", sym->mangled);
        return (CwExpr){ NULL, NULL };
    }
    /* todo-89: 带载荷枚举返回走 sret 路径, 不需要直接 LLVM 类型 */
    CgEnumAbi ret_penum_probe;
    memset(&ret_penum_probe, 0, sizeof(ret_penum_probe));
    const bool ret_penum_ok = !ret_void && !ret_optstr
        && cg_is_enum_type(g, ret_name)
        && cg_ext_enum_abi(g, ret_name, &ret_penum_probe);
    if (!ret_void && !ret_optstr && !ret_penum_ok
        && !cg_extern_llvm_type(g, ret_name)) {
        cg_error(g, "extern function %s has an unsupported return type: %s",
                 sym->mangled, ret_name);
        free(namepool);
        return (CwExpr){ NULL, NULL };
    }

    const size_t n = cwmodule_fn_param_count(decl);
    /* todo-87: '...' 之后可有任意个额外实参, argv 按实参总数分配 */
    const bool is_variadic = cg_decl_is_variadic(decl);
    cw_value* ext_args_arr = cw_object_get(node, "args");
    const size_t ext_nargs =
        (ext_args_arr && cw_typeof(ext_args_arr) == CW_ARRAY)
            ? cw_array_size(ext_args_arr) : 0;
    /* todo-59: *mut S 写回登记 */
    struct {
        LLVMValueRef img;
        LLVMValueRef base;
        const CwLayout_t* L;
    }* wb = NULL;
    /* +1: sret 隐式首参 */
    LLVMTypeRef* pt = (LLVMTypeRef*)malloc((n + 1)
                                            * sizeof(LLVMTypeRef));
    const char** want = (const char**)malloc((n ? n : 1)
                                              * sizeof(const char*));
    LLVMValueRef* argv = (LLVMValueRef*)malloc(
        ((n > ext_nargs ? n : (ext_nargs ? ext_nargs : 1)) + 1)
        * sizeof(LLVMValueRef));
    bool* byval = (bool*)malloc((n ? n : 1) * sizeof(bool));
    bool* regs = (bool*)malloc((n ? n : 1) * sizeof(bool));
    bool* decay = (bool*)malloc((n ? n : 1) * sizeof(bool));
    bool* ptragg = (bool*)calloc(n ? n : 1, sizeof(bool));
    bool* ptrwback = (bool*)calloc(n ? n : 1, sizeof(bool));
    const CwLayout_t** podL = (const CwLayout_t**)malloc(
        (n ? n : 1) * sizeof(CwLayout_t*));
    CgEnumAbi** enumL = (CgEnumAbi**)calloc(n ? n : 1,
                                            sizeof(CgEnumAbi*));
    if (!pt || !want || !argv || !byval || !regs || !decay || !podL
        || !ptragg || !ptrwback || !enumL) {
        CG_EXT_FREE_ALL();
        cg_error(g, "failed to allocate the extern call buffers");
        return (CwExpr){ NULL, NULL };
    }
    for (size_t i = 0; i < n; i++) {
        cw_value* p = cwmodule_fn_param(decl, i);
        cw_value* ptype = p ? cw_object_get(p, "type") : NULL;
        want[i] = (ptype && cw_typeof(ptype) == CW_OBJECT
                   && cg_ext_full_type_name(g, ptype,
                                            namepool + i * 192, 192))
            ? namepool + i * 192 : NULL;
    }
    LLVMTypeRef sret_ty = NULL;
    const CwLayout_t* ret_pod = NULL;
    bool ret_regs = false;
    bool ret_penum = false;
    CgEnumAbi ret_enum_ai;
    memset(&ret_enum_ai, 0, sizeof(ret_enum_ai));
    LLVMTypeRef fty = NULL;
    if (!cg_ext_build_signature(
            g, sym->mangled, want, n,
            ret_void ? NULL : ret_name, ret_void,
            byval, podL, regs, decay, ptragg, ptrwback, NULL, enumL,
            pt, &sret_ty, &ret_pod, &ret_regs, &ret_penum,
            &ret_enum_ai, is_variadic, &fty)) {
        CG_EXT_FREE_ALL();
        return (CwExpr){ NULL, NULL };
    }
    LLVMValueRef fn = LLVMGetNamedFunction(g->ll->module, sym->mangled);
    if (!fn) {
        fn = LLVMAddFunction(g->ll->module, sym->mangled, fty);
        cg_ext_apply_attrs(g, fn, false, n, byval, podL,
                           (const CgEnumAbi* const*)enumL, sret_ty);
    }

    cw_value* args = ext_args_arr;
    const size_t nargs = ext_nargs;
    if (nargs != n && !(is_variadic && nargs >= n)) {
        cg_error(g,
                 is_variadic
                     ? "extern function %s expects at least %zu argument(s), "
                       "got %zu"
                     : "extern function %s expects %zu argument(s), got %zu",
                 sym->mangled, n, nargs);
        CG_EXT_FREE_ALL();
        return (CwExpr){ NULL, NULL };
    }
    size_t k = 0;
    LLVMValueRef sret_buf = NULL;
    const bool ret_sret = sret_ty && (ret_pod || ret_penum);
    if (sret_ty && ret_sret) {
        /* todo-61/89: sret 返回缓冲 (C 把结果写进这里) */
        sret_buf = cg_alloca(g, sret_ty, "ext.sret");
        argv[k++] = sret_buf;
    }
    /* todo-59: *mut S 写回登记 */
    wb = (void*)calloc(n ? n : 1, sizeof(*wb));
    size_t nwb = 0;
    for (size_t i = 0; i < nargs; i++) {
        cw_value* arg = cw_array_get(args, i);
        /* 有 sret 时形参整体后移一位, pt[] 已平移, 取类型须用偏移后下标 */
        const unsigned pti = (unsigned)(i + (ret_sret ? 1 : 0));
        if ((size_t)i >= n && is_variadic) {
            /* todo-87: '...' 之后按调用点实参类型做 C 默认提升:
             * 窄整数 -> i32, Bool -> i32, Int64/UInt64 原样,
             * Float/Float64 -> double, String / 裸指针 -> 地址直传;
             * 其余类型没有 C 变参表示, 拒绝。 */
            cw_value* av = cw_object_get(arg, "value");
            CwExpr a = cg_expr(g, av);
            if (g->failed) {
                CG_EXT_FREE_ALL();
                return (CwExpr){ NULL, NULL };
            }
            cw_value* tv = cg_node_ann_type(av);
            char vt[192];
            if (!tv || !cg_ext_full_type_name(g, tv, vt, sizeof(vt))) {
                cg_error(g, "extern call '%s' cannot determine the type "
                            "of variadic argument %zu",
                         sym->mangled, (size_t)i);
                CG_EXT_FREE_ALL();
                return (CwExpr){ NULL, NULL };
            }
            if (strcmp(vt, "String") == 0 || cg_is_rawptr(vt)) {
                argv[k++] = LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, a),
                    LLVMPointerType(LLVMInt8TypeInContext(cg_ctx(g)), 0), "");
            } else if (strcmp(vt, "Bool") == 0) {
                LLVMValueRef v = cg_load_value(
                    g, a, LLVMInt8TypeInContext(cg_ctx(g)));
                argv[k++] = LLVMBuildZExt(cg_b(g), v,
                    LLVMInt32TypeInContext(cg_ctx(g)), "");
            } else if (strcmp(vt, "Int64") == 0
                       || strcmp(vt, "UInt64") == 0) {
                argv[k++] = cg_load_value(
                    g, a, LLVMInt64TypeInContext(cg_ctx(g)));
            } else if (strcmp(vt, "Float") == 0
                       || strcmp(vt, "Float64") == 0) {
                /* C 默认提升: float -> double */
                CwExpr b = cg_coerce_scalar(g, a, "Float64");
                argv[k++] = cg_load_value(
                    g, b, cg_scalar_type(g, "Float64", NULL));
            } else if (cg_is_int(vt)) {
                CwExpr b = cg_coerce_scalar(g, a, "Int32");
                argv[k++] = cg_load_value(
                    g, b, cg_scalar_type(g, "Int32", NULL));
            } else {
                cg_error(g, "extern call '%s' cannot pass type '%s' "
                            "through '...' (only numeric/bool scalars, "
                            "String and raw pointers may cross)",
                         sym->mangled, vt);
                CG_EXT_FREE_ALL();
                return (CwExpr){ NULL, NULL };
            }
            continue;
        }
        if (strncmp(want[i], "fn(", 3) == 0) {
            /* todo-54: C 回调形参 (裸 extern 函数名直传地址,
             * 裸 CWind 函数名经适配器包装) */
            argv[k] = cg_callback_argument(g, arg, want[i]);
            if (g->failed) {
                CG_EXT_FREE_ALL();
                return (CwExpr){ NULL, NULL };
            }
            k++;
            continue;
        }
        CwExpr a = cg_expr(g, cw_object_get(arg, "value"));
        if (g->failed) {
            CG_EXT_FREE_ALL();
            return (CwExpr){ NULL, NULL };
        }
        a = cg_coerce_scalar(g, a, want[i]);
        if (g->failed) {
            CG_EXT_FREE_ALL();
            return (CwExpr){ NULL, NULL };
        }
        char ptee[128];
        if (ptragg[i]
            && cg_ext_pointee_of(want[i], ptee, sizeof(ptee), NULL)) {
            /* todo-59: 结构体指针形参。
             * 实参为 &expr (实例借用): blob 扁平化进对齐临时 C 缓冲,
             * 按地址传递, *mut 方向调用后写回实例;
             * 其余 (C 返回的指针值等): 地址原样直传。 */
            cw_value* av = cw_object_get(arg, "value");
            cw_value* avop = av ? cw_object_get(av, "op") : NULL;
            const bool is_borrow =
                av && strcmp(cg_node_kind(av), "UnaryOp") == 0
                && avop && cw_typeof(avop) == CW_STRING
                && strcmp(cw_string_cstr(avop), "&") == 0;
            if (!is_borrow) {
                argv[k++] = LLVMBuildIntToPtr(cg_b(g),
                    cg_handle_addr(g, a), pt[pti], "ext.ptr.raw");
                continue;
            }
            CgAggInfo pai;
            if (!cg_ext_agg_classify(g, ptee, &pai)) {
                cg_error(g, "extern struct pointer has an unsupported "
                            "pointee: %s", ptee);
                CG_EXT_FREE_ALL();
                return (CwExpr){ NULL, NULL };
            }
            LLVMValueRef img = cg_ext_aligned_buf(g, pai.size);
            cg_ext_flatten_fields(g,
                LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, a),
                                  cg_rt_i8_ptr(g), "ext.pb"), pai.L, 0,
                img);
            if (ptrwback[i]) {
                wb[nwb].img = img;
                wb[nwb].base = LLVMBuildIntToPtr(cg_b(g),
                    cg_handle_addr(g, a), cg_rt_i8_ptr(g), "ext.wb");
                wb[nwb].L = pai.L;
                nwb++;
            }
            argv[k++] = LLVMBuildBitCast(cg_b(g), img, pt[pti], "");
        } else if (byval[i] && enumL[i]) {
            /* todo-89: 带载荷枚举 -> C 视图临时缓冲 */
            LLVMValueRef img = cg_ext_aligned_buf(g, enumL[i]->total);
            cg_ext_enum_to_c_view(g, a, enumL[i], img);
            argv[k++] = LLVMBuildBitCast(cg_b(g), img, pt[pti], "");
        } else if (decay[i]) {
            /* todo-67: [T; N] 形参退化为 T* 指针
             * (句柄 address 即元素数据起始地址) */
            argv[k++] = LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, a),
                                          pt[pti], "ext.arr");
        } else if (byval[i] && podL[i]) {
            /* todo-61/66: 内存约定聚合按指针传递
             * (平铺直指 blob 的 C 视图区, 含嵌套先扁平化到栈缓冲) */
            CgAggInfo ai;
            cg_ext_agg_classify(g, want[i], &ai);
            argv[k++] = LLVMBuildBitCast(cg_b(g),
                                         cg_ext_agg_image_ptr(g, a, &ai),
                                         pt[pti], "");
        } else if (regs[i]) {
            /* todo-65: SysV 寄存器对聚合 -> 一等结构体实参 */
            CgAggInfo ai;
            cg_ext_agg_classify(g, want[i], &ai);
            LLVMTypeRef sty = cg_ext_pod_llvm_type(g, ai.L);
            LLVMValueRef img = cg_ext_agg_image_ptr(g, a, &ai);
            argv[k++] = LLVMBuildLoad2(cg_b(g), sty,
                                       LLVMBuildBitCast(cg_b(g), img,
                                                        LLVMPointerType(sty, 0),
                                                        ""), "ext.regs");
        } else if (cg_is_rawptr(want[i])) {
            /* 句柄 address 即指针值 */
            argv[k++] = LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, a),
                                          pt[pti], "ext.ptr");
        } else if (strcmp(want[i], "String") == 0) {
            /* todo-51: String 句柄 address 即字节指针, 按 char* 直传 */
            argv[k++] = LLVMBuildIntToPtr(cg_b(g), cg_handle_addr(g, a),
                                          pt[pti], "ext.str");
        } else if (cg_is_struct_type(g, want[i])) {
            /* todo-52: PACK 聚合 -> 打包整数寄存器
             * (镜像整块载入, 按位与 clang/gcc 的小结构体降级一致) */
            CgAggInfo ai;
            if (!cg_ext_agg_classify(g, want[i], &ai)
                || ai.mode != CG_AGG_PACK) {
                cg_error(g,
                         "extern function %s has an unsupported aggregate "
                         "parameter type: %s",
                         sym->mangled, want[i]);
                CG_EXT_FREE_ALL();
                return (CwExpr){ NULL, NULL };
            }
            LLVMTypeRef ity = cg_ext_pack_int_ty(g, &ai);
            LLVMValueRef img = cg_ext_agg_image_ptr(g, a, &ai);
            LLVMValueRef slot = cg_alloca(g, ity, "ext.pack");
            LLVMBuildMemCpy(cg_b(g), slot, 1, img, 1,
                            cg_i64(g, (uint64_t)ai.size));
            argv[k++] = LLVMBuildLoad2(cg_b(g), ity, slot, "ext.packed");
        } else if (cg_is_enum_type(g, want[i])) {
            /* todo-52: 无载荷枚举按 i32 判别值传递 */
            argv[k++] = cg_ext_enum_to_c(g, a);
        } else {
            argv[k++] = cg_load_value(
                g, a, cg_scalar_type(g, want[i], NULL));
        }
    }

    LLVMValueRef res = LLVMBuildCall2(cg_b(g), fty, fn, argv,
                                      (unsigned)k,
                                      (ret_void || ret_sret || ret_regs) ? "" : "ext.call");
    cg_ext_apply_attrs(g, res, true, n, byval, podL,
                       (const CgEnumAbi* const*)enumL, sret_ty);
    /* todo-59: C 写回内容拷回实例 blob (*mut S 出参模式) */
    for (size_t w = 0; w < nwb; w++) {
        cg_ext_copyback_fields(g, wb[w].img, wb[w].base, wb[w].L, 0);
    }
    /* 结果段仍需 ret_name; namepool 即将释放, 先拷到栈缓冲 */
    char retname_buf[192];
    snprintf(retname_buf, sizeof(retname_buf), "%s", ret_name);
    ret_name = retname_buf;
    CG_EXT_FREE_ALL();
#undef CG_EXT_FREE_ALL

    if (ret_penum && sret_buf) {
        /* todo-89: sret 缓冲 -> 枚举实例 -> fnret.ext 全局缓冲 */
        CwExpr out = cg_ext_enum_from_c_view(g,
            LLVMBuildBitCast(cg_b(g), sret_buf, cg_rt_i8_ptr(g),
                             "ext.en.src"),
            ret_name, &ret_enum_ai);
        if (g->failed) return (CwExpr){ NULL, NULL };
        const size_t bsz = cg_enum_blob_size(g, ret_name);
        char gname[192];
        snprintf(gname, sizeof(gname), "fnret.ext.%s", sym->mangled);
        LLVMTypeRef arr = LLVMArrayType(
            LLVMInt8TypeInContext(cg_ctx(g)), (unsigned)bsz);
        LLVMValueRef gv = LLVMGetNamedGlobal(g->ll->module, gname);
        if (!gv) {
            gv = LLVMAddGlobal(g->ll->module, arr, gname);
            LLVMSetInitializer(gv, LLVMConstNull(arr));
        }
        LLVMValueRef dst = LLVMBuildBitCast(cg_b(g), gv,
                                            cg_rt_i8_ptr(g), "");
        LLVMBuildMemCpy(cg_b(g), dst, 1, cg_expr_blob_i8(g, out), 1,
                        cg_i64(g, (uint64_t)bsz));
        LLVMValueRef addr = LLVMBuildPtrToInt(cg_b(g), gv,
            LLVMInt64TypeInContext(cg_ctx(g)), "ext.en.addr");
        return (CwExpr){
            cg_build_value(g, addr,
                            cg_i64(g, cg_enum_slot_count(g, ret_name)),
                            cg_i64(g, 0)),
            ret_name,
        };
    }
    if (ret_optstr) {
        /* todo-88: NULL -> None / 否则 Some(String) */
        char gname[192];
        snprintf(gname, sizeof(gname), "fnret.ext.%s", sym->mangled);
        return cg_ext_opt_string_result(g, res, gname);
    }
    if (ret_pod && sret_buf) {
        /* todo-61/66: sret 缓冲镜像 -> CWind 结构体实例 */
        LLVMValueRef src = LLVMBuildBitCast(cg_b(g), sret_buf,
                                            cg_rt_i8_ptr(g), "ext.pod.src");
        CwExpr out = cg_ext_unflatten(g, src, ret_name);
        if (g->failed) return (CwExpr){ NULL, NULL };
        return cg_fixup_call_result(g, out.handle, ret_name,
                                    cg_node_ann_type(node));
    }
    if (ret_regs && !ret_void) {
        /* todo-65: SysV 寄存器对返回值 -> 镜像快照重组 blob */
        CgAggInfo ai;
        cg_ext_agg_classify(g, ret_name, &ai);
        LLVMTypeRef sty = cg_ext_pod_llvm_type(g, ai.L);
        LLVMValueRef buf = cg_alloca(g, sty, "ext.regs.ret");
        LLVMBuildStore(cg_b(g), res, buf);
        LLVMValueRef src = cg_blob_i8(g, buf);
        CwExpr out = cg_ext_unflatten(g, src, ret_name);
        if (g->failed) return (CwExpr){ NULL, NULL };
        return cg_fixup_call_result(g, out.handle, ret_name,
                                    cg_node_ann_type(node));
    }
    if (ret_void) {
        /* bug-37: `!` 返回保留 never 类型 (调用后不可达, 与
         * builtins::exit 一致); 其余 void 返回仍是 None */
        const bool is_never = ret_name && strcmp(ret_name, "!") == 0;
        return (CwExpr){ cg_null_handle(g), is_never ? "!" : "None" };
    }
    if (cg_is_struct_type(g, ret_name)) {
        /* todo-52: PACK 打包整数返回 -> 镜像快照重组 blob */
        CgAggInfo ai;
        if (!cg_ext_agg_classify(g, ret_name, &ai)
            || ai.mode != CG_AGG_PACK) {
            cg_error(g, "extern function %s has an unsupported aggregate "
                        "return type: %s", sym->mangled, ret_name);
            return (CwExpr){ NULL, NULL };
        }
        LLVMTypeRef ity = cg_ext_pack_int_ty(g, &ai);
        LLVMValueRef buf = cg_alloca(g, ity, "ext.pack.ret");
        LLVMBuildStore(cg_b(g), res, buf);
        CwExpr out = cg_ext_unflatten(g, cg_blob_i8(g, buf), ret_name);
        if (g->failed) return (CwExpr){ NULL, NULL };
        return cg_fixup_call_result(g, out.handle, ret_name,
                                    cg_node_ann_type(node));
    }
    if (cg_is_enum_type(g, ret_name)) {
        /* todo-52: i32 判别值 -> 无载荷枚举句柄 */
        return cg_ext_c_to_enum(g, res, ret_name);
    }
    if (cg_is_rawptr(ret_name)) {
        /* todo-120: *const S / *mut S 返回的地址指向 C 布局内存; cursor
         * 槽 (对该类指针原为 0, 无他用) 置 1 标记 C 布局, 便于解引用时
         * 经 cg_ext_unflatten 重建 CWind 实例; 0 保持 CWind-blob 借用语义。
         * 地址不变, 指针判等 / 回传 C 路径均与原行为一致。 */
        const char* pointee = strchr(ret_name, ' ') + 1;
        const bool c_layout = pointee && cg_is_struct_type(g, pointee);
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), res, LLVMInt64TypeInContext(cg_ctx(g)), "ext.addr");
        return (CwExpr){
            cg_build_value(g, addr, cg_i64(g, 0),
                            cg_i64(g, c_layout ? 1 : 0)),
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
            cg_build_value(g, addr, len, cg_i64(g, 0)),
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
    LLVMValueRef h = cg_build_value(g, addr,
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

/* Vector 内置方法体 (owner 分派子例程); rec8 指向接收者的 CWValue */
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
        LLVMValueRef er = cg_value_cell(g, a, cg_node_ann_type(arg_val));
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
        LLVMValueRef out = cg_cell_alloca(g, "pop.out");
        LLVMBuildStore(cg_b(g), cg_null_handle(g), out);
        LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                             cg_rt_i8_ptr(g), "");
        LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
        LLVMValueRef f = cg_rt_declare(
            g, "cwvec_pop", LLVMInt1TypeInContext(cg_ctx(g)), pt, 2);
        LLVMValueRef av[2] = { rec8, out8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 2,
                       "");
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                        out, "vh");
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
            LLVMValueRef out = cg_cell_alloca(g, "get.out");
            LLVMBuildStore(cg_b(g), cg_null_handle(g), out);
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
            LLVMValueRef h = LLVMBuildLoad2(
                cg_b(g), g->ll->handle_type, out, "vh");
            const char* t = cg_node_type_name(g, node);
            CwExpr e = { h, t ? t : "Any" };
            return e;
        }
        if (strcmp(mname, "set") == 0 && nargs == 2) {
            cw_value* vv = cw_object_get(cw_array_get(args, 1), "value");
            CwExpr v = cg_expr(g, vv);
            if (g->failed) return (CwExpr){ NULL, NULL };
            v = cg_coerce_scalar(g, v, cg_receiver_arg(g, objv, 0));
            LLVMValueRef er = cg_value_cell(g, v, cg_node_ann_type(vv));
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
        return cg_method_length(g, rec8, CWVector);
    }
    if (strcmp(mname, "clear") == 0 && nargs == 0) {
        return cg_method_clear(g, rec8, "cwvec_clear");
    }
    if (strcmp(mname, "contains") == 0 && nargs == 1) {
        CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                            "value"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
        return cg_method_contains_rec(g, rec8, CWVector, a);
    }
    if (strcmp(mname, "extend_with") == 0 && nargs == 1) {
        CwExpr o = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                            "value"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef oc = cg_cell_alloca(g, "ext.recv");
        LLVMBuildStore(cg_b(g), o.handle, oc);
        LLVMValueRef or8 = LLVMBuildBitCast(cg_b(g), oc,
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
        LLVMValueRef er = cg_value_cell(g, v, cg_node_ann_type(iv));
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
        LLVMValueRef er = cg_cell_alloca(g, "idx.item");
        LLVMBuildStore(cg_b(g), v.handle, er);
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

/* Map 内置方法体 (owner 分派子例程); rec8 指向接收者的 CWValue */
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
    if ((strcmp(mname, "get") == 0 || strcmp(mname, "set") == 0
         || strcmp(mname, "remove") == 0)
        && nargs >= 1) {
        CwExpr k = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                            "value"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        k = cg_coerce_scalar(g, k, cg_receiver_arg(g, objv, 0));
        LLVMValueRef kr = cg_value_cell(g, k, NULL);
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef kr8 = LLVMBuildBitCast(cg_b(g), kr,
                                            cg_rt_i8_ptr(g), "");
        if (strcmp(mname, "remove") == 0 && nargs == 1) {
            LLVMTypeRef pt[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
            LLVMValueRef f = cg_rt_declare(
                g, "cwmap_remove", LLVMInt1TypeInContext(cg_ctx(g)),
                pt, 2);
            LLVMValueRef av[2] = { rec8, kr8 };
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f,
                           av, 2, "");
            CwExpr none = { cg_null_handle(g), "None" };
            return none;
        }
        if (strcmp(mname, "get") == 0 && nargs == 1) {
            LLVMValueRef out = cg_cell_alloca(g, "get.out");
            LLVMBuildStore(cg_b(g), cg_null_handle(g), out);
            LLVMValueRef out8 = LLVMBuildBitCast(
                cg_b(g), out, cg_rt_i8_ptr(g), "");
            LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g),
                                  cg_rt_i8_ptr(g),
                                  cg_rt_i8_ptr(g) };
            LLVMValueRef f = cg_rt_declare(
                g, "cwmap_get", LLVMInt1TypeInContext(cg_ctx(g)),
                pt, 3);
            LLVMValueRef av[3] = { rec8, kr8, out8 };
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f,
                           av, 3, "");
            LLVMValueRef h = LLVMBuildLoad2(
                cg_b(g), g->ll->handle_type, out, "vh");
            const char* t = cg_node_type_name(g, node);
            CwExpr e = { h, t ? t : "Any" };
            return e;
        }
        if (strcmp(mname, "set") == 0 && nargs == 2) {
            cw_value* vv = cw_object_get(cw_array_get(args, 1), "value");
            CwExpr v = cg_expr(g, vv);
            if (g->failed) return (CwExpr){ NULL, NULL };
            v = cg_coerce_scalar(g, v, cg_receiver_arg(g, objv, 1));
            LLVMValueRef vr = cg_value_cell(
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
    if (strcmp(mname, "length") == 0 && nargs == 0) {
        return cg_method_length(g, rec8, CWMap);
    }
    if (strcmp(mname, "clear") == 0 && nargs == 0) {
        return cg_method_clear(g, rec8, "cwmap_clear");
    }
    if (strcmp(mname, "contains") == 0 && nargs == 1) {
        CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                            "value"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
        return cg_method_contains_rec(g, rec8, CWMap, a);
    }
    return (CwExpr){ NULL, NULL }; /* 未命中: 交回上层继续匹配 */
}

/* 接收者的 CWValue 指针: 值类型变量直接用 slot, 其它表达式物化临时 */
static LLVMValueRef cg_expr_value_ptr(
    CwCodegen_t* g, const cw_value*node
) {
    if (node && strcmp(cg_node_kind(node), "Name") == 0) {
        cw_value* parts = cw_object_get(node, "parts");
        if (parts && cw_typeof(parts) == CW_ARRAY
            && cw_array_size(parts) == 1) {
            const char* name = cw_string_cstr(cw_array_get(parts, 0));
            CwVar_t* v = cg_var_find(g, name);
            if (v && v->is_value) return v->slot;
        }
    }
    CwExpr e = cg_expr(g, node);
    if (g->failed) return NULL;
    LLVMValueRef cell = cg_cell_alloca(g, "recv.val");
    LLVMBuildStore(cg_b(g), e.handle, cell);
    return cell;
}

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
    /* 接收者 = 其 CWValue 指针 (值变量直用 slot, 临时值物化) */
    LLVMValueRef rec = cg_expr_value_ptr(g, objv);
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g),
                                         "");
    cw_value* args = cw_object_get(node, "args");
    const size_t nargs = (args && cw_typeof(args) == CW_ARRAY)
        ? cw_array_size(args) : 0;
    const int otid0 = cg_type_id(owner);

    if (strcmp(owner, "Vector") == 0) {
        CwExpr r = cg_vec_method(g, node, objv, mname, args, nargs, rec8);
        if (r.handle || g->failed) return r;
    } else if (strcmp(owner, "Map") == 0) {
        CwExpr r = cg_map_method(g, node, objv, mname, args, nargs, rec8);
        if (r.handle || g->failed) return r;
    } else if (strcmp(owner, "String") == 0) {
        if (strcmp(mname, "length") == 0 && nargs == 0) {
            return cg_method_length(g, rec8, CWString);
        }
        if (strcmp(mname, "contains") == 0 && nargs == 1) {
            CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
            return cg_method_contains_rec(g, rec8, CWString, a);
        }
    } else if (strcmp(owner, "Set") == 0) {
        if ((strcmp(mname, "add") == 0 || strcmp(mname, "remove") == 0)
            && nargs == 1) {
            CwExpr a = cg_expr(g, cw_object_get(
                cw_array_get(args, 0), "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            a = cg_coerce_scalar(g, a, cg_receiver_arg(g, objv, 0));
            LLVMValueRef er = cg_value_cell(g, a, NULL);
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
            return cg_method_length(g, rec8, CWSet);
        }
        if (strcmp(mname, "clear") == 0 && nargs == 0) {
            return cg_method_clear(g, rec8, "cwset_clear");
        }
        if (strcmp(mname, "contains") == 0 && nargs == 1) {
            CwExpr a = cg_expr(g, cw_object_get(cw_array_get(args, 0),
                                                "value"));
            if (g->failed) return (CwExpr){ NULL, NULL };
            return cg_method_contains_rec(g, rec8, CWSet, a);
        }
    } else if (strcmp(owner, "Tuple") == 0) {
        if (strcmp(mname, "length") == 0 && nargs == 0) {
            return cg_method_length(g, rec8, CWTuple);
        }
    }
    if (strcmp(mname, "to_string") == 0 && nargs == 0) {
        /* Display::to_string: 任意内置值 -> String (rt 格式化) */
        return cg_call_to_string_owned(
            g, otid0 >= 0 ? otid0 : 0, rec8);
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
        if (bname && strcmp(bname, "gc_collect") == 0) {
            return cg_builtin_gc_collect(g, node);
        }
        if (bname && strcmp(bname, "gc_alloc_bytes") == 0) {
            return cg_builtin_gc_u64(g, "cwgc_alloc_bytes");
        }
        if (bname && strcmp(bname, "gc_live_bytes") == 0) {
            return cg_builtin_gc_u64(g, "cwgc_live_bytes");
        }
        if (bname && strcmp(bname, "gc_pause_ns") == 0) {
            return cg_builtin_gc_u64(g, "cwgc_pause_ns");
        }
        if (bname && strcmp(bname, "gc_enable") == 0) {
            return cg_builtin_gc_enable(g, node);
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
    if (cg_is_array_type(ot)) {
        /* 定长数组 (todo-60): 句柄 address -> 内联数据,
         * 元素直接按偏移读取, 动态索引做运行时边界检查 */
        char elem[128];
        size_t n = 0;
        if (!cg_array_info(ot, elem, sizeof(elem), &n)) {
            cg_error(g, "unsupported array type: %s", ot);
            return (CwExpr){ NULL, NULL };
        }
        CwExpr oe = cg_expr(g, obj);
        if (g->failed) return (CwExpr){ NULL, NULL };
        CwExpr idx = cg_expr(g, cw_object_get(node, "index"));
        if (g->failed) return (CwExpr){ NULL, NULL };
        LLVMValueRef ix = cg_index_i64(g, idx);
        cg_array_bounds_check(g, ix, n);
        size_t esz = 0;
        LLVMTypeRef evt = cg_scalar_type(g, elem, &esz);
        LLVMValueRef base = LLVMBuildIntToPtr(cg_b(g),
                                              cg_handle_addr(g, oe),
                                              cg_rt_i8_ptr(g), "arr.base");
        LLVMValueRef off[1] = {
            LLVMBuildMul(cg_b(g), ix, cg_i64(g, (uint64_t)esz), "arr.off")
        };
        LLVMValueRef p = LLVMBuildGEP2(cg_b(g),
                                       LLVMInt8TypeInContext(cg_ctx(g)),
                                       base, off, 1, "arr.p");
        LLVMValueRef v = LLVMBuildLoad2(cg_b(g), evt,
                                        LLVMBuildBitCast(cg_b(g), p,
                                                         LLVMPointerType(evt, 0),
                                                         ""), "arr.v");
        /* 类型名必须是稳定指针: 经类型表 interning, 不能指向栈缓冲 */
        const CwTypeId eid = cwtype_intern(g->ll->types, elem, NULL, 0);
        return cg_make_scalar(g, v, evt,
                              cwtype_name(g->ll->types, eid), esz);
    }
    LLVMValueRef rec = cg_expr_value_ptr(g, obj);
    if (g->failed) return (CwExpr){ NULL, NULL };
    LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g), "");
    LLVMValueRef out = cg_cell_alloca(g, "idx.out");
    LLVMBuildStore(cg_b(g), cg_null_handle(g), out);
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
        LLVMValueRef kr = cg_cell_alloca(g, "idx.key");
        LLVMBuildStore(cg_b(g), k.handle, kr);
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
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, out, "vh");
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
        LLVMValueRef trec = cg_cell_alloca(g, "tup.recv");
        LLVMBuildStore(cg_b(g), obj.handle, trec);
        LLVMValueRef trec8 = LLVMBuildBitCast(cg_b(g), trec,
                                              cg_rt_i8_ptr(g), "");
        LLVMValueRef out = cg_cell_alloca(g, "tup.at");
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
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, out,
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
    size_t fi = 0;
    bool found = false;
    for (size_t i = 0; i < L->field_count; i++) {
        if (strcmp(L->fields[i].name, fname) == 0) {
            fi = i;
            found = true;
            break;
        }
    }
    if (!found) {
        cg_error_at(g, node, "struct has no field %s", fname);
        return (CwExpr){ NULL, NULL };
    }
    LLVMValueRef base = cg_expr_blob_i8(g, obj);
    const char* t = cg_node_type_name(g, node);
    if (!t) {
        /* 泛型方法内字段访问的 ann.type 可能是 null/opaque (前端留白),
         * 布局里已做实参替换, 用它兜底 (也让标量返回走 fnret 全局) */
        t = cwtype_name(g->ll->types, L->fields[fi].type);
    }
    return cg_read_struct_field(g, base, L, fi, t);
}

/* todo-17: 数值 as 转换 —— SA 把两侧限定为数值; 截断/符号扩展/
 * int<->float 语义复用既有 cg_convert_scalar (经 cg_coerce_scalar)。 */
static CwExpr cg_expr_cast(
    CwCodegen_t* g,
    const cw_value*node
) {
    cw_value* tgt = cw_object_get(node, "target");
    const char* want = tgt ? cg_type_name_of(g, tgt) : NULL;
    if (!want || (!cg_is_int(want)
        && strcmp(want, "Float") != 0 && strcmp(want, "Float64") != 0)) {
        cg_error_at(g, node, "'as' requires a numeric target type");
        return (CwExpr){ NULL, NULL };
    }
    CwExpr e = cg_expr(g, cw_object_get(node, "operand"));
    if (g->failed) return (CwExpr){ NULL, NULL };
    return cg_coerce_scalar(g, e, want);
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
    if (strcmp(kind, "CastExpr") == 0) return cg_expr_cast(g, node);
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
    /* 绑定类型为容器时, 把泛型实参 tag 传给静态 new (bug-47 语义) */
    const bool is_container_decl = type_v != NULL
        && (cg_type_id(type_name) == CWVector
            || cg_type_id(type_name) == CWMap
            || cg_type_id(type_name) == CWSet);
    if (is_container_decl && !g->has_exp_tags) {
        g->exp_tags[0] = cg_ann_arg_tag(g, node, 0);
        g->exp_tags[1] = cg_ann_arg_tag(g, node, 1);
        g->has_exp_tags = true;
    }
    CwExpr e = cg_expr(g, cw_object_get(node, "value"));
    if (is_container_decl) g->has_exp_tags = false;
    if (!g->failed) cg_var_store(g, v, e);
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

/* 下标赋值: Vector[i] = v / Map[k] = v / 数组 a[i] = v (todo-60) */
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
    if (cg_is_array_type(ot)) {
        /* 定长数组: 句柄 address + i*esz 处标量写入 */
        char elem[128];
        size_t n = 0;
        if (!cg_array_info(ot, elem, sizeof(elem), &n)) {
            cg_error(g, "unsupported array type: %s", ot);
            return;
        }
        CwExpr oe = cg_expr(g, tobj);
        if (g->failed) return;
        CwExpr idx = cg_expr(g, cw_object_get(target, "index"));
        if (g->failed) return;
        LLVMValueRef ix = cg_index_i64(g, idx);
        cg_array_bounds_check(g, ix, n);
        CwExpr val = cg_expr(g, cw_object_get(node, "value"));
        if (g->failed) return;
        val = cg_coerce_scalar(g, val, elem);
        if (g->failed) return;
        size_t esz = 0;
        LLVMTypeRef evt = cg_scalar_type(g, elem, &esz);
        LLVMValueRef v = cg_load_value(g, val, evt);
        LLVMValueRef base = LLVMBuildIntToPtr(cg_b(g),
                                              cg_handle_addr(g, oe),
                                              cg_rt_i8_ptr(g), "arr.base");
        LLVMValueRef off[1] = {
            LLVMBuildMul(cg_b(g), ix, cg_i64(g, (uint64_t)esz), "arr.off")
        };
        LLVMValueRef p = LLVMBuildGEP2(cg_b(g),
                                       LLVMInt8TypeInContext(cg_ctx(g)),
                                       base, off, 1, "arr.p");
        LLVMBuildStore(cg_b(g), v, LLVMBuildBitCast(
            cg_b(g), p, LLVMPointerType(evt, 0), ""));
        return;
    }
    LLVMValueRef rec = cg_expr_value_ptr(g, tobj);
    if (g->failed) return;
    LLVMValueRef rec8 = LLVMBuildBitCast(cg_b(g), rec, cg_rt_i8_ptr(g),
                                         "");
    if (strcmp(ot, "Vector") == 0) {
        CwExpr idx = cg_expr(g, cw_object_get(target, "index"));
        if (g->failed) return;
        LLVMValueRef ix = cg_index_i64(g, idx);
        /* todo-151: RHS 的 T::new() 从元素类型上下文取泛型实参 tag */
        cw_value* targs = cw_object_get(
            cg_node_ann_type(tobj), "args");
        cg_push_expected_tags_from_ann(g,
            (targs && cw_typeof(targs) == CW_ARRAY
             && cw_array_size(targs) > 0)
                ? cw_array_get(targs, 0) : NULL);
        CwExpr val = cg_expr(g, cw_object_get(node, "value"));
        if (g->failed) return;
        g->has_exp_tags = false;
        LLVMValueRef er = cg_value_cell(
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
        LLVMValueRef kr = cg_value_cell(g, k, NULL);
        if (g->failed) return;
        LLVMValueRef kr8 = LLVMBuildBitCast(cg_b(g), kr, cg_rt_i8_ptr(g),
                                            "");
        /* todo-151: RHS 的 T::new() 从值类型上下文取泛型实参 tag */
        cw_value* targs = cw_object_get(
            cg_node_ann_type(tobj), "args");
        cg_push_expected_tags_from_ann(g,
            (targs && cw_typeof(targs) == CW_ARRAY
             && cw_array_size(targs) > 1)
                ? cw_array_get(targs, 1) : NULL);
        CwExpr val = cg_expr(g, cw_object_get(node, "value"));
        if (g->failed) return;
        g->has_exp_tags = false;
        LLVMValueRef vr = cg_value_cell(
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
    size_t fi = 0;
    for (size_t i = 0; i < L->field_count; i++) {
        if (L->fields[i].offset == off) {
            fi = i;
            break;
        }
    }
    /* todo-151: RHS 的 T::new() 与变量重赋值同纪律 —— 容器字段的
     * 泛型实参 tag 从布局字段类型 (实例化后) 取, 防新容器 key/value
     * tag 落 0 后 get/contains 按值比较全部失配 (bug-47 同根)。 */
    if (!g->has_exp_tags) {
        const int ftid = (int)cg_type_id(
            cwtype_name(g->ll->types, L->fields[fi].type));
        if (ftid == CWVector || ftid == CWMap || ftid == CWSet) {
            const char* a0 = cg_field_arg_type_name(g, L, fi, 0);
            const char* a1 = cg_field_arg_type_name(g, L, fi, 1);
            const int t0 = a0 ? cg_type_id(a0) : 0;
            const int t1 = a1 ? cg_type_id(a1) : 0;
            if (t0 > 0 || t1 > 0) {
                g->exp_tags[0] = t0 > 0 ? t0 : 0;
                g->exp_tags[1] = t1 > 0 ? t1 : 0;
                g->has_exp_tags = true;
            }
        }
    }
    CwExpr val = cg_expr(g, cw_object_get(node, "value"));
    if (g->failed) return;
    g->has_exp_tags = false;
    LLVMValueRef base = cg_expr_blob_i8(g, obj);
    if (strcmp(op, "=") == 0) {
        cg_store_struct_field(g, base, L, fi, val);
        return;
    }
    /* 复合字段赋值: 读字段 -> 运算 -> 写回 (kind-aware 读取) */
    const char* ftype = cwtype_name(g->ll->types, L->fields[fi].type);
    CwExpr cur = cg_read_struct_field(g, base, L, fi, ftype);
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
    /* 容器变量重赋值 (m = Map::new()): 与 cg_stmt_let 同纪律 —— 从绑定
     * 类型 ann 取泛型实参 tag 传给静态 new。否则新容器的 key/value
     * type tag 落 0, 后续按值比较的 get/contains 全部失配 (实测)。 */
    const bool is_container_var = cg_type_id(v->type_name) == CWVector
        || cg_type_id(v->type_name) == CWMap
        || cg_type_id(v->type_name) == CWSet;
    if (is_container_var && !g->has_exp_tags) {
        g->exp_tags[0] = cg_ann_arg_tag(g, target, 0);
        g->exp_tags[1] = cg_ann_arg_tag(g, target, 1);
        g->has_exp_tags = true;
    }
    CwExpr e = cg_expr(g, cw_object_get(node, "value"));
    if (is_container_var) g->has_exp_tags = false;
    if (g->failed) return;

    if (strcmp(op, "=") == 0) {
        cg_var_store(g, v, e);
        return;
    }

    /* String += : 拼接当前值 + 右值, 结果引用语义写回变量 */
    if (strcmp(op, "+=") == 0 && strcmp(v->type_name, "String") == 0) {
        CwExpr cur = cg_var_read(g, v);
        CwExpr res = cg_builtin_concat(g, cur, e);
        if (g->failed) return;
        cg_var_store(g, v, res);
        return;
    }

    /* 复合赋值: 仅标量 */
    if (!v->slot || !cg_is_scalar(v->type_name)) {
        cg_error(g, "compound assignment supports scalars only: %s =%s", v->name, op);
        return;
    }
    LLVMTypeRef vt = cg_scalar_type(g, v->type_name, NULL);
    LLVMValueRef cur = LLVMBuildLoad2(cg_b(g), vt, v->slot, "cur");
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
    cg_var_store_value(g, v, res, v->type_name);
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
    const bool via_ref = ptype && cg_type_is_ref(ptype);
    if (ptype) pointee = cg_type_name_of(g, ptype);
    if (pointee && (strncmp(pointee, "*const ", 7) == 0
                    || strncmp(pointee, "*mut ", 5) == 0)) {
        pointee += (strchr(pointee, ' ') - pointee) + 1;
    }
    if (!pointee) pointee = ptr.type_name;
    if (!cg_is_rawptr(ptr.type_name) && !via_ref) {
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
        LLVMValueRef h = cg_build_value(g, addr,
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
        LLVMValueRef h = cg_build_value(g, addr,
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
        LLVMValueRef addr = LLVMBuildPtrToInt(
            cg_b(g), g->ret_struct_global, LLVMInt64TypeInContext(cg_ctx(g)),
            "ret.addr");
        LLVMValueRef h = cg_build_value(g, addr,
                                         cg_i64(g, g->ret_struct_size),
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
        LLVMValueRef h = cg_build_value(g, addr,
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
    /* bug-53: 分支体是独立作用域 */
    cg_var_push_scope(g);
    cg_block(g, cw_object_get(node, "then"));
    cg_var_pop_scope(g);
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
            /* bug-53: elif 体同样是独立作用域 */
            cg_var_push_scope(g);
            cg_block(g, cw_object_get(elif, "body"));
            cg_var_pop_scope(g);
            if (!g->failed && !cg_block_terminated(g)) {
                LLVMBuildBr(cg_b(g), end_bb);
            }
            LLVMPositionBuilderAtEnd(cg_b(g), enext);
        }
        cw_value* else_ = cw_object_get(node, "else_");
        if (else_ && cw_typeof(else_) == CW_OBJECT) {
            cg_var_push_scope(g);
            cg_block(g, else_);
            cg_var_pop_scope(g);
        }
        if (!g->failed && !cg_block_terminated(g)) {
            LLVMBuildBr(cg_b(g), end_bb);
        }
    } else {
        cw_value* else_ = cw_object_get(node, "else_");
        if (else_ && cw_typeof(else_) == CW_OBJECT) {
            cg_var_push_scope(g);
            cg_block(g, else_);
            cg_var_pop_scope(g);
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
    /* bug-53: 循环体是独立作用域 —— 兄弟循环的同名 let 不得互撞 */
    cg_var_push_scope(g);
    cg_block(g, cw_object_get(node, "body"));
    cg_var_pop_scope(g);
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
    LLVMValueRef rec = cg_expr_value_ptr(g, iter_recv);
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
    /* bug-53: for 体 (含迭代变量) 是独立作用域 ——
     * 兄弟 for-in 的同名迭代变量/let 不得互撞 */
    cg_var_push_scope(g);
    if (is_map) {
        /* Map 迭代变量 = 每轮构造的 (key, value) Tuple
         * (键/值类型来自接收者泛型实参, 元数据分区: data 头) */
        LLVMValueRef key_out = cg_cell_alloca(g, "map.key");
        LLVMValueRef val_out = cg_cell_alloca(g, "map.val");
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

        const char* map_elem = (elem_type && strcmp(elem_type, "Any") != 0)
            ? elem_type : "Tuple";
        if (!cg_var_declare(g, var, map_elem, NULL)) return;
        CwVar_t* v = cg_var_find(g, var);
        /* (key, value) Tuple: 类型表 + cell 对 (cwtuple_init v2 四参) */
        const char* ktn = cg_receiver_arg(g, iter_recv, 0);
        const char* vtn = cg_receiver_arg(g, iter_recv, 1);
        const int ktid = ktn ? cg_type_id(ktn) : -1;
        const int vtid = vtn ? cg_type_id(vtn) : -1;
        LLVMValueRef tarr = cg_alloca(
            g, LLVMArrayType(LLVMInt32TypeInContext(cg_ctx(g)), 2),
            "map.pair.types");
        LLVMValueRef pair = cg_alloca(
            g, LLVMArrayType(g->ll->handle_type, 2), "map.pair");
        LLVMValueRef kt0 = LLVMBuildGEP2(
            cg_b(g), LLVMInt32TypeInContext(cg_ctx(g)), tarr,
            (LLVMValueRef[1]){ cg_i64(g, 0) }, 1, "pt.k");
        LLVMValueRef kt1 = LLVMBuildGEP2(
            cg_b(g), LLVMInt32TypeInContext(cg_ctx(g)), tarr,
            (LLVMValueRef[1]){ cg_i64(g, 1) }, 1, "pt.v");
        LLVMBuildStore(cg_b(g),
                       cg_i32(g, (uint32_t)(ktid >= 0 ? ktid : 0)), kt0);
        LLVMBuildStore(cg_b(g),
                       cg_i32(g, (uint32_t)(vtid >= 0 ? vtid : 0)), kt1);
        LLVMValueRef kslot = LLVMBuildGEP2(
            cg_b(g), g->ll->handle_type, pair,
            (LLVMValueRef[1]){ cg_i64(g, 0) }, 1, "pair.k");
        LLVMValueRef vslot = LLVMBuildGEP2(
            cg_b(g), g->ll->handle_type, pair,
            (LLVMValueRef[1]){ cg_i64(g, 1) }, 1, "pair.v");
        LLVMBuildStore(cg_b(g),
                       LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                      key_out, "pk"), kslot);
        LLVMBuildStore(cg_b(g),
                       LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                      val_out, "pv"), vslot);
        LLVMValueRef tval = cg_cell_alloca(g, "map.tup");
        LLVMBuildStore(cg_b(g), cg_null_handle(g), tval);
        LLVMTypeRef pr_init[4] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g),
                                   cg_rt_i8_ptr(g),
                                   LLVMInt64TypeInContext(cg_ctx(g)) };
        LLVMValueRef init = cg_rt_declare(
            g, "cwtuple_init", LLVMInt1TypeInContext(cg_ctx(g)),
            pr_init, 4);
        LLVMValueRef init_args[4] = {
            LLVMBuildBitCast(cg_b(g), tval, cg_rt_i8_ptr(g), ""),
            LLVMBuildBitCast(cg_b(g), tarr, cg_rt_i8_ptr(g), ""),
            LLVMBuildBitCast(cg_b(g), pair, cg_rt_i8_ptr(g), ""),
            cg_i64(g, 2),
        };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(init), init,
                       init_args, 4, "");
        LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                        tval, "th");
        CwExpr te = { h, "Tuple" };
        cg_var_store(g, v, te);
    } else {
        if (!cg_var_declare(g, var, elem_type, NULL)) return;
        CwVar_t* v = cg_var_find(g, var);
        LLVMValueRef out = cg_cell_alloca(g, "elem.out");
        LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out,
                                             cg_rt_i8_ptr(g), "");
        LLVMTypeRef pt_val[2] = { cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
        LLVMValueRef val = cg_rt_declare(g, iname,
                                         LLVMInt1TypeInContext(cg_ctx(g)),
                                         pt_val, 2);
        LLVMValueRef val_args[2] = { it8, out8 };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(val), val,
                       val_args, 2, "");
        /* 元素值写回循环变量 (cell -> 变量自然存储) */
        if (v && v->is_value) {
            LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                            out, "ev");
            LLVMBuildStore(cg_b(g), h, v->slot);
        } else if (v && v->blob) {
            /* 结构体/枚举元素: cell 指向 arena blob 拷贝, 整块拷回 */
            LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                            out, "ev");
            LLVMValueRef a = LLVMBuildExtractValue(cg_b(g), h, 0, "ev.a");
            LLVMValueRef src = LLVMBuildIntToPtr(cg_b(g), a,
                                                 cg_rt_i8_ptr(g), "ev.p");
            LLVMBuildMemCpy(cg_b(g), cg_blob_i8(g, v->blob), 1, src, 1,
                            cg_i64(g, (uint64_t)v->blob_size));
        } else if (v && v->slot) {
            /* 标量元素: 从 cell 指向的 arena 单元加载值 */
            LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type,
                                            out, "ev");
            LLVMValueRef a = LLVMBuildExtractValue(cg_b(g), h, 0, "ev.a");
            LLVMValueRef p = LLVMBuildIntToPtr(
                cg_b(g), a,
                LLVMPointerType(LLVMVoidTypeInContext(cg_ctx(g)), 0),
                "ev.p");
            size_t wsz = 0;
            LLVMTypeRef vt = cg_scalar_type(g, v->type_name, &wsz);
            if (!vt && (cg_is_fnptr(v->type_name)
                        || cg_is_rawptr(v->type_name))) {
                vt = LLVMInt64TypeInContext(cg_ctx(g));
            }
            if (vt) {
                LLVMValueRef x = LLVMBuildLoad2(cg_b(g), vt, p, "ev.v");
                LLVMBuildStore(cg_b(g), x, v->slot);
            }
        }
    }
    if (!cg_loop_push(g, end_bb, next_bb)) return;
    cg_block(g, cw_object_get(node, "body"));
    cg_var_pop_scope(g);
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
        LLVMValueRef sc = cg_cell_alloca(g, "pat.s");
        LLVMValueRef lc = cg_cell_alloca(g, "pat.l");
        LLVMBuildStore(cg_b(g), subj.handle, sc);
        LLVMBuildStore(cg_b(g), le.handle, lc);
        LLVMValueRef sr8 = LLVMBuildBitCast(cg_b(g), sc, cg_rt_i8_ptr(g), "");
        LLVMValueRef lr8 = LLVMBuildBitCast(cg_b(g), lc, cg_rt_i8_ptr(g), "");
        LLVMTypeRef pt[3] = { LLVMInt32TypeInContext(cg_ctx(g)),
                              cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
        LLVMValueRef f = cg_rt_declare(
            g, "cwobj_value_equal", LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
        LLVMValueRef av[3] = { cg_i32(g, (uint32_t)CWString), sr8, lr8 };
        return LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3,
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
    LLVMValueRef trec = cg_cell_alloca(g, "pat.recv");
    LLVMBuildStore(cg_b(g), tup.handle, trec);
    LLVMValueRef trec8 = LLVMBuildBitCast(cg_b(g), trec, cg_rt_i8_ptr(g), "");
    LLVMValueRef out = cg_cell_alloca(g, "pat.tup");
    LLVMValueRef out8 = LLVMBuildBitCast(cg_b(g), out, cg_rt_i8_ptr(g), "");
    LLVMTypeRef pt[3] = { cg_rt_i8_ptr(g),
                          LLVMInt64TypeInContext(cg_ctx(g)),
                          cg_rt_i8_ptr(g) };
    LLVMValueRef f = cg_rt_declare(
        g, "cwtuple_at", LLVMInt1TypeInContext(cg_ctx(g)), pt, 3);
    LLVMValueRef av[3] = { trec8, cg_i64(g, (uint64_t)idx), out8 };
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(f), f, av, 3, "");
    LLVMValueRef h = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, out, "th");
    return (CwExpr){ h, elem_type };
}

/* 结构体模式某字段 → 子表达式 (句柄 + 字段类型) */
static CwExpr cg_pattern_struct_field(
    CwCodegen_t* g, CwExpr obj,
    const CwLayout_t* L,
    const char* fname,
    const char* ftype
) {
    size_t fi = 0;
    bool found = false;
    for (size_t i = 0; i < L->field_count; i++) {
        if (strcmp(L->fields[i].name, fname) == 0) {
            fi = i;
            found = true;
            break;
        }
    }
    if (!found) {
        cg_error(g, "struct pattern has no field %s", fname);
        return (CwExpr){ NULL, NULL };
    }
    LLVMValueRef base = cg_expr_blob_i8(g, obj);
    return cg_read_struct_field(g, base, L, fi, ftype);
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
        if (!cg_var_store(g, v, be)) return false;
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
        if (!cg_var_store(g, rv, val)) {
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
                /* bug-53: 条件 elif 体的 let 也要收进独立作用域
                 * (模式分支在下方 push, 两条路径各 push 一次) */
                cg_var_push_scope(g);
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
            cg_var_pop_scope(g);
            eval_bb = next_fail;
        }
    }
    if (has_else) {
        LLVMPositionBuilderAtEnd(cg_b(g), fallthrough);
        cg_var_push_scope(g);
        cg_block(g, else_v);
        cg_var_pop_scope(g);
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
        if (!cg_var_store(g, v, a)) return;
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
            if (!cg_var_store(g, v, a)) return false;
            continue;
        }
        /* self / &T 按引用传递: 形参别名调用者的数据 (值 = CWValue,
         * address 直指调用者 blob/容器数据), 字段修改可传回调用者 */
        v->is_ref_param = true;
        if (!v->is_value) {
            v->slot = cg_alloca(g, g->ll->handle_type, pname);
            v->blob = NULL;
            v->is_enum = false;
            v->is_array = false;
        }
        LLVMBuildStore(cg_b(g), arg, v->slot);
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
    CwTypeId* targ_alloc = NULL;
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
    } else if (e->kind == CW_SYM_METHOD && e->owner) {
        /* todo-147: 具体类型特化 (``extra Cell<Int>``): owner decl 无
         * 泛型形参, 但目标类型携带具体实参 —— 以「结构体形参名 → 特化
         * 实参」建立替换上下文, 方法体内的 T 与 Self 引用按实参解析
         * (cg_type_id_of 的 Self 路径按 current_owner + targs intern)。 */
        const CwNode_t* owner_decl = NULL;
        for (size_t i = 0; i < cwmodule_binding_count(g->m); i++) {
            const CwBinding_t* bx = cwmodule_binding(g->m, i);
            if (bx->owner && strcmp(bx->owner, e->owner) == 0
                && bx->fn_id == e->decl->id) {
                owner_decl = cwmodule_node(g->m, bx->decl_id);
                break;
            }
        }
        if (owner_decl) {
            cw_value* otp = cw_object_get(owner_decl->value, "params");
            const size_t n_owner = (otp && cw_typeof(otp) == CW_ARRAY)
                ? cw_array_size(otp) : 0;
            cw_value* st = cw_object_get(owner_decl->value, "struct");
            cw_value* sargs = st ? cw_object_get(st, "args") : NULL;
            const size_t na = (sargs && cw_typeof(sargs) == CW_ARRAY)
                ? cw_array_size(sargs) : 0;
            if (n_owner == 0 && na > 0) {
                const CwSymbol_t* os = cwmodule_find_symbol(g->m, e->owner);
                const CwNode_t* sdecl =
                    os ? cwmodule_node(g->m, os->ref) : NULL;
                cw_value* sp =
                    sdecl ? cw_object_get(sdecl->value, "params") : NULL;
                const size_t nsp = (sp && cw_typeof(sp) == CW_ARRAY)
                    ? cw_array_size(sp) : 0;
                if (sdecl && nsp == na) {
                    char** names = (char**)malloc(na * sizeof(char*));
                    targ_alloc = (CwTypeId*)malloc(na * sizeof(CwTypeId));
                    if (!names || !targ_alloc) {
                        free(names);
                        free(targ_alloc);
                        targ_alloc = NULL;
                        cg_error(g,
                                 "failed to allocate specialized method "
                                 "substitution context");
                        return;
                    }
                    size_t ok = 1;
                    for (size_t i = 0; i < na; i++) {
                        cw_value* a = cw_array_get(sargs, i);
                        names[i] = (char*)cg_json_name(cw_array_get(sp, i));
                        targ_alloc[i] =
                            a ? cg_type_id_of(g, a) : CW_TYPE_INVALID;
                        if (!names[i] || targ_alloc[i] == CW_TYPE_INVALID) {
                            ok = 0;
                            break;
                        }
                    }
                    if (ok) {
                        tparam_alloc = names;
                        g->tparam_names = (const char**)names;
                        g->targs = targ_alloc;
                        g->tcount = na;
                    } else {
                        free(names);
                        free(targ_alloc);
                        targ_alloc = NULL;
                    }
                }
            }
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
    free(targ_alloc);
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
    /* todo-122: extra 块内的关联常量同样在 main 前初始化。
     * 存储键 = "<Owner>.<NAME>", 与 cg_name_member 读取路径一致。 */
    const size_t nn = cwmodule_node_count(g->m);
    for (size_t i = 0; i < nn && !g->failed; i++) {
        const CwNode_t* nd = cwmodule_node_at(g->m, i);
        if (!nd || strcmp(nd->kind, "ExtraDecl") != 0) continue;
        cw_value* st = cw_object_get(nd->value, "struct");
        const char* sname = cg_json_name(st);
        if (!sname) continue;
        cw_value* consts = cw_object_get(nd->value, "consts");
        if (!consts || cw_typeof(consts) != CW_ARRAY) continue;
        const size_t nc = cw_array_size(consts);
        for (size_t j = 0; j < nc && !g->failed; j++) {
            cw_value* c = cw_array_get(consts, j);
            const char* cn = cg_json_name(c);
            if (!cn) {
                cg_error(g, "associated const in extra of %s "
                            "is missing a name", sname);
                return;
            }
            cw_value* type_obj = cw_object_get(c, "type");
            cw_value* ann = cw_object_get(c, "ann");
            cw_value* at = ann ? cw_object_get(ann, "type") : NULL;
            if (at && cw_typeof(at) == CW_OBJECT) type_obj = at;
            const char* tname = type_obj
                ? cg_type_name_of(g, type_obj) : NULL;
            if (!tname) {
                cg_error(g, "associated const %s::%s is missing a type",
                         sname, cn);
                return;
            }
            char key[256];
            snprintf(key, sizeof(key), "%s.%s", sname, cn);
            LLVMValueRef fn = cg_const_init_fn(
                g, key, c, tname, type_obj);
            if (!fn || g->failed) return;
            LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(fn),
                           fn, NULL, 0, "");
        }
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

    /* bug-30: C 入口带标准 (argc, argv) 签名; 用户 main 声明一个
     * Vector<String> 形参时, 由 rt 的 cw_builtin_main_args 打包注入 */
    LLVMTypeRef ret_i32 = LLVMInt32TypeInContext(cg_ctx(g));
    LLVMTypeRef main_pt[2] = { ret_i32, cg_rt_i8_ptr(g) };
    LLVMValueRef main_fn = LLVMAddFunction(g->ll->module, "main",
                                           LLVMFunctionType(ret_i32, main_pt,
                                                            2, false));
    LLVMBasicBlockRef entry = LLVMAppendBasicBlockInContext(cg_ctx(g), main_fn,
                                                            "entry");
    LLVMPositionBuilderAtEnd(cg_b(g), entry);
    g->current_fn = main_fn;
    g->current_owner = NULL;

    /* GC 初始化 (todo-35): 栈底在此记录 (主线程); 静态根在 inits 后注册 */
    LLVMTypeRef gc_pr[2] = { LLVMPointerType(
                                 LLVMVoidTypeInContext(cg_ctx(g)), 0),
                             LLVMInt64TypeInContext(cg_ctx(g)) };
    LLVMValueRef gc_init = cg_rt_declare(
        g, "cwgc_init", LLVMVoidTypeInContext(cg_ctx(g)), NULL, 0);
    LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(gc_init), gc_init,
                   NULL, 0, "");

    cg_emit_const_inits(g);
    if (g->failed) return;

    cg_emit_static_inits(g);
    if (g->failed) return;

    /* 静态/常量全局注册为 GC 根 (名字前缀匹配, 尺寸按 ABI 计算) */
    LLVMValueRef reg_fn = cg_rt_declare(
        g, "cwgc_global_register", LLVMInt1TypeInContext(cg_ctx(g)),
        gc_pr, 2);
    for (LLVMValueRef gv = LLVMGetFirstGlobal(g->ll->module); gv;
         gv = LLVMGetNextGlobal(gv)) {
        const char* gname = LLVMGetValueName(gv);
        if (!gname || (strncmp(gname, "cwind.static.", 13) != 0
                       && strncmp(gname, "cwind.const.", 12) != 0)) {
            continue;
        }
        LLVMTypeRef gty = LLVMGlobalGetValueType(gv);
        const size_t gsz = cwllvm_abisize(g->ll, gty);
        if (gsz == 0) continue;
        LLVMValueRef rav[2] = { gv, cg_i64(g, (uint64_t)gsz) };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(reg_fn), reg_fn,
                       rav, 2, "");
    }

    bool want_args = false;
    if (cwmodule_fn_param_count(main_sym->decl) == 1) {
        cw_value* p = cwmodule_fn_param(main_sym->decl, 0);
        cw_value* ptype = p ? cw_object_get(p, "type") : NULL;
        const char* tname = ptype ? cg_type_name_of(g, ptype) : NULL;
        want_args = tname && strcmp(tname, "Vector") == 0;
    }

    const unsigned nparams = LLVMCountParams(user_main);
    LLVMValueRef* argv = (LLVMValueRef*)malloc(
        (nparams ? nparams : 1) * sizeof(LLVMValueRef));
    if (!argv) {
        cg_error(g, "failed to allocate the main argument array");
        return;
    }
    for (unsigned i = 0; i < nparams; i++) argv[i] = cg_null_handle(g);
    if (want_args) {
        /* bug-30: argc/argv 打包为 Vector<String> 值 */
        LLVMValueRef val = cg_cell_alloca(g, "args.val");
        LLVMBuildStore(cg_b(g), cg_null_handle(g), val);
        LLVMTypeRef pr_pack[3] = { LLVMInt32TypeInContext(cg_ctx(g)),
                                   cg_rt_i8_ptr(g), cg_rt_i8_ptr(g) };
        LLVMValueRef pack = cg_rt_declare(g, "cw_builtin_main_args",
                                          LLVMInt1TypeInContext(cg_ctx(g)),
                                          pr_pack, 3);
        LLVMValueRef pack_args[3] = {
            LLVMGetParam(main_fn, 0),
            LLVMGetParam(main_fn, 1),
            LLVMBuildBitCast(cg_b(g), val, cg_rt_i8_ptr(g), ""),
        };
        LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(pack), pack,
                       pack_args, 3, "");
        argv[0] = LLVMBuildLoad2(cg_b(g), g->ll->handle_type, val,
                                 "args.h");
    }
    LLVMValueRef h = LLVMBuildCall2(cg_b(g), LLVMGlobalGetValueType(user_main),
                                    user_main, argv, nparams, "main.call");
    free(argv);

    cw_value* rt = cwmodule_fn_return_type(main_sym->decl);
    const char* ret_type = rt ? cg_type_name_of(g, rt) : NULL;
    if (ret_type && cg_is_int(ret_type)) {
        LLVMValueRef addr = LLVMBuildExtractValue(cg_b(g), h, 0, "addr");
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






