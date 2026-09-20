#ifndef CWIND_OBJECT_H
    #define CWIND_OBJECT_H

    #include <stdint.h>
    #include <stdbool.h>
    #include "./cwind_type.h"
    #include "./cwind_obj_forward.h"

    /*
     * ABI v3 值模型 (todo-209: ≤8B 标量内联; 承接 todo-50 元数据分区)
     *
     * CWValue_t = 值本体 (24B 纯数据):
     *  - 标量:   address = 标量位模式 (低 width 字节, 高位零扩展),
     *            length = 宽度标记 1/2/4/8, cursor = 0
     *  - String: address -> 字节流 (NUL 结尾), length = 字节数
     *  - 容器:   address -> 容器 data (内存中心), length = 元素数,
     *            Vector 的 cursor = 容量
     *  - None:   全 0 (length 0 = 无值; 标量 0 的 width >= 1, 天然可辨)
     *  - 裸指针/借用: address = 地址, length/cursor 由类型上下文约定
     *  值不携带类型: 类型由调用点静态 tag / 容器 data 头 / CWCell 提供;
     *  address 中的标量位与指针同名不同义, 消歧完全靠静态类型。
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
    const char* cwobj_type_name(CWindBaseType_t type_id);

    /* 标量值宽度 (字节); 非标量类型返回 0 */
    size_t cwobj_scalar_width(CWindBaseType_t type_id);

    /* 值相等: 标量按内联位+宽度标记比、String 按字节比、None 恒等、
     * 容器按 data 地址身份比较 (同一容器实例) */
    bool cwobj_value_equal(int32_t type_id,
                           const CWValue_t* a, const CWValue_t* b);

    /* 值哈希: 标量按内联位低字节、String 按字节流、容器按 data 地址身份 */
    uint64_t cwobj_value_hash(int32_t type_id, const CWValue_t* v);

    /* 取 String 字节流 (address/length); 非字符串或空地址返回 false */
    bool cwobj_string_view(const CWValue_t* v,
                           const char** data, uint64_t* len);

    /* 构造一个指向已有存储的指针/字节流值 (不拷贝数据; String/容器/裸
     * 指针用)。标量请用 cwval_scalar / cwval_scalar_mem。 */
    void cwval_wrap(CWValue_t* out, const void* storage,
                    uint64_t length);

    /* None 值 (全 0) */
    void cwval_none(CWValue_t* out);

    /* ---- ABI v3 标量内联构造/读取 ---- */

    /* 标量位模式内联: address = bits 低 width 字节 (高位清零),
     * length = width (必须 1/2/4/8), cursor = 0 */
    void cwval_scalar(CWValue_t* out, uint64_t bits, uint64_t width);

    /* 从存储读 width 字节内联 (小端语义; width 必须 1/2/4/8) */
    void cwval_scalar_mem(CWValue_t* out, const void* storage,
                          uint64_t width);

    /* 内联标量位模式 (address 字段原样); None/null 返回 0 */
    uint64_t cwval_scalar_bits(const CWValue_t* v);

    /* 值的宽度标记 (length): None/null 为 0, 标量为 1/2/4/8 */
    uint64_t cwval_scalar_len(const CWValue_t* v);

    /* 按位模式取浮点 (internal: 以小端解释 address 低位) */
    float  cwval_f32(const CWValue_t* v);
    double cwval_f64(const CWValue_t* v);

#endif
