/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwindc.c
 */

/**
 * cwindc: CWind 后端编译器驱动 (v0 只做 TypedAST 装载与模块摘要)
 *
 * 用法:
 *   cwindc <typed-ast.json>        装载并打印模块摘要
 *   cwindc --check <file.json>     装载并审计符号 / 绑定位置
 *   cwindc --emit-llvm <out.ll> <file.json>
 *                                 装载 -> 声明 -> 函数体 -> LLVM IR 文本
 */

#define _CRT_SECURE_NO_WARNINGS 1

#include "cwmodule.h"
#include "cwcodegen.h"
#include "cwlayout.h"
#include "cwsymbol.h"
#include "cwtype.h"
#include "../rt-src/include/stl/json/cwind_json.h"

#include <stdio.h>
#include <string.h>

int main(int argc, char** argv) {
    if (argc == 4 && strcmp(argv[1], "--emit-llvm") == 0) {
        CwModule_t* m = cwmodule_load_file(argv[3]);
        if (!m) {
            fprintf(stderr, "cwindc: %s\n", cwmodule_error());
            return 1;
        }
        CwTypeTable_t types;
        CwLayoutCache_t layouts;
        CwSymTable_t syms;
        cwtype_table_init(&types);
        cwlayout_cache_init(&layouts, &types);
        cwsym_table_init(&syms);
        if (!cwsym_build_from_module(&syms, m)) {
            fprintf(stderr, "cwindc: 符号表构建失败\n");
            return 1;
        }
        CwLlvm_t ll;
        if (!cwllvm_init(&ll, "cwind", &types, &layouts, &syms)
            || !cwllvm_declare_symbols(&ll)) {
            fprintf(stderr, "cwindc: LLVM 初始化失败\n");
            return 1;
        }
        CwCodegen_t cg;
        if (!cwcodegen_init(&cg, &ll, m) || !cwcodegen_emit(&cg)) {
            fprintf(stderr, "cwindc: %s\n", cwcodegen_error(&cg));
            return 1;
        }
        char* ir = cwllvm_dump(&ll);
        FILE* f = fopen(argv[2], "w");
        if (!ir || !f) {
            fprintf(stderr, "cwindc: 写 %s 失败\n", argv[2]);
            return 1;
        }
        fputs(ir, f);
        fclose(f);
        LLVMDisposeMessage(ir);
        cwcodegen_destroy(&cg);
        cwllvm_destroy(&ll);
        cwsym_table_destroy(&syms);
        cwlayout_cache_destroy(&layouts);
        cwtype_table_destroy(&types);
        cwmodule_free(m);
        return 0;
    }

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
