/**
 * Copyright (C) 2026 StarWindv
 * License: BSD-3.0
 * Author : StarWindv
 * Location: rt-src/include/rt/cwind_abi.h
 */

/**
 * CWind ABI v1 (草案)
 *
 * 本文固化运行时值表示与内存约定, 是后端编译器 (LLVM) 与 rt 之间的契约:
 *  - 所有运行时值统一为 32 字节 CWObjHandle;
 *  - 对象记录 = 8 字节公共头 + 32 字节句柄 = 40 字节;
 *  - 帧 = 哨兵帧链 + 变量表 (记录数组) + 懒分配值栈 (2 MiB + 保护页);
 *  - 内存中心只管底层簿记内存 (帧、变量表、容器数组/节点),
 *    不管高层变量值 (值栈 / GC 页负责)。
 *
 * 本头文件在编译期用 _Static_assert 校验实际结构体与 ABI 一致,
 * 任何头文件漂移都会直接编译失败。
 */

#ifndef CWIND_ABI_H
    #define CWIND_ABI_H

    #include <stddef.h>
    #include <stdint.h>

    #include "../object/cwind_object.h"
    #include "../rt/stackframe.h"

    #define CWIND_ABI_VERSION 1

    /* ---- 尺寸 ---- */

    #define CWIND_ABI_HEAD_SIZE        ((size_t)8)   /* 公共头 */
    #define CWIND_ABI_HANDLE_SIZE      ((size_t)32)  /* 句柄 */
    #define CWIND_ABI_OBJECT_RECORD    ((size_t)40)  /* 完整对象记录 */

    /* ---- 公共头布局: CWindObject_t ---- */
    /* type_id: 偏移 0, 4 字节 (CWindBaseType_t, 见 cwind_type.h) */
    /* gc_cnt : 偏移 4, 1 字节 (栈上对象恒为 0) */

    /* ---- 句柄布局: CWObjHandle_t ---- */
    /* object : 偏移 0, 8 字节, 指向所属对象记录 (base) */
    /* address: 偏移 8, 8 字节, 标量值 / 字节流 / 容器数据地址 */
    /* length : 偏移 16, 8 字节, 标量字节数 / 字符串长度 / 容器元素数 */
    /* cursor : 偏移 24, 8 字节, 迭代游标 / Vector 容量 */

    /* ---- 句柄语义 (按类型) ---- */
    /* 标量 (Int/UInt/Int8/UInt8/Float/Bool/Byte):
     *     address -> 值所在存储 (帧值栈 / 外部), length = 值字节数 */
    /* String: address -> 字节流 (NUL 结尾), length = 字节数 */
    /* None  : address = 0, length = 0 */
    /* 容器 (Tuple/Vector/Map/Set):
     *     address -> 容器数据 (内存中心), length = 元素数,
     *     Vector 的 cursor = 容量 */

    /* ---- 帧布局: CWStackFrame_t ---- */
    /* 哨兵帧 (cwframe_create) 是 main 帧; pre/head = NULL, tail = 栈顶 */
    /* 非哨兵帧: next/pre 组成双向链, head/tail 字段未使用 (NULL) */
    /* stack_vars: CWFSArray, 元素 = 40 字节对象记录 */
    /* 值栈: VirtualAlloc/mmap 2 MiB + 首页保护, true_beginning 指向可写区 */

    /* ---- 编译期校验 ---- */

    _Static_assert(sizeof(CWindObject_t) == CWIND_ABI_HEAD_SIZE,
                   "ABI: CWindObject_t 必须为 8 字节");
    _Static_assert(offsetof(CWindObject_t, type_id) == 0,
                   "ABI: type_id 偏移必须为 0");
    _Static_assert(offsetof(CWindObject_t, gc_cnt) == 4,
                   "ABI: gc_cnt 偏移必须为 4");

    _Static_assert(sizeof(CWObjHandle_t) == CWIND_ABI_HANDLE_SIZE,
                   "ABI: CWObjHandle_t 必须为 32 字节");
    _Static_assert(offsetof(CWObjHandle_t, object) == 0,
                   "ABI: handle.object 偏移必须为 0");
    _Static_assert(offsetof(CWObjHandle_t, address) == 8,
                   "ABI: handle.address 偏移必须为 8");
    _Static_assert(offsetof(CWObjHandle_t, length) == 16,
                   "ABI: handle.length 偏移必须为 16");
    _Static_assert(offsetof(CWObjHandle_t, cursor) == 24,
                   "ABI: handle.cursor 偏移必须为 24");

    _Static_assert(CWIND_OBJECT_RECORD_SIZE == CWIND_ABI_OBJECT_RECORD,
                   "ABI: 对象记录必须为 40 字节");

    /* 基础类型编号固定, 后端按值翻译 */
    _Static_assert(CWInt   == 1,  "ABI: CWInt = 1");
    _Static_assert(CWUInt  == 2,  "ABI: CWUInt = 2");
    _Static_assert(CWFloat == 3,  "ABI: CWFloat = 3");
    _Static_assert(CWBool  == 4,  "ABI: CWBool = 4");
    _Static_assert(CWByte  == 5,  "ABI: CWByte = 5");
    _Static_assert(CWString == 6, "ABI: CWString = 6");
    _Static_assert(CWNone  == 8,  "ABI: CWNone = 8");
    _Static_assert(CWTuple == 9,  "ABI: CWTuple = 9");
    _Static_assert(CWVector == 10, "ABI: CWVector = 10");
    _Static_assert(CWMap   == 11, "ABI: CWMap = 11");
    _Static_assert(CWSet   == 12, "ABI: CWSet = 12");
    _Static_assert(CWInt8  == 13, "ABI: CWInt8 = 13");
    _Static_assert(CWUInt8 == 14, "ABI: CWUInt8 = 14");

    /* 帧配置 */
    _Static_assert(CWSTACK_VALUE_STACK_SIZE == ((size_t)2 * 1024 * 1024),
                   "ABI: 值栈默认 2 MiB");

#endif /* CWIND_ABI_H */
