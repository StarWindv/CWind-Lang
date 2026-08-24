//
// Created by 星灿长风v on 2026/8/2.
//

#ifndef GET_BASE_VALUES_H
    #define GET_BASE_VALUES_H

    #include <stdint.h>
    #include "../object/cwind_object.h"
    #include <stdbool.h>

    static int16_t builtin_get_i16(const CWindIntObject_t *obj) {
        return *(int16_t *)obj->handle.address;
    }

    static uint16_t builtin_get_u16(const CWindUIntObject_t *obj) {
        return *(uint16_t *)obj->handle.address;
    }

    static int8_t builtin_get_i8(const CWindInt8Object_t *obj) {
        return *(int8_t *)obj->handle.address;
    }

    static uint8_t builtin_get_u8(const CWindUInt8Object_t *obj) {
        return *(uint8_t *)obj->handle.address;
    }

    static int16_t builtin_get_int16(const CWindInt16Object_t *obj) {
        return *(int16_t *)obj->handle.address;
    }

    static uint16_t builtin_get_uint16(const CWindUInt16Object_t *obj) {
        return *(uint16_t *)obj->handle.address;
    }

    static float builtin_get_f32(const CWindFloatObject_t *obj) {
        return *(float *)obj->handle.address;
    }

    static char* builtin_get_string(const CWindStringObject_t *obj) {
        return (char *)obj->handle.address;
    }

    static bool builtin_get_bool(const CWindBoolObject_t *obj) {
        return *(bool * )obj->handle.address;
    }

    static char builtin_get_byte(const CWindByteObject_t *obj) {
        return *(char * )obj->handle.address;
    }

#endif // GET_BASE_VALUES_H
