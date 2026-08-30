/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_builtin.c
 */

#include "../include/rt/cwind_builtin.h"

#include "../include/object/cwind_container.h"
#include "../include/memory/cwind_memcenter.h"
#include "../include/gc/cwind_gc.h"

#include <errno.h>
#include <limits.h>
#include <math.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32)
    #include <io.h>
    #include <windows.h>
#endif

/* 格式化递归深度上限: 防自引用容器无限递归 */
#define CWBUILTIN_FMT_MAX_DEPTH 32

/* ---- v0 进程期 arena ----
 * String 拼接 / 枚举载荷单元 / 容器标量元素统一从这里分配, 进程期存活;
 * GC 分配落地后由 GC 取代本机制。
 * 段从内存中心分配 (cwmc_alloc, 大对象独立映射): 块不移动, 已发出的
 * 指针在扩容后保持稳定 (realloc 会移动 base, 让旧值悬垂)。
 */
typedef struct CwArenaSeg {
    char* data;
    size_t used;
    size_t cap;
    struct CwArenaSeg* next;
} CwArenaSeg_t;

static CwArenaSeg_t g_arena = { NULL, 0, 0, NULL };
static size_t g_arena_blocks = 0;

/* 值 arena 返回指针至少 16 字节对齐: 调用方会按 int64/double* 写单元 */
#define CWARENA_ALIGN ((size_t)16)

/* 通用进程期 arena (v0): String 拼接与枚举载荷单元都从这里分配,
 * 直到进程退出归还 OS; GC 分配落地后由 GC 取代。 */
void* cwrt_arena_alloc(size_t size) {
    const size_t need_raw = size + 1;
    if (need_raw < size) return NULL; /* 溢出 */
    const size_t need = (need_raw + (CWARENA_ALIGN - 1))
        & ~(CWARENA_ALIGN - 1);
    CwArenaSeg_t* s = &g_arena;
    while (s->next) s = s->next;
    if (s->cap - s->used < need) {
        size_t ncap = s->cap ? s->cap : (64u * 1024u);
        while (ncap < need) {
            if (ncap > SIZE_MAX / 2) {
                ncap = need;
                break;
            }
            ncap *= 2;
        }
        const size_t hdr = (sizeof(CwArenaSeg_t) + CWARENA_ALIGN - 1)
            & ~(CWARENA_ALIGN - 1);
        CwArenaSeg_t* ns = (CwArenaSeg_t*)cwmc_alloc(hdr + ncap);
        if (!ns) return NULL;
        ns->data = (char*)ns + hdr;
        ns->used = 0;
        ns->cap = ncap;
        ns->next = NULL;
        s->next = ns;
        s = ns;
        g_arena_blocks++;
        /* arena 段进程期存活: 注册为 GC 全局根 (保守扫描段内引用) */
        cwgc_global_register(ns, hdr + ncap);
    }
    char* p = s->data + s->used;
    s->used += need;
    return p;
}

/* 已分配的 arena 段数 (进程期存活, 对应内存中心的 active_allocs) */
size_t cwrt_arena_blocks(void) {
    return g_arena_blocks;
}

/* 把一段字节拷进 arena, 写成 String 值 (owned) */
static bool cwstr_owned_init(CWValue_t* out, const char* data,
                             size_t len) {
    if (!out || (len > 0 && !data)) return false;
    char* buf = (char*)cwrt_arena_alloc(len);
    if (!buf) return false;
    if (len > 0) memcpy(buf, data, len);
    buf[len] = '\0';
    cwval_wrap(out, buf, (uint64_t)len);
    return true;
}

typedef struct CwFmtCtx {
    char* buf;
    size_t cap;
    size_t off;
    int depth;
} CwFmtCtx_t;

static bool cwfmt_push(CwFmtCtx_t* c, const char* s) {
    const size_t n = strlen(s);
    if (c->off + n + 1 > c->cap) return false;
    memcpy(c->buf + c->off, s, n);
    c->off += n;
    c->buf[c->off] = '\0';
    return true;
}

static bool cwfmt_printf(CwFmtCtx_t* c, const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    const int n = vsnprintf(c->buf + c->off, c->cap - c->off, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= c->cap - c->off) return false;
    c->off += (size_t)n;
    return true;
}

static bool cwfmt_bytes(CwFmtCtx_t* c, const char* data, size_t len) {
    if (c->off + len + 1 > c->cap) return false;
    memcpy(c->buf + c->off, data, len);
    c->off += len;
    c->buf[c->off] = '\0';
    return true;
}


// pre def
static bool cwfmt_value(CwFmtCtx_t* c, int32_t type_id, const CWValue_t* v);

static bool cwfmt_container(CwFmtCtx_t* c, int32_t type_id,
                            const CWValue_t* v,
                            const char* open, const char* close) {
    if (c->depth >= CWBUILTIN_FMT_MAX_DEPTH) return false;
    c->depth++;
    bool ok = cwfmt_push(c, open);

    CWindVectorIter_t vit;
    CWindTupleIter_t tit;
    CWindMapIter_t mit;
    CWindSetIter_t sit;
    cwvec_iter_begin(v, &vit);
    cwtuple_iter_begin(v, &tit);
    cwmap_iter_begin(v, &mit);
    cwset_iter_begin(v, &sit);

    bool first = true;

    switch (type_id) {
    case CWVector: {
        const int32_t et = cwvec_elem_type(v);
        while (ok && cwvec_iter_valid(&vit)) {
            CWValue_t e;
            if (!cwvec_iter_value(&vit, &e)) { ok = false; break; }
            if (!first && !cwfmt_push(c, ", ")) { ok = false; break; }
            first = false;
            ok = cwfmt_value(c, et, &e);
            cwvec_iter_next(&vit);
        }
        break;
    }
    case CWTuple: {
        while (ok && cwtuple_iter_valid(&tit)) {
            CWValue_t e;
            if (!cwtuple_iter_value(&tit, &e)) { ok = false; break; }
            if (!first && !cwfmt_push(c, ", ")) { ok = false; break; }
            first = false;
            ok = cwfmt_value(c, cwtuple_elem_type(v, tit.index), &e);
            cwtuple_iter_next(&tit);
        }
        break;
    }
    case CWMap: {
        CWValue_t k, val;
        CWValue_t probe;
        cwval_none(&probe);
        (void)probe;
        while (ok && cwmap_iter_valid(&mit)) {
            if (!cwmap_iter_key(&mit, &k) || !cwmap_iter_value(&mit, &val)) {
                ok = false;
                break;
            }
            if (!first && !cwfmt_push(c, ", ")) { ok = false; break; }
            first = false;
            ok = cwfmt_value(c, cwmap_key_type(v), &k)
              && cwfmt_push(c, ": ")
              && cwfmt_value(c, cwmap_value_type(v), &val);
            cwmap_iter_next(&mit);
        }
        break;
    }
    case CWSet: {
        const int32_t et = cwset_elem_type(v);
        while (ok && cwset_iter_valid(&sit)) {
            CWValue_t e;
            if (!cwset_iter_item(&sit, &e)) { ok = false; break; }
            if (!first && !cwfmt_push(c, ", ")) { ok = false; break; }
            first = false;
            ok = cwfmt_value(c, et, &e);
            cwset_iter_next(&sit);
        }
        break;
    }
    default:
        ok = false;
        break;
    }
    c->depth--;
    return ok && cwfmt_push(c, close);
}


typedef bool (*CwFmtHandler_t)(CwFmtCtx_t* c, const CWValue_t* v);

static bool cwfmt_handler_int(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%d",
                        *(const int16_t*)(uintptr_t)v->address);
}

static bool cwfmt_handler_uint(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%u",
                        *(const uint16_t*)(uintptr_t)v->address);
}

static bool cwfmt_handler_int8(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%d",
                        *(const int8_t*)(uintptr_t)v->address);
}

static bool cwfmt_handler_uint8(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%u",
                        *(const uint8_t*)(uintptr_t)v->address);
}

static bool cwfmt_handler_int16(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%d",
                        *(const int16_t*)(uintptr_t)v->address);
}

static bool cwfmt_handler_uint16(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%u",
                        *(const uint16_t*)(uintptr_t)v->address);
}

static bool cwfmt_handler_int32(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%lld",
                        (long long)*(const int32_t*)(uintptr_t)v->address);
}

static bool cwfmt_handler_uint32(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%llu",
                        (unsigned long long)*(const uint32_t*)(uintptr_t)v->address);
}

static bool cwfmt_handler_int64(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%lld",
                        (long long)*(const int64_t*)(uintptr_t)v->address);
}

static bool cwfmt_handler_uint64(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%llu",
                        (unsigned long long)*(const uint64_t*)(uintptr_t)v->address);
}

static bool cwfmt_handler_float(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%g", *(const float*)(uintptr_t)v->address);
}

static bool cwfmt_handler_float64(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_printf(c, "%g", *(const double*)(uintptr_t)v->address);
}

static bool cwfmt_handler_bool(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_push(c, *(const bool*)(uintptr_t)v->address
                          ? "true" : "false");
}

static bool cwfmt_handler_string(CwFmtCtx_t* c, const CWValue_t* v) {
    return v->address
        ? cwfmt_bytes(c, (const char*)(uintptr_t)v->address,
                      (size_t)v->length)
        : cwfmt_push(c, "");
}

static bool cwfmt_handler_none(CwFmtCtx_t* c, const CWValue_t* v) {
    (void)v;
    return cwfmt_push(c, "None");
}

static bool cwfmt_handler_vector(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_container(c, CWVector, v, "[", "]");
}

static bool cwfmt_handler_tuple(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_container(c, CWTuple, v, "(", ")");
}

static bool cwfmt_handler_map(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_container(c, CWMap, v, "{", "}");
}

static bool cwfmt_handler_set(CwFmtCtx_t* c, const CWValue_t* v) {
    return cwfmt_container(c, CWSet, v, "{", "}");
}

/* 按 type_id 直接索引; 7 号是已取消的 CWInstance, 保持空槽。 */
static const CwFmtHandler_t k_cwfmt_handlers[] = {
    [CWInt]      = cwfmt_handler_int,
    [CWUInt]     = cwfmt_handler_uint,
    [CWFloat]    = cwfmt_handler_float,
    [CWBool]     = cwfmt_handler_bool,
    [CWByte]     = cwfmt_handler_uint8,
    [CWString]   = cwfmt_handler_string,
    [CWNone]     = cwfmt_handler_none,
    [CWTuple]    = cwfmt_handler_tuple,
    [CWVector]   = cwfmt_handler_vector,
    [CWMap]      = cwfmt_handler_map,
    [CWSet]      = cwfmt_handler_set,
    [CWInt8]     = cwfmt_handler_int8,
    [CWUInt8]    = cwfmt_handler_uint8,
    [CWInt16]    = cwfmt_handler_int16,
    [CWUInt16]   = cwfmt_handler_uint16,
    [CWInt32]    = cwfmt_handler_int32,
    [CWUInt32]   = cwfmt_handler_uint32,
    [CWInt64]    = cwfmt_handler_int64,
    [CWUInt64]   = cwfmt_handler_uint64,
    [CWFloat64]  = cwfmt_handler_float64,
};

static bool cwfmt_value(CwFmtCtx_t* c, int32_t type_id, const CWValue_t* v) {
    if (!v) return cwfmt_push(c, "?");
    /* 标量 handler 会解引用 address; 失败查找 (cwvec_at/cwmap_get 返回
     * false) 留下的空句柄 address=0, 按缺数据格式化而不是崩溃 */
    if (v->address == 0 && type_id != CWString && type_id != CWNone
        && type_id != CWTuple && type_id != CWVector && type_id != CWMap
        && type_id != CWSet) {
        return cwfmt_push(c, "?");
    }
    if (type_id < 0
        || type_id >= (int)(sizeof(k_cwfmt_handlers)
                            / sizeof(k_cwfmt_handlers[0]))) {
        return cwfmt_push(c, "?");
    }
    const CwFmtHandler_t fn = k_cwfmt_handlers[type_id];
    return fn ? fn(c, v) : cwfmt_push(c, "?");
}

bool cwobj_format(int32_t type_id, const CWValue_t* v,
                  char* buf, size_t cap) {
    if (!buf || cap == 0) return false;
    buf[0] = '\0';
    if (!v) return false;
    CwFmtCtx_t c = { buf, cap, 0, 0 };
    return cwfmt_value(&c, type_id, v);
}

/* 输出一段 UTF-8 字节:
 *  - Windows 交互控制台: 转 UTF-16 后走 WriteConsoleW, 不受控制台代码页
 *    影响, 中文等 Unicode 不会乱码;
 *  - 重定向/管道/文件: 原样写 UTF-8 字节, 保证落盘内容始终是合法 UTF-8。
 */
static bool cwbuiltin_write_utf8(FILE* f, const char* data, size_t len) {
    if (!data || len == 0) return len == 0;
#if defined(_WIN32)
    const int fd = _fileno(f);
    if (fd >= 0 && _isatty(fd)) {
        HANDLE h = (HANDLE)_get_osfhandle(fd);
        DWORD mode = 0;
        if (h != INVALID_HANDLE_VALUE && GetConsoleMode(h, &mode)
            && len <= (size_t)INT_MAX) {
            const int wlen = MultiByteToWideChar(
                CP_UTF8, MB_ERR_INVALID_CHARS, data, (int)len, NULL, 0);
            if (wlen > 0) {
                wchar_t* wbuf = (wchar_t*)malloc(
                    (size_t)wlen * sizeof(wchar_t));
                if (!wbuf) return false;
                const int rc = MultiByteToWideChar(
                    CP_UTF8, MB_ERR_INVALID_CHARS, data, (int)len,
                    wbuf, wlen);
                DWORD written = 0;
                const BOOL ok = rc > 0
                    && WriteConsoleW(h, wbuf, (DWORD)wlen, &written, NULL);
                free(wbuf);
                if (ok && written == (DWORD)wlen) return true;
                /* 宽字符写入失败时退化为原样写 UTF-8, 不丢数据 */
            }
        }
    }
#endif
    return fwrite(data, 1, len, f) == len;
}

bool cw_builtin_print_to(FILE* f, int32_t type_id, const CWValue_t* v) {
    if (!f || !v) return false;
    if (type_id == CWString) {
        if (!cwbuiltin_write_utf8(f, (const char*)(uintptr_t)v->address,
                                  (size_t)v->length)) {
            return false;
        }
        return cwbuiltin_write_utf8(f, "\n", 1);
    }
    char buf[4096];
    if (!cwobj_format(type_id, v, buf, sizeof(buf))) return false;
    return cwbuiltin_write_utf8(f, buf, strlen(buf))
        && cwbuiltin_write_utf8(f, "\n", 1);
}

bool cw_builtin_print(int32_t type_id, const CWValue_t* v) {
    return cw_builtin_print_to(stdout, type_id, v);
}

bool cw_builtin_type_of(int32_t type_id, char* buf, size_t cap) {
    if (!buf || cap == 0) return false;
    const char* name = cwobj_type_name(type_id);
    const size_t n = strlen(name);
    if (n + 1 > cap) return false;
    memcpy(buf, name, n + 1);
    return true;
}

bool cw_builtin_length(int32_t type_id, const CWValue_t* v, uint64_t* out) {
    if (!v || !out) return false;
    switch (type_id) {
    case CWString:
        *out = v->length;
        return true;
    case CWVector:
        *out = (uint64_t)cwvec_size(v);
        return true;
    case CWMap:
        *out = (uint64_t)cwmap_size(v);
        return true;
    case CWSet:
        *out = (uint64_t)cwset_size(v);
        return true;
    case CWTuple:
        *out = (uint64_t)cwtuple_size(v);
        return true;
    default:
        return false;
    }
}

static bool cwbuiltin_string_contains(const CWValue_t* haystack,
                                      const CWValue_t* needle,
                                      bool* out) {
    const char* hd = NULL;
    const char* nd = NULL;
    uint64_t hl = 0, nl = 0;
    if (!cwobj_string_view(haystack, &hd, &hl)
        || !cwobj_string_view(needle, &nd, &nl)) {
        return false;
    }
    if (nl == 0) {
        *out = true;
        return true;
    }
    if (nl > hl) {
        *out = false;
        return true;
    }
    for (uint64_t i = 0; i + nl <= hl; i++) {
        if (memcmp(hd + i, nd, (size_t)nl) == 0) {
            *out = true;
            return true;
        }
    }
    *out = false;
    return true;
}

bool cw_builtin_contains(int32_t container_type, const CWValue_t* c,
                         int32_t item_type, const CWValue_t* item,
                         bool* out) {
    if (!c || !item || !out) return false;
    switch (container_type) {
    case CWString:
        return cwbuiltin_string_contains(c, item, out);
    case CWVector: {
        CWindVectorIter_t it;
        cwvec_iter_begin(c, &it);
        while (cwvec_iter_valid(&it)) {
            CWValue_t e;
            if (cwvec_iter_value(&it, &e)
                && cwobj_value_equal(cwvec_elem_type(c), &e, item)) {
                *out = true;
                return true;
            }
            cwvec_iter_next(&it);
        }
        *out = false;
        return true;
    }
    case CWSet:
        *out = cwset_contains(c, item);
        return true;
    case CWMap:
        *out = cwmap_get(c, item, NULL);
        return true;
    default:
        (void)item_type;
        return false;
    }
}

bool cw_builtin_to_string(int32_t type_id, const CWValue_t* v,
                          char* buf, size_t cap) {
    return cwobj_format(type_id, v, buf, cap);
}

/* 目标数值宽度 (字节); 不支持的类型返回 false */
static bool cwbuiltin_parse_width(int32_t type_id, size_t* width) {
    const size_t w = cwobj_scalar_width(type_id);
    if (w == 0) return false;
    *width = w;
    return true;
}

bool cw_builtin_parse_owned(const CWValue_t* src,
                            int32_t target_type_id, CWValue_t* out) {
    if (!src || !out) return false;
    size_t width = 0;
    if (!cwbuiltin_parse_width(target_type_id, &width)) return false;

    const char* text = (const char*)(uintptr_t)src->address;
    const size_t len = (size_t)src->length;
    char buf[128];
    if (len >= sizeof(buf)) return false;
    if (len > 0 && text) memcpy(buf, text, len);
    buf[len] = '\0';

    char* end = NULL;
    int64_t iv = 0;
    uint64_t uv = 0;
    double dv = 0;
    bool ok = true;
    errno = 0;
    switch (target_type_id) {
    case CWInt:
    case CWInt8:
    case CWInt16:
    case CWInt32:
    case CWInt64:
        iv = strtoll(buf, &end, 10);
        if (end == buf || *end != '\0' || errno == ERANGE) ok = false;
        break;
    case CWUInt:
    case CWUInt8:
    case CWUInt16:
    case CWUInt32:
    case CWUInt64:
    case CWByte:
        /* strtoull 接受可选符号: 无符号目标显式拒绝 '-' 输入 */
        {
            const char* q = buf;
            while (*q == ' ' || *q == '\t' || *q == '\n'
                   || *q == '\r' || *q == '\v' || *q == '\f') {
                q++;
            }
            if (*q == '-') ok = false;
        }
        if (!ok) break;
        uv = strtoull(buf, &end, 10);
        if (end == buf || *end != '\0' || errno == ERANGE) ok = false;
        break;
    case CWFloat:
    case CWFloat64:
        dv = strtod(buf, &end);
        if (end == buf || *end != '\0' || errno == ERANGE) ok = false;
        break;
    default:
        return false;
    }

    /* 窄目标范围检查: 解析成功但超出目标宽度也算失败 */
    if (ok) {
        switch (target_type_id) {
        case CWInt8:
            ok = iv >= INT8_MIN && iv <= INT8_MAX;
            break;
        case CWUInt8:
        case CWByte:
            ok = uv <= UINT8_MAX;
            break;
        case CWInt:
            ok = iv >= INT16_MIN && iv <= INT16_MAX;
            break;
        case CWUInt:
            ok = uv <= UINT16_MAX;
            break;
        case CWInt16:
            ok = iv >= INT16_MIN && iv <= INT16_MAX;
            break;
        case CWUInt16:
            ok = uv <= UINT16_MAX;
            break;
        case CWInt32:
            ok = iv >= INT32_MIN && iv <= INT32_MAX;
            break;
        case CWUInt32:
            ok = uv <= UINT32_MAX;
            break;
        case CWInt64:
        case CWUInt64:
            break; /* strto* + ERANGE 已覆盖 */
        case CWFloat:
            ok = isfinite((float)dv);
            break;
        case CWFloat64:
            ok = isfinite(dv);
            break;
        default:
            return false;
        }
    }

    /* 失败也写 0, 保证 out 一定是可读的合法数值 */
    void* cell = cwrt_arena_alloc(width);
    if (!cell) return false;
    switch (target_type_id) {
    case CWInt8:   *(int8_t*)cell  = ok ? (int8_t)iv  : 0; break;
    case CWByte:   *(uint8_t*)cell = ok ? (uint8_t)uv : 0; break;
    case CWUInt8:  *(uint8_t*)cell = ok ? (uint8_t)uv : 0; break;
    case CWInt16:  *(int16_t*)cell = ok ? (int16_t)iv  : 0; break;
    case CWUInt16: *(uint16_t*)cell = ok ? (uint16_t)uv : 0; break;
    case CWInt:    *(int16_t*)cell = ok ? (int16_t)iv  : 0; break;
    case CWUInt:   *(uint16_t*)cell = ok ? (uint16_t)uv : 0; break;
    case CWInt32:  *(int32_t*)cell = ok ? (int32_t)iv  : 0; break;
    case CWUInt32: *(uint32_t*)cell = ok ? (uint32_t)uv : 0; break;
    case CWFloat:  *(float*)cell    = ok ? (float)dv    : 0.0f; break;
    case CWInt64:  *(int64_t*)cell  = ok ? iv : 0; break;
    case CWUInt64: *(uint64_t*)cell = ok ? uv : 0; break;
    case CWFloat64: *(double*)cell  = ok ? dv : 0.0; break;
    default:
        return false;
    }

    cwval_wrap(out, cell, (uint64_t)width);
    return ok;
}

bool cw_builtin_to_string_owned(int32_t type_id, const CWValue_t* v,
                                CWValue_t* out) {
    char buf[4096];
    if (!cwobj_format(type_id, v, buf, sizeof(buf))) return false;
    return cwstr_owned_init(out, buf, strlen(buf));
}

bool cw_builtin_concat(const CWValue_t* a, const CWValue_t* b,
                       CWValue_t* out) {
    if (!a || !b || !out) return false;
    const char* sa = (const char*)(uintptr_t)a->address;
    const char* sb = (const char*)(uintptr_t)b->address;
    const size_t la = (size_t)a->length;
    const size_t lb = (size_t)b->length;
    if ((la > 0 && !sa) || (lb > 0 && !sb)) return false;
    if (SIZE_MAX - la < lb) return false; /* 长度溢出 */
    char* buf = (char*)cwrt_arena_alloc(la + lb);
    if (!buf) return false;
    if (la > 0) memcpy(buf, sa, la);
    if (lb > 0) memcpy(buf + la, sb, lb);
    buf[la + lb] = '\0';
    cwval_wrap(out, buf, (uint64_t)(la + lb));
    return true;
}

/* ---- String::format: 基础栈机式模板扫描 ----
 *
 * 模板按原始文本传入 (转义保留), 逐字节扫描:
 *  - 普通字符直接输出;
 *  - `\` 转义: `\{`/`\}` 输出字面花括号, 其余与字符串字面量一致,
 *    反斜杠换行是续行 (不输出);
 *  - `{}` 空占位符: 按顺序消费一个参数并格式化;
 *  - 其它占位符内容 / 参数不足 / 多余 `}`: 失败, out 置空串。
 * 结果字节分配在字符串 arena 中, 与 concat 同一归属。
 */

typedef struct CwFmtDynBuf {
    char* data;
    size_t len;
    size_t cap;
} CwFmtDynBuf_t;

static bool cwfmt_dyn_reserve(CwFmtDynBuf_t* b, size_t need) {
    if (need <= b->cap) return true;
    size_t ncap = b->cap ? b->cap : 64;
    while (ncap < need) {
        if (ncap > SIZE_MAX / 2) {
            ncap = need;
            break;
        }
        ncap *= 2;
    }
    char* nb = (char*)realloc(b->data, ncap);
    if (!nb) return false;
    b->data = nb;
    b->cap = ncap;
    return true;
}

static bool cwfmt_dyn_append(CwFmtDynBuf_t* b, const char* s, size_t n) {
    if (n == 0) return true;
    if (n > SIZE_MAX - b->len - 1) return false; /* 长度溢出 */
    if (!cwfmt_dyn_reserve(b, b->len + n + 1)) return false;
    memcpy(b->data + b->len, s, n);
    b->len += n;
    b->data[b->len] = '\0';
    return true;
}

static bool cwfmt_is_space(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r'
        || c == '\v' || c == '\f';
}

/* 把值格式化进动态缓冲区: 先试小栈缓冲, 不够再堆上翻倍 */
/* 翻倍上限 64 MiB: MSVCRT 的 vsnprintf 对超大 size 参数会破坏堆 (实测
 * 4 GiB 即崩), 且 v0 也不该为一次格式化无界分配; 超限视为失败。 */
#define CWBUILTIN_FMT_MAX_DYN (64u * 1024u * 1024u)
static bool cwfmt_dyn_append_value(CwFmtDynBuf_t* b, int32_t type_id,
                                   const CWValue_t* v) {
    char stack[256];
    size_t cap = sizeof(stack);
    char* buf = stack;
    for (;;) {
        if (cwobj_format(type_id, v, buf, cap)) {
            const bool ok = cwfmt_dyn_append(b, buf, strlen(buf));
            if (buf != stack) free(buf);
            return ok;
        }
        /* cwobj_format 失败不一定是缓冲区不够 (如自引用容器命中深度上限),
         * 不能无界翻倍 (超大 vsnprintf size 会破坏 MSVCRT 堆, 也会触发
         * -Walloc-size-larger-than); 到上限就放弃。 */
        if (cap >= CWBUILTIN_FMT_MAX_DYN) {
            if (buf != stack) free(buf);
            return false;
        }
        cap *= 2;
        char* nb = (char*)malloc(cap);
        if (!nb) {
            if (buf != stack) free(buf);
            return false;
        }
        if (buf != stack) free(buf);
        buf = nb;
    }
}

static bool cwfmt_append_escape(CwFmtDynBuf_t* b, char e) {
    switch (e) {
    case 'n':  return cwfmt_dyn_append(b, "\n", 1);
    case 'r':  return cwfmt_dyn_append(b, "\r", 1);
    case 't':  return cwfmt_dyn_append(b, "\t", 1);
    case '\\': return cwfmt_dyn_append(b, "\\", 1);
    case '\'': return cwfmt_dyn_append(b, "'", 1);
    case '"':  return cwfmt_dyn_append(b, "\"", 1);
    case '0':  return cwfmt_dyn_append(b, "\0", 1);
    case 'b':  return cwfmt_dyn_append(b, "\b", 1);
    case 'v':  return cwfmt_dyn_append(b, "\v", 1);
    case '{':  return cwfmt_dyn_append(b, "{", 1);
    case '}':  return cwfmt_dyn_append(b, "}", 1);
    default:
        /* 未知转义: 与 lexer 一致, 原样保留 */
        return cwfmt_dyn_append(b, "\\", 1)
            && cwfmt_dyn_append(b, &e, 1);
    }
}

bool cw_builtin_format(const CWValue_t* self,
                       const CWCell_t* args, size_t nargs,
                       CWValue_t* out) {
    if (!self || !out) return false;
    const char* s = (const char*)(uintptr_t)self->address;
    const size_t n = (size_t)self->length;
    if (n > 0 && !s) return false;

    CwFmtDynBuf_t buf = { NULL, 0, 0 };
    size_t ai = 0;
    size_t i = 0;
    bool ok = true;
    while (i < n) {
        const char c = s[i];
        if (c == '\\') {
            if (i + 1 >= n) {
                ok = cwfmt_dyn_append(&buf, "\\", 1);
                i += 1;
                continue;
            }
            const char e = s[i + 1];
            if (e == '\n') {
                i += 2; /* 反斜杠换行续行 */
                continue;
            }
            if (e == '\r' && i + 2 < n && s[i + 2] == '\n') {
                i += 3;
                continue;
            }
            ok = cwfmt_append_escape(&buf, e);
            i += 2;
            continue;
        }
        if (c == '{') {
            /* 找配对的 '}' (前端已做花括号配平粗检, v0 不嵌套) */
            size_t j = i + 1;
            while (j < n && s[j] != '}') j++;
            if (j >= n) {
                ok = false;
                break;
            }
            size_t k = i + 1;
            while (k < j && cwfmt_is_space(s[k])) k++;
            size_t ke = j;
            while (ke > k && cwfmt_is_space(s[ke - 1])) ke--;
            if (k == ke) {
                if (ai >= nargs) {
                    ok = false;
                    break;
                }
                ok = cwfmt_dyn_append_value(&buf, args[ai].type_id,
                                            &args[ai].value);
                ai++;
            } else {
                /* 非空占位符 (命名/表达式) v0 不支持 */
                ok = false;
                break;
            }
            i = j + 1;
            continue;
        }
        if (c == '}') {
            ok = false;
            break;
        }
        ok = cwfmt_dyn_append(&buf, &c, 1);
        i += 1;
        if (!ok) break;
    }
    if (!ok) {
        free(buf.data);
        cwstr_owned_init(out, "", 0);
        return false;
    }
    ok = cwstr_owned_init(out, buf.data ? buf.data : "", buf.len);
    free(buf.data);
    return ok;
}

bool cw_builtin_type_of_owned(int32_t type_id, const CWValue_t* v,
                              CWValue_t* out) {
    (void)v;
    if (!out) return false;
    char tmp[64];
    if (!cw_builtin_type_of(type_id, tmp, sizeof(tmp))) return false;
    return cwstr_owned_init(out, tmp, strlen(tmp));
}

bool cw_builtin_readline(CWValue_t* out) {
    if (!out) return false;
    char* tmp = NULL;
    size_t cap = 0;
    size_t len = 0;
    bool got_line = false;
    for (;;) {
        int c = fgetc(stdin);
        if (c == EOF) break;
        if (c == '\n') {
            got_line = true;
            break;
        }
        if (c != '\r') {
            if (len + 2 > cap) {
                size_t ncap = cap ? cap * 2 : 128;
                char* nb = (char*)realloc(tmp, ncap);
                if (!nb) {
                    free(tmp);
                    return false;
                }
                tmp = nb;
                cap = ncap;
            }
            tmp[len++] = (char)c;
        }
    }
    if (!got_line && len == 0) {
        /* 无任何输入的 EOF: 写入空串而非裸返回 false (Rust read_line 语义).
         * 调用方 (代码生成) 不检查返回值, out 必须恒为有效值,
         * 否则未初始化栈内存会以野地址流进 print (写越界崩溃). */
        return cwstr_owned_init(out, "", 0);
    }
    const bool ok = cwstr_owned_init(out, tmp, len);
    free(tmp);
    return ok;
}

_Noreturn void cw_builtin_exit(int code) {
    exit(code);
}

/* ---- bug-30: 程序参数注入 ----
 *
 * 把 C main 收到的 argc/argv 打包成 Vector<String> 值:
 *  - 元素值直接引用 argv 存储 (进程期存活, 零拷贝),
 *    与字符串字面量指向全局常量字节是同一语义;
 *  - out 必须是可写的 CWValue, 成功写入 Vector 值并返回 true。
 */
bool cw_builtin_main_args(int argc, char** argv, CWValue_t* out) {
    if (!out) return false;
    const size_t want = (argc > 1) ? (size_t)(argc - 1) : 0;
    if (!cwvec_init(out, CWString, want)) return false;
    for (int i = 1; i < argc; i++) {
        const char* s = argv ? argv[i] : NULL;
        const size_t len = s ? strlen(s) : 0;
        CWValue_t cell;
        cwval_wrap(&cell, s, (uint64_t)len);
        if (!cwvec_push(out, &cell)) return false;
    }
    return true;
}
