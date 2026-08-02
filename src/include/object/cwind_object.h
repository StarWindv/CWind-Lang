#ifndef CWIND_OBJECT_H
    #define CWIND_OBJECT_H
    
    #include <stdint.h>
    #include "./cwind_type.h"
    #include "./cwind_obj_handle.h"

    typedef struct CWindObject {
        CWindBaseType_t type_id;
        CWObjHandle_t handle;
        uint8_t  gc_cnt; // 栈对象此处为 0
    } CWindObject_t;
    /**
     * 8 * 3 + 1 + 1 = 26
     * 32 - 26 = 6
     * 啧, 不知道做点什么了, 总之先空着吧
     */

#endif
