/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_builtin.c
 */

#include "../include/rt/cwind_builtin.h"

#include "../include/object/cwind_container.h"
#include "../include/memory/cwind_memcenter.h"

#include <errno.h>
#include <limits.h>
#include <math.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

/* 格式化递归深度上限: 防自引用容器无限递归 */
#define CWBUILTIN_FMT_MAX_DEPTH 32

static const CWObjHandle_t* cwbuiltin_handle(const CWindObject_t* obj) {
    return (const CWObjHandle_t*)((const char*)obj + sizeof(CWindObject_t));
}

static CWObjHandle_t* cwbuiltin_handle_mut(CWindObject_t* obj) {
    return (CWObjHandle_t*)((char*)obj + sizeof(CWindObject_t));
}

/* ---- v0 进程期 arena ----
 * String 拼接 / 枚举载荷单元 / 容器标量元素统一从这里分配, 进程期存活;
 * GC 页 (cwind_mempage/wal) 落地后由 GC 分配取代本机制。
 * 段从内存中心分配 (cwmc_alloc, 大对象独立映射): 块不移动, 已发出的
 * 指针在扩容后保持稳定 (realloc 会移动 base, 让旧句柄悬垂)。
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
 * 直到进程退出归还 OS; GC 页 (cwind_mempage/wal) 落地后由 GC 取代。 */
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
    }
    char* p = s->data + s->used;
    s->used += need;
    return p;
}

/* 已分配的 arena 段数 (进程期存活, 对应内存中心的 active_allocs) */
size_t cwrt_arena_blocks(void) {
    return g_arena_blocks;
}

/* 把一段字节拷进 arena, 写成 String 记录 (owned) */
static bool cwstr_owned_init(CWindObject_t* out, const char* data,
                             size_t len) {
    if (!out || (len > 0 && !data)) return false;
    char* buf = (char*)cwrt_arena_alloc(len);
    if (!buf) return false;
    if (len > 0) memcpy(buf, data, len);
    buf[len] = '\0';
    cwobj_init(out, CWString);
    CWObjHandle_t* ho = cwbuiltin_handle_mut(out);
    ho->object  = out;
    ho->address = (uint64_t)(uintptr_t)buf;
    ho->length  = (uint64_t)len;
    ho->cursor  = 0;
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

static bool cwfmt_record(CwFmtCtx_t* c, const CWindObject_t* obj);

static bool cwfmt_container(CwFmtCtx_t* c, const CWindObject_t* obj,
                            const char* open, const char* close) {
    if (c->depth >= CWBUILTIN_FMT_MAX_DEPTH) return false;
    c->depth++;
    bool ok = cwfmt_push(c, open);

    CWindVectorIter_t vit;
    CWindTupleIter_t tit;
    CWindMapIter_t mit;
    CWindSetIter_t sit;
    cwvec_iter_begin((const CWindVectorObject_t*)obj, &vit);
    cwtuple_iter_begin((const CWindTupleObject_t*)obj, &tit);
    cwmap_iter_begin((const CWindMapObject_t*)obj, &mit);
    cwset_iter_begin((const CWindSetObject_t*)obj, &sit);

    bool first = true;

    switch (obj->type_id) {
    case CWVector:
        while (ok && cwvec_iter_valid(&vit)) {
            CWindIntObject_t r;
            if (!cwvec_iter_value(&vit, &r)) { ok = false; break; }
            if (!first && !cwfmt_push(c, ", ")) { ok = false; break; }
            first = false;
            ok = cwfmt_record(c, &r.head);
            cwvec_iter_next(&vit);
        }
        break;
    case CWTuple:
        while (ok && cwtuple_iter_valid(&tit)) {
            CWindIntObject_t r;
            if (!cwtuple_iter_value(&tit, &r)) { ok = false; break; }
            if (!first && !cwfmt_push(c, ", ")) { ok = false; break; }
            first = false;
            ok = cwfmt_record(c, &r.head);
            cwtuple_iter_next(&tit);
        }
        break;
    case CWMap:
        while (ok && cwmap_iter_valid(&mit)) {
            CWindIntObject_t k, v;
            if (!cwmap_iter_key(&mit, &k) || !cwmap_iter_value(&mit, &v)) {
                ok = false;
                break;
            }
            if (!first && !cwfmt_push(c, ", ")) { ok = false; break; }
            first = false;
            ok = cwfmt_record(c, &k.head)
              && cwfmt_push(c, ": ")
              && cwfmt_record(c, &v.head);
            cwmap_iter_next(&mit);
        }
        break;
    case CWSet:
        while (ok && cwset_iter_valid(&sit)) {
            CWindIntObject_t r;
            if (!cwset_iter_item(&sit, &r)) { ok = false; break; }
            if (!first && !cwfmt_push(c, ", ")) { ok = false; break; }
            first = false;
            ok = cwfmt_record(c, &r.head);
            cwset_iter_next(&sit);
        }
        break;
    default:
        ok = false;
        break;
    }
    c->depth--;
    return ok && cwfmt_push(c, close);
}

static bool cwfmt_record(CwFmtCtx_t* c, const CWindObject_t* obj) {
    if (!obj) return cwfmt_push(c, "?");
    const CWObjHandle_t* h = cwbuiltin_handle(obj);

    switch (obj->type_id) {
    case CWInt:
        return cwfmt_printf(c, "%d", *(const int16_t*)(uintptr_t)h->address);
    case CWUInt:
        return cwfmt_printf(c, "%u", *(const uint16_t*)(uintptr_t)h->address);
    case CWInt8:
        return cwfmt_printf(c, "%d", *(const int8_t*)(uintptr_t)h->address);
    case CWUInt8:
    case CWByte:
        return cwfmt_printf(c, "%u", *(const uint8_t*)(uintptr_t)h->address);
    case CWInt32:
        return cwfmt_printf(c, "%lld",
                            (long long)*(const int32_t*)(uintptr_t)h->address);
    case CWUInt32:
        return cwfmt_printf(c, "%llu",
                            (unsigned long long)*(const uint32_t*)(uintptr_t)h->address);
    case CWInt64:
        return cwfmt_printf(c, "%lld",
                            (long long)*(const int64_t*)(uintptr_t)h->address);
    case CWUInt64:
        return cwfmt_printf(c, "%llu",
                            (unsigned long long)*(const uint64_t*)(uintptr_t)h->address);
    case CWFloat:
        return cwfmt_printf(c, "%g", *(const float*)(uintptr_t)h->address);
    case CWFloat64:
        return cwfmt_printf(c, "%g", *(const double*)(uintptr_t)h->address);
    case CWBool:
        return cwfmt_push(c, *(const bool*)(uintptr_t)h->address
                              ? "true" : "false");
    case CWString:
        return h->address
            ? cwfmt_bytes(c, (const char*)(uintptr_t)h->address,
                          (size_t)h->length)
            : cwfmt_push(c, "");
    case CWNone:
        return cwfmt_push(c, "None");
    case CWVector:
        return cwfmt_container(c, obj, "[", "]");
    case CWTuple:
        return cwfmt_container(c, obj, "(", ")");
    case CWMap:
        return cwfmt_container(c, obj, "{", "}");
    case CWSet:
        return cwfmt_container(c, obj, "{", "}");
    default:
        return cwfmt_push(c, "?");
    }
}

bool cwobj_format(const CWindObject_t* obj, char* buf, size_t cap) {
    if (!buf || cap == 0) return false;
    buf[0] = '\0';
    if (!obj) return false;
    CwFmtCtx_t c = { buf, cap, 0, 0 };
    return cwfmt_record(&c, obj);
}

bool cw_builtin_print_to(FILE* f, const CWindObject_t* obj) {
    if (!f || !obj) return false;
    if (obj->type_id == CWString) {
        const CWObjHandle_t* h = cwbuiltin_handle(obj);
        if (fwrite((const char*)(uintptr_t)h->address, 1, (size_t)h->length,
                   f) != h->length) {
            return false;
        }
        return fputc('\n', f) != EOF;
    }
    char buf[4096];
    if (!cwobj_format(obj, buf, sizeof(buf))) return false;
    return fputs(buf, f) != EOF && fputc('\n', f) != EOF;
}

bool cw_builtin_print(const CWindObject_t* obj) {
    return cw_builtin_print_to(stdout, obj);
}

bool cw_builtin_type_of(const CWindObject_t* obj, char* buf, size_t cap) {
    if (!obj || !buf || cap == 0) return false;
    const char* name = cwobj_type_name(obj->type_id);
    const size_t n = strlen(name);
    if (n + 1 > cap) return false;
    memcpy(buf, name, n + 1);
    return true;
}

bool cw_builtin_length(const CWindObject_t* obj, uint64_t* out) {
    if (!obj || !out) return false;
    const CWObjHandle_t* h = cwbuiltin_handle(obj);
    switch (obj->type_id) {
    case CWString:
        *out = h->length;
        return true;
    case CWVector:
        *out = (uint64_t)cwvec_size((const CWindVectorObject_t*)obj);
        return true;
    case CWMap:
        *out = (uint64_t)cwmap_size((const CWindMapObject_t*)obj);
        return true;
    case CWSet:
        *out = (uint64_t)cwset_size((const CWindSetObject_t*)obj);
        return true;
    case CWTuple:
        *out = (uint64_t)cwtuple_size((const CWindTupleObject_t*)obj);
        return true;
    default:
        return false;
    }
}

static bool cwbuiltin_string_contains(const CWindStringObject_t* haystack,
                                      const CWindStringObject_t* needle,
                                      bool* out) {
    const char* hd = NULL;
    const char* nd = NULL;
    uint64_t hl = 0, nl = 0;
    if (!cwobj_string_get(haystack, &hd, &hl)
        || !cwobj_string_get(needle, &nd, &nl)) {
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

bool cw_builtin_contains(const CWindObject_t* container,
                         const CWindObject_t* item, bool* out) {
    if (!container || !item || !out) return false;
    switch (container->type_id) {
    case CWString:
        return cwbuiltin_string_contains(
            (const CWindStringObject_t*)container,
            (const CWindStringObject_t*)item, out);
    case CWVector: {
        CWindVectorIter_t it;
        cwvec_iter_begin((const CWindVectorObject_t*)container, &it);
        while (cwvec_iter_valid(&it)) {
            CWindIntObject_t r;
            if (cwvec_iter_value(&it, &r)
                && cwobj_equal(&r.head, item)) {
                *out = true;
                return true;
            }
            cwvec_iter_next(&it);
        }
        *out = false;
        return true;
    }
    case CWSet:
        *out = cwset_contains((const CWindSetObject_t*)container, item);
        return true;
    case CWMap:
        *out = cwmap_get((const CWindMapObject_t*)container, item, NULL);
        return true;
    default:
        return false;
    }
}

bool cw_builtin_to_string(const CWindObject_t* obj, char* buf, size_t cap) {
    return cwobj_format(obj, buf, cap);
}

/* 目标数值宽度 (字节); 不支持的类型返回 false */
static bool cwbuiltin_parse_width(int32_t type_id, size_t* width) {
    switch (type_id) {
    case CWInt8:
    case CWUInt8:
    case CWByte:
        *width = 1;
        return true;
    case CWInt:
    case CWUInt:
        *width = 2;
        return true;
    case CWInt32:
    case CWUInt32:
    case CWFloat:
        *width = 4;
        return true;
    case CWInt64:
    case CWUInt64:
    case CWFloat64:
        *width = 8;
        return true;
    default:
        return false;
    }
}

bool cw_builtin_parse_owned(const CWindObject_t* src,
                            int32_t target_type_id,
                            CWindObject_t* out) {
    if (!src || !out || !cwobj_type_is(src, CWString)) return false;
    size_t width = 0;
    if (!cwbuiltin_parse_width(target_type_id, &width)) return false;

    const CWObjHandle_t* h = cwbuiltin_handle(src);
    const char* text = (const char*)(uintptr_t)h->address;
    const size_t len = (size_t)h->length;
    char buf[128];
    if (len >= sizeof(buf)) return false;
    if (len > 0) memcpy(buf, text, len);
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
    case CWInt32:
    case CWInt64:
        iv = strtoll(buf, &end, 10);
        if (end == buf || *end != '\0' || errno == ERANGE) ok = false;
        break;
    case CWUInt:
    case CWUInt8:
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
    case CWUInt8:  *(uint8_t*)cell = ok ? (uint8_t)uv : 0; break;
    case CWByte:   *(uint8_t*)cell = ok ? (uint8_t)uv : 0; break;
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

    cwobj_init(out, (CWindBaseType_t)target_type_id);
    CWObjHandle_t* ho = cwbuiltin_handle_mut(out);
    ho->object  = out;
    ho->address = (uint64_t)(uintptr_t)cell;
    ho->length  = (uint64_t)width;
    ho->cursor  = 0;
    return ok;
}

bool cw_builtin_to_string_owned(const CWindObject_t* obj,
                                CWindObject_t* out) {
    char buf[4096];
    if (!cwobj_format(obj, buf, sizeof(buf))) return false;
    return cwstr_owned_init(out, buf, strlen(buf));
}

bool cw_builtin_concat(const CWindObject_t* a, const CWindObject_t* b,
                       CWindObject_t* out) {
    if (!a || !b || !out) return false;
    if (!cwobj_type_is(a, CWString) || !cwobj_type_is(b, CWString)) {
        return false;
    }
    const CWObjHandle_t* ha = cwbuiltin_handle(a);
    const CWObjHandle_t* hb = cwbuiltin_handle(b);
    const char* sa = (const char*)(uintptr_t)ha->address;
    const char* sb = (const char*)(uintptr_t)hb->address;
    const size_t la = (size_t)ha->length;
    const size_t lb = (size_t)hb->length;
    if ((la > 0 && !sa) || (lb > 0 && !sb)) return false;
    if (SIZE_MAX - la < lb) return false; /* 长度溢出 */
    char* buf = (char*)cwrt_arena_alloc(la + lb);
    if (!buf) return false;
    if (la > 0) memcpy(buf, sa, la);
    if (lb > 0) memcpy(buf + la, sb, lb);
    buf[la + lb] = '\0';
    cwobj_init(out, CWString);
    CWObjHandle_t* ho = cwbuiltin_handle_mut(out);
    ho->object  = out;
    ho->address = (uint64_t)(uintptr_t)buf;
    ho->length  = (uint64_t)(la + lb);
    ho->cursor  = 0;
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

/* 把对象格式化进动态缓冲区: 先试小栈缓冲, 不够再堆上翻倍 */
/* 翻倍上限 64 MiB: MSVCRT 的 vsnprintf 对超大 size 参数会破坏堆 (实测
 * 4 GiB 即崩), 且 v0 也不该为一次格式化无界分配; 超限视为失败。 */
#define CWBUILTIN_FMT_MAX_DYN (64u * 1024u * 1024u)
static bool cwfmt_dyn_append_obj(CwFmtDynBuf_t* b,
                                 const CWindObject_t* obj) {
    char stack[256];
    size_t cap = sizeof(stack);
    char* buf = stack;
    for (;;) {
        if (cwobj_format(obj, buf, cap)) {
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

bool cw_builtin_format(const CWindObject_t* self,
                       const CWindObject_t* const* args, size_t nargs,
                       CWindObject_t* out) {
    if (!self || !cwobj_type_is(self, CWString) || !out) return false;
    const CWObjHandle_t* h = cwbuiltin_handle(self);
    const char* s = (const char*)(uintptr_t)h->address;
    const size_t n = (size_t)h->length;
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
                ok = cwfmt_dyn_append_obj(&buf, args[ai]);
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

bool cw_builtin_type_of_owned(const CWindObject_t* obj,
                              CWindObject_t* out) {
    if (!obj || !out) return false;
    char tmp[64];
    if (!cw_builtin_type_of(obj, tmp, sizeof(tmp))) return false;
    return cwstr_owned_init(out, tmp, strlen(tmp));
}

bool cw_builtin_readline(CWindObject_t* out) {
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
    if (!got_line && len == 0) return false; /* 无任何输入的 EOF */
    const bool ok = cwstr_owned_init(out, tmp, len);
    free(tmp);
    return ok;
}

_Noreturn void cw_builtin_exit(int code) {
    exit(code);
}
