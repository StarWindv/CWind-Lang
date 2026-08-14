/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_builtin.c
 */

#include "../include/rt/cwind_builtin.h"

#include "../include/object/cwind_container.h"

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

/* ---- v0 字符串 arena ----
 * 拼接结果统一分配到这里, 进程期存活, 直到进程退出归还 OS;
 * GC 页 (cwind_mempage/wal) 落地后由 GC 分配取代本机制。
 */
typedef struct CwStrArena {
    char* base;
    size_t used;
    size_t cap;
} CwStrArena_t;

static CwStrArena_t g_str_arena = { NULL, 0, 0 };

static char* cwstr_arena_alloc(size_t size) {
    const size_t need = size + 1;
    if (need < size) return NULL; /* 溢出 */
    if (g_str_arena.cap - g_str_arena.used < need) {
        size_t ncap = g_str_arena.cap ? g_str_arena.cap : 64;
        while (ncap - g_str_arena.used < need) {
            if (ncap > SIZE_MAX / 2) {
                ncap = g_str_arena.used + need;
                break;
            }
            ncap *= 2;
        }
        char* nb = (char*)realloc(g_str_arena.base, ncap);
        if (!nb) return NULL;
        g_str_arena.base = nb;
        g_str_arena.cap = ncap;
    }
    char* p = g_str_arena.base + g_str_arena.used;
    g_str_arena.used += need;
    return p;
}

/* 把一段字节拷进 arena, 写成 String 记录 (owned) */
static bool cwstr_owned_init(CWindObject_t* out, const char* data,
                             size_t len) {
    if (!out || (len > 0 && !data)) return false;
    char* buf = cwstr_arena_alloc(len);
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
    char* buf = cwstr_arena_alloc(la + lb);
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
