/**
 * 独立测试: rt 内置函数 (print/length/contains/to_string/type_of) — ABI v2
 * (异构入口收 type tag + CWValue; tag 由调用点提供, 值不携带元数据)
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwbuiltin.exe test_cwbuiltin.c
 *       ../../rt-src/rt/cwind_builtin.c
 *       ../../rt-src/rt/cwind_builtin_table.c
 *       ../../rt-src/rt/cwind_container.c
 *       ../../rt-src/rt/cwind_object.c
 *       ../../rt-src/rt/cwind_memcenter.c
 */

/* Windows SDK 对 fopen/getenv 的弃用告警, 测试不需要 fopen_s */
#define _CRT_SECURE_NO_WARNINGS 1

#include "../../rt-src/include/rt/cwind_builtin.h"
#include "../../rt-src/include/rt/cwind_builtin_table.h"
#include "../../rt-src/include/object/cwind_container.h"
#include "../../rt-src/include/memory/cwind_memcenter.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

static CWValue_t mk_int(int16_t* storage, int16_t v) {
    *storage = v;
    CWValue_t r;
    cwval_wrap(&r, storage, 2);
    return r;
}

static CWValue_t mk_i32(int32_t* storage, int32_t v) {
    *storage = v;
    CWValue_t r;
    cwval_wrap(&r, storage, 4);
    return r;
}

static CWValue_t mk_str(const char* s) {
    CWValue_t r;
    cwval_wrap(&r, s, (uint64_t)strlen(s));
    return r;
}

static CWCell_t mk_cell(int32_t tid, CWValue_t v) {
    CWCell_t c;
    c.type_id = tid;
    c._pad = 0;
    c.value = v;
    return c;
}

/* 系统临时目录里的可写文件 (MinGW 的 tmpfile 常返回 NULL) */
static FILE* open_tmp_file(const char* name) {
    char path[512];
#if defined(_WIN32)
    const char* tmp = getenv("TEMP");
    if (!tmp) tmp = ".";
    snprintf(path, sizeof(path), "%s\\%s", tmp, name);
#else
    snprintf(path, sizeof(path), "/tmp/%s", name);
#endif
    return fopen(path, "w+");
}

static void close_tmp_file(FILE* f, const char* name) {
    if (!f) return;
    fclose(f);
#if defined(_WIN32)
    char path[512];
    const char* tmp = getenv("TEMP");
    if (!tmp) tmp = ".";
    snprintf(path, sizeof(path), "%s\\%s", tmp, name);
    remove(path);
#else
    char path[512];
    snprintf(path, sizeof(path), "/tmp/%s", name);
    remove(path);
#endif
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    cwmc_init();
    printf("CWindBuiltin tests (ABI v2):\n\n");

    printf(" - cwobj_format (scalars / string)\n");
    int16_t si;
    CWValue_t iobj = mk_int(&si, -1234);
    char buf[256];
    T("int format", cwobj_format(CWInt, &iobj, buf, sizeof(buf))
      && strcmp(buf, "-1234") == 0);
    si = 42;
    T("int format 42", cwobj_format(CWInt, &iobj, buf, sizeof(buf))
      && strcmp(buf, "42") == 0);

    float sf = 3.5f;
    CWValue_t fobj;
    cwval_wrap(&fobj, &sf, 4);
    T("float format", cwobj_format(CWFloat, &fobj, buf, sizeof(buf))
      && strcmp(buf, "3.5") == 0);

    bool sb = true;
    CWValue_t bobj;
    cwval_wrap(&bobj, &sb, 1);
    T("bool format true", cwobj_format(CWBool, &bobj, buf, sizeof(buf))
      && strcmp(buf, "true") == 0);
    sb = false;
    T("bool format false", cwobj_format(CWBool, &bobj, buf, sizeof(buf))
      && strcmp(buf, "false") == 0);

    CWValue_t nobj;
    cwval_none(&nobj);
    T("none format", cwobj_format(CWNone, &nobj, buf, sizeof(buf))
      && strcmp(buf, "None") == 0);

    int8_t si8 = -5;
    CWValue_t i8obj;
    cwval_wrap(&i8obj, &si8, 1);
    T("int8 format", cwobj_format(CWInt8, &i8obj, buf, sizeof(buf))
      && strcmp(buf, "-5") == 0);
    uint8_t su8 = 200;
    CWValue_t u8obj;
    cwval_wrap(&u8obj, &su8, 1);
    T("uint8 format", cwobj_format(CWUInt8, &u8obj, buf, sizeof(buf))
      && strcmp(buf, "200") == 0);
    int16_t si16w = -30000;
    CWValue_t i16wobj;
    cwval_wrap(&i16wobj, &si16w, 2);
    T("int16 format", cwobj_format(CWInt16, &i16wobj, buf, sizeof(buf))
      && strcmp(buf, "-30000") == 0);
    uint16_t su16w = 60000;
    CWValue_t u16wobj;
    cwval_wrap(&u16wobj, &su16w, 2);
    T("uint16 format", cwobj_format(CWUInt16, &u16wobj, buf, sizeof(buf))
      && strcmp(buf, "60000") == 0);
    uint8_t sby = 0xAB;
    CWValue_t byobj;
    cwval_wrap(&byobj, &sby, 1);
    T("byte format", cwobj_format(CWByte, &byobj, buf, sizeof(buf))
      && strcmp(buf, "171") == 0);

    int32_t si32;
    CWValue_t i32obj = mk_i32(&si32, -123456789);
    T("int32 format", cwobj_format(CWInt32, &i32obj, buf, sizeof(buf))
      && strcmp(buf, "-123456789") == 0);
    uint32_t su32 = 4000000000U;
    CWValue_t u32obj;
    cwval_wrap(&u32obj, &su32, 4);
    T("uint32 format", cwobj_format(CWUInt32, &u32obj, buf, sizeof(buf))
      && strcmp(buf, "4000000000") == 0);
    int64_t si64 = -9223372036854775807LL;
    CWValue_t i64obj;
    cwval_wrap(&i64obj, &si64, 8);
    T("int64 format", cwobj_format(CWInt64, &i64obj, buf, sizeof(buf))
      && strcmp(buf, "-9223372036854775807") == 0);
    uint64_t su64 = 18446744073709551615ULL;
    CWValue_t u64obj;
    cwval_wrap(&u64obj, &su64, 8);
    T("uint64 format", cwobj_format(CWUInt64, &u64obj, buf, sizeof(buf))
      && strcmp(buf, "18446744073709551615") == 0);
    double sf64 = 1.25;
    CWValue_t f64obj;
    cwval_wrap(&f64obj, &sf64, 8);
    T("float64 format", cwobj_format(CWFloat64, &f64obj, buf, sizeof(buf))
      && strcmp(buf, "1.25") == 0);

    CWValue_t so1 = mk_str("hello");
    T("string format raw", cwobj_format(CWString, &so1, buf, sizeof(buf))
      && strcmp(buf, "hello") == 0);

    CWValue_t so2 = mk_str("a long string that exceeds tiny");
    char tiny[8];
    T("format too small fails",
      !cwobj_format(CWString, &so2, tiny, sizeof(tiny)));
    T("format NULL fails", !cwobj_format(CWInt, NULL, buf, sizeof(buf)));
    /* 未知类型 tag: 输出 ? (与旧行为一致) */
    T("unknown type tag prints ?",
      cwobj_format(777, &iobj, buf, sizeof(buf)) && strcmp(buf, "?") == 0);

    printf("\n - cwobj_format (containers, 类型走 data 头)\n");
    int16_t es[3];
    CWValue_t vec;
    memset(&vec, 0, sizeof(vec));
    cwvec_init(&vec, CWInt16, 1);
    for (int i = 0; i < 3; i++) {
        CWValue_t r = mk_int(&es[i], (int16_t)(i + 1));
        cwvec_push(&vec, &r);
    }
    T("vector format", cwobj_format(CWVector, &vec, buf, sizeof(buf))
      && strcmp(buf, "[1, 2, 3]") == 0);

    CWValue_t empty_vec;
    memset(&empty_vec, 0, sizeof(empty_vec));
    cwvec_init(&empty_vec, CWInt16, 0);
    T("empty vector format",
      cwobj_format(CWVector, &empty_vec, buf, sizeof(buf))
      && strcmp(buf, "[]") == 0);

    int16_t ts = 7;
    CWValue_t tcells[2];
    tcells[0] = mk_int(&ts, 7);
    tcells[1] = mk_str("x");
    int32_t ttypes[2] = { CWInt, CWString };
    CWValue_t tup;
    memset(&tup, 0, sizeof(tup));
    cwtuple_init(&tup, ttypes, tcells, 2);
    T("tuple format", cwobj_format(CWTuple, &tup, buf, sizeof(buf))
      && strcmp(buf, "(7, x)") == 0);
    CWValue_t empty_tup;
    memset(&empty_tup, 0, sizeof(empty_tup));
    cwtuple_init(&empty_tup, NULL, NULL, 0);
    T("empty tuple format",
      cwobj_format(CWTuple, &empty_tup, buf, sizeof(buf))
      && strcmp(buf, "()") == 0);

    int16_t k1s = 1, k2s = 2;
    CWValue_t map;
    memset(&map, 0, sizeof(map));
    cwmap_init(&map, CWInt, CWString);
    CWValue_t k1 = mk_int(&k1s, 1);
    CWValue_t v1 = mk_str("one");
    cwmap_put(&map, &k1, &v1);
    T("map single format", cwobj_format(CWMap, &map, buf, sizeof(buf))
      && strcmp(buf, "{1: one}") == 0);
    CWValue_t k2 = mk_int(&k2s, 2);
    CWValue_t v2 = mk_str("two");
    cwmap_put(&map, &k2, &v2);
    T("map multi format has both",
      cwobj_format(CWMap, &map, buf, sizeof(buf))
      && strstr(buf, "1: one") != NULL && strstr(buf, "2: two") != NULL);
    CWValue_t empty_map;
    memset(&empty_map, 0, sizeof(empty_map));
    cwmap_init(&empty_map, CWInt, CWString);
    T("empty map format",
      cwobj_format(CWMap, &empty_map, buf, sizeof(buf))
      && strcmp(buf, "{}") == 0);

    int16_t s5 = 5;
    CWValue_t set;
    memset(&set, 0, sizeof(set));
    cwset_init(&set, CWInt);
    CWValue_t e5 = mk_int(&s5, 5);
    cwset_add(&set, &e5);
    T("set single format", cwobj_format(CWSet, &set, buf, sizeof(buf))
      && strcmp(buf, "{5}") == 0);
    CWValue_t empty_set;
    memset(&empty_set, 0, sizeof(empty_set));
    cwset_init(&empty_set, CWInt);
    T("empty set format",
      cwobj_format(CWSet, &empty_set, buf, sizeof(buf))
      && strcmp(buf, "{}") == 0);

    /* 嵌套容器 (独立存储): ABI v2 容器按 data 头元素类型同构,
     * 外层容器元素类型 = CWVector 才能装内层 Vector */
    int16_t ies[2];
    CWValue_t inner;
    memset(&inner, 0, sizeof(inner));
    cwvec_init(&inner, CWInt16, 1);
    CWValue_t r10 = mk_int(&ies[0], 10);
    CWValue_t r20 = mk_int(&ies[1], 20);
    cwvec_push(&inner, &r10);
    cwvec_push(&inner, &r20);
    T("nested vector format",
      cwobj_format(CWVector, &inner, buf, sizeof(buf))
      && strcmp(buf, "[10, 20]") == 0);
    CWValue_t outer;
    memset(&outer, 0, sizeof(outer));
    cwvec_init(&outer, CWVector, 1);
    cwvec_push(&outer, &inner);
    T("nested-in-outer format",
      cwobj_format(CWVector, &outer, buf, sizeof(buf))
      && strcmp(buf, "[[10, 20]]") == 0);

    /* 嵌套 map: 值类型 = CWVector */
    CWValue_t nested_map;
    memset(&nested_map, 0, sizeof(nested_map));
    cwmap_init(&nested_map, CWInt, CWVector);
    cwmap_put(&nested_map, &k1, &inner);
    T("nested map format",
      cwobj_format(CWMap, &nested_map, buf, sizeof(buf))
      && strcmp(buf, "{1: [10, 20]}") == 0);

    /* 自引用容器: 深度上限, 不得无限递归 */
    int16_t sk = 1;
    CWValue_t selfmap;
    memset(&selfmap, 0, sizeof(selfmap));
    cwmap_init(&selfmap, CWInt, CWMap);
    CWValue_t selfk = mk_int(&sk, 1);
    cwmap_put(&selfmap, &selfk, &selfmap);
    T("self-referential map format fails (depth cap)",
      !cwobj_format(CWMap, &selfmap, buf, sizeof(buf)));

    printf("\n - length\n");
    uint64_t len = 0;
    T("string length 5",
      cw_builtin_length(CWString, &so1, &len) && len == 5);
    T("vector length 3",
      cw_builtin_length(CWVector, &vec, &len) && len == 3);
    T("map length 2", cw_builtin_length(CWMap, &map, &len) && len == 2);
    T("set length 1", cw_builtin_length(CWSet, &set, &len) && len == 1);
    T("tuple length 2",
      cw_builtin_length(CWTuple, &tup, &len) && len == 2);
    T("empty containers length 0",
      cw_builtin_length(CWVector, &empty_vec, &len) && len == 0
      && cw_builtin_length(CWMap, &empty_map, &len) && len == 0
      && cw_builtin_length(CWSet, &empty_set, &len) && len == 0
      && cw_builtin_length(CWTuple, &empty_tup, &len) && len == 0);
    T("int length rejected", !cw_builtin_length(CWInt, &iobj, &len));
    T("length NULL rejected", !cw_builtin_length(CWInt, NULL, &len));

    printf("\n - contains\n");
    bool found = false;
    CWValue_t needle = mk_str("ell");
    T("string contains",
      cw_builtin_contains(CWString, &so1, CWString, &needle, &found)
      && found);
    needle = mk_str("xyz");
    T("string not contains",
      cw_builtin_contains(CWString, &so1, CWString, &needle, &found)
      && !found);
    needle = mk_str("");
    T("empty needle contains",
      cw_builtin_contains(CWString, &so1, CWString, &needle, &found)
      && found);

    int16_t p1 = 2, p2 = 99, p3 = 5, p4 = 1;
    CWValue_t probe = mk_int(&p1, 2);
    T("vector contains value (new storage)",
      cw_builtin_contains(CWVector, &vec, CWInt16, &probe, &found)
      && found);
    probe = mk_int(&p2, 99);
    T("vector not contains",
      cw_builtin_contains(CWVector, &vec, CWInt16, &probe, &found)
      && !found);
    probe = mk_int(&p3, 5);
    T("set contains",
      cw_builtin_contains(CWSet, &set, CWInt, &probe, &found) && found);
    probe = mk_int(&p4, 1);
    T("map contains key",
      cw_builtin_contains(CWMap, &map, CWInt, &probe, &found) && found);
    T("int container rejected",
      !cw_builtin_contains(CWInt, &iobj, CWInt, &probe, &found));
    T("empty vector not contains",
      cw_builtin_contains(CWVector, &empty_vec, CWInt16, &probe, &found)
      && !found);
    T("empty map not contains",
      cw_builtin_contains(CWMap, &empty_map, CWInt, &probe, &found)
      && !found);
    T("empty set not contains",
      cw_builtin_contains(CWSet, &empty_set, CWInt, &probe, &found)
      && !found);

    printf("\n - type_of / to_string\n");
    T("type_of Int",
      cw_builtin_type_of(CWInt, buf, sizeof(buf))
      && strcmp(buf, "Int") == 0);
    T("type_of Int16",
      cw_builtin_type_of(CWInt16, buf, sizeof(buf))
      && strcmp(buf, "Int16") == 0);
    T("type_of UInt16",
      cw_builtin_type_of(CWUInt16, buf, sizeof(buf))
      && strcmp(buf, "UInt16") == 0);
    T("type_of Vector",
      cw_builtin_type_of(CWVector, buf, sizeof(buf))
      && strcmp(buf, "Vector") == 0);
    T("type_of String",
      cw_builtin_type_of(CWString, buf, sizeof(buf))
      && strcmp(buf, "String") == 0);
    char tiny3[3];
    T("type_of small buf fails",
      !cw_builtin_type_of(CWInt, tiny3, sizeof(tiny3)));
    T("to_string equals format",
      cw_builtin_to_string(CWVector, &outer, buf, sizeof(buf))
      && strcmp(buf, "[[10, 20]]") == 0);

    printf("\n - concat\n");
    CWValue_t cat_a = mk_str("foo");
    CWValue_t cat_b = mk_str("bar");
    CWValue_t cat;
    T("concat basic", cw_builtin_concat(&cat_a, &cat_b, &cat));
    const char* cat_data = NULL;
    uint64_t cat_len = 0;
    T("concat content",
      cwobj_string_view(&cat, &cat_data, &cat_len)
      && cat_len == 6 && memcmp(cat_data, "foobar", 6) == 0);
    T("concat NUL terminated",
      cat_data != NULL && cat_data[cat_len] == '\0');

    CWValue_t se1 = mk_str("");
    CWValue_t se2 = mk_str("x");
    T("concat empty left",
      cw_builtin_concat(&se1, &se2, &cat)
      && cwobj_string_view(&cat, &cat_data, &cat_len)
      && cat_len == 1 && cat_data[0] == 'x');
    CWValue_t se3 = mk_str("a");
    T("concat empty right",
      cw_builtin_concat(&se3, &se1, &cat)
      && cwobj_string_view(&cat, &cat_data, &cat_len)
      && cat_len == 1 && cat_data[0] == 'a');
    T("concat both empty",
      cw_builtin_concat(&se1, &se1, &cat)
      && cwobj_string_view(&cat, &cat_data, &cat_len)
      && cat_len == 0 && cat_data != NULL && cat_data[0] == '\0');

    CWValue_t mid;
    CWValue_t cat_c = mk_str("c");
    T("concat chained",
      cw_builtin_concat(&cat_a, &cat_b, &mid)
      && cw_builtin_concat(&mid, &cat_c, &cat)
      && cwobj_string_view(&cat, &cat_data, &cat_len)
      && cat_len == 7 && memcmp(cat_data, "foobarc", 7) == 0);

    char big1s[101], big2s[101];
    memset(big1s, 'A', 100);
    big1s[100] = '\0';
    memset(big2s, 'B', 100);
    big2s[100] = '\0';
    CWValue_t big1 = mk_str(big1s);
    CWValue_t big2 = mk_str(big2s);
    T("concat arena growth",
      cw_builtin_concat(&big1, &big2, &cat)
      && cwobj_string_view(&cat, &cat_data, &cat_len)
      && cat_len == 200
      && memcmp(cat_data, big1s, 100) == 0
      && memcmp(cat_data + 100, big2s, 100) == 0);

    T("concat NULL rejected",
      !cw_builtin_concat(NULL, &cat_b, &cat)
      && !cw_builtin_concat(&cat_a, NULL, &cat)
      && !cw_builtin_concat(&cat_a, &cat_b, NULL));

    printf("\n - parse_owned (String -> numeric)\n");
    CWValue_t ps_ok = mk_str("42");
    CWValue_t ps_u8 = mk_str("300");
    CWValue_t ps_i8 = mk_str("999");
    CWValue_t ps_neg = mk_str("-1");
    CWValue_t ps_ovf = mk_str("99999999999999999999999999");
    CWValue_t ps_bad = mk_str("abc");
    CWValue_t ps_f = mk_str("3.5");
    CWValue_t ps_i16w_neg = mk_str("-300");
    CWValue_t ps_u16w_ok = mk_str("40000");
    CWValue_t ps_u16w_ovf = mk_str("65536");
    CWValue_t pout;
    T("parse Int 42",
      cw_builtin_parse_owned(&ps_ok, CWInt, &pout)
      && *(int16_t*)(uintptr_t)pout.address == 42);
    T("parse UInt8 300 fails -> 0",
      !cw_builtin_parse_owned(&ps_u8, CWUInt8, &pout)
      && *(uint8_t*)(uintptr_t)pout.address == 0);
    T("parse Int8 999 fails -> 0",
      !cw_builtin_parse_owned(&ps_i8, CWInt8, &pout)
      && *(int8_t*)(uintptr_t)pout.address == 0);
    T("parse UInt '-1' fails -> 0",
      !cw_builtin_parse_owned(&ps_neg, CWUInt, &pout)
      && *(uint16_t*)(uintptr_t)pout.address == 0);
    T("parse Int '-1' ok",
      cw_builtin_parse_owned(&ps_neg, CWInt, &pout)
      && *(int16_t*)(uintptr_t)pout.address == -1);
    T("parse overflow fails -> 0",
      !cw_builtin_parse_owned(&ps_ovf, CWUInt64, &pout)
      && *(uint64_t*)(uintptr_t)pout.address == 0);
    T("parse junk fails -> 0",
      !cw_builtin_parse_owned(&ps_bad, CWInt, &pout)
      && *(int16_t*)(uintptr_t)pout.address == 0);
    T("parse Float64 3.5 ok",
      cw_builtin_parse_owned(&ps_f, CWFloat64, &pout)
      && *(double*)(uintptr_t)pout.address == 3.5);
    T("parse Int16 -300 ok",
      cw_builtin_parse_owned(&ps_i16w_neg, CWInt16, &pout)
      && *(int16_t*)(uintptr_t)pout.address == -300);
    T("parse Int16 '40000' fails -> 0",
      !cw_builtin_parse_owned(&ps_u16w_ok, CWInt16, &pout)
      && *(int16_t*)(uintptr_t)pout.address == 0);
    T("parse UInt16 40000 ok",
      cw_builtin_parse_owned(&ps_u16w_ok, CWUInt16, &pout)
      && *(uint16_t*)(uintptr_t)pout.address == 40000);
    T("parse UInt16 65536 fails -> 0",
      !cw_builtin_parse_owned(&ps_u16w_ovf, CWUInt16, &pout)
      && *(uint16_t*)(uintptr_t)pout.address == 0);
    T("parse UInt16 '-1' fails -> 0",
      !cw_builtin_parse_owned(&ps_neg, CWUInt16, &pout)
      && *(uint16_t*)(uintptr_t)pout.address == 0);
    T("parse NULL rejected",
      !cw_builtin_parse_owned(NULL, CWInt, &pout)
      && !cw_builtin_parse_owned(&ps_ok, CWInt, NULL));

    printf("\n - format (stack-machine template scan, CWCell 参数)\n");
    CWValue_t fmt1 = mk_str("v={}, b={}");
    CWValue_t fmt2 = mk_str("a={} and \\{lit\\}!");
    CWValue_t fmt3 = mk_str("{}:{}");
    CWValue_t fmt4 = mk_str("plain");
    CWValue_t fmt5 = mk_str("x\\ny");
    CWValue_t fmt6 = mk_str("a={} b={}");
    CWValue_t fmt7 = mk_str("a={name}");
    CWValue_t fmt_out;
    sb = true;
    CWCell_t fmt_args[2];
    fmt_args[0] = mk_cell(CWInt, iobj);
    fmt_args[1] = mk_cell(CWBool, bobj);
    T("format positional",
      cw_builtin_format(&fmt1, fmt_args, 2, &fmt_out)
      && cwobj_string_view(&fmt_out, &cat_data, &cat_len)
      && cat_len == 12
      && memcmp(cat_data, "v=42, b=true", 12) == 0);
    fmt_args[0] = mk_cell(CWString, so1);
    fmt_args[1] = mk_cell(CWInt, iobj);
    T("format string+int",
      cw_builtin_format(&fmt3, fmt_args, 2, &fmt_out)
      && cwobj_string_view(&fmt_out, &cat_data, &cat_len)
      && cat_len == 8
      && memcmp(cat_data, "hello:42", 8) == 0);
    fmt_args[0] = mk_cell(CWString, so1);
    T("format escaped braces",
      cw_builtin_format(&fmt2, fmt_args, 1, &fmt_out)
      && cwobj_string_view(&fmt_out, &cat_data, &cat_len)
      && cat_len == 18
      && memcmp(cat_data, "a=hello and {lit}!", 18) == 0);
    T("format no placeholders",
      cw_builtin_format(&fmt4, NULL, 0, &fmt_out)
      && cwobj_string_view(&fmt_out, &cat_data, &cat_len)
      && cat_len == 5 && memcmp(cat_data, "plain", 5) == 0);
    T("format escape newline",
      cw_builtin_format(&fmt5, NULL, 0, &fmt_out)
      && cwobj_string_view(&fmt_out, &cat_data, &cat_len)
      && cat_len == 3 && cat_data[0] == 'x' && cat_data[1] == '\n'
      && cat_data[2] == 'y');
    T("format NUL terminated",
      cat_data != NULL && cat_data[cat_len] == '\0');
    T("format missing args rejected",
      !cw_builtin_format(&fmt1, NULL, 0, &fmt_out));
    T("format too few args rejected",
      !cw_builtin_format(&fmt6, fmt_args, 1, &fmt_out));
    T("format named placeholder rejected",
      !cw_builtin_format(&fmt7, fmt_args, 1, &fmt_out));
    T("format failure leaves empty string",
      cwobj_string_view(&fmt_out, &cat_data, &cat_len)
      && cat_len == 0 && cat_data[0] == '\0');
    T("format NULL rejected",
      !cw_builtin_format(NULL, fmt_args, 1, &fmt_out)
      && !cw_builtin_format(&fmt1, fmt_args, 1, NULL));
    /* 自引用容器: cwobj_format 因深度上限失败, 缓冲翻倍必须有限界 */
    CWCell_t selfarg = mk_cell(CWMap, selfmap);
    T("format arg self-referential rejected",
      !cw_builtin_format(&fmt1, &selfarg, 1, &fmt_out));

    printf("\n - type_of_owned\n");
    CWValue_t to_obj;
    T("type_of_owned Int",
      cw_builtin_type_of_owned(CWInt, &iobj, &to_obj)
      && cwobj_string_view(&to_obj, &cat_data, &cat_len)
      && cat_len == 3 && memcmp(cat_data, "Int", 3) == 0);
    T("type_of_owned String",
      cw_builtin_type_of_owned(CWString, &so1, &to_obj)
      && cwobj_string_view(&to_obj, &cat_data, &cat_len)
      && cat_len == 6 && memcmp(cat_data, "String", 6) == 0);
    /* type_of 只需要 tag (值指针仅为签名统一, 不解引用) */
    T("type_of_owned NULL out rejected",
      !cw_builtin_type_of_owned(CWInt, &iobj, NULL));

    printf("\n - print_to (tag + 值)\n");
    FILE* f = open_tmp_file("cwbuiltin_print_test.txt");
    T("tmp file open", f != NULL);
    if (f) {
        T("print string", cw_builtin_print_to(f, CWString, &so1));
        T("print int", cw_builtin_print_to(f, CWInt, &iobj));
        T("print vector", cw_builtin_print_to(f, CWVector, &empty_vec));
        rewind(f);
        char line[64];
        T("string line", fgets(line, sizeof(line), f) != NULL
          && strcmp(line, "hello\n") == 0);
        T("int line", fgets(line, sizeof(line), f) != NULL
          && strcmp(line, "42\n") == 0);
        T("vector line", fgets(line, sizeof(line), f) != NULL
          && strcmp(line, "[]\n") == 0);
        close_tmp_file(f, "cwbuiltin_print_test.txt");
    }
    /* 重定向/文件路径: 输出必须是原样 UTF-8 字节 (不经过代码页转换) */
    char up[512];
#if defined(_WIN32)
    const char* utmp = getenv("TEMP");
    if (!utmp) utmp = ".";
    snprintf(up, sizeof(up), "%s\\%s", utmp, "cwbuiltin_print_utf8.txt");
#else
    snprintf(up, sizeof(up), "/tmp/%s", "cwbuiltin_print_utf8.txt");
#endif
    FILE* uf = fopen(up, "wb+");
    T("utf8 tmp file open", uf != NULL);
    if (uf) {
        static const char kZh[] = "中文 UTF-8";
        CWValue_t zh = mk_str(kZh);
        T("print utf8 string", cw_builtin_print_to(uf, CWString, &zh));
        rewind(uf);
        char got[64];
        const size_t got_n = fread(got, 1, sizeof(got), uf);
        const size_t want_n = sizeof(kZh) - 1 + 1; /* 内容 + \n */
        T("utf8 bytes exact", got_n == want_n
          && memcmp(got, kZh, sizeof(kZh) - 1) == 0
          && got[want_n - 1] == '\n');
        close_tmp_file(uf, "cwbuiltin_print_utf8.txt");
    }

    printf("\n - readline\n");
    char rp[512];
#if defined(_WIN32)
    const char* rtmp = getenv("TEMP");
    if (!rtmp) rtmp = ".";
    snprintf(rp, sizeof(rp), "%s\\%s", rtmp,
             "cwbuiltin_readline_input.txt");
#else
    snprintf(rp, sizeof(rp), "/tmp/%s", "cwbuiltin_readline_input.txt");
#endif
    FILE* rfin = fopen(rp, "w");
    T("readline input file open", rfin != NULL);
    if (rfin) {
        fputs("hello-line\n", rfin);
        fclose(rfin);
    }
    T("readline stdin redirect", freopen(rp, "r", stdin) != NULL);
    CWValue_t rl;
    T("readline line",
      cw_builtin_readline(&rl)
      && cwobj_string_view(&rl, &cat_data, &cat_len)
      && cat_len == 10 && memcmp(cat_data, "hello-line", 10) == 0);
    /* EOF: 返回 true 且 out 是空串值 (Rust read_line 语义).
     * 旧契约 (EOF -> false 且不动 out) 会把未初始化栈内存以野地址
     * 流进 print, 实测 _write 扫野指针崩溃. */
    T("readline EOF yields empty string",
      cw_builtin_readline(&rl)
      && cwobj_string_view(&rl, &cat_data, &cat_len)
      && cat_len == 0);
    remove(rp);

    printf("\n - builtin symbol table\n");
    /* 5 模块级 + 21 类型方法 + gc_collect (stash 后加入) +
     * gc_alloc_bytes/gc_live_bytes/gc_pause_ns/gc_enable (todo-35 投影) */
    T("table non-empty", cw_builtin_count() == 30);
    T("entry(0) print", cw_builtin_entry(0) != NULL
      && strcmp(cw_builtin_entry(0)->name, "print") == 0
      && strcmp(cw_builtin_entry(0)->symbol, "cw_builtin_print") == 0);
    T("module print symbol",
      strcmp(cw_builtin_symbol(NULL, "print"), "cw_builtin_print") == 0);
    T("module type_of symbol",
      strcmp(cw_builtin_symbol(NULL, "type_of"), "cw_builtin_type_of") == 0);
    T("display to_string symbol",
      strcmp(cw_builtin_symbol(NULL, "to_string"),
             "cw_builtin_to_string") == 0);
    T("format not registered (no silent fallback)",
      cw_builtin_symbol(NULL, "format") == NULL);
    T("vector push_back symbol",
      strcmp(cw_builtin_symbol("Vector", "push_back"), "cwvec_push") == 0);
    T("map get symbol",
      strcmp(cw_builtin_symbol("Map", "get"), "cwmap_get") == 0);
    T("vector length symbol",
      strcmp(cw_builtin_symbol("Vector", "length"),
             "cw_builtin_length") == 0);
    T("string contains symbol",
      strcmp(cw_builtin_symbol("String", "contains"),
             "cw_builtin_contains") == 0);
    T("unregistered matches -> NULL",
      cw_builtin_symbol("String", "matches") == NULL);
    T("unknown type -> NULL",
      cw_builtin_symbol("Foo", "length") == NULL);
    T("module length -> NULL",
      cw_builtin_symbol(NULL, "length") == NULL);
    T("NULL name -> NULL", cw_builtin_symbol(NULL, NULL) == NULL);

    printf("\n - leak check\n");
    /* 进程期值 arena (String/枚举/容器标量元素) 的段也来自内存中心,
     * 段进程期存活、不随容器归还; 泄漏检查只要求容器簿记内存全部归还,
     * 剩余存活分配恰好是 arena 段数。 */
    cwvec_destroy(&vec);
    cwvec_destroy(&inner);
    cwvec_destroy(&outer);
    cwvec_destroy(&empty_vec);
    cwtuple_destroy(&tup);
    cwtuple_destroy(&empty_tup);
    cwmap_destroy(&map);
    cwmap_destroy(&selfmap);
    cwmap_destroy(&nested_map);
    cwmap_destroy(&empty_map);
    cwset_destroy(&set);
    cwset_destroy(&empty_set);
    CWMemCenterStats_t ms;
    cwmc_stats(&ms);
    T("all container memory returned",
      ms.active_allocs == cwrt_arena_blocks());
    cwmc_shutdown();

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
