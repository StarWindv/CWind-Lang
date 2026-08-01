//
// Created by 星灿长风v on 2026/7/31.
//

#ifndef CWIND_CWIND_OBJ_HANDLE_H
    #define CWIND_CWIND_OBJ_HANDLE_H

    #include <stdint.h>
    #include "./cwind_object.h"

    typedef struct CWObjHandle {
        uint64_t address;
        CWindObject_t *object;
    } CWObjHandle_t;

#endif //CWIND_CWIND_OBJ_HANDLE_H
