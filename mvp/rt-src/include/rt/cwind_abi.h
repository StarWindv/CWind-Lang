/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/rt/cwind_abi.h
 */

/**
 * CWind ABI v2 (todo-50: 拆胖对象, 值类型 + 元数据分区存放)
 *
 * 本文固化运行时值表示与内存约定, 是后端编译器 (LLVM) 与 rt 之间的契约:
 *  - 所有运行时值统一为 24 字节 CWValue (纯数据, 无类型头、无自指元数据);
 *  - 异构边界 (帧变量表 / print / format 等入口) 用 32 字节 CWCell
 *    (4B 类型 tag + 24B 值), tag 由代码生成的调用点静态提供;
 *  - 类型元数据分区: 调用点静态 tag / 容器 data 头 (元素类型) /
 *    memcenter 槽头 (GC 位 + 分配点描述符), 不内联进值;
 *  - 帧模型 = 哨兵帧链 + 变量表 (32B CWCell 数组) + 懒分配值栈
 *    (2 MiB + 保护页) — 生成代码目前不消费帧模型 (LLVM 栈 alloca 自治),
 *    仅供 ABI 测试与未来的帧扫描根集合使用;
 *  - 内存中心管簿记内存 (帧、变量表、容器 data/节点), 值存储走
 *    arena 单元 (String 字节流 / 枚举载荷 / 容器标量元素)。
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

    #define CWIND_ABI_VERSION 2

    /* ---- 尺寸 ---- */

    #define CWIND_ABI_VALUE_SIZE       ((size_t)24)  /* 值 */
    #define CWIND_ABI_CELL_SIZE        ((size_t)32)  /* 异构单元 */

    /* ---- 值布局: CWValue_t ---- */
    /* address: 偏移 0,  8 字节, 标量值 / 字节流 / 容器数据地址 */
    /* length : 偏移 8,  8 字节, 标量字节数 / 字符串长度 / 容器元素数 */
    /* cursor : 偏移 16, 8 字节, 迭代游标 / Vector 容量 */

    /* ---- 异构单元布局: CWCell_t ---- */
    /* type_id: 偏移 0,  4 字节 (CWindBaseType_t, 见 cwind_type.h) */
    /* _pad   : 偏移 4,  4 字节 */
    /* value  : 偏移 8,  24 字节 (CWValue_t) */

    /* ---- 值语义 (按类型) ---- */
    /* 标量 (Int/UInt/Int8/UInt8/Float/Bool/Byte):
     *     address -> 值所在存储 (LLVM alloca / arena 单元), length = 值字节数 */
    /* String: address -> 字节流 (NUL 结尾), length = 字节数 */
    /* None  : address = 0, length = 0 */
    /* 容器 (Tuple/Vector/Map/Set):
     *     address -> 容器 data (内存中心), length = 元素数,
     *     Vector 的 cursor = 容量; data 头带元素类型 tag */

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

    /* 帧配置 */
    _Static_assert(CWSTACK_VALUE_STACK_SIZE == ((size_t)2 * 1024 * 1024),
                   "ABI: value stack defaults to 2 MiB");

#endif /* CWIND_ABI_H */
