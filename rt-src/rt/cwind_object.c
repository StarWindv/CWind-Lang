/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_object.c
 */

#include "../include/object/cwind_object.h"

#include <string.h>

/*
 * 布局约定:
 *  - 对象记录 (如 CWindIntObject_t) = 公共头 + 句柄, 所有子类型等大;
 *  - 标量值存放在 storage (帧值栈 / 调用方提供), 句柄.address 记录其地址;
 *  - 字符串是胖指针: address -> 字节流, length = 字节数, 且保证 NUL 结尾;
 *  - 容器对象只建头, 句柄清零, 等容器组件挂载。
 */

_Static_assert(sizeof(CWindObject_t) == 8,
               "CWindObject_t 应为 8 字节 (type_id + gc_cnt + padding)");
_Static_assert(sizeof(CWObjHandle_t) == 32,
               "CWObjHandle_t 应为 32 字节 (ABI)");
_Static_assert(sizeof(CWindIntObject_t) == sizeof(CWindUIntObject_t)
            && sizeof(CWindIntObject_t) == sizeof(CWindFloatObject_t)
            && sizeof(CWindIntObject_t) == sizeof(CWindInt8Object_t)
            && sizeof(CWindIntObject_t) == sizeof(CWindUInt8Object_t)
            && sizeof(CWindIntObject_t) == sizeof(CWindBoolObject_t)
            && sizeof(CWindIntObject_t) == sizeof(CWindByteObject_t)
            && sizeof(CWindIntObject_t) == sizeof(CWindStringObject_t)
            && sizeof(CWindIntObject_t) == sizeof(CWindNoneObject_t)
            && sizeof(CWindIntObject_t) == sizeof(CWindTupleObject_t)
            && sizeof(CWindIntObject_t) == sizeof(CWindVectorObject_t)
            && sizeof(CWindIntObject_t) == sizeof(CWindSetObject_t)
            && sizeof(CWindIntObject_t) == sizeof(CWindMapObject_t),
               "所有 CWind 对象记录必须等大, 才能放进统一变量表");

static CWObjHandle_t* cwobj_handle_of(CWindObject_t* obj) {
    return (CWObjHandle_t*)((char*)obj + sizeof(CWindObject_t));
}

static const CWObjHandle_t* cwobj_handle_of_c(const CWindObject_t* obj) {
    return (const CWObjHandle_t*)((const char*)obj + sizeof(CWindObject_t));
}

static void cwobj_handle_reset(CWindObject_t* obj) {
    CWObjHandle_t* h = cwobj_handle_of(obj);
    h->object  = obj;
    h->address = 0;
    h->length  = 0;
    h->cursor  = 0;
}

void cwobj_init(CWindObject_t* obj, CWindBaseType_t type) {
    if (!obj) return;
    obj->type_id = type;
    obj->gc_cnt  = 0;
}

bool cwobj_type_is(const CWindObject_t* obj, CWindBaseType_t type) {
    return obj != NULL && obj->type_id == type;
}

const char* cwobj_type_name(CWindBaseType_t type) {
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
    default:        return "Invalid";
    }
}

static void cwobj_scalar_new(CWindObject_t* obj,
                             CWindBaseType_t type,
                             void* storage,
                             uint64_t value_size,
                             const void* value) {
    cwobj_init(obj, type);
    cwobj_handle_reset(obj);
    CWObjHandle_t* h = cwobj_handle_of(obj);
    h->address = (uint64_t)(uintptr_t)storage;
    h->length  = value_size;
    if (storage && value) {
        memcpy(storage, value, (size_t)value_size);
    }
}

CWindIntObject_t* cwobj_int_new(CWindIntObject_t* obj,
                                void* storage, int16_t value) {
    if (!obj || !storage) return NULL;
    cwobj_scalar_new(&obj->head, CWInt, storage, sizeof(value), &value);
    return obj;
}

CWindUIntObject_t* cwobj_uint_new(CWindUIntObject_t* obj,
                                  void* storage, uint16_t value) {
    if (!obj || !storage) return NULL;
    cwobj_scalar_new(&obj->head, CWUInt, storage, sizeof(value), &value);
    return obj;
}

CWindInt8Object_t* cwobj_int8_new(CWindInt8Object_t* obj,
                                  void* storage, int8_t value) {
    if (!obj || !storage) return NULL;
    cwobj_scalar_new(&obj->head, CWInt8, storage, sizeof(value), &value);
    return obj;
}

CWindUInt8Object_t* cwobj_uint8_new(CWindUInt8Object_t* obj,
                                    void* storage, uint8_t value) {
    if (!obj || !storage) return NULL;
    cwobj_scalar_new(&obj->head, CWUInt8, storage, sizeof(value), &value);
    return obj;
}

CWindFloatObject_t* cwobj_float_new(CWindFloatObject_t* obj,
                                    void* storage, float value) {
    if (!obj || !storage) return NULL;
    cwobj_scalar_new(&obj->head, CWFloat, storage, sizeof(value), &value);
    return obj;
}

CWindBoolObject_t* cwobj_bool_new(CWindBoolObject_t* obj,
                                  void* storage, bool value) {
    if (!obj || !storage) return NULL;
    cwobj_scalar_new(&obj->head, CWBool, storage, sizeof(value), &value);
    return obj;
}

CWindByteObject_t* cwobj_byte_new(CWindByteObject_t* obj,
                                  void* storage, uint8_t value) {
    if (!obj || !storage) return NULL;
    cwobj_scalar_new(&obj->head, CWByte, storage, sizeof(value), &value);
    return obj;
}

CWindNoneObject_t* cwobj_none_new(CWindNoneObject_t* obj) {
    if (!obj) return NULL;
    cwobj_init(&obj->head, CWNone);
    cwobj_handle_reset(&obj->head);
    return obj;
}

CWindStringObject_t* cwobj_string_new(CWindStringObject_t* obj,
                                      char* storage,
                                      const char* data, uint64_t len) {
    if (!obj || !storage || (len > 0 && !data)) return NULL;
    cwobj_init(&obj->head, CWString);
    cwobj_handle_reset(&obj->head);
    CWObjHandle_t* h = cwobj_handle_of(&obj->head);
    h->address = (uint64_t)(uintptr_t)storage;
    h->length  = len;
    if (len > 0) memcpy(storage, data, (size_t)len);
    storage[len] = '\0';
    return obj;
}

#define CWOBJ_DEFINE_ACCESSORS(SUFFIX, STRUCT, ENUM, CTYPE)                  \
    bool cwobj_get_##SUFFIX(const CWind##STRUCT##Object_t* obj, CTYPE* out) {\
        if (!obj || !out || !cwobj_type_is(&obj->head, ENUM)) return false;   \
        const CWObjHandle_t* h = cwobj_handle_of_c(&obj->head);             \
        if (h->address == 0) return false;                                   \
        *out = *(CTYPE*)(uintptr_t)h->address;                               \
        return true;                                                         \
    }                                                                        \
    bool cwobj_set_##SUFFIX(CWind##STRUCT##Object_t* obj, CTYPE value) {     \
        if (!obj || !cwobj_type_is(&obj->head, ENUM)) return false;           \
        CWObjHandle_t* h = cwobj_handle_of(&obj->head);                      \
        if (h->address == 0) return false;                                   \
        *(CTYPE*)(uintptr_t)h->address = value;                              \
        return true;                                                         \
    }

CWOBJ_DEFINE_ACCESSORS(i16,    Int,    CWInt,   int16_t)
CWOBJ_DEFINE_ACCESSORS(uint16, UInt,   CWUInt,  uint16_t)
CWOBJ_DEFINE_ACCESSORS(int8,   Int8,   CWInt8,  int8_t)
CWOBJ_DEFINE_ACCESSORS(uint8,  UInt8,  CWUInt8, uint8_t)
CWOBJ_DEFINE_ACCESSORS(float,  Float,  CWFloat, float)
CWOBJ_DEFINE_ACCESSORS(bool,   Bool,   CWBool,  bool)
CWOBJ_DEFINE_ACCESSORS(byte,   Byte,   CWByte,  uint8_t)

#undef CWOBJ_DEFINE_ACCESSORS

bool cwobj_string_get(const CWindStringObject_t* obj,
                      const char** data, uint64_t* len) {
    if (!obj || !data || !len || !cwobj_type_is(&obj->head, CWString)) {
        return false;
    }
    const CWObjHandle_t* h = cwobj_handle_of_c(&obj->head);
    if (h->address == 0) return false;
    *data = (const char*)(uintptr_t)h->address;
    *len  = h->length;
    return true;
}

bool cwobj_string_set(CWindStringObject_t* obj,
                      const char* data, uint64_t len) {
    if (!obj || !cwobj_type_is(&obj->head, CWString)) return false;
    CWObjHandle_t* h = cwobj_handle_of(&obj->head);
    if (h->address == 0 || (len > 0 && !data)) return false;
    char* storage = (char*)(uintptr_t)h->address;
    if (len > 0) memcpy(storage, data, (size_t)len);
    storage[len] = '\0';
    h->length = len;
    return true;
}

void cwobj_container_init(CWindObject_t* obj, CWindBaseType_t type) {
    if (!obj) return;
    switch (type) {
    case CWTuple:
    case CWVector:
    case CWMap:
    case CWSet:
        cwobj_init(obj, type);
        cwobj_handle_reset(obj);
        break;
    default:
        break;
    }
}

bool cwobj_equal(const CWindObject_t* a, const CWindObject_t* b) {
    if (a == b) return true;
    if (!a || !b || a->type_id != b->type_id) return false;

    const CWObjHandle_t* ha = cwobj_handle_of_c(a);
    const CWObjHandle_t* hb = cwobj_handle_of_c(b);

    switch (a->type_id) {
    case CWInt:
        return ha->address && hb->address
            && *(const int16_t*)(uintptr_t)ha->address
            == *(const int16_t*)(uintptr_t)hb->address;
    case CWUInt:
        return ha->address && hb->address
            && *(const uint16_t*)(uintptr_t)ha->address
            == *(const uint16_t*)(uintptr_t)hb->address;
    case CWInt8:
        return ha->address && hb->address
            && *(const int8_t*)(uintptr_t)ha->address
            == *(const int8_t*)(uintptr_t)hb->address;
    case CWUInt8:
    case CWByte:
        return ha->address && hb->address
            && *(const uint8_t*)(uintptr_t)ha->address
            == *(const uint8_t*)(uintptr_t)hb->address;
    case CWFloat:
        return ha->address && hb->address
            && *(const float*)(uintptr_t)ha->address
            == *(const float*)(uintptr_t)hb->address;
    case CWBool:
        return ha->address && hb->address
            && *(const bool*)(uintptr_t)ha->address
            == *(const bool*)(uintptr_t)hb->address;
    case CWString:
        return ha->length == hb->length
            && (ha->length == 0
                || memcmp((const void*)(uintptr_t)ha->address,
                          (const void*)(uintptr_t)hb->address,
                          (size_t)ha->length) == 0);
    case CWNone:
        return true;
    default:
        /* 容器对象: v0 按对象身份比较 (同一容器实例) */
        return ha->address == hb->address;
    }
}

uint64_t cwobj_hash(const CWindObject_t* obj) {
    if (!obj) return 0;
    const CWObjHandle_t* h = cwobj_handle_of_c(obj);
    const unsigned char* p =
        (const unsigned char*)(uintptr_t)h->address;

    uint64_t hash = UINT64_C(14695981039346656037);
    const unsigned char type_byte = (unsigned char)obj->type_id;
    hash ^= type_byte;
    hash *= UINT64_C(1099511628211);

    size_t n = 0;
    switch (obj->type_id) {
    case CWInt:    n = sizeof(int16_t); break;
    case CWUInt:   n = sizeof(uint16_t); break;
    case CWInt8:   n = sizeof(int8_t); break;
    case CWUInt8:
    case CWByte : {
            n = sizeof(uint8_t);
            break;
        }
    case CWFloat:  n = sizeof(float); break;
    case CWBool:   n = sizeof(bool); break;
    case CWString: n = (size_t)h->length; break;
    case CWNone:   return hash;
    default:
        /* 容器: 身份哈希, 直接哈希地址值本身 */
        p = (const unsigned char*)&h->address;
        n = sizeof(h->address);
        break;
    }
    if (p) {
        for (size_t i = 0; i < n; i++) {
            hash ^= p[i];
            hash *= UINT64_C(1099511628211);
        }
    }
    return hash;
}
