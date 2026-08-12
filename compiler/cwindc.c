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
 *   cwindc --emit-obj <out.obj> <file.json>
 *                                 同上, 发射目标文件
 *   cwindc --emit-exe <out.exe> <file.json>
 *                                 同上, 发射目标文件并链接 rt 出可执行文件
 */

#define _CRT_SECURE_NO_WARNINGS 1

#ifndef CWINDC_CLANG_DEFAULT
    #define CWINDC_CLANG_DEFAULT "clang"
#endif
#ifndef CWINDC_RT_DIR
    #define CWINDC_RT_DIR "rt-src/rt"
#endif
#ifndef CWINDC_GCC
    #define CWINDC_GCC "gcc"
#endif
#ifndef CWINDC_GCC_DIR
    #define CWINDC_GCC_DIR "E:/MSYS2/mingw64/bin"
#endif

#include "cwmodule.h"
#include "cwcodegen.h"
#include "cwlayout.h"
#include "cwsymbol.h"
#include "cwtype.h"
#include "../rt-src/include/stl/json/cwind_json.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32)
    #include <windows.h>
#endif

#ifdef _WIN32
  #define strncasecmp _strnicmp
  #define strcasecmp _stricmp
#else
  #include <strings.h>
#endif

typedef struct CwPipeline {
    CwModule_t* m;
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    CwLlvm_t ll;
    CwCodegen_t cg;
} CwPipeline_t;

#if defined(_WIN32)
/* 构造子进程环境块: 在 PATH 前置 extra_path (gcc 需要 MSYS2 运行库 DLL) */
static char* cw_build_env(const char* extra_path) {
    LPCH env = GetEnvironmentStringsA();
    if (!env) return NULL;
    size_t cap = 16384;
    char* out = (char*)malloc(cap);
    if (!out) {
        FreeEnvironmentStringsA(env);
        return NULL;
    }
    size_t len = 0;
    bool path_done = false;
    for (LPCH p = env; *p; p += strlen(p) + 1) {
        const bool is_path = strncasecmp(p, "PATH=", 5) == 0;
        const size_t n = strlen(p);
        if (is_path && extra_path && *extra_path) {
            const size_t need = n + strlen(extra_path) + 2;
            if (len + need + 1 > cap) {
                cap = (len + need + 1) * 2;
                char* nb = (char*)realloc(out, cap);
                if (!nb) { free(out); FreeEnvironmentStringsA(env); return NULL; }
                out = nb;
            }
            memcpy(out + len, "PATH=", 5);
            len += 5;
            memcpy(out + len, extra_path, strlen(extra_path));
            len += strlen(extra_path);
            out[len++] = ';';
            memcpy(out + len, p + 5, n - 5);
            len += n - 5;
            out[len++] = '\0';
            path_done = true;
        } else {
            if (len + n + 2 > cap) {
                cap = (len + n + 2) * 2;
                char* nb = (char*)realloc(out, cap);
                if (!nb) { free(out); FreeEnvironmentStringsA(env); return NULL; }
                out = nb;
            }
            memcpy(out + len, p, n + 1);
            len += n + 1;
        }
    }
    if (extra_path && *extra_path && !path_done) {
        const size_t need = 5 + strlen(extra_path) + 1;
        if (len + need + 1 > cap) {
            cap = (len + need + 1) * 2;
            char* nb = (char*)realloc(out, cap);
            if (!nb) { free(out); FreeEnvironmentStringsA(env); return NULL; }
            out = nb;
        }
        memcpy(out + len, "PATH=", 5);
        len += 5;
        memcpy(out + len, extra_path, strlen(extra_path));
        len += strlen(extra_path);
        out[len++] = '\0';
    }
    out[len++] = '\0'; /* 环境块双层 NULL 结尾 */
    FreeEnvironmentStringsA(env);
    return out;
}
#endif

/* 执行外部命令; Windows 用 CreateProcess 绕开 cmd 对引号首 token 的解析 */
static int cw_run_command(const char* cmd, const char* extra_path) {
#if defined(_WIN32)
    if (!cmd) return -1;
    char* buf = (char*)malloc(strlen(cmd) + 1);
    if (!buf) return -1;
    memcpy(buf, cmd, strlen(cmd) + 1);
    char* env_block = cw_build_env(extra_path);
    STARTUPINFOA si;
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi;
    memset(&pi, 0, sizeof(pi));
    const BOOL ok = CreateProcessA(NULL, buf, NULL, NULL, FALSE, 0,
                                   env_block, NULL, &si, &pi);
    free(env_block);
    free(buf);
    if (!ok) return -1;
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
#else
    (void)extra_path;
    return system(cmd);
#endif
}

static void pipeline_free(CwPipeline_t* p);

static bool pipeline_init(CwPipeline_t* p, const char* in) {
    memset(p, 0, sizeof(*p));
    p->m = cwmodule_load_file(in);
    if (!p->m) {
        fprintf(stderr, "cwindc: %s\n", cwmodule_error());
        return false;
    }
    cwtype_table_init(&p->types);
    cwlayout_cache_init(&p->layouts, &p->types);
    cwsym_table_init(&p->syms);
    if (!cwsym_build_from_module(&p->syms, p->m)) {
        fprintf(stderr, "cwindc: 符号表构建失败\n");
        pipeline_free(p);
        return false;
    }
    if (!cwllvm_init(&p->ll, "cwind", &p->types, &p->layouts, &p->syms)
        || !cwllvm_declare_symbols(&p->ll)) {
        fprintf(stderr, "cwindc: LLVM 初始化失败\n");
        pipeline_free(p);
        return false;
    }
    if (!cwcodegen_init(&p->cg, &p->ll, p->m)
        || !cwcodegen_emit(&p->cg)) {
        fprintf(stderr, "cwindc: %s\n", cwcodegen_error(&p->cg));
        pipeline_free(p);
        return false;
    }
    return true;
}

static void pipeline_free(CwPipeline_t* p) {
    cwcodegen_destroy(&p->cg);
    cwllvm_destroy(&p->ll);
    cwsym_table_destroy(&p->syms);
    cwlayout_cache_destroy(&p->layouts);
    cwtype_table_destroy(&p->types);
    cwmodule_free(p->m);
}

static int cmd_emit_llvm(const char* out, const char* in) {
    CwPipeline_t p;
    if (!pipeline_init(&p, in)) return 1;
    char* ir = cwllvm_dump(&p.ll);
    FILE* f = fopen(out, "w");
    if (!ir || !f) {
        fprintf(stderr, "cwindc: 写 %s 失败\n", out);
        pipeline_free(&p);
        return 1;
    }
    fputs(ir, f);
    fclose(f);
    LLVMDisposeMessage(ir);
    pipeline_free(&p);
    return 0;
}

static int cmd_emit_obj(const char* out, const char* in) {
    CwPipeline_t p;
    if (!pipeline_init(&p, in)) return 1;
    char ll_path[4096];
    snprintf(ll_path, sizeof(ll_path), "%s.ll", out);
    char* ir = cwllvm_dump(&p.ll);
    FILE* f = fopen(ll_path, "w");
    if (!ir || !f) {
        fprintf(stderr, "cwindc: 写 %s 失败\n", ll_path);
        pipeline_free(&p);
        return 1;
    }
    fputs(ir, f);
    fclose(f);
    LLVMDisposeMessage(ir);
    const char* clang = getenv("CWIND_CLANG");
    if (!clang || !*clang) clang = CWINDC_CLANG_DEFAULT;
    char cmd[8192];
    snprintf(cmd, sizeof(cmd),
             "\"%s\" -Wno-override-module -mno-stack-arg-probe"
             " -c \"%s\" -o \"%s\"",
             clang, ll_path, out);
    const int rc = cw_run_command(cmd, NULL);
    remove(ll_path);
    pipeline_free(&p);
    return rc == 0 ? 0 : 1;
}

static int cmd_emit_exe(const char* out, const char* in) {
    CwPipeline_t p;
    if (!pipeline_init(&p, in)) return 1;
    char ll_path[4096];
    snprintf(ll_path, sizeof(ll_path), "%s.ll", out);
    char* ir = cwllvm_dump(&p.ll);
    FILE* f = fopen(ll_path, "w");
    if (!ir || !f) {
        fprintf(stderr, "cwindc: 写 %s 失败\n", ll_path);
        pipeline_free(&p);
        return 1;
    }
    fputs(ir, f);
    fclose(f);
    LLVMDisposeMessage(ir);

    /* 1) clang 只把 IR 编成 obj (不需要 C 头); 2) gcc 链接 rt 出 exe
     * (gcc 自带 C 运行库头, 不依赖 MSVC 环境) */
    const char* clang = getenv("CWIND_CLANG");
    if (!clang || !*clang) clang = CWINDC_CLANG_DEFAULT;
    const char* gcc = getenv("CWIND_GCC");
    if (!gcc || !*gcc) gcc = CWINDC_GCC;
    const char* gcc_dir = getenv("CWIND_GCC_DIR");
    if (!gcc_dir || !*gcc_dir) gcc_dir = CWINDC_GCC_DIR;
    char obj_path[4096];
    snprintf(obj_path, sizeof(obj_path), "%s.o", out);
    char cmd[8192];
    snprintf(cmd, sizeof(cmd),
             "\"%s\" -Wno-override-module -mno-stack-arg-probe"
             " -c \"%s\" -o \"%s\"",
             clang, ll_path, obj_path);
    int rc = cw_run_command(cmd, NULL);
    if (rc != 0) {
        remove(ll_path);
        remove(obj_path);
        pipeline_free(&p);
        return 1;
    }
    snprintf(cmd, sizeof(cmd),
             "\"%s\" \"%s\""
             " \"%s/cwind_memcenter.c\""
             " \"%s/cwind_object.c\""
             " \"%s/cwind_container.c\""
             " \"%s/cwind_builtin.c\""
             " \"%s/cwind_builtin_table.c\""
             " \"%s/stackframe.c\""
             " \"%s/cwind_chkstk.c\""
             " -o \"%s\"",
             gcc, obj_path,
             CWINDC_RT_DIR, CWINDC_RT_DIR, CWINDC_RT_DIR,
             CWINDC_RT_DIR, CWINDC_RT_DIR, CWINDC_RT_DIR,
             CWINDC_RT_DIR, out);
    rc = cw_run_command(cmd, gcc_dir);
    remove(ll_path);
    remove(obj_path);
    pipeline_free(&p);
    return rc == 0 ? 0 : 1;
}

int main(int argc, char** argv) {
    if (argc == 4 && strcmp(argv[1], "--emit-llvm") == 0) {
        return cmd_emit_llvm(argv[2], argv[3]);
    }
    if (argc == 4 && strcmp(argv[1], "--emit-obj") == 0) {
        return cmd_emit_obj(argv[2], argv[3]);
    }
    if (argc == 4 && strcmp(argv[1], "--emit-exe") == 0) {
        return cmd_emit_exe(argv[2], argv[3]);
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
