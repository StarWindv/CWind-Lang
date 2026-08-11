/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwmodule.c
 */

/* cwind_json.h 内部用 fopen, Windows SDK 会告警; 局部压掉 (不改 STL) */
#define _CRT_SECURE_NO_WARNINGS 1

#define CW_JSON_IMPLEMENTATION
#include "../rt-src/include/stl/json/cwind_json.h"

#include "cwmodule.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct CwModule {
    cw_doc* doc;
    cw_value* root;
    cw_value* ast;
    const char* format;
    int64_t version;

    CwSymbol_t* symbols;
    size_t symbol_count;
    CwBinding_t* bindings;
    size_t binding_count;
    CwNode_t* nodes;
    size_t node_count;
};

#define CWMODULE_ERR_BUF 512

static char g_err[CWMODULE_ERR_BUF] = "";

static void cwmodule_set_error(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g_err, sizeof(g_err), fmt, ap);
    va_end(ap);
}

static bool cwmodule_is_string(const cw_value* v) {
    return v && cw_typeof(v) == CW_STRING;
}

static bool cwmodule_is_int(const cw_value* v) {
    return v && cw_typeof(v) == CW_INT;
}

static bool cwmodule_as_string(const cw_value* v, const char** out) {
    if (!cwmodule_is_string(v)) return false;
    *out = cw_string_cstr(v);
    return true;
}

static bool cwmodule_as_int(const cw_value* v, int64_t* out) {
    return cwmodule_is_int(v) && cw_as_int(v, out) == CW_OK;
}

/* ---- 节点池 ---- */

static bool cwmodule_collect_nodes(CwModule_t* m, cw_value* v, int depth) {
    if (!v || depth > 512) return true;

    switch (cw_typeof(v)) {
    case CW_OBJECT: {
        cw_value* kind = cw_object_get(v, "kind");
        cw_value* idv  = cw_object_get(v, "id");
        if (cwmodule_is_string(kind) && cwmodule_is_int(idv)) {
            CwNode_t* nn = (CwNode_t*)realloc(
                m->nodes, (m->node_count + 1) * sizeof(CwNode_t));
            if (!nn) {
                cwmodule_set_error("node 池扩容失败");
                return false;
            }
            m->nodes = nn;
            int64_t id = 0;
            cw_as_int(idv, &id);
            m->nodes[m->node_count].id    = id;
            m->nodes[m->node_count].kind  = cw_string_cstr(kind);
            m->nodes[m->node_count].value = v;
            m->node_count++;
        }

        size_t idx = 0;
        const char* key = NULL;
        size_t key_len = 0;
        cw_value* val = NULL;
        while (cw_object_iter(v, &idx, &key, &key_len, &val)) {
            if (!cwmodule_collect_nodes(m, val, depth + 1)) return false;
        }
        break;
    }
    case CW_ARRAY: {
        const size_t n = cw_array_size(v);
        for (size_t i = 0; i < n; i++) {
            if (!cwmodule_collect_nodes(m, cw_array_get(v, i), depth + 1)) {
                return false;
            }
        }
        break;
    }
    default:
        break;
    }
    return true;
}

static int cwmodule_node_cmp(const void* a, const void* b) {
    const CwNode_t* na = (const CwNode_t*)a;
    const CwNode_t* nb = (const CwNode_t*)b;
    return (na->id > nb->id) - (na->id < nb->id);
}

static bool cwmodule_finalize_nodes(CwModule_t* m) {
    qsort(m->nodes, m->node_count, sizeof(CwNode_t), cwmodule_node_cmp);
    for (size_t i = 1; i < m->node_count; i++) {
        if (m->nodes[i].id == m->nodes[i - 1].id) {
            cwmodule_set_error("节点 id 重复: %lld", (long long)m->nodes[i].id);
            return false;
        }
    }
    return true;
}

const CwNode_t* cwmodule_node(const CwModule_t* m, int64_t id) {
    if (!m) return NULL;
    size_t lo = 0, hi = m->node_count;
    while (lo < hi) {
        const size_t mid = lo + (hi - lo) / 2;
        if (m->nodes[mid].id < id) lo = mid + 1;
        else hi = mid;
    }
    return (lo < m->node_count && m->nodes[lo].id == id)
        ? &m->nodes[lo] : NULL;
}

/* ---- 深度校验: 种类映射与绑定位置 ---- */

static const char* cwmodule_symbol_node_kind(const char* kind) {
    if (strcmp(kind, "const") == 0)  return "ConstDecl";
    if (strcmp(kind, "type") == 0)   return "TypeDecl";
    if (strcmp(kind, "struct") == 0) return "StructDecl";
    if (strcmp(kind, "enum") == 0)   return "EnumDecl";
    if (strcmp(kind, "trait") == 0)  return "TraitDecl";
    if (strcmp(kind, "fn") == 0)     return "FnDecl";
    if (strcmp(kind, "group") == 0)  return "GroupDecl";
    return NULL;
}

static bool cwmodule_decl_type_name(const CwModule_t* m, int64_t id,
                                    const char* field,
                                    const char** out) {
    const CwNode_t* n = cwmodule_node(m, id);
    if (!n) return false;
    cw_value* t = cw_object_get(n->value, field);
    if (!t || cw_typeof(t) != CW_OBJECT) return false;
    return cwmodule_as_string(cw_object_get(t, "name"), out);
}

/* ---- 校验 ---- */

static bool cwmodule_parse_root(CwModule_t* m) {
    m->root = cw_doc_root(m->doc);
    if (!m->root || cw_typeof(m->root) != CW_OBJECT) {
        cwmodule_set_error("根节点必须是对象");
        return false;
    }

    if (!cwmodule_as_string(cw_object_get(m->root, "format"), &m->format)
        || strcmp(m->format, CWMODULE_FORMAT) != 0) {
        cwmodule_set_error("format 缺失或不是 %s", CWMODULE_FORMAT);
        return false;
    }
    if (!cwmodule_as_int(cw_object_get(m->root, "version"), &m->version)
        || m->version != CWMODULE_VERSION) {
        cwmodule_set_error("version 缺失或不支持: %lld",
                           (long long)m->version);
        return false;
    }

    m->ast = cw_object_get(m->root, "ast");
    if (!m->ast || cw_typeof(m->ast) != CW_OBJECT) {
        cwmodule_set_error("ast 缺失或不是对象");
        return false;
    }

    /* 符号表 */
    cw_value* syms = cw_object_get(m->root, "symbols");
    if (!syms || cw_typeof(syms) != CW_ARRAY) {
        cwmodule_set_error("symbols 缺失或不是数组");
        return false;
    }
    const size_t nsym = cw_array_size(syms);
    m->symbols = (CwSymbol_t*)calloc(nsym ? nsym : 1, sizeof(CwSymbol_t));
    if (!m->symbols) {
        cwmodule_set_error("符号表分配失败");
        return false;
    }
    for (size_t i = 0; i < nsym; i++) {
        cw_value* s = cw_array_get(syms, i);
        if (!s || cw_typeof(s) != CW_OBJECT
            || !cwmodule_as_string(cw_object_get(s, "name"),
                                   &m->symbols[i].name)
            || !cwmodule_as_string(cw_object_get(s, "kind"),
                                   &m->symbols[i].kind)
            || !cwmodule_as_int(cw_object_get(s, "ref"),
                                &m->symbols[i].ref)
            || m->symbols[i].ref <= 0) {
            cwmodule_set_error("symbols[%zu] 字段缺失或类型错误", i);
            return false;
        }
        m->symbol_count++;
    }

    /* 绑定表 */
    cw_value* binds = cw_object_get(m->root, "bindings");
    if (!binds || cw_typeof(binds) != CW_ARRAY) {
        cwmodule_set_error("bindings 缺失或不是数组");
        return false;
    }
    const size_t nbind = cw_array_size(binds);
    m->bindings = (CwBinding_t*)calloc(nbind ? nbind : 1,
                                       sizeof(CwBinding_t));
    if (!m->bindings) {
        cwmodule_set_error("绑定表分配失败");
        return false;
    }
    for (size_t i = 0; i < nbind; i++) {
        cw_value* b = cw_array_get(binds, i);
        if (!b || cw_typeof(b) != CW_OBJECT) {
            cwmodule_set_error("bindings[%zu] 不是对象", i);
            return false;
        }
        CwBinding_t* out = &m->bindings[i];
        if (!cwmodule_as_int(cw_object_get(b, "id"), &out->id)
            || !cwmodule_as_int(cw_object_get(b, "decl_id"), &out->decl_id)
            || !cwmodule_as_int(cw_object_get(b, "fn_id"), &out->fn_id)) {
            cwmodule_set_error("bindings[%zu] id/decl_id/fn_id 缺失或类型错误",
                               i);
            return false;
        }
        cw_value* owner = cw_object_get(b, "owner");
        cw_value* trait = cw_object_get(b, "trait");
        if (owner && cw_typeof(owner) != CW_NULL) {
            if (!cwmodule_as_string(owner, &out->owner)) {
                cwmodule_set_error("bindings[%zu].owner 类型错误", i);
                return false;
            }
        }
        if (trait && cw_typeof(trait) != CW_NULL) {
            if (!cwmodule_as_string(trait, &out->trait)) {
                cwmodule_set_error("bindings[%zu].trait 类型错误", i);
                return false;
            }
        }
        if (i > 0 && out->id <= m->bindings[i - 1].id) {
            cwmodule_set_error("bindings id 必须严格递增 (id=%lld)",
                               (long long)out->id);
            return false;
        }
        m->binding_count++;
    }

    /* 节点池 */
    if (!cwmodule_collect_nodes(m, m->ast, 0)
        || !cwmodule_finalize_nodes(m)) {
        return false;
    }

    /* 引用完整性 */
    for (size_t i = 0; i < m->symbol_count; i++) {
        const CwNode_t* n = cwmodule_node(m, m->symbols[i].ref);
        if (!n) {
            cwmodule_set_error("symbol '%s' 的 ref=%lld 指向不存在的节点",
                               m->symbols[i].name,
                               (long long)m->symbols[i].ref);
            return false;
        }
        const char* want = cwmodule_symbol_node_kind(m->symbols[i].kind);
        if (!want) {
            cwmodule_set_error("symbol '%s' 的 kind 未知: %s",
                               m->symbols[i].name, m->symbols[i].kind);
            return false;
        }
        if (strcmp(n->kind, want) != 0) {
            cwmodule_set_error("symbol '%s' kind=%s 但节点种类是 %s",
                               m->symbols[i].name, m->symbols[i].kind,
                               n->kind);
            return false;
        }
    }
    for (size_t i = 0; i < m->binding_count; i++) {
        const CwNode_t* decl = cwmodule_node(m, m->bindings[i].decl_id);
        if (!decl) {
            cwmodule_set_error("binding id=%lld 的 decl_id=%lld 不存在",
                               (long long)m->bindings[i].id,
                               (long long)m->bindings[i].decl_id);
            return false;
        }
        if (strcmp(decl->kind, "ImplDecl") != 0
            && strcmp(decl->kind, "ExtraDecl") != 0) {
            cwmodule_set_error("binding id=%lld 的 decl_id=%lld 不是 "
                               "ImplDecl/ExtraDecl, 而是 %s",
                               (long long)m->bindings[i].id,
                               (long long)m->bindings[i].decl_id,
                               decl->kind);
            return false;
        }
        const CwNode_t* fn = cwmodule_node(m, m->bindings[i].fn_id);
        if (!fn) {
            cwmodule_set_error("binding id=%lld 的 fn_id=%lld 不存在",
                               (long long)m->bindings[i].id,
                               (long long)m->bindings[i].fn_id);
            return false;
        }
        if (strcmp(fn->kind, "FnDecl") != 0) {
            cwmodule_set_error("binding id=%lld 的 fn_id=%lld 不是 FnDecl, "
                               "而是 %s",
                               (long long)m->bindings[i].id,
                               (long long)m->bindings[i].fn_id,
                               fn->kind);
            return false;
        }

        /* owner 必须等于声明节点的 struct.name */
        const char* struct_name = NULL;
        if (!cwmodule_decl_type_name(m, m->bindings[i].decl_id,
                                     "struct", &struct_name)
            || !m->bindings[i].owner
            || strcmp(struct_name, m->bindings[i].owner) != 0) {
            cwmodule_set_error("binding id=%lld 的 owner=%s 与声明 struct "
                               "名不一致 (%s)",
                               (long long)m->bindings[i].id,
                               m->bindings[i].owner ? m->bindings[i].owner
                                                    : "(null)",
                               struct_name ? struct_name : "?");
            return false;
        }

        /* trait: ImplDecl 必须匹配, ExtraDecl 必须为 null */
        if (strcmp(decl->kind, "ImplDecl") == 0) {
            const char* trait_name = NULL;
            if (!cwmodule_decl_type_name(m, m->bindings[i].decl_id,
                                         "trait", &trait_name)
                || !m->bindings[i].trait
                || strcmp(trait_name, m->bindings[i].trait) != 0) {
                cwmodule_set_error("binding id=%lld 的 trait=%s 与声明 "
                                   "trait 名不一致 (%s)",
                                   (long long)m->bindings[i].id,
                                   m->bindings[i].trait
                                       ? m->bindings[i].trait : "(null)",
                                   trait_name ? trait_name : "?");
                return false;
            }
        } else if (m->bindings[i].trait != NULL) {
            cwmodule_set_error("binding id=%lld 是 extra 方法, trait 应为 "
                               "null, 实际是 %s",
                               (long long)m->bindings[i].id,
                               m->bindings[i].trait);
            return false;
        }
    }
    return true;
}

/* ---- 公开 API ---- */

static CwModule_t* cwmodule_load_common(cw_doc* doc) {
    if (!doc) {
        cwmodule_set_error("JSON 解析失败");
        return NULL;
    }
    CwModule_t* m = (CwModule_t*)calloc(1, sizeof(CwModule_t));
    if (!m) {
        cw_doc_free(doc);
        cwmodule_set_error("模块分配失败");
        return NULL;
    }
    m->doc = doc;
    if (!cwmodule_parse_root(m)) {
        cwmodule_free(m);
        return NULL;
    }
    return m;
}

CwModule_t* cwmodule_load_file(const char* path) {
    if (!path) {
        cwmodule_set_error("路径为空");
        return NULL;
    }
    return cwmodule_load_common(cw_load_file(path));
}

CwModule_t* cwmodule_load_string(const char* json, size_t len) {
    if (!json) {
        cwmodule_set_error("JSON 文本为空");
        return NULL;
    }
    return cwmodule_load_common(cw_parse(json, len));
}

void cwmodule_free(CwModule_t* m) {
    if (!m) return;
    free(m->symbols);
    free(m->bindings);
    free(m->nodes);
    cw_doc_free(m->doc);
    free(m);
}

const char* cwmodule_error(void) {
    return g_err;
}

const char* cwmodule_format(const CwModule_t* m) {
    return m ? m->format : NULL;
}

int64_t cwmodule_version(const CwModule_t* m) {
    return m ? m->version : -1;
}

size_t cwmodule_symbol_count(const CwModule_t* m) {
    return m ? m->symbol_count : 0;
}

const CwSymbol_t* cwmodule_symbol(const CwModule_t* m, size_t i) {
    if (!m || i >= m->symbol_count) return NULL;
    return &m->symbols[i];
}

const CwSymbol_t* cwmodule_find_symbol(const CwModule_t* m,
                                       const char* name) {
    if (!m || !name) return NULL;
    for (size_t i = 0; i < m->symbol_count; i++) {
        if (strcmp(m->symbols[i].name, name) == 0) return &m->symbols[i];
    }
    return NULL;
}

size_t cwmodule_binding_count(const CwModule_t* m) {
    return m ? m->binding_count : 0;
}

const CwBinding_t* cwmodule_binding(const CwModule_t* m, size_t i) {
    if (!m || i >= m->binding_count) return NULL;
    return &m->bindings[i];
}

size_t cwmodule_node_count(const CwModule_t* m) {
    return m ? m->node_count : 0;
}

const CwNode_t* cwmodule_node_at(const CwModule_t* m, size_t i) {
    if (!m || i >= m->node_count) return NULL;
    return &m->nodes[i];
}

cw_value* cwmodule_ast_root(const CwModule_t* m) {
    return m ? m->ast : NULL;
}

/* ---- 类型化节点访问 ---- */

cw_value* cwmodule_node_field(const CwNode_t* n, const char* key) {
    if (!n || !key) return NULL;
    return cw_object_get(n->value, key);
}

bool cwmodule_type_is(cw_value* v) {
    if (!v || cw_typeof(v) != CW_OBJECT) return false;
    const char* kind = NULL;
    return cwmodule_as_string(cw_object_get(v, "kind"), &kind)
        && strcmp(kind, "Type") == 0;
}

const char* cwmodule_type_name(cw_value* type) {
    if (!cwmodule_type_is(type)) return NULL;
    const char* name = NULL;
    return cwmodule_as_string(cw_object_get(type, "name"), &name)
        ? name : NULL;
}

size_t cwmodule_type_arg_count(cw_value* type) {
    if (!cwmodule_type_is(type)) return 0;
    cw_value* args = cw_object_get(type, "args");
    return (args && cw_typeof(args) == CW_ARRAY) ? cw_array_size(args) : 0;
}

cw_value* cwmodule_type_arg(cw_value* type, size_t i) {
    if (!cwmodule_type_is(type)) return NULL;
    cw_value* args = cw_object_get(type, "args");
    if (!args || cw_typeof(args) != CW_ARRAY) return NULL;
    return cw_array_get(args, i);
}

const char* cwmodule_fn_name(const CwNode_t* n) {
    if (!n || strcmp(n->kind, "FnDecl") != 0) return NULL;
    const char* name = NULL;
    return cwmodule_as_string(cw_object_get(n->value, "name"), &name)
        ? name : NULL;
}

size_t cwmodule_fn_param_count(const CwNode_t* n) {
    if (!n || strcmp(n->kind, "FnDecl") != 0) return 0;
    cw_value* params = cw_object_get(n->value, "params");
    return (params && cw_typeof(params) == CW_ARRAY)
        ? cw_array_size(params) : 0;
}

cw_value* cwmodule_fn_param(const CwNode_t* n, size_t i) {
    if (!n || strcmp(n->kind, "FnDecl") != 0) return NULL;
    cw_value* params = cw_object_get(n->value, "params");
    if (!params || cw_typeof(params) != CW_ARRAY) return NULL;
    return cw_array_get(params, i);
}

cw_value* cwmodule_fn_return_type(const CwNode_t* n) {
    if (!n || strcmp(n->kind, "FnDecl") != 0) return NULL;
    cw_value* rt = cw_object_get(n->value, "return_type");
    return (rt && cw_typeof(rt) == CW_OBJECT) ? rt : NULL;
}

cw_value* cwmodule_fn_body(const CwNode_t* n) {
    if (!n || strcmp(n->kind, "FnDecl") != 0) return NULL;
    cw_value* body = cw_object_get(n->value, "body");
    return (body && cw_typeof(body) == CW_OBJECT) ? body : NULL;
}

const char* cwmodule_param_name(const CwNode_t* n) {
    if (!n || strcmp(n->kind, "Param") != 0) return NULL;
    const char* name = NULL;
    return cwmodule_as_string(cw_object_get(n->value, "name"), &name)
        ? name : NULL;
}

bool cwmodule_param_is_self(const CwNode_t* n) {
    const char* name = cwmodule_param_name(n);
    return name && strcmp(name, "self") == 0;
}

cw_value* cwmodule_param_type(const CwNode_t* n) {
    if (!n || strcmp(n->kind, "Param") != 0) return NULL;
    cw_value* t = cw_object_get(n->value, "type");
    return (t && cw_typeof(t) == CW_OBJECT) ? t : NULL;
}
