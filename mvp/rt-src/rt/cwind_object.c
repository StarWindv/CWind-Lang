/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_object.c
 */

#include "../include/object/cwind_object.h"

#include <string.h>
#include <stddef.h>

/*
 * ABI v3 值模型 (todo-209: ≤8B 标量内联, 承接 todo-50 元数据分区):
 *  - 值 = 24B CWValue_t 纯数据, 不携带类型;
 *  - 标量/函数指针: address = 低 width 字节位模式 (高位零扩展),
 *    length = 宽度标记 1/2/4/8;
 *  - 字符串是胖指针: address -> 字节流, length = 字节数, 保证 NUL 结尾;
 *  - None/null: 全 0 (length 0 = 无值);
 *  - 类型元数据在值外: CWCell tag / 容器 data 头 / 调用点静态 tag。
 */

_Static_assert(sizeof(CWValue_t) == CWIND_VALUE_SIZE,
               "CWValue_t must be 24 bytes (ABI v3)");
_Static_assert(sizeof(CWCell_t) == CWIND_CELL_SIZE,
               "CWCell_t must be 32 bytes (ABI v3)");
_Static_assert(offsetof(CWCell_t, value) == 8,
               "ABI: cell.value offset must be 8");

const char* cwobj_type_name(CWindBaseType_t type_id) {
    switch (type_id) {
    case CWInt:     return "Int";
    case CWUInt:    return "UInt";
    case CWFloat:   return "Float";
    case CWBool:    return "Bool";
    case CWByte:    return "Byte";
    case CWString:  return "String";
    case CWNone:    return "None";
    case CWTuple:   return "Tuple";
    case CWVector:  return "Vector";
    case CWMap:     return "Map";
    case CWSet:     return "Set";
    case CWInt8:    return "Int8";
    case CWUInt8:   return "UInt8";
    case CWInt16:   return "Int16";
    case CWUInt16:  return "UInt16";
    case CWInt32:   return "Int32";
    case CWUInt32:  return "UInt32";
    case CWInt64:   return "Int64";
    case CWUInt64:  return "UInt64";
    case CWFloat64: return "Float64";
    default:        return "Invalid";
    }
}

size_t cwobj_scalar_width(CWindBaseType_t type_id) {
    switch (type_id) {
    case CWInt8:
    case CWUInt8:
    case CWByte:
    case CWBool:
        return 1;
    case CWInt:
    case CWUInt:
    case CWInt16:
    case CWUInt16:
        return 2;
    case CWInt32:
    case CWUInt32:
    case CWFloat:
        return 4;
    case CWInt64:
    case CWUInt64:
    case CWFloat64:
        return 8;
    default:
        return 0;
    }
}

bool cwobj_string_view(const CWValue_t* v,
                       const char** data, uint64_t* len) {
    if (!v || !data || !len) return false;
    if (v->address == 0) {
        *data = NULL;
        *len = 0;
        return true; /* 空串是合法字符串值 */
    }
    *data = (const char*)(uintptr_t)v->address;
    *len  = v->length;
    return true;
}

void cwval_wrap(CWValue_t* out, const void* storage, uint64_t length) {
    if (!out) return;
    out->address = (uint64_t)(uintptr_t)storage;
    out->length  = length;
    out->cursor  = 0;
}

void cwval_none(CWValue_t* out) {
    if (!out) return;
    out->address = 0;
    out->length  = 0;
    out->cursor  = 0;
}

/* ---- ABI v3 标量内联 ---- */

void cwval_scalar(CWValue_t* out, uint64_t bits, uint64_t width) {
    if (!out) return;
    if (width > sizeof(uint64_t)) width = sizeof(uint64_t);
    const uint64_t mask = (width >= sizeof(uint64_t))
        ? UINT64_MAX
        : ((UINT64_C(1) << (width * 8)) - 1);
    out->address = bits & mask;
    out->length  = width;
    out->cursor  = 0;
}

void cwval_scalar_mem(CWValue_t* out, const void* storage, uint64_t width) {
    uint64_t bits = 0;
    if (storage && width > 0 && width <= sizeof(bits)) {
        memcpy(&bits, storage, (size_t)width);
    }
    cwval_scalar(out, bits, width);
}

uint64_t cwval_scalar_bits(const CWValue_t* v) {
    return v ? v->address : 0;
}

uint64_t cwval_scalar_len(const CWValue_t* v) {
    return v ? v->length : 0;
}

float cwval_f32(const CWValue_t* v) {
    const uint32_t bits = (uint32_t)cwval_scalar_bits(v);
    float f = 0.0f;
    memcpy(&f, &bits, sizeof(f));
    return f;
}

double cwval_f64(const CWValue_t* v) {
    const uint64_t bits = cwval_scalar_bits(v);
    double d = 0.0;
    memcpy(&d, &bits, sizeof(d));
    return d;
}

bool cwobj_value_equal(int32_t type_id,
                       const CWValue_t* a, const CWValue_t* b) {
    if (a == b) return true;
    if (!a || !b) return false;

    const size_t w = cwobj_scalar_width(type_id);
    if (w > 0) {
        /* 标量: 宽度标记必须匹配 (0 = 缺值/None, 与标量 0 区分),
         * 相等 = address 低 width 字节相同 */
        if (a->length != (uint64_t)w || b->length != (uint64_t)w) {
            return a->length == 0 && b->length == 0;
        }
        const uint64_t mask = (w >= 8)
            ? UINT64_MAX : ((UINT64_C(1) << (w * 8)) - 1);
        return ((a->address ^ b->address) & mask) == 0;
    }

    if (a->address == 0 && b->address == 0) return true;
    switch (type_id) {
    case CWString:
        return a->length == b->length
            && (a->length == 0
                || memcmp((const void*)(uintptr_t)a->address,
                          (const void*)(uintptr_t)b->address,
                          (size_t)a->length) == 0);
    case CWNone:
        return true;
    default:
        /* 容器/未知: 按 data 地址身份比较 (同一容器实例) */
        return a->address == b->address;
    }
}

uint64_t cwobj_value_hash(int32_t type_id, const CWValue_t* v) {
    if (!v) return 0;

    uint64_t hash = UINT64_C(14695981039346656037);
    const unsigned char type_byte = (unsigned char)type_id;
    hash ^= type_byte;
    hash *= UINT64_C(1099511628211);

    const size_t w = cwobj_scalar_width(type_id);
    if (w > 0) {
        /* 标量: 哈希内联位模式低 width 字节 (小端字节序) */
        uint64_t bits = v->address;
        for (size_t i = 0; i < w; i++) {
            hash ^= (unsigned char)(bits & 0xFFu);
            hash *= UINT64_C(1099511628211);
            bits >>= 8;
        }
        return hash;
    }

    const unsigned char* p = NULL;
    size_t n = 0;
    switch (type_id) {
    case CWString:
        p = (const unsigned char*)(uintptr_t)v->address;
        n = (size_t)v->length;
        break;
    case CWNone:
        return hash;
    default:
        /* 容器: 身份哈希, 直接哈希 data 地址值本身 */
        p = (const unsigned char*)&v->address;
        n = sizeof(v->address);
        break;
    }
    for (size_t i = 0; i < n; i++) {
        hash ^= p[i];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}
