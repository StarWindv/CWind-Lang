/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwsymbol.c
 */

#include "cwsymbol.h"

#include "../rt-src/include/stl/json/cwind_json.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static bool cwsym_buf_append(char* buf, size_t cap, size_t* off,
                             const char* s) {
    const size_t n = strlen(s);
    if (*off + n + 1 > cap) return false;
    memcpy(buf + *off, s, n);
    *off += n;
    buf[*off] = '\0';
    return true;
}

bool cw_mangle_fn(char* buf, size_t cap, const char* name) {
    if (!buf || cap == 0 || !name) return false;
    size_t off = 0;
    return cwsym_buf_append(buf, cap, &off, "cwind.fn.")
        && cwsym_buf_append(buf, cap, &off, name);
}

bool cw_mangle_method(char* buf, size_t cap,
                      const char* owner, const char* name) {
    if (!buf || cap == 0 || !owner || !name) return false;
    size_t off = 0;
    return cwsym_buf_append(buf, cap, &off, "cwind.method.")
        && cwsym_buf_append(buf, cap, &off, owner)
        && cwsym_buf_append(buf, cap, &off, ".")
        && cwsym_buf_append(buf, cap, &off, name);
}

bool cw_mangle_instance(char* buf, size_t cap,
                        const char* base,
                        const CwTypeTable_t* types,
                        const CwTypeId* args, size_t arg_count) {
    if (!buf || cap == 0 || !base || (arg_count > 0 && !args)) return false;
    size_t off = 0;
    if (!cwsym_buf_append(buf, cap, &off, base)) return false;
    for (size_t i = 0; i < arg_count; i++) {
        const char* name = cwtype_name(types, args[i]);
        if (!name) return false;
        if (!cwsym_buf_append(buf, cap, &off, ".")) return false;
        if (!cwsym_buf_append(buf, cap, &off, name)) return false;
    }
    return true;
}

void cwsym_table_init(CwSymTable_t* s) {
    if (!s) return;
    memset(s, 0, sizeof(*s));
}

static void cwsym_item_destroy(CwSymEntry_t* e) {
    free(e->mangled);
    free(e->inst_args);
}

void cwsym_table_destroy(CwSymTable_t* s) {
    if (!s) return;
    for (size_t i = 0; i < s->count; i++) {
        cwsym_item_destroy(&s->items[i]);
    }
    free(s->items);
    memset(s, 0, sizeof(*s));
}

const CwSymEntry_t* cwsym_add(CwSymTable_t* s, const char* mangled,
                              const char* name, CwSymKind_t kind,
                              const char* owner, const char* trait,
                              const CwTypeId* inst_args,
                              size_t inst_count,
                              const CwNode_t* decl) {
    if (!s || !mangled || !name) return NULL;
    const CwSymEntry_t* hit = cwsym_find_mangled(s, mangled);
    if (hit) return hit;

    if (s->count == s->cap) {
        const size_t nc = s->cap ? s->cap * 2 : 16;
        CwSymEntry_t* ni = (CwSymEntry_t*)realloc(
            s->items, nc * sizeof(CwSymEntry_t));
        if (!ni) return NULL;
        s->items = ni;
        s->cap = nc;
    }

    CwSymEntry_t* e = &s->items[s->count];
    memset(e, 0, sizeof(*e));
    e->mangled = (char*)malloc(strlen(mangled) + 1);
    if (!e->mangled) return NULL;
    memcpy(e->mangled, mangled, strlen(mangled) + 1);
    e->name = name;
    e->kind = kind;
    e->owner = owner;
    e->trait = trait;
    e->decl = decl;
    if (inst_count > 0) {
        e->inst_args = (CwTypeId*)malloc(inst_count * sizeof(CwTypeId));
        if (!e->inst_args) {
            free(e->mangled);
            return NULL;
        }
        memcpy(e->inst_args, inst_args, inst_count * sizeof(CwTypeId));
    }
    e->inst_count = inst_count;
    s->count++;
    return e;
}

static bool cwsym_json_has_params(cw_value* fn_node) {
    cw_value* tp = cw_object_get(fn_node, "type_params");
    return tp && cw_typeof(tp) == CW_ARRAY && cw_array_size(tp) > 0;
}

bool cwsym_build_from_module(CwSymTable_t* s, const CwModule_t* m) {
    if (!s || !m) return false;

    for (size_t i = 0; i < cwmodule_symbol_count(m); i++) {
        const CwSymbol_t* sym = cwmodule_symbol(m, i);
        if (strcmp(sym->kind, "fn") != 0) continue;
        const CwNode_t* decl = cwmodule_node(m, sym->ref);
        if (!decl) continue;
        char mangled[512];
        if (!cw_mangle_fn(mangled, sizeof(mangled), sym->name)) return false;
        const CwSymKind_t kind = cwsym_json_has_params(decl->value)
            ? CW_SYM_TEMPLATE : CW_SYM_FN;
        if (!cwsym_add(s, mangled, sym->name, kind, NULL, NULL,
                       NULL, 0, decl)) {
            return false;
        }
    }

    for (size_t i = 0; i < cwmodule_binding_count(m); i++) {
        const CwBinding_t* b = cwmodule_binding(m, i);
        const CwNode_t* decl = cwmodule_node(m, b->fn_id);
        if (!decl) continue;
        const char* fname = cwmodule_fn_name(decl);
        if (!fname) continue;
        char mangled[512];
        if (!cw_mangle_method(mangled, sizeof(mangled),
                              b->owner, fname)) {
            return false;
        }
        const CwSymKind_t kind = cwsym_json_has_params(decl->value)
            ? CW_SYM_TEMPLATE : CW_SYM_METHOD;
        if (!cwsym_add(s, mangled, fname, kind, b->owner, b->trait,
                       NULL, 0, decl)) {
            return false;
        }
    }
    return true;
}

const CwSymEntry_t* cwsym_find_mangled(const CwSymTable_t* s,
                                       const char* mangled) {
    if (!s || !mangled) return NULL;
    for (size_t i = 0; i < s->count; i++) {
        if (strcmp(s->items[i].mangled, mangled) == 0) {
            return &s->items[i];
        }
    }
    return NULL;
}

const CwSymEntry_t* cwsym_find(const CwSymTable_t* s,
                               const char* owner, const char* name) {
    if (!s || !name) return NULL;
    for (size_t i = 0; i < s->count; i++) {
        const CwSymEntry_t* e = &s->items[i];
        const bool owner_ok = (owner == NULL && e->owner == NULL)
            || (owner != NULL && e->owner != NULL
                && strcmp(owner, e->owner) == 0);
        if (owner_ok && strcmp(e->name, name) == 0) return e;
    }
    return NULL;
}
