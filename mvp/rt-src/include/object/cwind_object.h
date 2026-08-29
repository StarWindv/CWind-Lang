#ifndef CWIND_OBJECT_H
    #define CWIND_OBJECT_H

    #include <stdint.h>
    #include <stdbool.h>
    #include <stddef.h>
    #include "./cwind_type.h"
    #include "./cwind_obj_forward.h"

    /*
     * ABI v2 值模型 (todo-50: 拆胖对象, 元数据分区存放)
     *
     * CWValue_t = 值本体 (24B 纯数据):
     *  - 标量:   address -> 值存储, length = 值字节数, cursor = 0
     *  - String: address -> 字节流 (NUL 结尾), length = 字节数
     *  - 容器:   address -> 容器 data (内存中心), length = 元素数,
     *            Vector 的 cursor = 容量
     *  - None:   全 0
     *  值不携带类型: 类型由调用点静态 tag / 容器 data 头 / CWCell 提供。
     *
     * CWCell_t = 异构边界单元 (32B = 4B 类型 tag + 4B pad + 24B 值):
     *  帧变量表与 rt 异构入口 (print/format/...) 的统一形态。
     */

    typedef struct CWValue {
        uint64_t address; /* 数据地址 (标量存储 / 字节流 / 容器 data / blob) */
        uint64_t length;  /* 标量字节数 / 字符串字节数 / 容器元素数 */
        uint64_t cursor;  /* Vector 容量 / 迭代游标 */
    } CWValue_t;

    typedef struct CWCell {
        int32_t  type_id;  /* CWindBaseType_t, 元数据分区: tag 在值外 */
        uint32_t _pad;
        CWValue_t value;
    } CWCell_t;

    #define CWIND_VALUE_SIZE   ((size_t)24)
    #define CWIND_CELL_SIZE    ((size_t)32)

    /* ---- 值操作 (实现于 rt-src/rt/cwind_object.c) ---- */

    /* 类型名 (builtins::type_of 用); 未知类型返回 "Invalid" */
    const char* cwobj_type_name(int32_t type_id);

    /* 标量值宽度 (字节); 非标量类型返回 0 */
    size_t cwobj_scalar_width(int32_t type_id);

    /* 值相等: 标量按宽度比、String 按字节比、None 恒等、
     * 容器按 data 地址身份比较 (同一容器实例) */
    bool cwobj_value_equal(int32_t type_id,
                           const CWValue_t* a, const CWValue_t* b);

    /* 值哈希: 标量按字节流、String 按字节流、容器按 data 地址身份 */
    uint64_t cwobj_value_hash(int32_t type_id, const CWValue_t* v);

    /* 取 String 字节流 (address/length); 非字符串或空地址返回 false */
    bool cwobj_string_view(const CWValue_t* v,
                           const char** data, uint64_t* len);

    /* 构造一个指向已有存储的标量/字符串值 (不拷贝数据) */
    void cwval_wrap(CWValue_t* out, const void* storage,
                    uint64_t length);

    /* None 值 (全 0) */
    void cwval_none(CWValue_t* out);

#endif
