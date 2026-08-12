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
    case CWFloat:
        return cwfmt_printf(c, "%g", *(const float*)(uintptr_t)h->address);
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

_Noreturn void cw_builtin_exit(int code) {
    exit(code);
}
