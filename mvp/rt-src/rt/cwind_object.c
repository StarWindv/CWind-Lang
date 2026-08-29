/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_object.c
 */

#include "../include/object/cwind_object.h"

#include <string.h>

/*
 * ABI v2 值模型 (todo-50: 拆胖对象, 元数据分区存放):
 *  - 值 = 24B CWValue_t 纯数据, 不携带类型;
 *  - 标量值存放在 storage (调用方提供), value.address 记录其地址;
 *  - 字符串是胖指针: address -> 字节流, length = 字节数, 保证 NUL 结尾;
 *  - 类型元数据在值外: CWCell tag / 容器 data 头 / 调用点静态 tag。
 */

_Static_assert(sizeof(CWValue_t) == CWIND_VALUE_SIZE,
               "CWValue_t must be 24 bytes (ABI v2)");
_Static_assert(sizeof(CWCell_t) == CWIND_CELL_SIZE,
               "CWCell_t must be 32 bytes (ABI v2)");
_Static_assert(offsetof(CWCell_t, value) == 8,
               "ABI: cell.value offset must be 8");

const char* cwobj_type_name(int32_t type) {
    switch (type) {
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

size_t cwobj_scalar_width(int32_t type) {
    switch (type) {
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

bool cwobj_value_equal(int32_t type_id,
                       const CWValue_t* a, const CWValue_t* b) {
    if (a == b) return true;
    if (!a || !b) return false;
    if (a->address == 0 && b->address == 0) return true;

    const size_t w = cwobj_scalar_width(type_id);
    if (w > 0) {
        return a->address && b->address
            && memcmp((const void*)(uintptr_t)a->address,
                      (const void*)(uintptr_t)b->address, w) == 0;
    }
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

    const unsigned char* p = NULL;
    size_t n = 0;
    const size_t w = cwobj_scalar_width(type_id);
    if (w > 0) {
        p = (const unsigned char*)(uintptr_t)v->address;
        n = w;
    } else {
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
    }
    for (size_t i = 0; i < n; i++) {
        hash ^= p[i];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}
