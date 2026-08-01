#ifndef CWIND_OBJECT_H
    #define CWIND_OBJECT_H
    
    #include <stdint.h>
    #include "./cwind_type.h"

    typedef struct CWindObject {
        CWindBaseType_t type_id;
        uint8_t  gc_cnt; // 栈对象此处为 0
        uint64_t target; // 句柄指向数据存储链表或外部堆 / 栈对象此处为栈上内存
        uint64_t length; // 地址持续长度
        uint64_t cursor; // 当前相对于自身数据地址起始处的 offset
    } CWindObject_t;
    /**
     * 8 * 3 + 1 + 1 = 26
     * 32 - 26 = 6
     * 啧, 不知道做点什么了, 总之先空着吧
     */

#endif
