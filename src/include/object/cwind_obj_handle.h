//
// Created by 星灿长风v on 2026/7/31.
//

#ifndef CWIND_CWIND_OBJ_HANDLE_H
    #define CWIND_CWIND_OBJ_HANDLE_H

    #include <stdint.h>
    #include "./cwind_object.h"

    typedef struct CWObjHandle {
        CWindObject_t *object;
        uint64_t address; // 地址指向数据存储链表或外部堆 / 栈对象此处为栈上内存
        uint64_t length; // 地址持续长度
        uint64_t cursor; // 当前相对于自身数据地址起始处的 offset
    } CWObjHandle_t;

#endif //CWIND_CWIND_OBJ_HANDLE_H
