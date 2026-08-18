#ifndef CWIND_TYPE_H
    #define CWIND_TYPE_H

    typedef float float32;

    typedef enum CWindBaseType {
        CWInt     = 1, // i16
        CWUInt    = 2, // u16
        CWFloat   = 3, // f32
        CWBool    = 4,
        CWByte    = 5,
        CWString  = 6, // 普通字符串, 胖指针 + 字节流, 较快
        
        // CWInstance = 7, // 取消此类型
        CWNone     = 8,

        CWTuple  = 9,
        CWVector = 10,
        CWMap    = 11,
        CWSet    = 12,
        CWInt8   = 13,
        CWUInt8  = 14,
        CWInt32  = 15,
        CWUInt32 = 16,
        CWInt64  = 17,
        CWUInt64 = 18,
        CWFloat64 = 19,
    } CWindBaseType_t;
    // 其它数值类型其实可以用 Vector<短数值> 来模拟, 遂不再成为基础类型
    // 举个例子, 我们承认 Byte, 而 Bytes 则是 Vector<Byte>
    // 像是 u32 也不过是两个标准 Int 的组合


#endif
