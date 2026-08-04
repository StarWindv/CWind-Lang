#ifndef CWIND_OBJECT_H
    #define CWIND_OBJECT_H
    
    #include <stdint.h>
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

    typedef struct CWindInstanceObject {
        CWindObject_t head;
        CWObjHandle_t handle;
    } CWindInstanceObject_t;

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


#endif
