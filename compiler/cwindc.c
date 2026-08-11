/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwindc.c
 */

/**
 * cwindc: CWind 后端编译器驱动 (v0 只做 TypedAST 装载与模块摘要)
 *
 * 用法:
 *   cwindc <typed-ast.json>       装载并打印模块摘要
 *   cwindc --check <file.json>    装载并审计符号 / 绑定位置
 */

#include "cwmodule.h"
#include "../rt-src/include/stl/json/cwind_json.h"

#include <stdio.h>
#include <string.h>

int main(int argc, char** argv) {
    bool check = false;
    const char* path = NULL;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--check") == 0) check = true;
        else if (!path) path = argv[i];
    }
    if (!path) {
        fprintf(stderr, "用法: cwindc [--check] <typed-ast.json>\n");
        return 2;
    }

    CwModule_t* m = cwmodule_load_file(path);
    if (!m) {
        fprintf(stderr, "cwindc: %s\n", cwmodule_error());
        return 1;
    }

    if (check) {
        printf("audit: format=%s version=%lld symbols=%zu bindings=%zu "
               "nodes=%zu\n",
               cwmodule_format(m), (long long)cwmodule_version(m),
               cwmodule_symbol_count(m), cwmodule_binding_count(m),
               cwmodule_node_count(m));
        for (size_t i = 0; i < cwmodule_symbol_count(m); i++) {
            const CwSymbol_t* s = cwmodule_symbol(m, i);
            const CwNode_t* n = cwmodule_node(m, s->ref);
            printf("  symbol %-16s %-8s -> %-12s ok\n",
                   s->name, s->kind, n ? n->kind : "?");
        }
        for (size_t i = 0; i < cwmodule_binding_count(m); i++) {
            const CwBinding_t* b = cwmodule_binding(m, i);
            const CwNode_t* decl = cwmodule_node(m, b->decl_id);
            const CwNode_t* fn = cwmodule_node(m, b->fn_id);
            const char* struct_name = NULL;
            const char* trait_name = NULL;
            if (decl) {
                cw_value* st = cw_object_get(decl->value, "struct");
                cw_value* tr = cw_object_get(decl->value, "trait");
                if (st && cw_typeof(st) == CW_OBJECT) {
                    struct_name = cw_string_cstr(cw_object_get(st, "name"));
                }
                if (tr && cw_typeof(tr) == CW_OBJECT) {
                    trait_name = cw_string_cstr(cw_object_get(tr, "name"));
                }
            }
            printf("  binding id=%-2lld decl=%lld(%-10s) owner=%-8s "
                   "trait=%-6s fn=%lld(%s)\n",
                   (long long)b->id, (long long)b->decl_id,
                   decl ? decl->kind : "?",
                   struct_name ? struct_name : "?",
                   trait_name ? trait_name : "null",
                   (long long)b->fn_id, fn ? fn->kind : "?");
        }
        cwmodule_free(m);
        return 0;
    }

    printf("format  = %s\n", cwmodule_format(m));
    printf("version = %lld\n", (long long)cwmodule_version(m));
    printf("symbols = %zu\n", cwmodule_symbol_count(m));
    printf("bindings = %zu\n", cwmodule_binding_count(m));
    printf("nodes   = %zu\n", cwmodule_node_count(m));

    for (size_t i = 0; i < cwmodule_symbol_count(m); i++) {
        const CwSymbol_t* s = cwmodule_symbol(m, i);
        printf("  symbol: %s (%s) -> node %lld\n",
               s->name, s->kind, (long long)s->ref);
    }
    for (size_t i = 0; i < cwmodule_binding_count(m); i++) {
        const CwBinding_t* b = cwmodule_binding(m, i);
        printf("  binding: id=%lld decl=%lld fn=%lld owner=%s trait=%s\n",
               (long long)b->id, (long long)b->decl_id,
               (long long)b->fn_id,
               b->owner ? b->owner : "(null)",
               b->trait ? b->trait : "(null)");
    }

    cwmodule_free(m);
    return 0;
}
