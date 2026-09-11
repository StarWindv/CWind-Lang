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

/* 链接器同目录惯例: cwindc 与 .LLVM18/ 同级部署 (CMake 把
 * CWINDC_CLANG_SIBLING 设为 exe 旁的 clang 相对路径), PATH 上
 * 的裸名 clang 可能是任意版本 —— bitcode 的 attribute group 带
 * producer 版本戳, 跨大版本消费直接拒载 (18 写 19 读实测报
 * "Invalid attribute group entry"), 因此默认锚定同源 clang。 */
#ifndef CWINDC_CLANG_DEFAULT
    #define CWINDC_CLANG_DEFAULT "clang"
#endif
#ifndef CWINDC_CLANG_SIBLING
    #define CWINDC_CLANG_SIBLING "../.LLVM18/bin/clang.exe"
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

#include <cwap.h>

#include <llvm-c/Bitwriter.h>

#if defined(_WIN32)
    #include <fcntl.h>
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

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
static const char* g_target_cpu = NULL; /* todo: --target-cpu, "native" 直通 */
static const char* g_lto = NULL;       /* todo: --lto <off|fat>, fat 走 -flto */
static bool g_fast_math = false;       /* todo: --fast-math, 浮点放宽 IEEE 754 */

/* 优化级别合法值: 0/1/2/3/s/z (对应 -O0..-O3/-Os/-Oz) */
static bool cw_opt_valid(
    const char* lv
) {
    return lv && (strcmp(lv, "0") == 0 || strcmp(lv, "1") == 0
                  || strcmp(lv, "2") == 0 || strcmp(lv, "3") == 0
                  || strcmp(lv, "s") == 0 || strcmp(lv, "z") == 0);
}

/* LTO 合法值: off (默认, 不加任何 -flto) / fat (clang -flto=full + gcc -flto) */
static bool cw_lto_valid(
    const char* lv
) {
    return lv && (strcmp(lv, "off") == 0 || strcmp(lv, "fat") == 0);
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

/* 组装 "-march=<cpu>"; 未设置时返回空串 ("native" 由 clang/gcc 自行展开) */
static const char* cw_target_cpu_flag(
    void
) {
    static char buf[64];
    if (!g_target_cpu) return "";
    snprintf(buf, sizeof(buf), " -march=%s", g_target_cpu);
    return buf;
}

/* dump 前的进程内 IR 处理统一入口: --fast-math 标注 + opt 管线。
 * 返回 false 时调用方终止 (管线报错经 *errored 区分)。 */
static bool cw_ir_optimize(
    CwLlvm_t* ll,
    bool* errored
) {
    if (g_fast_math) {
        cwllvm_apply_fast_math(ll->module);
    }
    return cwllvm_run_opt_pipeline(ll, g_opt_level, g_target_cpu, errored);
}

/* clang 解析: CWIND_CLANG 环境变量 > 同源 sibling (cwindc exe 旁
 * 的 ../.LLVM18/bin/clang.exe, 与进程内 LLVM-C 同版本, bitcode
 * producer 戳一致) > PATH 裸名。返回值指向静态缓冲, 调用方只在
 * 同一表达式内使用。 */static const char* cw_clang_exe(
    void
) {
    static char buf[4096];
    char env_buf[4096];
    if (cw_env_get("CWIND_CLANG", env_buf, sizeof(env_buf)) && env_buf[0]) {
        return env_buf[0] ? env_buf : CWINDC_CLANG_DEFAULT;
    }
#if defined(_WIN32)
    DWORD n = GetModuleFileNameA(NULL, buf, (DWORD)sizeof(buf));
    if (n > 0 && n < sizeof(buf)) {
        char* sep = strrchr(buf, '\\');
        if (sep) {
            *sep = '\0';
            const size_t dir_len = strlen(buf);
            snprintf(buf + dir_len, sizeof(buf) - dir_len,
                     "\\%s", CWINDC_CLANG_SIBLING);
            DWORD attr = GetFileAttributesA(buf);
            if (attr != INVALID_FILE_ATTRIBUTES
                && !(attr & FILE_ATTRIBUTE_DIRECTORY)) {
                return buf;
            }
        }
    }
#endif
    return CWINDC_CLANG_DEFAULT;
}

/* clang obj 步的 LTO 片段: 恒空 (见 cw_lto_gcc_flag 的工具链边界注)。 */
static const char* cw_lto_clang_flag(
    void
) {
    (void)g_lto;
    return "";
}

/* 组装 LTO 片段: fat 时 gcc 步 (rt .c 编译 + 链接) -flto。
 *
 * 注意: clang 侧 (-flto=full) 产物是 LLVM bitcode, MinGW gcc 的
 * 链接器无法消费 (工具链边界: clang(LLVM18, MSVC target) vs gcc
 * (MSYS2 MinGW)); 因此 fat LTO 只对 gcc 侧的 rt 编译+链接生效,
 * 主 IR obj 保持原生格式参与。rt 是热路径大头 (GC/分配器/内建),
 * 单侧 fat 仍有可观收益。 */
static const char* cw_lto_gcc_flag(
    void
) {
    if (g_lto && strcmp(g_lto, "fat") == 0) return " -flto";
    return "";
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
    {
        bool opt_err = false;
        if (!cw_ir_optimize(&p.ll, &opt_err)) {
            if (opt_err) { pipeline_free(&p); return 1; }
        }
    }
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

static int cw_write_bitcode(
    LLVMModuleRef module,
    const char* path
) {
    /* LLVM sys::fs 把窄字符路径当 UTF-8; cwindc 的 argv 是 ANSI
     * (GBK 等代码页) 字节 —— 先 ACP -> UTF-16 -> UTF-8 归一, 再交
     * LLVMWriteBitcodeToFile (DLL 内部打开, fd 语义自洽)。 */
    wchar_t wpath[4096];
    char utf8[4096];
    if (MultiByteToWideChar(CP_ACP, 0, path, -1, wpath, 4096) <= 0) {
        return 1;
    }
    if (WideCharToMultiByte(CP_UTF8, 0, wpath, -1, utf8, sizeof(utf8),
                            NULL, NULL) <= 0) {
        return 1;
    }
    return LLVMWriteBitcodeToFile(module, utf8) != 0;
}

static int cmd_emit_obj(
    const char* out,
    const char* in
) {
    CwPipeline_t p;
    if (!pipeline_init(&p, in)) return 1;
    {
        bool opt_err = false;
        if (!cw_ir_optimize(&p.ll, &opt_err)) {
            if (opt_err) { pipeline_free(&p); return 1; }
        }
    }
    /* opt 管线产物直写 bitcode 喂 clang: 文本 .ll 序列化会产生
     * `trunc nuw` 这类本版 LLParser 尚不认的语法 (instcombine 18
     * 已产出该标志, 文本语法 19 才收编), bitcode 编码无此约束。 */
    char bc_path[4096];
    snprintf(bc_path, sizeof(bc_path), "%s.bc", out);
    if (cw_write_bitcode(p.ll.module, bc_path)) {
        fprintf(stderr, "cwindc: failed to write %s\n", bc_path);
        pipeline_free(&p);
        return 1;
    }
    char clang_buf[4096];
    const char* clang = cw_clang_exe();
    char cmd[8192];
    snprintf(cmd, sizeof(cmd),
             "\"%s\"%s%s%s -Wno-override-module -mno-stack-arg-probe"
             " -c \"%s\" -o \"%s\"",
             clang, cw_opt_flag(), cw_target_cpu_flag(), cw_lto_clang_flag(),
             bc_path, out);
    const int rc = cw_run_command(cmd, NULL);
    remove(bc_path);
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
            /* todo-49: kind 感知 —— static/dylib 控制链接器对
             * lib<name>.a / lib<name>.dll.a (或 Unix 的 .a/.so) 的
             * 择取。MinGW 裸 -l 默认搜索序先命中静态库, 与工具链
             * 隐式链接的动态 winpthread 撞多重定义 (bug: time 模块
             * clock_gettime 显式声明 winpthread 依赖时); 显式 kind
             * 用 -Bstatic/-Bdynamic 锁定, 并在用后恢复默认状态,
             * 不影响后续追加的库。 */
            if (l->kind && strcmp(l->kind, "static") == 0) {
                snprintf(piece, sizeof(piece),
                         " -Wl,-Bstatic -l%s -Wl,-Bdynamic", l->name);
            } else if (l->kind && strcmp(l->kind, "dylib") == 0) {
                snprintf(piece, sizeof(piece),
                         " -Wl,-Bdynamic -l%s", l->name);
            } else {
                snprintf(piece, sizeof(piece), " -l%s", l->name);
            }
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
    {
        bool opt_err = false;
        if (!cw_ir_optimize(&p.ll, &opt_err)) {
            if (opt_err) { pipeline_free(&p); return 1; }
        }
    }
    /* opt 管线产物直写 bitcode 喂 clang (同 cmd_emit_obj: 文本 .ll
     * 的 `trunc nuw` 语法本版 LLParser 不认)。1) clang 把 bitcode
     * 编成 obj; 2) gcc 链接 rt 出 exe (gcc 自带 C 运行库头, 不依赖
     * MSVC 环境)。 */
    char bc_path[4096];
    snprintf(bc_path, sizeof(bc_path), "%s.bc", out);
    if (cw_write_bitcode(p.ll.module, bc_path)) {
        fprintf(stderr, "cwindc: Failed to write: %s\n", bc_path);
        pipeline_free(&p);
        return 1;
    }

    char clang_buf[4096];
    const char* clang = cw_clang_exe();
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
             "\"%s\"%s%s%s -Wno-override-module -mno-stack-arg-probe"
             " -c \"%s\" -o \"%s\"",
             clang, cw_opt_flag(), cw_target_cpu_flag(), cw_lto_clang_flag(),
             bc_path, obj_path);
    int rc = cw_run_command(cmd, NULL);
    if (rc != 0) {
        remove(bc_path);
        remove(obj_path);
        pipeline_free(&p);
        return 1;
    }
    snprintf(cmd, sizeof(cmd),
             "\"%s\"%s%s%s \"%s\""
             " \"%s/cwind_memcenter.c\""
             " \"%s/cwind_object.c\""
             " \"%s/cwind_container.c\""
             " \"%s/cwind_builtin.c\""
             " \"%s/cwind_builtin_table.c\""
             " \"%s/stackframe.c\""
             " \"%s/cwind_unwind.c\""
             " \"%s/cwind_chkstk.c\""
              " \"%s/cwind_gc.c\"",
              gcc_exe, cw_opt_flag(), cw_target_cpu_flag(),
              cw_lto_gcc_flag(), obj_path,
              CWINDC_RT_DIR, CWINDC_RT_DIR, CWINDC_RT_DIR,
              CWINDC_RT_DIR, CWINDC_RT_DIR, CWINDC_RT_DIR,
              CWINDC_RT_DIR,              CWINDC_RT_DIR, CWINDC_RT_DIR);
    /* unwind 的符号解析: dbghelp 走 LoadLibrary 动态加载 (无链接
     * 依赖), 主模块由 rt 自解析 COFF 符号表 —— 无需 -ldbghelp,
     * 也无需 --export-all-symbols (符号名来自 COFF 符号表而非导
     * 出表, strip 前的镜像默认带表)。 */
    /* extern 声明的库放在对象之后 (-l 顺序敏感); 追加失败按命令过长处理 */
    if (!cw_append_lib_flags(cmd, sizeof(cmd), p.m)) {
        fprintf(stderr, "cwindc: link command is too long\n");
        remove(bc_path);
        remove(obj_path);
        pipeline_free(&p);
        return 1;
    }
    {
        const size_t off = strlen(cmd);
        snprintf(cmd + off, sizeof(cmd) - off, " -o \"%s\"", out);
    }
    rc = cw_run_command(cmd, gcc_dir);
    remove(bc_path);
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
    int64_t version = 0;
    // long long version = 0;
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
                (long long)version);
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

/* -- 帮助/诊断渲染 (cwap 只给数据, 文本由宿主输出) ---------------- */

static const char* k_opt_levels[] = {"0", "1", "2", "3", "s", "z", NULL};
static const char* k_lto_modes[] = {"off", "fat", NULL};
static const char* k_emit_modes[] = {"llvm", "obj", "exe", NULL};

/* 把一个选项渲染成 "[-s, ]--name VALUE" 形态, 返回写入长度 */
static int cw_format_option_head(
    const cwap_option* o,
    char* buf,
    size_t cap
) {
    size_t n = 0;
    if (o->short_name) {
        n += (size_t)snprintf(buf + n, cap - n, "-%c", o->short_name);
        if (o->name) n += (size_t)snprintf(buf + n, cap - n, ", ");
    } else {
        n += (size_t)snprintf(buf + n, cap - n, "    ");
    }
    if (o->name) n += (size_t)snprintf(buf + n, cap - n, "--%s", o->name);
    switch (o->style) {
        case CWAP_REQUIRED_VALUE:
            n += (size_t)snprintf(buf + n, cap - n, " %s",
                                  o->metavar ? o->metavar : "ARG");
            break;
        case CWAP_OPTIONAL_VALUE:
            n += (size_t)snprintf(buf + n, cap - n, "[=%s]",
                                  o->metavar ? o->metavar : "ARG");
            break;
        default:
            break;
    }
    return (int)n;
}

static void cw_print_help_to(
    cwap_context* c,
    FILE* fp
) {
    fprintf(fp, "cwindc - CWind typed-AST compiler driver\n\n");
    fprintf(fp, "Usage:\n");
    fprintf(fp, "  cwindc [OPTIONS] <input.json>          "
                "compile (default: exe)\n");
    fprintf(fp, "  cwindc [OPTIONS] -o <out> <input.json> "
                "compile to a chosen path\n");
    fprintf(fp, "  cwindc --check <input.json>            "
                "audit the document\n\n");
    fprintf(fp, "Options:\n");
    /* 先扫一遍求选项头最大宽度, 帮助文本按列对齐 */
    size_t maxw = 0;
    for (size_t i = 0; i < cwap_option_count(c); i++) {
        const cwap_option* o = cwap_option_at(c, i);
        if (o->hidden) continue;
        char head[128];
        const int n = cw_format_option_head(o, head, sizeof(head));
        if (n > 0 && (size_t)n > maxw) maxw = (size_t)n;
    }
    for (size_t i = 0; i < cwap_option_count(c); i++) {
        const cwap_option* o = cwap_option_at(c, i);
        if (o->hidden) continue;
        char head[128];
        const int n = cw_format_option_head(o, head, sizeof(head));
        fprintf(fp, "  %-*s  %s\n", (int)maxw, head,
                o->help ? o->help : "");
    }
    fprintf(fp, "\nExamples:\n");
    fprintf(fp, "  cwindc program.typed.json                "
                "# build program.exe\n");
    fprintf(fp, "  cwindc -o out.exe program.typed.json\n");
    fprintf(fp, "  cwindc --emit llvm program.typed.json    "
                "# -> program.ll\n");
    fprintf(fp, "  cwindc --emit exe -O3 --target-cpu native "
                "--fast-math program.json\n");
    fprintf(fp, "  cwindc --check program.typed.json\n");
}

static void cw_report_error(
    cwap_context* c
) {
    cwap_status es = cwap_error_status(c);
    fprintf(stderr, "cwindc: %s", cwap_status_string(es));
    if (cwap_error_token(c)) {
        fprintf(stderr, " at argument %d: '%s'",
                cwap_error_argc_index(c), cwap_error_token(c));
    }
    if (cwap_error_option_name(c)) {
        fprintf(stderr, " (option --%s)", cwap_error_option_name(c));
    }
    fprintf(stderr, "\n");
    if (cwap_error_suggestion(c)) {
        fprintf(stderr, "hint: did you mean --%s?\n",
                cwap_error_suggestion(c));
    }
}

int main(
    int argc,
    char** argv
) {
    const char* out = NULL;
    const char* in = NULL;
    const char* out_path = NULL;   /* -o/--output */
    const char* pos_out = NULL;    /* INPUT [OUT] 的第二个位置参数 */
    int g_flag_emit_llvm = 0;
    int g_flag_emit_obj = 0;
    int g_flag_emit_exe = 0;
    int check = 0;
    int show_help = 0;
    int show_version = 0;
    const char* emit = NULL;
    const char* opt_level = NULL;
    const char* target_cpu = NULL;
    const char* lto = NULL;
    int fast_math = 0;

    cwap_context* c = cwap_context_new(NULL);
    cwap_flag(c, 'h', "help", &show_help, "show this help and exit");
    cwap_flag(c, 'V', "version", &show_version, "print version and exit");
    cwap_flag(c, 0, "check", &check, "audit the typed-AST document");
    /* -O3 / -O 3 / --opt 3 / --opt=3 由同槽 short+long 承载,
     * choices 白名单约束 0/1/2/3/s/z */
    cwap_option opt_o = {
        .short_name = 'O',
        .name = "opt",
        .style = CWAP_REQUIRED_VALUE,
        .type = CWAP_TYPE_STRING,
        .metavar = "LEVEL",
        .help = "opt level {0,1,2,3,s,z} (default 0)",
        .choices = k_opt_levels,
        .dest = &opt_level,
    };
    cwap_add_option(c, &opt_o);
    cwap_str(c, 0, "target-cpu", "CPU", &target_cpu,
             "code generation cpu ('native' = host)");
    {
        int li = cwap_str(c, 0, "lto", "MODE", &lto,
                          "LTO mode {off,fat} (gcc-side)");
        (void)li;
    }
    /* --lto 用 choice 白名单 */
    {
        cwap_option opt_l = {
            .name = "lto",
            .style = CWAP_REQUIRED_VALUE,
            .type = CWAP_TYPE_STRING,
            .metavar = "MODE",
            .help = "LTO mode {off,fat} (gcc-side)",
            .choices = k_lto_modes,
            .dest = &lto,
        };
        cwap_add_option(c, &opt_l);
    }
    cwap_flag(c, 0, "fast-math", &fast_math,
              "allow unsafe FP transforms (reassoc/contract/...)");
    cwap_choice(c, 0, "emit", "KIND", k_emit_modes, &emit,
                "output kind {llvm,obj,exe} (default exe)");
    /* 旧版三形态 (--emit-llvm/obj/exe) 保留为 --emit 的等价别名
     * (测试与脚本兼容); 与 --emit 同时出现时后者优先报错。 */
    cwap_flag(c, 0, "emit-llvm", &g_flag_emit_llvm,
              "alias of --emit llvm (legacy)");
    cwap_flag(c, 0, "emit-obj", &g_flag_emit_obj,
              "alias of --emit obj (legacy)");
    cwap_flag(c, 0, "emit-exe", &g_flag_emit_exe,
              "alias of --emit exe (legacy)");
    cwap_str(c, 'o', "output", "FILE", &out_path,
             "output path (default: input basename + kind suffix)");

    cwap_pos_str(c, "input", "IN", &in, "typed-ast.json / project.json");
    /* 旧式 `cwindc --emit-<kind> OUT IN` 的第二个位置 token 落进
     * trailing; emit 模式下解释为输出路径 (脚本/测试兼容)。 */
    cwap_set_allow_trailing_positionals(c, 1);

    cwap_status st = cwap_parse(c, argc, argv);
    if (argc <= 1) {
        /* 无参数裸跑: 完整帮助 -> stderr, 退出码 2 (gcc/clang 惯例) */
        cw_print_help_to(c, stderr);
        cwap_context_free(c);
        return 2;
    }
    if (show_help) {
        cw_print_help_to(c, stdout);
        cwap_context_free(c);
        return 0;
    }
    if (st != CWAP_OK) {
        cw_report_error(c);
        fprintf(stderr, "try 'cwindc --help' for usage\n");
        cwap_context_free(c);
        return 2;
    }
    if (show_version) {
        printf("cwindc %s\n", cwap_version_string());
        cwap_context_free(c);
        return 0;
    }

    if (opt_level && !cw_opt_valid(opt_level)) {
        fprintf(stderr, "cwindc: unknown optimization level %s\n", opt_level);
        cwap_context_free(c);
        return 2;
    }
    if (opt_level) {
        g_opt_level = opt_level;
    }
    if (target_cpu && target_cpu[0]) {
        g_target_cpu = target_cpu;
    }
    if (lto) {
        g_lto = lto;
    }
    g_fast_math = fast_math != 0;

    const char* emit_mode = NULL;
    {
        /* legacy 三形态与 --emit 归一; 冲突时后注册的赢不了, 报错 */
        int legacy = g_flag_emit_llvm ? 1 : g_flag_emit_obj ? 2
                   : g_flag_emit_exe ? 3 : 0;
        if (emit && legacy) {
            fprintf(stderr,
                    "cwindc: --emit and legacy --emit-llvm/obj/exe "
                    "are mutually exclusive\n");
            cwap_context_free(c);
            return 2;
        }
        if (!emit && legacy) {
            emit = (legacy == 1) ? "llvm" : (legacy == 2) ? "obj" : "exe";
        }
        /* 非 check 模式缺省 exe: 对齐 gcc/clang/rustc 的
         * "给输入就出可执行文件" 直觉。 */
        if (!emit && !check) {
            emit = "exe";
        }
        if (emit) {
            emit_mode = (strcmp(emit, "llvm") == 0) ? "--emit-llvm"
                      : (strcmp(emit, "obj") == 0)  ? "--emit-obj"
                      :                               "--emit-exe";
        }
    }
    const char* kind = emit ? emit : "exe";

    /* 输出路径归一: -o > 旧式第二个位置 token (OUT) > 按输入名推导
     * (默认 emit exe, 对齐 gcc/clang/rustc 的 "给输入就出可执行文件"
     * 直觉)。 */
    static char def_out[4096];
    out = out_path;
    if (out && cwap_trailing_count(c) > 0) {
        fprintf(stderr,
                "cwindc: -o and positional OUT are mutually exclusive\n");
        cwap_context_free(c);
        return 2;
    }
    if (!out && cwap_trailing_count(c) > 0) {
        /* 旧式双位置 `--emit-exe OUT IN`: cwap 按顺序填充, IN 槽
         * 吃到第一个 token (旧式 OUT), trailing[0] 才是输入, 交换。 */
        out = in;
        in = cwap_trailing_argv(c)[0];
    }
    if (!in && out && !check) {
        /* `cwindc some.json` 的直觉形态: 唯一位置参数是输入 */
        in = out;
        out = NULL;
    }
    if (check) {
        /* check/审计: 输入必填 (优先 IN 槽, 兼容旧 OUT 槽), 无输出 */
        if (!in) {
            in = out;
        }
        out = NULL;
        if (!in) {
            fprintf(stderr, "cwindc: --check requires an input\n");
            cwap_context_free(c);
            return 2;
        }
    } else {
        if (!in) {
            fprintf(stderr,
                    "cwindc: no input; usage: cwindc [-o OUT] "
                    "[--emit KIND] <in.json>\n");
            cwap_context_free(c);
            return 2;
        }
        if (!out) {
            /* x.typed.json -> x.exe / x.obj / x.ll */
            const char* base = in;
            const char* slash = strrchr(in, '/');
            const char* bslash = strrchr(in, '\\');
            if (bslash && bslash > slash) slash = bslash;
            if (slash) base = slash + 1;
            const char* dot = strrchr(base, '.');
            size_t blen = dot ? (size_t)(dot - base) : strlen(base);
            const char* ext = (strcmp(kind, "llvm") == 0) ? ".ll"
                            : (strcmp(kind, "obj") == 0) ? ".obj"
                            : ".exe";
            if ((base - in) + blen + strlen(ext) + 1 > sizeof(def_out)) {
                fprintf(stderr,
                        "cwindc: derived output name is too long\n");
                cwap_context_free(c);
                return 2;
            }
            memcpy(def_out, in, (size_t)((base - in) + blen));
            strcpy(def_out + (base - in) + blen, ext);
            out = def_out;
        }
    }

    /* todo-100: project.json 输入先解析出整程序 TypedAST 工件路径,
     * 之后所有模式按既有管线消费; 非 project 文档原样通过。*/
    {
        /* 输入在所有模式下都是 in (check 已归一); 输出只有 emit
         * 模式需要, check/summary 不消费 out。 */
        char* resolved = NULL;
        int rs = resolve_project_input(in, &resolved);
        if (rs == 0) {
            in = resolved;
        } else if (rs < 0) {
            cwap_context_free(c);
            return 1;
        }
    }

    int code;
    if (emit_mode) {
        if (strcmp(emit_mode, "--emit-llvm") == 0) {
            code = cmd_emit_llvm(out, in);
        } else if (strcmp(emit_mode, "--emit-obj") == 0) {
            code = cmd_emit_obj(out, in);
        } else {
            code = cmd_emit_exe(out, in);
        }
        cwap_context_free(c);
        return code;
    }

    const char* path = in;
    if (!path) {
        cwap_context_free(c);
        return 2;
    }

    CwModule_t* m = cwmodule_load_file(path);
    if (!m) {
        fprintf(stderr, "cwindc: %s\n", cwmodule_error());
        cwap_context_free(c);
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
        cwap_context_free(c);
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
    cwap_context_free(c);
    return 0;
}
