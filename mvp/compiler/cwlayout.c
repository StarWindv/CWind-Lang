/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwlayout.c
 */

#include "cwlayout.h"

#include "../rt-src/include/stl/json/cwind_json.h"

#include <stdlib.h>
#include <string.h>

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
    L->field_count = live;
    L->fields = live ? (CwFieldLayout_t*)malloc(live * sizeof(CwFieldLayout_t))
                     : NULL;
    if (live && !L->fields) {
        free(L);
        free(params);
        return NULL;
    }

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
        L->fields[idx].name   = fname;
        L->fields[idx].offset = idx * CWLAYOUT_SLOT_SIZE;
        L->fields[idx].type   = tid;
        idx++;
    }
    free(params);

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
