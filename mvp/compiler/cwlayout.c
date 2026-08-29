/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwlayout.c
 */

#include "cwlayout.h"

#include "../rt-src/include/stl/json/cwind_json.h"

#include <stdlib.h>
#include <string.h>

/*
 * C-Like-Layout (todo-50): 字段按 C 规则自然对齐放置,
 * 嵌套结构体内联展开 (递归, 深度受限), 引用型字段收进 24B CWValue cell。
 * 实例 blob 无头无槽, 整块 memcpy 即深拷贝。
 */

static bool cwlayout_json_bool(
    cw_value* v,
    bool* out
) {
    if (!v || cw_typeof(v) != CW_BOOL) return false;
    return cw_as_bool(v, out) == CW_OK;
}

static const char* cwlayout_json_name(
    cw_value* obj
) {
    if (!obj || cw_typeof(obj) != CW_OBJECT) return NULL;
    cw_value* name = cw_object_get(obj, "name");
    if (!name || cw_typeof(name) != CW_STRING) return NULL;
    return cw_string_cstr(name);
}

/* 泛型替换: 叶子名匹配参数名 -> 具体实参; 否则递归替换 args 后 intern */
static CwTypeId cwlayout_subst(
    CwTypeTable_t* t,
    cw_value* type_obj,
    const char** params, size_t nparams,
    const CwTypeId* args, size_t nargs
) {
    if (!t || !type_obj || cw_typeof(type_obj) != CW_OBJECT) {
        return CW_TYPE_INVALID;
    }
    const char* name = cwlayout_json_name(type_obj);
    if (!name) return CW_TYPE_INVALID;

    for (size_t i = 0; i < nparams; i++) {
        if (params[i] && strcmp(name, params[i]) == 0) {
            return (i < nargs) ? args[i] : CW_TYPE_INVALID;
        }
    }

    cw_value* args_v = cw_object_get(type_obj, "args");
    const size_t ac = (args_v && cw_typeof(args_v) == CW_ARRAY)
        ? cw_array_size(args_v) : 0;
    CwTypeId* sub = NULL;
    if (ac > 0) {
        sub = (CwTypeId*)malloc(ac * sizeof(CwTypeId));
        if (!sub) return CW_TYPE_INVALID;
        for (size_t i = 0; i < ac; i++) {
            sub[i] = cwlayout_subst(t, cw_array_get(args_v, i),
                                    params, nparams, args, nargs);
            if (sub[i] == CW_TYPE_INVALID) {
                free(sub);
                return CW_TYPE_INVALID;
            }
        }
    }
    CwTypeId id = cwtype_intern(t, name, sub, ac);
    free(sub);
    return id;
}

/* ---- 字段尺寸/对齐 (C 规则) ---- */

static size_t cwlayout_scalar_size(const char* name) {
    if (!name) return 0;
    if (strcmp(name, "Int") == 0 || strcmp(name, "UInt") == 0
        || strcmp(name, "Int16") == 0 || strcmp(name, "UInt16") == 0) {
        return 2;
    }
    if (strcmp(name, "Int8") == 0 || strcmp(name, "UInt8") == 0
        || strcmp(name, "Byte") == 0 || strcmp(name, "Bool") == 0) {
        return 1;
    }
    if (strcmp(name, "Int32") == 0 || strcmp(name, "UInt32") == 0
        || strcmp(name, "Float") == 0) {
        return 4;
    }
    if (strcmp(name, "Int64") == 0 || strcmp(name, "UInt64") == 0
        || strcmp(name, "Float64") == 0) {
        return 8;
    }
    return 0;
}

/* "[T; N]" 解析 */
static bool cwlayout_array_info(
    const char* tname,
    char* elem, size_t elem_cap,
    size_t* out_n
) {
    if (!tname || tname[0] != '[') return false;
    const char* semi = strrchr(tname, ';');
    const size_t len = strlen(tname);
    if (!semi || len < 3 || tname[len - 1] != ']') return false;
    size_t lo = 1;
    size_t hi = (size_t)(semi - tname);
    while (lo < hi && (tname[lo] == ' ' || tname[lo] == '\t')) lo++;
    while (hi > lo && (tname[hi - 1] == ' ' || tname[hi - 1] == '\t')) hi--;
    if (lo >= hi || hi - lo + 1 >= elem_cap) return false;
    memcpy(elem, tname + lo, hi - lo);
    elem[hi - lo] = '\0';
    const char* p = semi + 1;
    while (*p == ' ' || *p == '\t') p++;
    char* end = NULL;
    const unsigned long long v = strtoull(p, &end, 10);
    if (!end || end == p) return false;
    while (*end == ' ' || *end == '\t') end++;
    if (*end != ']') return false;
    if (v == 0 || v > 65536) return false;
    if (out_n) *out_n = (size_t)v;
    return true;
}

static size_t cwlayout_align_up(size_t v, size_t a) {
    return (v + a - 1) & ~(a - 1);
}

/* 按类型名查结构体符号 */
static const CwNode_t* cwlayout_struct_decl(
    const CwLayoutCache_t* c, const CwModule_t* m,
    const char* name
) {
    if (!c || !m || !name) return NULL;
    const CwSymbol_t* sym = cwmodule_find_symbol(m, name);
    if (!sym || strcmp(sym->kind, "struct") != 0 || !sym->ref) return NULL;
    return cwmodule_node(m, sym->ref);
}

/* 单个字段的尺寸/对齐; inline_struct 输出是否为内联嵌套结构体。
 * depth 防自包含结构体 (值语义下无解, 深度超限拒绝)。 */
static bool cwlayout_field_meta(
    CwLayoutCache_t* c, const CwModule_t* m,
    const char* fname,
    size_t* size, size_t* align,
    size_t depth
) {
    const size_t sz = cwlayout_scalar_size(fname);
    if (sz > 0) {
        *size = sz;
        *align = sz;
        return true;
    }
    char elem[128];
    size_t n = 0;
    if (cwlayout_array_info(fname, elem, sizeof(elem), &n)) {
        const size_t esz = cwlayout_scalar_size(elem);
        if (esz == 0) return false; /* 数组元素必须定宽标量 */
        *size = esz * n;
        *align = esz;
        return true;
    }
    if (fname && (strncmp(fname, "*const ", 7) == 0
                  || strncmp(fname, "*mut ", 5) == 0
                  || strncmp(fname, "fn(", 3) == 0)) {
        *size = 8; /* 地址即值 (rawptr 值的 address 就是地址本身) */
        *align = 8;
        return true;
    }
    /* 嵌套结构体: 内联展开 (递归; CwNodeId_t 无泛型上下文, 嵌套泛型
     * 结构体走 cell — 实参替换后的非泛型名才内联) */
    if (fname && depth < CWLAYOUT_MAX_DEPTH) {
        const CwNode_t* decl = cwlayout_struct_decl(c, m, fname);
        if (decl) {
            const CwLayout_t* inner = cwlayout_get(c, m, decl, NULL, 0);
            if (inner) {
                *size = inner->size;
                *align = inner->align;
                return true;
            }
        }
    }
    /* 其余引用型: String/Vector/Map/Set/Tuple/枚举/泛型遗留 -> cell */
    *size = CWLAYOUT_CELL_SIZE;
    *align = 8;
    return true;
}

bool cwlayout_cache_init(
    CwLayoutCache_t* c,
    CwTypeTable_t* types
) {
    if (!c || !types) return false;
    memset(c, 0, sizeof(*c));
    c->types = types;
    return true;
}

void cwlayout_cache_destroy(
    CwLayoutCache_t* c
) {
    if (!c) return;
    for (size_t i = 0; i < c->count; i++) {
        free(c->items[i]->fields);
        free(c->items[i]);
    }
    free(c->items);
    memset(c, 0, sizeof(*c));
}

const CwLayout_t* cwlayout_get(
    CwLayoutCache_t* c,
    const CwModule_t* m,
    const CwNode_t* struct_decl,
    const CwTypeId* args,
    size_t arg_count
) {
    if (!c || !m || !struct_decl
        || strcmp(struct_decl->kind, "StructDecl") != 0) {
        return NULL;
    }

    const char* name = cwlayout_json_name(struct_decl->value);
    if (!name) return NULL;
    const CwTypeId inst = cwtype_intern(c->types, name, args, arg_count);
    if (inst == CW_TYPE_INVALID) return NULL;

    for (size_t i = 0; i < c->count; i++) {
        if (c->items[i]->type == inst) return c->items[i];
    }

    /* 收集泛型参数名 */
    cw_value* params_v = cw_object_get(struct_decl->value, "params");
    const size_t nparams = (params_v && cw_typeof(params_v) == CW_ARRAY)
        ? cw_array_size(params_v) : 0;
    const char** params = NULL;
    if (nparams > 0) {
        params = (const char**)malloc(nparams * sizeof(const char*));
        if (!params) return NULL;
        for (size_t i = 0; i < nparams; i++) {
            params[i] = cwlayout_json_name(cw_array_get(params_v, i));
        }
    }

    /* 统计非 static 字段数 */
    cw_value* fields_v = cw_object_get(struct_decl->value, "fields");
    if (!fields_v || cw_typeof(fields_v) != CW_ARRAY) {
        free(params);
        return NULL;
    }
    const size_t nf = cw_array_size(fields_v);
    size_t live = 0;
    for (size_t i = 0; i < nf; i++) {
        cw_value* f = cw_array_get(fields_v, i);
        bool is_static = false;
        cwlayout_json_bool(cw_object_get(f, "static"), &is_static);
        if (!is_static) live++;
    }

    CwLayout_t* L = (CwLayout_t*)malloc(sizeof(CwLayout_t));
    if (!L) {
        free(params);
        return NULL;
    }
    L->type = inst;
    L->size = 0;
    L->align = 1;
    L->field_count = live;
    L->fields = live ? (CwFieldLayout_t*)malloc(live * sizeof(CwFieldLayout_t))
                     : NULL;
    if (live && !L->fields) {
        free(L);
        free(params);
        return NULL;
    }

    /* C-Like-Layout: 偏移按字段对齐累进 (声明序) */
    size_t off = 0;
    size_t idx = 0;
    for (size_t i = 0; i < nf; i++) {
        cw_value* f = cw_array_get(fields_v, i);
        bool is_static = false;
        cwlayout_json_bool(cw_object_get(f, "static"), &is_static);
        if (is_static) continue;

        const char* fname = cwlayout_json_name(f);
        cw_value* ftype = cw_object_get(f, "type");
        /* bug-23/29: 优先采用 SA 解析后的 ann.type (typedef 别名展开/
         * 精化还原); 泛型参数叶在 ann.type 里保留原参数名, 替换不受影响 */
        cw_value* fann = cw_object_get(f, "ann");
        cw_value* resolved = fann ? cw_object_get(fann, "type") : NULL;
        if (resolved && cw_typeof(resolved) == CW_OBJECT) {
            ftype = resolved;
        }
        const CwTypeId tid = cwlayout_subst(c->types, ftype,
                                            params, nparams,
                                            args, arg_count);
        if (!fname || tid == CW_TYPE_INVALID) {
            free(L->fields);
            free(L);
            free(params);
            return NULL;
        }
        const char* rname = cwtype_name(c->types, tid);
        size_t fsz = 0;
        size_t fal = 1;
        if (!cwlayout_field_meta(c, m, rname, &fsz, &fal, 0)) {
            free(L->fields);
            free(L);
            free(params);
            return NULL;
        }
        if (fal > L->align) L->align = fal;
        off = cwlayout_align_up(off, fal);
        L->fields[idx].name   = fname;
        L->fields[idx].offset = off;
        L->fields[idx].size   = fsz;
        L->fields[idx].align  = fal;
        L->fields[idx].type   = tid;
        off += fsz;
        idx++;
    }
    free(params);

    /* 尾部按最大对齐补齐 */
    L->size = cwlayout_align_up(off, L->align);
    if (L->size == 0) L->size = 1; /* 空结构体至少占 1 字节 */

    if (c->count == c->cap) {
        const size_t nc = c->cap ? c->cap * 2 : 16;
        CwLayout_t** ni = (CwLayout_t**)realloc(
            c->items, nc * sizeof(CwLayout_t*));
        if (!ni) {
            free(L->fields);
            free(L);
            return NULL;
        }
        c->items = ni;
        c->cap = nc;
    }
    c->items[c->count++] = L;
    return L;
}
