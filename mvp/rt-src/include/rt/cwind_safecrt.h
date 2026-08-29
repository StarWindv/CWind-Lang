/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/rt/cwind_safecrt.h
 */

/**
 * todo-152: MSVC/UCRT 头把 fopen/getenv 标记为 deprecated (C4996),
 * 编译时刷屏. 这里集中提供跨平台的安全包装:
 *  - MSVC 家族头 (_MSC_VER, 含 clang + MSVC/UCRT 头) 走 _s 版本;
 *  - 其它工具链 (MinGW gcc / POSIX) 走 ISO C 原生函数, 语义等价;
 *  - 调用方不再需要 _CRT_SECURE_NO_WARNINGS 压制.
 * STL 例外: rt-src/include/stl 内部仍直接用 fopen, 属用户钦定
 * 不可改动区域, 由 cwmodule.c 的既有压制宏兜底.
 */

#ifndef CWIND_ABI_SAFECRT_H
#define CWIND_ABI_SAFECRT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* fopen 安全包装; 失败返回 NULL, 其余语义与 fopen 一致 */
static inline FILE* cw_fopen(const char* path, const char* mode) {
#if defined(_MSC_VER)
    FILE* f = NULL;
    if (fopen_s(&f, path, mode) != 0) return NULL;
    return f;
#else
    return fopen(path, mode);
#endif
}

/* freopen 安全包装 (stdin 重定向等); 失败返回 NULL */
static inline FILE* cw_freopen(const char* path, const char* mode,
                               FILE* stream) {
#if defined(_MSC_VER)
    FILE* f = NULL;
    if (freopen_s(&f, path, mode, stream) != 0) return NULL;
    return f;
#else
    return freopen(path, mode, stream);
#endif
}

/* 读环境变量进 buf (含终止 NUL).
 * 不存在返回 false; 值 (含 NUL) 超出 cap 返回 false —— 与 MSVC
 * getenv_s 的 ERANGE 语义对齐, 不做静默截断. */
static inline bool cw_env_get(const char* name, char* buf, size_t cap) {
#if defined(_MSC_VER)
    size_t need = 0;
    if (getenv_s(&need, buf, cap, name) != 0) return false;
    return need != 0;
#else
    const char* v = getenv(name);
    if (!v) return false;
    const size_t n = strlen(v) + 1;
    if (n > cap) return false;
    memcpy(buf, v, n);
    return true;
#endif
}

/* 环境变量是否存在 (任意值, 含空串) */
static inline bool cw_env_has(const char* name) {
#if defined(_MSC_VER)
    size_t need = 0;
    return getenv_s(&need, NULL, 0, name) == 0 && need > 0;
#else
    return getenv(name) != NULL;
#endif
}

#endif /* CWIND_ABI_SAFECRT_H */
