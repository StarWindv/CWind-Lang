#ifndef CWIND_OBJECT_H
    #define CWIND_OBJECT_H
    
    #include <stdint.h>
    #include <stdbool.h>
    #include "./cwind_type.h"
    #include "./cwind_obj_handle.h"
    #include "./cwind_obj_forward.h"

    typedef struct CWindObject {
        CWindBaseType_t type_id; // enum,
        uint8_t  gc_cnt; // Stack object here is 0
    } CWindObject_t;


    typedef struct CWindIntObject {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindIntObject_t;

    typedef struct CWindUIntObject {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindUIntObject_t;
    typedef struct CWindFloatObject {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindFloatObject_t;
    typedef struct CWindInt8Object  {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindInt8Object_t;
    typedef struct CWindUInt8Object {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindUInt8Object_t;
    typedef struct CWindBoolObject  {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindBoolObject_t;
    typedef struct CWindByteObject  {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindByteObject_t;
    typedef struct CWindStringObject{
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindStringObject_t;
    //
    // typedef struct CWindInstanceObject {
    //     CWindObject_t head;
    //     CWObjHandle_t handle;
    // } CWindInstanceObject_t;

    typedef struct CWindNoneObject     {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindNoneObject_t;
    typedef struct CWindTupleObject    {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindTupleObject_t;
    typedef struct CWindVectorObject   {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindVectorObject_t;
    typedef struct CWindSetObject      {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindSetObject_t;
    typedef struct CWindMapObject      {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindMapObject_t;

    /*
     * 所有对象记录都是「公共头 + 32 字节句柄」的同一布局,
     * 栈帧变量表按此大小存储任意对象记录。
     * cwind_object.c 里用 _Static_assert 保证所有子类型等大。
     */
    #define CWIND_OBJECT_RECORD_SIZE (sizeof(CWindIntObject_t))

    /* ---- 基础操作 (实现于 rt-src/rt/cwind_object.c) ---- */

    void cwobj_init(CWindObject_t* obj, CWindBaseType_t type);
    bool cwobj_type_is(const CWindObject_t* obj, CWindBaseType_t type);
    const char* cwobj_type_name(CWindBaseType_t type);
    bool cwobj_equal(const CWindObject_t* a, const CWindObject_t* b);
    uint64_t cwobj_hash(const CWindObject_t* obj);

    /* 构造: 把对象记录写入 obj (帧变量槽 / 外部内存), 值写入 storage */
    CWindIntObject_t*  cwobj_int_new(CWindIntObject_t* obj,
                                     void* storage, int16_t value);
    CWindUIntObject_t* cwobj_uint_new(CWindUIntObject_t* obj,
                                      void* storage, uint16_t value);
    CWindInt8Object_t* cwobj_int8_new(CWindInt8Object_t* obj,
                                      void* storage, int8_t value);
    CWindUInt8Object_t* cwobj_uint8_new(CWindUInt8Object_t* obj,
                                        void* storage, uint8_t value);
    CWindFloatObject_t* cwobj_float_new(CWindFloatObject_t* obj,
                                        void* storage, float value);
    CWindBoolObject_t* cwobj_bool_new(CWindBoolObject_t* obj,
                                      void* storage, bool value);
    CWindByteObject_t* cwobj_byte_new(CWindByteObject_t* obj,
                                      void* storage, uint8_t value);
    CWindNoneObject_t* cwobj_none_new(CWindNoneObject_t* obj);
    CWindStringObject_t* cwobj_string_new(CWindStringObject_t* obj,
                                          char* storage,
                                          const char* data, uint64_t len);

    /* 读写: 校验 NULL / 类型 / 存储指针, 失败返回 false */
    bool cwobj_get_i16(const CWindIntObject_t* obj, int16_t* out);
    bool cwobj_set_i16(CWindIntObject_t* obj, int16_t value);
    bool cwobj_get_uint16(const CWindUIntObject_t* obj, uint16_t* out);
    bool cwobj_set_uint16(CWindUIntObject_t* obj, uint16_t value);
    bool cwobj_get_int8(const CWindInt8Object_t* obj, int8_t* out);
    bool cwobj_set_int8(CWindInt8Object_t* obj, int8_t value);
    bool cwobj_get_uint8(const CWindUInt8Object_t* obj, uint8_t* out);
    bool cwobj_set_uint8(CWindUInt8Object_t* obj, uint8_t value);
    bool cwobj_get_float(const CWindFloatObject_t* obj, float* out);
    bool cwobj_set_float(CWindFloatObject_t* obj, float value);
    bool cwobj_get_bool(const CWindBoolObject_t* obj, bool* out);
    bool cwobj_set_bool(CWindBoolObject_t* obj, bool value);
    bool cwobj_get_byte(const CWindByteObject_t* obj, uint8_t* out);
    bool cwobj_set_byte(CWindByteObject_t* obj, uint8_t value);
    bool cwobj_string_get(const CWindStringObject_t* obj,
                          const char** data, uint64_t* len);
    bool cwobj_string_set(CWindStringObject_t* obj,
                          const char* data, uint64_t len);

    /* 容器对象: 先建头 + 清空句柄, 容器主体后续由容器组件挂载 */
    void cwobj_container_init(CWindObject_t* obj, CWindBaseType_t type);

#endif
