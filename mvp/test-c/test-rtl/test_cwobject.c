/**
 * 独立测试: CWValue / CWCell 值模型 (ABI v2, todo-50)
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwobject.exe test_cwobject.c ../../rt-src/rt/cwind_object.c
 */

#include "../../rt-src/include/object/cwind_object.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

_Static_assert(sizeof(CWValue_t) == 24, "值应为 24 字节 (ABI v2)");
_Static_assert(sizeof(CWCell_t) == 32, "异构单元应为 32 字节 (ABI v2)");
_Static_assert(sizeof(CWCell_t) == 8 + sizeof(CWValue_t),
               "cell = 4B tag + 4B pad + 24B 值");

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWValue model tests:\n\n");

    printf(" - type table\n");
    T("type_name(Int) == \"Int\"", strcmp(cwobj_type_name(CWInt), "Int") == 0);
    T("type_name(Int8) == \"Int8\"",
      strcmp(cwobj_type_name(CWInt8), "Int8") == 0);
    T("type_name(UInt8) == \"UInt8\"",
      strcmp(cwobj_type_name(CWUInt8), "UInt8") == 0);
    T("type_name(Int16) == \"Int16\"",
      strcmp(cwobj_type_name(CWInt16), "Int16") == 0);
    T("type_name(UInt16) == \"UInt16\"",
      strcmp(cwobj_type_name(CWUInt16), "UInt16") == 0);
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

    printf("\n - scalar widths\n");
    T("width(Int) == 2", cwobj_scalar_width(CWInt) == 2);
    T("width(UInt) == 2", cwobj_scalar_width(CWUInt) == 2);
    T("width(Int8) == 1", cwobj_scalar_width(CWInt8) == 1);
    T("width(UInt8) == 1", cwobj_scalar_width(CWUInt8) == 1);
    T("width(Byte) == 1", cwobj_scalar_width(CWByte) == 1);
    T("width(Bool) == 1", cwobj_scalar_width(CWBool) == 1);
    T("width(Int16) == 2", cwobj_scalar_width(CWInt16) == 2);
    T("width(UInt16) == 2", cwobj_scalar_width(CWUInt16) == 2);
    T("width(Int32) == 4", cwobj_scalar_width(CWInt32) == 4);
    T("width(UInt32) == 4", cwobj_scalar_width(CWUInt32) == 4);
    T("width(Float) == 4", cwobj_scalar_width(CWFloat) == 4);
    T("width(Int64) == 8", cwobj_scalar_width(CWInt64) == 8);
    T("width(UInt64) == 8", cwobj_scalar_width(CWUInt64) == 8);
    T("width(Float64) == 8", cwobj_scalar_width(CWFloat64) == 8);
    T("width(String) == 0", cwobj_scalar_width(CWString) == 0);
    T("width(Vector) == 0", cwobj_scalar_width(CWVector) == 0);
    T("width(None) == 0", cwobj_scalar_width(CWNone) == 0);

    printf("\n - value wrap / none (值不携带任何元数据)\n");
    int16_t storage = -1234;
    CWValue_t v;
    cwval_wrap(&v, &storage, sizeof(storage));
    T("wrap address == storage",
      v.address == (uint64_t)(uintptr_t)&storage);
    T("wrap length == 2", v.length == 2);
    T("wrap cursor == 0", v.cursor == 0);
    T("storage untouched", storage == -1234);
    T("wrap(NULL storage) zero address", (cwval_wrap(&v, NULL, 0),
      v.address == 0));

    CWValue_t n;
    cwval_none(&n);
    T("none all zero",
      n.address == 0 && n.length == 0 && n.cursor == 0);
    cwval_none(NULL); /* 不崩 */
    cwval_wrap(NULL, &storage, 2); /* 不崩 */

    printf("\n - string view\n");
    char sbuf[32];
    CWValue_t sv;
    memcpy(sbuf, "hello", 5);
    sbuf[5] = '\0';
    cwval_wrap(&sv, sbuf, 5);
    const char* data = NULL;
    uint64_t len = 0;
    T("string view", cwobj_string_view(&sv, &data, &len)
      && data == sbuf && len == 5);
    CWValue_t empty;
    cwval_wrap(&empty, NULL, 0);
    T("empty string view (NULL 地址合法)",
      cwobj_string_view(&empty, &data, &len) && len == 0);
    T("string view NULL out false", !cwobj_string_view(&sv, NULL, &len));

    printf("\n - value equal (标量按字节, String 按字节, 容器按身份)\n");
    int16_t a1 = 100, a2 = 100, b = 200;
    CWValue_t va1, va2, vb;
    cwval_wrap(&va1, &a1, 2);
    cwval_wrap(&va2, &a2, 2);
    cwval_wrap(&vb, &b, 2);
    T("Int16 equal same value", cwobj_value_equal(CWInt16, &va1, &va2));
    T("Int16 not equal diff value", !cwobj_value_equal(CWInt16, &va1, &vb));
    /* tag 由调用方传入 (元数据分区), 同宽类型的比较是字节级语义 */
    T("UInt16 tag 同宽字节比较",
      cwobj_value_equal(CWUInt16, &va1, &va2));
    T("same storage equal", cwobj_value_equal(CWInt16, &va1, &va1));

    int32_t c1 = 0x41424344;
    CWValue_t vc1;
    cwval_wrap(&vc1, &c1, 4);
    T("Int32 equal itself", cwobj_value_equal(CWInt32, &vc1, &vc1));

    char sb1[8] = "abc";
    char sb2[8] = "abc";
    char sb3[8] = "abd";
    CWValue_t s1, s2, s3;
    cwval_wrap(&s1, sb1, 3);
    cwval_wrap(&s2, sb2, 3);
    cwval_wrap(&s3, sb3, 3);
    T("String equal same bytes", cwobj_value_equal(CWString, &s1, &s2));
    T("String not equal diff bytes", !cwobj_value_equal(CWString, &s1, &s3));
    CWValue_t s4;
    cwval_wrap(&s4, sb1, 2);
    T("String not equal diff length", !cwobj_value_equal(CWString, &s1, &s4));

    CWValue_t n1, n2;
    cwval_none(&n1);
    cwval_none(&n2);
    T("None equal None", cwobj_value_equal(CWNone, &n1, &n2));

    /* 容器: 按 data 地址身份比较 */
    CWValue_t cv1, cv2;
    cv1.address = 0x1000; cv1.length = 1; cv1.cursor = 4;
    cv2.address = 0x2000; cv2.length = 1; cv2.cursor = 4;
    T("container identity equal", cwobj_value_equal(CWVector, &cv1, &cv1));
    T("container identity not equal", !cwobj_value_equal(CWVector, &cv1, &cv2));
    T("NULL a/b safe", !cwobj_value_equal(CWInt, NULL, &va1));

    printf("\n - value hash\n");
    T("uint16 hash equals itself",
      cwobj_value_hash(CWUInt16, &va1) == cwobj_value_hash(CWUInt16, &va1));
    T("string hash equals itself",
      cwobj_value_hash(CWString, &s1) == cwobj_value_hash(CWString, &s2));
    T("none hash deterministic",
      cwobj_value_hash(CWNone, &n1) == cwobj_value_hash(CWNone, &n2));
    T("container hash by identity",
      cwobj_value_hash(CWMap, &cv1) == cwobj_value_hash(CWMap, &cv1));
    T("container hash differs by addr",
      cwobj_value_hash(CWMap, &cv1) != cwobj_value_hash(CWMap, &cv2));
    T("hash(NULL) == 0", cwobj_value_hash(CWInt, NULL) == 0);

    printf("\n - cell 布局 (tag 与值分离)\n");
    CWCell_t cell;
    cell.type_id = CWInt16;
    cell._pad = 0;
    cell.value = va1;
    T("cell tag", cell.type_id == CWInt16);
    T("cell value address",
      cell.value.address == (uint64_t)(uintptr_t)&a1);
    T("cell 内存布局: tag 在前值在后",
      (void*)&cell.value == (void*)((char*)&cell + 8));

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
