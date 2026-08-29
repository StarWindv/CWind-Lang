/**
* Copyright (C) 2026 StarWindv
 * License: BSD-3.0
 * Author : StarWindv
 * Location: rt-src/include/object/cwind_obj_forward.h
 */

#ifndef CWIND_OBJ_FORWARD_H
    #define CWIND_OBJ_FORWARD_H

/*
 * ABI v2 值模型 (todo-50: 拆胖对象, 元数据分区存放):
 *  - CWValue_t 是唯一的值表示: 24B 纯数据, 无类型头、无自指元数据;
 *  - CWCell_t 是异构边界单元: 4B 类型 tag + 24B 值 (帧变量表 / rt 入口);
 *  - 类型元数据分区: 调用点静态 tag / 容器 data 头 / memcenter 槽头,
 *    不再内联进值本身 (原 8B 头 + 32B 自指句柄的胖记录已删除)。
 */

    #include <stdint.h>

    struct CWValue;
    struct CWCell;

    typedef struct CWValue CWValue_t;
    typedef struct CWCell  CWCell_t;

#endif //CWIND_OBJ_FORWARD_H
