/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/rt/cwind_abi.h
 */

/**
 * CWind ABI v3 (todo-209: 标量值内联; 承接 todo-50 的值类型 + 元数据分区)
 *
 * 本文固化运行时值表示与内存约定, 是后端编译器 (LLVM) 与 rt 之间的契约:
 *  - 所有运行时值统一为 24 字节 CWValue (纯数据, 无类型头、无自指元数据);
 *  - 异构边界 (帧变量表 / print / format 等入口) 用 32 字节 CWCell
 *    (4B 类型 tag + 24B 值), tag 由代码生成的调用点静态提供;
 *  - 类型元数据分区: 调用点静态 tag / 容器 data 头 (元素类型) /
 *    memcenter 槽头 (GC 位 + 分配点描述符), 不内联进值;
 *  - v3 表示变更: ≤8B 标量 (含 Bool/Byte/各宽度整数与浮点、函数指针)
 *    不再指向存储, 值本体直接内联进 CWValue.address 低字节;
 *  - 帧模型 = 哨兵帧链 + 变量表 (32B CWCell 数组) + 懒分配值栈
 *    (2 MiB + 保护页) — 生成代码目前不消费帧模型 (LLVM 栈 alloca 自治),
 *    仅供 ABI 测试与未来的帧扫描根集合使用;
 *  - 内存中心管簿记内存 (帧、变量表、容器 data/节点), 非标量值存储走
 *    arena 单元 (String 字节流 / 结构体与枚举 blob 载荷)。
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

    #define CWIND_ABI_VERSION 3

    /* ---- 尺寸 ---- */

    #define CWIND_ABI_VALUE_SIZE       ((size_t)24)  /* 值 */
    #define CWIND_ABI_CELL_SIZE        ((size_t)32)  /* 异构单元 */

    /* ---- 值布局: CWValue_t ---- */
    /* address: 偏移 0,  8 字节, 标量位模式 / 字节流 / 容器数据地址 */
    /* length : 偏移 8,  8 字节, 标量宽度标记 1/2/4/8 / 字符串长度 / 元素数 */
    /* cursor : 偏移 16, 8 字节, 迭代游标 / Vector 容量 / C 布局标记 */

    /* ---- 异构单元布局: CWCell_t ---- */
    /* type_id: 偏移 0,  4 字节 (CWindBaseType_t, 见 cwind_type.h) */
    /* _pad   : 偏移 4,  4 字节 */
    /* value  : 偏移 8,  24 字节 (CWValue_t) */

    /* ---- 值语义 (按类型, todo-209 标量内联) ----
     *
     * 标量 (Int/UInt/Int8/UInt8/Int16/UInt16/Int32/UInt32/Int64/UInt64/
     *       Float/Float64/Bool/Byte) 与函数指针:
     *     address = 标量位模式 (低 width 字节有效, 规范形态高位零扩展),
     *     length  = 宽度标记 {1, 2, 4, 8}, cursor = 0。
     *     - 浮点按位型零扩展 (Float -> uint32_t, Float64 -> uint64_t);
     *     - 标量 0 = address 0 + length = 宽度标记 (非 0):
     *       与 None/null (length 0) 天然可区分, 无需类型即可分辨「无值」;
     *     - == / hash 按 length 宽度比较/哈希 address 低字节。
     *
     * String: address -> 字节流 (NUL 结尾), length = 字节数。
     * None  : address = 0, length = 0, cursor = 0 (宽度标记 0 = 无值)。
     * 容器 (Tuple/Vector/Map/Set):
     *     address -> 容器 data (内存中心), length = 元素数,
     *     Vector 的 cursor = 容量; data 头带元素类型 tag。
     * 裸指针 (*const T / *mut T) 与 &T 借用句柄:
     *     address = 地址, length/cursor 语义由类型上下文决定
     *     (裸指针 length = 0; 结构体指针 cursor = C 布局标记)。
     *
     * 消歧: 标量位模式与指针共用 address 字段, 语义完全由调用点静态
     * 类型承担 (CWCell.type_id / 容器 data 头元素 tag / 生成代码静态
     * 类型) —— 值本身不携带任何类型判别位。
     */

    /* 标量宽度标记 (length 字段) 的固定取值; 0 专用于 None/null */
    #define CWIND_ABI_SCALAR_LEN_NONE ((uint64_t)0)
    #define CWIND_ABI_SCALAR_LEN_1    ((uint64_t)1)
    #define CWIND_ABI_SCALAR_LEN_2    ((uint64_t)2)
    #define CWIND_ABI_SCALAR_LEN_4    ((uint64_t)4)
    #define CWIND_ABI_SCALAR_LEN_8    ((uint64_t)8)
    #define CWIND_ABI_SCALAR_WIDTH_MAX ((size_t)8)


    /* ---- 帧布局: CWStackFrame_t ---- */
    /* 哨兵帧 (cwframe_create) 是 main 帧; pre/head = NULL, tail = 栈顶 */
    /* 非哨兵帧: next/pre 组成双向链, head/tail 字段未使用 (NULL) */
    /* stack_vars: CWFSArray, 元素 = 32 字节 CWCell */
    /* 值栈: VirtualAlloc/mmap 2 MiB + 首页保护, true_beginning 指向可写区 */

    /* ---- 编译期校验 ---- */

    _Static_assert(sizeof(CWValue_t) == CWIND_ABI_VALUE_SIZE,
                   "ABI: CWValue_t must be 24 bytes");
    _Static_assert(offsetof(CWValue_t, address) == 0,
                   "ABI: value.address offset must be 0");
    _Static_assert(offsetof(CWValue_t, length) == 8,
                   "ABI: value.length offset must be 8");
    _Static_assert(offsetof(CWValue_t, cursor) == 16,
                   "ABI: value.cursor offset must be 16");

    _Static_assert(sizeof(CWCell_t) == CWIND_ABI_CELL_SIZE,
                   "ABI: CWCell_t must be 32 bytes");
    _Static_assert(offsetof(CWCell_t, type_id) == 0,
                   "ABI: cell.type_id offset must be 0");
    _Static_assert(offsetof(CWCell_t, value) == 8,
                   "ABI: cell.value offset must be 8");

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
    _Static_assert(CWInt32   == 15, "ABI: CWInt32 = 15");
    _Static_assert(CWUInt32  == 16, "ABI: CWUInt32 = 16");
    _Static_assert(CWInt64   == 17, "ABI: CWInt64 = 17");
    _Static_assert(CWUInt64  == 18, "ABI: CWUInt64 = 18");
    _Static_assert(CWFloat64 == 19, "ABI: CWFloat64 = 19");
    _Static_assert(CWInt16   == 20, "ABI: CWInt16 = 20");
    _Static_assert(CWUInt16  == 21, "ABI: CWUInt16 = 21");

    /* 标量宽度标记固定 (v3): None/null = 0, 标量 = 1/2/4/8 */
    _Static_assert(CWIND_ABI_SCALAR_LEN_NONE == 0,
                   "ABI: scalar len 0 means None/null");
    _Static_assert(CWIND_ABI_SCALAR_LEN_1 == 1
                   && CWIND_ABI_SCALAR_LEN_2 == 2
                   && CWIND_ABI_SCALAR_LEN_4 == 4
                   && CWIND_ABI_SCALAR_LEN_8 == 8,
                   "ABI: scalar width markers must be 1/2/4/8");
    _Static_assert(CWIND_ABI_SCALAR_WIDTH_MAX == sizeof(uint64_t),
                   "ABI: inline scalar storage upper bound is 8 bytes");

    /* 帧配置 */
    _Static_assert(CWSTACK_VALUE_STACK_SIZE == ((size_t)2 * 1024 * 1024),
                   "ABI: value stack defaults to 2 MiB");

#endif /* CWIND_ABI_H */
