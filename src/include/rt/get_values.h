//
// Created by 星灿长风v on 2026/8/2.
//

#ifndef CWIND_GET_VALUES_H
    #define CWIND_GET_VALUES_H

    #include <stdint.h>
    #include "../object/cwind_object.h"

    static int16_t _builtin_get_i16(const CWindObject_t *obj) {
        return *(int16_t *)obj->handle.address;
    }

    static uint16_t _builtin_get_u16(const CWindObject_t *obj) {
        return *(uint16_t *)obj->handle.address;
    }

    static int8_t _builtin_get_i8(const CWindObject_t *obj) {
        return *(int8_t *)obj->handle.address;
    }

    static uint8_t _builtin_get_u8(const CWindObject_t *obj) {
        return *(uint8_t *)obj->handle.address;
    }

    static float _builtin_get_f32(const CWindObject_t *obj) {
        return *(float *)obj->handle.address;
    }

    static char* _builtin_get_string(const CWindObject_t *obj) {
        return (char *)obj->handle.address;
    }

#endif //CWIND_GET_VALUES_H
