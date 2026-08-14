/**
 * 独立测试: CWindObject 基础操作
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwobject.exe test_cwobject.c ../../rt-src/rt/cwind_object.c
 */

#include "../../rt-src/include/object/cwind_object.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

_Static_assert(sizeof(CWindObject_t) == 8, "head 应为 8 字节");
_Static_assert(sizeof(CWObjHandle_t) == 32, "handle 应为 32 字节 (ABI)");
_Static_assert(CWIND_OBJECT_RECORD_SIZE == sizeof(CWindUIntObject_t)
            && CWIND_OBJECT_RECORD_SIZE == sizeof(CWindFloatObject_t)
            && CWIND_OBJECT_RECORD_SIZE == sizeof(CWindStringObject_t)
            && CWIND_OBJECT_RECORD_SIZE == sizeof(CWindVectorObject_t)
            && CWIND_OBJECT_RECORD_SIZE == sizeof(CWindMapObject_t),
               "对象记录必须等大");

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWindObject tests:\n\n");

    printf(" - type table\n");
    T("type_name(Int) == \"Int\"", strcmp(cwobj_type_name(CWInt), "Int") == 0);
    T("type_name(UInt8) == \"UInt8\"",
      strcmp(cwobj_type_name(CWUInt8), "UInt8") == 0);
    T("type_name(Int32) == \"Int32\"",
      strcmp(cwobj_type_name(CWInt32), "Int32") == 0);
    T("type_name(UInt64) == \"UInt64\"",
      strcmp(cwobj_type_name(CWUInt64), "UInt64") == 0);
    T("type_name(Float64) == \"Float64\"",
      strcmp(cwobj_type_name(CWFloat64), "Float64") == 0);
    T("type_name(Vector) == \"Vector\"",
      strcmp(cwobj_type_name(CWVector), "Vector") == 0);
    T("type_name(invalid) == \"Invalid\"",
      strcmp(cwobj_type_name((CWindBaseType_t)999), "Invalid") == 0);

    CWindObject_t base;
    cwobj_init(&base, CWInt);
    T("init sets type_id", base.type_id == CWInt);
    T("init zeroes gc_cnt", base.gc_cnt == 0);
    T("type_is ok", cwobj_type_is(&base, CWInt));
    T("type_is mismatch", !cwobj_type_is(&base, CWFloat));
    T("type_is(NULL) false", !cwobj_type_is(NULL, CWInt));

    printf("\n - scalar constructors / get / set\n");
    int16_t s_i16;
    CWindIntObject_t o_i16;
    T("int_new != NULL",
      cwobj_int_new(&o_i16, &s_i16, (int16_t)-1234) != NULL);
    T("int type", o_i16.head.type_id == CWInt);
    T("int handle.object back-ref", o_i16.handle.object == &o_i16.head);
    T("int handle.address == storage",
      o_i16.handle.address == (uint64_t)(uintptr_t)&s_i16);
    T("int handle.length == 2", o_i16.handle.length == 2);
    T("int handle.cursor == 0", o_i16.handle.cursor == 0);
    T("int storage written", s_i16 == -1234);
    int16_t v_i16 = 0;
    T("int get", cwobj_get_i16(&o_i16, &v_i16) && v_i16 == -1234);
    T("int set", cwobj_set_i16(&o_i16, 32000) && s_i16 == 32000);
    T("int get after set", cwobj_get_i16(&o_i16, &v_i16) && v_i16 == 32000);
    T("int_new(NULL storage) == NULL", cwobj_int_new(&o_i16, NULL, 1) == NULL);
    T("int_new(NULL obj) == NULL", cwobj_int_new(NULL, &s_i16, 1) == NULL);

    uint16_t s_ui16;
    CWindUIntObject_t o_ui16;
    cwobj_uint_new(&o_ui16, &s_ui16, 65535);
    uint16_t v_ui16 = 0;
    T("uint get", cwobj_get_uint16(&o_ui16, &v_ui16) && v_ui16 == 65535);
    T("uint set", cwobj_set_uint16(&o_ui16, 1) && s_ui16 == 1);

    int8_t s_i8;
    CWindInt8Object_t o_i8;
    cwobj_int8_new(&o_i8, &s_i8, -128);
    int8_t v_i8 = 0;
    T("int8 get", cwobj_get_int8(&o_i8, &v_i8) && v_i8 == -128);
    T("int8 set", cwobj_set_int8(&o_i8, 127) && s_i8 == 127);

    uint8_t s_ui8;
    CWindUInt8Object_t o_ui8;
    cwobj_uint8_new(&o_ui8, &s_ui8, 255);
    uint8_t v_ui8 = 0;
    T("uint8 get", cwobj_get_uint8(&o_ui8, &v_ui8) && v_ui8 == 255);
    T("uint8 set", cwobj_set_uint8(&o_ui8, 7) && s_ui8 == 7);

    float s_f;
    CWindFloatObject_t o_f;
    cwobj_float_new(&o_f, &s_f, 3.25f);
    float v_f = 0.0f;
    T("float get", cwobj_get_float(&o_f, &v_f) && v_f == 3.25f);
    T("float handle.length == 4", o_f.handle.length == 4);
    T("float set", cwobj_set_float(&o_f, -0.5f) && s_f == -0.5f);

    bool s_b;
    CWindBoolObject_t o_b;
    cwobj_bool_new(&o_b, &s_b, true);
    bool v_b = false;
    T("bool get", cwobj_get_bool(&o_b, &v_b) && v_b == true);
    T("bool set", cwobj_set_bool(&o_b, false) && s_b == false);

    uint8_t s_by;
    CWindByteObject_t o_by;
    cwobj_byte_new(&o_by, &s_by, 0xAB);
    uint8_t v_by = 0;
    T("byte get", cwobj_get_byte(&o_by, &v_by) && v_by == 0xAB);
    T("byte set", cwobj_set_byte(&o_by, 0xCD) && s_by == 0xCD);

    int32_t s_i32;
    CWindInt32Object_t o_i32;
    cwobj_int32_new(&o_i32, &s_i32, -2147483647);
    int32_t v_i32 = 0;
    T("int32 get", cwobj_get_i32(&o_i32, &v_i32) && v_i32 == -2147483647);
    T("int32 set",
      cwobj_set_i32(&o_i32, 2147483647) && s_i32 == 2147483647);
    T("int32 handle.length == 4", o_i32.handle.length == 4);

    uint32_t s_ui32;
    CWindUInt32Object_t o_ui32;
    cwobj_uint32_new(&o_ui32, &s_ui32, 4294967295U);
    uint32_t v_ui32 = 0;
    T("uint32 get",
      cwobj_get_uint32(&o_ui32, &v_ui32) && v_ui32 == 4294967295U);
    T("uint32 set", cwobj_set_uint32(&o_ui32, 7) && s_ui32 == 7);

    int64_t s_i64;
    CWindInt64Object_t o_i64;
    cwobj_int64_new(&o_i64, &s_i64, -9223372036854775807LL);
    int64_t v_i64 = 0;
    T("int64 get",
      cwobj_get_i64(&o_i64, &v_i64) && v_i64 == -9223372036854775807LL);
    T("int64 set",
      cwobj_set_i64(&o_i64, 9223372036854775807LL)
      && s_i64 == 9223372036854775807LL);
    T("int64 handle.length == 8", o_i64.handle.length == 8);

    uint64_t s_ui64;
    CWindUInt64Object_t o_ui64;
    cwobj_uint64_new(&o_ui64, &s_ui64, UINT64_MAX);
    uint64_t v_ui64 = 0;
    T("uint64 get",
      cwobj_get_uint64(&o_ui64, &v_ui64) && v_ui64 == UINT64_MAX);
    T("uint64 set", cwobj_set_uint64(&o_ui64, 1) && s_ui64 == 1);

    double s_f64;
    CWindFloat64Object_t o_f64;
    cwobj_float64_new(&o_f64, &s_f64, 3.25);
    double v_f64 = 0.0;
    T("float64 get", cwobj_get_float64(&o_f64, &v_f64) && v_f64 == 3.25);
    T("float64 handle.length == 8", o_f64.handle.length == 8);
    T("float64 set", cwobj_set_float64(&o_f64, -0.5) && s_f64 == -0.5);

    printf("\n - checked accessor error paths\n");
    int16_t dummy_i = 0;
    T("get_i16(NULL obj) false", !cwobj_get_i16(NULL, &dummy_i));
    T("get_i16(NULL out) false", !cwobj_get_i16(&o_i16, NULL));
    T("wrong type get false",
      !cwobj_get_uint16((const CWindUIntObject_t*)&o_i16, &v_ui16));
    T("wrong type set false",
      !cwobj_set_uint16((CWindUIntObject_t*)&o_i16, 5));
    T("wrong type i64 get false",
      !cwobj_get_i64((const CWindInt64Object_t*)&o_i16, &v_i64));
    T("wrong type float64 set false",
      !cwobj_set_float64((CWindFloat64Object_t*)&o_i16, 1.0));

    CWindFloatObject_t o_empty = {0};
    cwobj_init(&o_empty.head, CWFloat);
    T("no storage get false", !cwobj_get_float(&o_empty, &v_f));
    T("no storage set false", !cwobj_set_float(&o_empty, 1.0f));

    printf("\n - None\n");
    CWindNoneObject_t o_none;
    cwobj_none_new(&o_none);
    T("none type", o_none.head.type_id == CWNone);
    T("none handle zeroed",
      o_none.handle.address == 0 && o_none.handle.length == 0
      && o_none.handle.cursor == 0);
    T("none handle.object back-ref", o_none.handle.object == &o_none.head);

    printf("\n - String (fat pointer)\n");
    char s_str[32];
    CWindStringObject_t o_str;
    T("string_new != NULL",
      cwobj_string_new(&o_str, s_str, "hello", 5) != NULL);
    T("string type", o_str.head.type_id == CWString);
    const char* data = NULL;
    uint64_t len = 0;
    T("string get",
      cwobj_string_get(&o_str, &data, &len) && data == s_str && len == 5);
    T("string NUL terminated", s_str[5] == '\0');
    T("string data intact", memcmp(s_str, "hello", 5) == 0);
    T("string set",
      cwobj_string_set(&o_str, "hi", 2) && o_str.handle.length == 2);
    T("string set updated bytes", memcmp(s_str, "hi", 2) == 0 && s_str[2] == '\0');
    T("string set empty",
      cwobj_string_set(&o_str, "", 0) && o_str.handle.length == 0);
    T("string_new(NULL storage) == NULL",
      cwobj_string_new(&o_str, NULL, "x", 1) == NULL);

    printf("\n - containers (head only)\n");
    CWindVectorObject_t o_vec;
    cwobj_container_init(&o_vec.head, CWVector);
    T("vector type", o_vec.head.type_id == CWVector);
    T("vector handle zeroed",
      o_vec.handle.address == 0 && o_vec.handle.length == 0
      && o_vec.handle.cursor == 0);
    T("vector handle.object back-ref", o_vec.handle.object == &o_vec.head);

    CWindTupleObject_t o_tup = {0};
    CWindMapObject_t o_map = {0};
    CWindSetObject_t o_set = {0};
    cwobj_container_init(&o_tup.head, CWTuple);
    cwobj_container_init(&o_map.head, CWMap);
    cwobj_container_init(&o_set.head, CWSet);
    T("tuple/map/set types",
      o_tup.head.type_id == CWTuple && o_map.head.type_id == CWMap
      && o_set.head.type_id == CWSet);
    T("container_init rejects scalar", o_tup.head.gc_cnt == 0);
    cwobj_container_init(&o_tup.head, CWInt);
    T("container_init(CWInt) no-op", o_tup.head.type_id == CWTuple);

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
