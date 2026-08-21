/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwtype.c
 */

#include "cwtype.h"

/* 实现只在 cwmodule.c 编译一次, 这里只取声明 */
#include "../rt-src/include/stl/json/cwind_json.h"

#include <stdlib.h>
#include <string.h>

void cwtype_table_init(
    CwTypeTable_t* t
) {
    if (!t) return;
    memset(t, 0, sizeof(*t));
}

static char* cwtype_strdup(
    const char* s
) {
    const size_t n = strlen(s);
    char* p = (char*)malloc(n + 1);
    if (p) memcpy(p, s, n + 1);
    return p;
}

static void cwtype_item_destroy(
    CwType_t* item
) {
    free(item->name);
    free(item->args);
}

void cwtype_table_destroy(
    CwTypeTable_t* t
) {
    if (!t) return;
    for (size_t i = 0; i < t->count; i++) {
        cwtype_item_destroy(&t->items[i]);
    }
    free(t->items);
    memset(t, 0, sizeof(*t));
}

static bool cwtype_items_equal(
    const CwType_t* a,
    const char* name,
    const CwTypeId* args,
    size_t arg_count
) {
    if (strcmp(a->name, name) != 0 || a->arg_count != arg_count) {
        return false;
    }
    for (size_t i = 0; i < arg_count; i++) {
        /* id 本身就是结构等值的结果, 直接比较 */
        if (a->args[i] != args[i]) return false;
    }
    return true;
}

CwTypeId cwtype_intern(
    CwTypeTable_t* t, const char* name,
    const CwTypeId* args, size_t arg_count
) {
    if (!t || !name) return CW_TYPE_INVALID;

    for (size_t i = 0; i < t->count; i++) {
        if (cwtype_items_equal(&t->items[i], name, args, arg_count)) {
            return (CwTypeId)(i + 1);
        }
    }

    if (t->count == t->cap) {
        const size_t nc = t->cap ? t->cap * 2 : 16;
        CwType_t* ni = (CwType_t*)realloc(t->items, nc * sizeof(CwType_t));
        if (!ni) return CW_TYPE_INVALID;
        t->items = ni;
        t->cap = nc;
    }

    CwType_t* item = &t->items[t->count];
    memset(item, 0, sizeof(*item));
    item->name = cwtype_strdup(name);
    if (!item->name) return CW_TYPE_INVALID;
    if (arg_count > 0) {
        item->args = (CwTypeId*)malloc(arg_count * sizeof(CwTypeId));
        if (!item->args) {
            free(item->name);
            item->name = NULL;
            return CW_TYPE_INVALID;
        }
        memcpy(item->args, args, arg_count * sizeof(CwTypeId));
    }
    item->arg_count = arg_count;
    t->count++;
    return (CwTypeId)t->count;
}

static CwTypeId cwtype_intern_json(
    CwTypeTable_t* t,
    cw_value* type_obj
) {
    if (!type_obj || cw_typeof(type_obj) != CW_OBJECT) {
        return CW_TYPE_INVALID;
    }
    cw_value* name_v = cw_object_get(type_obj, "name");
    if (!name_v || cw_typeof(name_v) != CW_STRING) {
        return CW_TYPE_INVALID;
    }
    const char* name = cw_string_cstr(name_v);

    cw_value* args_v = cw_object_get(type_obj, "args");
    size_t arg_count = 0;
    CwTypeId* args = NULL;
    if (args_v && cw_typeof(args_v) == CW_ARRAY) {
        arg_count = cw_array_size(args_v);
        if (arg_count > 0) {
            args = (CwTypeId*)malloc(arg_count * sizeof(CwTypeId));
            if (!args) return CW_TYPE_INVALID;
            for (size_t i = 0; i < arg_count; i++) {
                args[i] = cwtype_intern_json(t, cw_array_get(args_v, i));
                if (args[i] == CW_TYPE_INVALID) {
                    free(args);
                    return CW_TYPE_INVALID;
                }
            }
        }
    }

    CwTypeId id = cwtype_intern(t, name, args, arg_count);
    free(args);
    if (id != CW_TYPE_INVALID) {
        cw_value* opaque_v = cw_object_get(type_obj, "opaque");
        bool op = false;
        if (opaque_v && cw_typeof(opaque_v) == CW_BOOL) {
            cw_as_bool(opaque_v, &op);
        }
        t->items[id - 1].opaque = op;
    }
    return id;
}

CwTypeId cwtype_from_json(
    CwTypeTable_t* t,
    cw_value* type_obj
) {
    if (!t) return CW_TYPE_INVALID;
    return cwtype_intern_json(t, type_obj);
}

const CwType_t* cwtype_get(
    const CwTypeTable_t* t,
    CwTypeId id
) {
    if (!t || id == CW_TYPE_INVALID || id > t->count) return NULL;
    return &t->items[id - 1];
}

const char* cwtype_name(
    const CwTypeTable_t* t,
    CwTypeId id
) {
    const CwType_t* ty = cwtype_get(t, id);
    return ty ? ty->name : NULL;
}

size_t cwtype_arg_count(
    const CwTypeTable_t* t,
    CwTypeId id
) {
    const CwType_t* ty = cwtype_get(t, id);
    return ty ? ty->arg_count : 0;
}

CwTypeId cwtype_arg(
    const CwTypeTable_t* t,
    CwTypeId id,
    size_t i
) {
    const CwType_t* ty = cwtype_get(t, id);
    if (!ty || i >= ty->arg_count) return CW_TYPE_INVALID;
    return ty->args[i];
}

bool cwtype_is_opaque(
    const CwTypeTable_t* t,
    CwTypeId id
) {
    const CwType_t* ty = cwtype_get(t, id);
    return ty ? ty->opaque : false;
}

bool cwtype_equal(
    const CwTypeTable_t* t,
    CwTypeId a,
    CwTypeId b
) {
    if (a == b) return true;
    const CwType_t* ta = cwtype_get(t, a);
    const CwType_t* tb = cwtype_get(t, b);
    if (!ta || !tb) return false;
    if (strcmp(ta->name, tb->name) != 0
        || ta->arg_count != tb->arg_count) {
        return false;
    }
    for (size_t i = 0; i < ta->arg_count; i++) {
        if (ta->args[i] != tb->args[i]) return false;
    }
    return true;
}
