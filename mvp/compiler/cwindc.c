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
 *   cwindc --emit-exe -O3 <out.exe> <file.json>
 *                                 同上, clang/gcc 使用 -O3 优化
 *   (优化级别: --opt <0|1|2|3|s|z> 或 -O0..-O3/-Os/-Oz, 默认不传)
 *
 * todo-100: 输入也可以是 cwindf --project 产出的 project.json
 * (format == "cwind-project"); 驱动按其 "target" 字段解析出整程序
 * TypedAST 工件后再走既有管线, 所有模式 (--check/--emit-*) 均适用。
 */

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
#include "../rt-src/include/rt/cwind_safecrt.h"

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
  #include <unistd.h>
#endif

typedef struct CwPipeline {
    CwModule_t* m;
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    CwLlvm_t ll;
    CwCodegen_t cg;
} CwPipeline_t;

static const char* g_opt_level = NULL; /* NULL = 不传 -O (clang 默认 -O0) */

/* 优化级别合法值: 0/1/2/3/s/z (对应 -O0..-O3/-Os/-Oz) */
static bool cw_opt_valid(
    const char* lv
) {
    return lv && (strcmp(lv, "0") == 0 || strcmp(lv, "1") == 0
                  || strcmp(lv, "2") == 0 || strcmp(lv, "3") == 0
                  || strcmp(lv, "s") == 0 || strcmp(lv, "z") == 0);
}

/* 组装 "-O<level>"; 未设置时返回空串 */
static const char* cw_opt_flag(
    void
) {
    static char buf[16];
    if (!g_opt_level) return "";
    snprintf(buf, sizeof(buf), " -O%s", g_opt_level);
    return buf;
}

/* todo-152: 环境变量经 cw_env_get 读入 buf, 未设置/为空回落 dflt */
static const char* cw_env_or(
    const char* name,
    char* buf,
    size_t cap,
    const char* dflt
) {
    return (cw_env_get(name, buf, cap) && *buf) ? buf : dflt;
}

#if defined(_WIN32)
/* 构造子进程环境块: 在 PATH 前置 extra_path (gcc 需要 MSYS2 运行库 DLL) */
static char* cw_build_env(
    const char* extra_path
) {
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
static int cw_run_command(
    const char* cmd,
    const char* extra_path
) {
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

static void pipeline_free(
    CwPipeline_t* p
);

static bool pipeline_init(
    CwPipeline_t* p,
    const char* in
) {
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
        fprintf(stderr, "cwindc: Failed to build symbols table\n");
        pipeline_free(p);
        return false;
    }
    if (!cwllvm_init(&p->ll, "cwind", &p->types, &p->layouts, &p->syms)
        || !cwllvm_declare_symbols(&p->ll)) {
        fprintf(stderr, "cwindc: Failed to initialize LLVM\n");
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

static void pipeline_free(
    CwPipeline_t* p
) {
    cwcodegen_destroy(&p->cg);
    cwllvm_destroy(&p->ll);
    cwsym_table_destroy(&p->syms);
    cwlayout_cache_destroy(&p->layouts);
    cwtype_table_destroy(&p->types);
    cwmodule_free(p->m);
}

static int cmd_emit_llvm(
    const char* out,
    const char* in
) {
    CwPipeline_t p;
    if (!pipeline_init(&p, in)) return 1;
    char* ir = cwllvm_dump(&p.ll);
    FILE* f = cw_fopen(out, "w");
    if (!ir || !f) {
        fprintf(stderr, "cwindc: Failed to write: %s\n", out);
        pipeline_free(&p);
        return 1;
    }
    fputs(ir, f);
    fclose(f);
    LLVMDisposeMessage(ir);
    pipeline_free(&p);
    return 0;
}

static int cmd_emit_obj(
    const char* out,
    const char* in
) {
    CwPipeline_t p;
    if (!pipeline_init(&p, in)) return 1;
    char ll_path[4096];
    snprintf(ll_path, sizeof(ll_path), "%s.ll", out);
    char* ir = cwllvm_dump(&p.ll);
    FILE* f = cw_fopen(ll_path, "w");
    if (!ir || !f) {
        fprintf(stderr, "cwindc: failed to write %s\n", ll_path);
        pipeline_free(&p);
        return 1;
    }
    fputs(ir, f);
    fclose(f);
    LLVMDisposeMessage(ir);
    char clang_buf[4096];
    const char* clang = cw_env_or("CWIND_CLANG", clang_buf, sizeof(clang_buf),
                                  CWINDC_CLANG_DEFAULT);
    char cmd[8192];
    snprintf(cmd, sizeof(cmd),
             "\"%s\"%s -Wno-override-module -mno-stack-arg-probe"
             " -c \"%s\" -o \"%s\"",
             clang, cw_opt_flag(), ll_path, out);
    const int rc = cw_run_command(cmd, NULL);
    remove(ll_path);
    pipeline_free(&p);
    return rc == 0 ? 0 : 1;
}

/* 判断路径是否为绝对路径 (Windows 盘符/根前缀, POSIX 根) */
static bool cw_path_is_absolute(
    const char* p
) {
    if (!p || !p[0]) return false;
#if defined(_WIN32)
    if (p[0] == '/' || p[0] == '\\') return true; /* 根路径 / UNC */
    if (((p[0] >= 'A' && p[0] <= 'Z') || (p[0] >= 'a' && p[0] <= 'z'))
        && p[1] == ':') {
        return true;
    }
    return false;
#else
    return p[0] == '/';
#endif
}

/* 取路径的目录部分 (不含末尾分隔符); 无分隔符时返回 false */
static bool cw_dir_of(
    const char* path,
    char* out,
    size_t cap
) {
    if (!path || !out || cap == 0) return false;
    const char* sep = strrchr(path, '/');
#if defined(_WIN32)
    const char* bs = strrchr(path, '\\');
    if (!sep || (bs && bs > sep)) sep = bs;
#endif
    if (!sep || sep == path) return false;
    const size_t len = (size_t)(sep - path);
    if (len + 1 > cap) return false;
    memcpy(out, path, len);
    out[len] = '\0';
    return true;
}

/* 解析 #[link] 的 path 参数 (todo-49/63):
 * 绝对路径不受限制, 原样使用; 相对路径按锚点目录 anchor 解析成绝对
 * 路径。锚点由调用方选择: 默认 cwindc 工作目录, relative = "source"
 * 时为源文件所在目录 (todo-63)。anchor 为空串时退回工作目录语义。
 * 解析失败返回 false, 调用方退回原始路径交给链接器处理。 */
static bool cw_resolve_lib_path(
    const char* path,
    const char* anchor,
    char* out,
    size_t cap
) {
    if (!path || !out || cap == 0) return false;
    if (cw_path_is_absolute(path)) {
        const size_t len = strlen(path);
        if (len + 1 > cap) return false;
        memcpy(out, path, len + 1);
        return true;
    }
    char joined[4096];
    if (anchor && anchor[0]) {
        const int jn = snprintf(joined, sizeof(joined), "%s/%s",
                                anchor, path);
        if (jn <= 0 || (size_t)jn >= sizeof(joined)) return false;
    } else {
        const size_t len = strlen(path);
        if (len + 1 > sizeof(joined)) return false;
        memcpy(joined, path, len + 1);
    }
#if defined(_WIN32)
    const DWORD n = GetFullPathNameA(joined, (DWORD)cap, out, NULL);
    return n > 0 && n < cap;
#else
    const size_t flen = strlen(joined);
    if (flen + 1 > cap) return false;
    memcpy(out, joined, flen + 1);
    return true;
#endif
}

/* 把 extern 块 #[link(...)] 声明的库追加到链接命令 (todo-49):
 * path 按锚点解析后作为一条链接输入; 只有 name 时转成 "-l<name>"。
 * 锚点: relative = "source" 且信封带源文件路径时取源文件目录
 * (todo-63), 否则 cwindc 工作目录。
 * 追加后保证缓冲仍以 '\0' 结尾; 空间不足返回 false。 */
static bool cw_append_lib_flags(
    char* cmd,
    size_t cap,
    const CwModule_t* m
) {
    char cwd[2048];
    cwd[0] = '\0';
#if defined(_WIN32)
    if (!GetCurrentDirectoryA(sizeof(cwd), cwd)) cwd[0] = '\0';
#else
    if (!getcwd(cwd, sizeof(cwd))) cwd[0] = '\0';
#endif
    const size_t n = m ? cwmodule_link_count(m) : 0;
    for (size_t i = 0; i < n; i++) {
        const CwLinkInfo_t* l = cwmodule_link(m, i);
        char piece[4352];
        if (l && l->path) {
            /* 相对 path 显式锚定解析, 不依赖链接器的隐式解析
             * (gcc 子进程的工作目录会被切到 gcc_dir); 绝对路径原样传递 */
            char anchor[4096];
            anchor[0] = '\0';
            if (l->relative && strcmp(l->relative, "source") == 0) {
                const char* src = cwmodule_source(m);
                if (!(src && cw_dir_of(src, anchor, sizeof(anchor)))) {
                    snprintf(anchor, sizeof(anchor), "%s", cwd);
                }
            } else {
                snprintf(anchor, sizeof(anchor), "%s", cwd);
            }
            char resolved[4096];
            const char* lib = cw_resolve_lib_path(l->path, anchor,
                                                  resolved,
                                                  sizeof(resolved))
                ? resolved : l->path;
            snprintf(piece, sizeof(piece), " \"%s\"", lib);
        } else if (l && l->name) {
            snprintf(piece, sizeof(piece), " -l%s", l->name);
        } else {
            continue;
        }
        const size_t off = strlen(cmd);
        const size_t need = strlen(piece);
        if (off + need + 1 > cap) return false;
        memcpy(cmd + off, piece, need + 1);
    }
    return true;
}

static int cmd_emit_exe(
    const char* out,
    const char* in
) {
    CwPipeline_t p;
    if (!pipeline_init(&p, in)) return 1;
    char ll_path[4096];
    snprintf(ll_path, sizeof(ll_path), "%s.ll", out);
    char* ir = cwllvm_dump(&p.ll);
    FILE* f = cw_fopen(ll_path, "w");
    if (!ir || !f) {
        fprintf(stderr, "cwindc: Failed to write: %s\n", ll_path);
        pipeline_free(&p);
        return 1;
    }
    fputs(ir, f);
    fclose(f);
    LLVMDisposeMessage(ir);

    /* 1) clang 只把 IR 编成 obj (不需要 C 头); 2) gcc 链接 rt 出 exe
     * (gcc 自带 C 运行库头, 不依赖 MSVC 环境) */
    char clang_buf[4096];
    const char* clang = cw_env_or("CWIND_CLANG", clang_buf, sizeof(clang_buf),
                                  CWINDC_CLANG_DEFAULT);
    char gcc_buf[4096];
    const char* gcc = cw_env_or("CWIND_GCC", gcc_buf, sizeof(gcc_buf),
                                CWINDC_GCC);
    char gcc_dir_buf[4096];
    const char* gcc_dir = cw_env_or("CWIND_GCC_DIR", gcc_dir_buf,
                                    sizeof(gcc_dir_buf), CWINDC_GCC_DIR);
    /* CreateProcessA 按父进程 PATH 解析可执行文件 (子进程环境块里的
     * PATH 前置对它无效), 裸名时直接拼 gcc_dir 的绝对路径, 否则在
     * ctest 等最小 PATH 环境里链接步会静默失败。 */
    char gcc_path[4096];
    const char* gcc_exe = gcc;
    if (!strchr(gcc, '/') && !strchr(gcc, '\\')) {
        snprintf(gcc_path, sizeof(gcc_path), "%s/%s", gcc_dir, gcc);
        gcc_exe = gcc_path;
    }
    char obj_path[4096];
    snprintf(obj_path, sizeof(obj_path), "%s.o", out);
    char cmd[8192];
    snprintf(cmd, sizeof(cmd),
             "\"%s\"%s -Wno-override-module -mno-stack-arg-probe"
             " -c \"%s\" -o \"%s\"",
             clang, cw_opt_flag(), ll_path, obj_path);
    int rc = cw_run_command(cmd, NULL);
    if (rc != 0) {
        remove(ll_path);
        remove(obj_path);
        pipeline_free(&p);
        return 1;
    }
    snprintf(cmd, sizeof(cmd),
             "\"%s\"%s \"%s\""
             " \"%s/cwind_memcenter.c\""
             " \"%s/cwind_object.c\""
             " \"%s/cwind_container.c\""
             " \"%s/cwind_builtin.c\""
             " \"%s/cwind_builtin_table.c\""
             " \"%s/stackframe.c\""
             " \"%s/cwind_unwind.c\""
             " \"%s/cwind_chkstk.c\""
             " \"%s/cwind_gc.c\"",
             gcc_exe, cw_opt_flag(), obj_path,
             CWINDC_RT_DIR, CWINDC_RT_DIR, CWINDC_RT_DIR,
             CWINDC_RT_DIR, CWINDC_RT_DIR, CWINDC_RT_DIR,
             CWINDC_RT_DIR,              CWINDC_RT_DIR, CWINDC_RT_DIR);
    /* extern 声明的库放在对象之后 (-l 顺序敏感); 追加失败按命令过长处理 */
    if (!cw_append_lib_flags(cmd, sizeof(cmd), p.m)) {
        fprintf(stderr, "cwindc: link command is too long\n");
        remove(ll_path);
        remove(obj_path);
        pipeline_free(&p);
        return 1;
    }
    {
        const size_t off = strlen(cmd);
        snprintf(cmd + off, sizeof(cmd) - off, " -o \"%s\"", out);
    }
    rc = cw_run_command(cmd, gcc_dir);
    remove(ll_path);
    remove(obj_path);
    pipeline_free(&p);
    return rc == 0 ? 0 : 1;
}

static char* cw_read_file_cstr(
    const char* path,
    size_t* len_out
) {
    FILE* f = cw_fopen(path, "rb");
    char* buf = NULL;
    long n = 0;
    size_t rd = 0;
    if (!f) {
        return NULL;
    }
    if (fseek(f, 0, SEEK_END) != 0 || (n = ftell(f)) < 0
        || fseek(f, 0, SEEK_SET) != 0) {
        fclose(f);
        return NULL;
    }
    buf = malloc((size_t)n + 1);
    if (!buf) {
        fclose(f);
        return NULL;
    }
    rd = fread(buf, 1, (size_t)n, f);
    fclose(f);
    buf[rd] = '\0';
    if (len_out) {
        *len_out = rd;
    }
    return buf;
}

/* todo-100: project.json 输入解析。
 *
 * 返回:
 *   1  输入不是 project 文档 (无 format 字段或解析失败) —— 调用方按
 *      旧的 TypedAST 信封路径继续, 错误由装载器报告;
 *   -1 输入自称 project 但无效 / 目标工件缺失 —— 已打印诊断;
 *   0  成功, *out_path 指向 malloc 出的整程序 TypedAST 路径。
 */
static int resolve_project_input(
    const char* in,
    char** out_path
) {
    static const char kProjectFormat[] = "cwind-project";
    char* text = NULL;
    size_t len = 0;
    cw_doc* doc = NULL;
    cw_value* root = NULL;
    cw_value* fmt = NULL;
    cw_value* ver = NULL;
    cw_value* tgt = NULL;
    const char* rel = NULL;
    const char* dir_end = NULL;
    size_t dir_len = 0;
    long long version = 0;
    char* joined = NULL;
    int status = 1;

    text = cw_read_file_cstr(in, &len);
    if (!text) {
        return 1;
    }
    doc = cw_parse(text, len);
    free(text);
    if (!doc) {
        return 1;
    }
    root = cw_doc_root(doc);
    fmt = root ? cw_object_get(root, "format") : NULL;
    if (!fmt || cw_typeof(fmt) != CW_STRING
        || strcmp(cw_string_cstr(fmt), kProjectFormat) != 0) {
        cw_doc_free(doc);
        return 1;
    }

    /* 自称 project 之后一律严格校验, 不再回退到旧路径。*/
    status = -1;
    ver = root ? cw_object_get(root, "version") : NULL;
    if (!ver || cw_typeof(ver) != CW_INT || cw_as_int(ver, &version) != CW_OK) {
        fprintf(stderr, "cwindc: project.json has no integer 'version'\n");
        cw_doc_free(doc);
        return status;
    }
    if (version != 1) {
        fprintf(stderr,
                "cwindc: unsupported project.json version %lld\n",
                version);
        cw_doc_free(doc);
        return status;
    }
    tgt = root ? cw_object_get(root, "target") : NULL;
    rel = (tgt && cw_typeof(tgt) == CW_STRING) ? cw_string_cstr(tgt) : NULL;
    if (!rel || !*rel) {
        fprintf(stderr, "cwindc: project.json has no 'target' artifact\n");
        cw_doc_free(doc);
        return status;
    }
    {
        int is_abs = (rel[0] == '/')
            || (rel[0] == '\\')
            || ((rel[0] != '\0') && rel[1] == ':');
        size_t rel_len = strlen(rel);
        if (is_abs) {
            joined = malloc(rel_len + 1);
            if (joined) {
                memcpy(joined, rel, rel_len + 1);
            }
        } else {
            dir_end = strrchr(in, '/');
            {
                const char* bs = strrchr(in, '\\');
                if (bs && (!dir_end || bs > dir_end)) {
                    dir_end = bs;
                }
            }
            dir_len = dir_end ? (size_t)(dir_end - in + 1) : 0;
            joined = malloc(dir_len + rel_len + 2);
            if (joined) {
                memcpy(joined, in, dir_len);
                if (dir_len && joined[dir_len - 1] != '/'
                    && joined[dir_len - 1] != '\\') {
                    joined[dir_len++] = '/';
                }
                memcpy(joined + dir_len, rel, rel_len + 1);
            }
        }
    }
    cw_doc_free(doc);
    if (!joined) {
        fprintf(stderr, "cwindc: out of memory resolving project target\n");
        return status;
    }
    *out_path = joined;
    return 0;
}

int main(
    int argc,
    char** argv
) {
    const char* emit_mode = NULL;
    const char* out = NULL;
    const char* in = NULL;
    bool check = false;
    for (int i = 1; i < argc; i++) {
        const char* a = argv[i];
        if (strcmp(a, "--check") == 0) {
            check = true;
        } else if (strcmp(a, "--emit-llvm") == 0
                   || strcmp(a, "--emit-obj") == 0
                   || strcmp(a, "--emit-exe") == 0) {
            emit_mode = a;
        } else if (strcmp(a, "--opt") == 0) {
            if (i + 1 >= argc || !cw_opt_valid(argv[i + 1])) {
                fprintf(stderr, "cwindc: --opt expects 0/1/2/3/s/z\n");
                return 2;
            }
            g_opt_level = argv[++i];
        } else if (a[0] == '-' && a[1] == 'O' && a[2] != '\0') {
            const char* lv = a + 2;
            if (!cw_opt_valid(lv)) {
                fprintf(stderr, "cwindc: unknown optimization level %s\n", a);
                return 2;
            }
            g_opt_level = lv;
        } else if (!out) {
            out = a;
        } else if (!in) {
            in = a;
        } else {
            fprintf(stderr, "cwindc: unexpected argument %s\n", a);
            return 2;
        }
    }

    if (emit_mode) {
        if (!out || !in) {
            fprintf(stderr,
                    "Usage: cwindc %s [--opt <0|1|2|3|s|z>] "
                    "<out> <in.json|project.json>\n",
                    emit_mode);
            return 2;
        }
    } else {
        const char* path = out ? out : in;
        if (!path || (out && in)) {
            fprintf(stderr,
                    "Usage: cwindc [--check] "
                    "<typed-ast.json|project.json>\n");
            return 2;
        }
    }

    /* todo-100: project.json 输入先解析出整程序 TypedAST 工件路径,
     * 之后所有模式按既有管线消费; 非 project 文档原样通过。*/
    {
        const char* input = emit_mode ? in : out;
        char* resolved = NULL;
        int rs = resolve_project_input(input, &resolved);
        if (rs == 0) {
            if (emit_mode) {
                in = resolved;
            } else {
                out = resolved;
            }
        } else if (rs < 0) {
            return 1;
        }
    }

    if (emit_mode) {
        if (strcmp(emit_mode, "--emit-llvm") == 0) {
            return cmd_emit_llvm(out, in);
        }
        if (strcmp(emit_mode, "--emit-obj") == 0) {
            return cmd_emit_obj(out, in);
        }
        return cmd_emit_exe(out, in);
    }

    const char* path = out ? out : in;
    if (!path || (out && in)) {
        fprintf(stderr, "Usage: cwindc [--check] <typed-ast.json>\n");
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
