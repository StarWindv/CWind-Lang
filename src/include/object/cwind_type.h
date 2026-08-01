#ifndef CWIND_TYPE_H
    #define CWIND_TYPE_H


    typedef enum CWindBaseType {
        CWInt     = 1, // i16
        CWUInt    = 2, // u16
        CWFloat   = 3, // f64
        CWBool    = 4,
        CWByte    = 5,
        CWString  = 6, // 普通字符串, 胖指针 + 字节流, 较快
        CWComStr  = 7,
        /**
         * 压缩型字符串，胖指针+字节流
         * 可以直接dump堆上的字节流并交给其它语言直接解码
         * 本质上是一个内化的字典, VeryLowSpeed
         *
         */
        
        CWFunc    = 8,
        CWInstance= 9,

        CWNone    = 10,
        CWTuple   = 11,
        CWVector  = 12,
        CWMap     = 13,
        CWSet     = 14,

        CWInt8    = 15,
        CWUInt8   = 16,
        CWFloat8  = 17,
    } CWindBaseType_t;
    // 其它数值类型其实可以用 Vector<短数值> 来模拟, 遂不再成为基础类型
    // 举个例子, 我们承认 Byte, 而 Bytes 则是 Vector<Byte>
    // 像是 u32 也不过是两个标准 Int 的组合

#endif
