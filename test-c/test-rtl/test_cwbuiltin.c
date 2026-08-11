/**
 * 独立测试: rt 内置函数 (print/length/contains/to_string/type_of) + 符号表
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

static CWindIntObject_t mk_int(int16_t* storage, int16_t v) {
    CWindIntObject_t r;
    cwobj_int_new(&r, storage, v);
    return r;
}

static CWindStringObject_t mk_str(char* storage, const char* s) {
    CWindStringObject_t r;
    cwobj_string_new(&r, storage, s, (uint64_t)strlen(s));
    return r;
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
    printf("CWindBuiltin tests:\n\n");

    printf(" - cwobj_format (scalars / string)\n");
    int16_t si = -1234;
    CWindIntObject_t iobj = mk_int(&si, -1234);
    char buf[256];
    T("int format", cwobj_format(&iobj.head, buf, sizeof(buf))
      && strcmp(buf, "-1234") == 0);
    cwobj_set_i16(&iobj, 42);
    T("int format 42", cwobj_format(&iobj.head, buf, sizeof(buf))
      && strcmp(buf, "42") == 0);

    float sf = 3.5f;
    CWindFloatObject_t fobj;
    cwobj_float_new(&fobj, &sf, 3.5f);
    T("float format", cwobj_format(&fobj.head, buf, sizeof(buf))
      && strcmp(buf, "3.5") == 0);

    bool sb = true;
    CWindBoolObject_t bobj;
    cwobj_bool_new(&bobj, &sb, true);
    T("bool format true", cwobj_format(&bobj.head, buf, sizeof(buf))
      && strcmp(buf, "true") == 0);
    sb = false;
    cwobj_bool_new(&bobj, &sb, false);
    T("bool format false", cwobj_format(&bobj.head, buf, sizeof(buf))
      && strcmp(buf, "false") == 0);

    CWindNoneObject_t nobj;
    cwobj_none_new(&nobj);
    T("none format", cwobj_format(&nobj.head, buf, sizeof(buf))
      && strcmp(buf, "None") == 0);

    char hs[32];
    CWindStringObject_t so1 = mk_str(hs, "hello");
    T("string format raw", cwobj_format(&so1.head, buf, sizeof(buf))
      && strcmp(buf, "hello") == 0);

    char long_stor[64];
    CWindStringObject_t so2 = mk_str(long_stor, "a long string that exceeds tiny");
    char tiny[8];
    T("format too small fails",
      !cwobj_format(&so2.head, tiny, sizeof(tiny)));
    T("format NULL fails", !cwobj_format(NULL, buf, sizeof(buf)));

    printf("\n - cwobj_format (containers)\n");
    int16_t es[3];
    CWindVectorObject_t vec;
    cwobj_container_init(&vec.head, CWVector);
    cwvec_init(&vec, 1);
    for (int i = 0; i < 3; i++) {
        CWindIntObject_t r = mk_int(&es[i], (int16_t)(i + 1));
        cwvec_push(&vec, &r);
    }
    T("vector format", cwobj_format(&vec.head, buf, sizeof(buf))
      && strcmp(buf, "[1, 2, 3]") == 0);

    CWindVectorObject_t empty_vec;
    cwobj_container_init(&empty_vec.head, CWVector);
    cwvec_init(&empty_vec, 0);
    T("empty vector format", cwobj_format(&empty_vec.head, buf, sizeof(buf))
      && strcmp(buf, "[]") == 0);

    int16_t ts = 7;
    char tb[16];
    unsigned char trec[2 * CWIND_OBJECT_RECORD_SIZE];
    CWindIntObject_t t1 = mk_int(&ts, 7);
    CWindStringObject_t t2 = mk_str(tb, "x");
    memcpy(trec, &t1, CWIND_OBJECT_RECORD_SIZE);
    memcpy(trec + CWIND_OBJECT_RECORD_SIZE, &t2, CWIND_OBJECT_RECORD_SIZE);
    CWindTupleObject_t tup;
    cwobj_container_init(&tup.head, CWTuple);
    cwtuple_init(&tup, trec, 2);
    T("tuple format", cwobj_format(&tup.head, buf, sizeof(buf))
      && strcmp(buf, "(7, x)") == 0);

    int16_t k1s = 1, k2s = 2;
    char v1s[16], v2s[16];
    CWindMapObject_t map;
    cwobj_container_init(&map.head, CWMap);
    cwmap_init(&map);
    CWindIntObject_t k1 = mk_int(&k1s, 1);
    CWindStringObject_t v1 = mk_str(v1s, "one");
    cwmap_put(&map, &k1, &v1);
    T("map single format", cwobj_format(&map.head, buf, sizeof(buf))
      && strcmp(buf, "{1: one}") == 0);
    CWindIntObject_t k2 = mk_int(&k2s, 2);
    CWindStringObject_t v2 = mk_str(v2s, "two");
    cwmap_put(&map, &k2, &v2);
    T("map multi format has both",
      cwobj_format(&map.head, buf, sizeof(buf))
      && strstr(buf, "1: one") != NULL && strstr(buf, "2: two") != NULL);

    int16_t s5 = 5;
    CWindSetObject_t set;
    cwobj_container_init(&set.head, CWSet);
    cwset_init(&set);
    CWindIntObject_t e5 = mk_int(&s5, 5);
    cwset_add(&set, &e5);
    T("set single format", cwobj_format(&set.head, buf, sizeof(buf))
      && strcmp(buf, "{5}") == 0);

    /* 嵌套容器 (独立存储) */
    int16_t ies[2];
    CWindVectorObject_t inner;
    cwobj_container_init(&inner.head, CWVector);
    cwvec_init(&inner, 1);
    CWindIntObject_t r10 = mk_int(&ies[0], 10);
    CWindIntObject_t r20 = mk_int(&ies[1], 20);
    cwvec_push(&inner, &r10);
    cwvec_push(&inner, &r20);
    cwvec_push(&vec, &inner.head);
    T("nested vector format", cwobj_format(&vec.head, buf, sizeof(buf))
      && strcmp(buf, "[1, 2, 3, [10, 20]]") == 0);

    /* 自引用容器: 深度上限, 不得无限递归 */
    int16_t sk = 1;
    CWindMapObject_t selfmap;
    cwobj_container_init(&selfmap.head, CWMap);
    cwmap_init(&selfmap);
    CWindIntObject_t selfk = mk_int(&sk, 1);
    cwmap_put(&selfmap, &selfk, &selfmap.head);
    T("self-referential map format fails (depth cap)",
      !cwobj_format(&selfmap.head, buf, sizeof(buf)));

    printf("\n - length\n");
    uint64_t len = 0;
    T("string length 5",
      cw_builtin_length(&so1.head, &len) && len == 5);
    T("vector length 4",
      cw_builtin_length(&vec.head, &len) && len == 4);
    T("map length 2",
      cw_builtin_length(&map.head, &len) && len == 2);
    T("set length 1",
      cw_builtin_length(&set.head, &len) && len == 1);
    T("tuple length 2",
      cw_builtin_length(&tup.head, &len) && len == 2);
    T("int length rejected", !cw_builtin_length(&iobj.head, &len));
    T("length NULL rejected", !cw_builtin_length(NULL, &len));

    printf("\n - contains\n");
    char n1[16], n2[16], n3[16];
    bool found = false;
    CWindStringObject_t needle = mk_str(n1, "ell");
    T("string contains",
      cw_builtin_contains(&so1.head, &needle.head, &found) && found);
    needle = mk_str(n2, "xyz");
    T("string not contains",
      cw_builtin_contains(&so1.head, &needle.head, &found) && !found);
    needle = mk_str(n3, "");
    T("empty needle contains",
      cw_builtin_contains(&so1.head, &needle.head, &found) && found);

    int16_t p1 = 2, p2 = 99, p3 = 5, p4 = 1;
    CWindIntObject_t probe = mk_int(&p1, 2);
    T("vector contains value (new storage)",
      cw_builtin_contains(&vec.head, &probe.head, &found) && found);
    probe = mk_int(&p2, 99);
    T("vector not contains",
      cw_builtin_contains(&vec.head, &probe.head, &found) && !found);
    probe = mk_int(&p3, 5);
    T("set contains",
      cw_builtin_contains(&set.head, &probe.head, &found) && found);
    probe = mk_int(&p4, 1);
    T("map contains key",
      cw_builtin_contains(&map.head, &probe.head, &found) && found);
    T("int container rejected",
      !cw_builtin_contains(&iobj.head, &probe.head, &found));

    printf("\n - type_of / to_string\n");
    T("type_of Int",
      cw_builtin_type_of(&iobj.head, buf, sizeof(buf))
      && strcmp(buf, "Int") == 0);
    T("type_of Vector",
      cw_builtin_type_of(&vec.head, buf, sizeof(buf))
      && strcmp(buf, "Vector") == 0);
    T("type_of String",
      cw_builtin_type_of(&so1.head, buf, sizeof(buf))
      && strcmp(buf, "String") == 0);
    char tiny3[3];
    T("type_of small buf fails",
      !cw_builtin_type_of(&iobj.head, tiny3, sizeof(tiny3)));
    T("to_string equals format",
      cw_builtin_to_string(&vec.head, buf, sizeof(buf))
      && strcmp(buf, "[1, 2, 3, [10, 20]]") == 0);

    printf("\n - print_to\n");
    FILE* f = open_tmp_file("cwbuiltin_print_test.txt");
    T("tmp file open", f != NULL);
    if (f) {
        T("print string", cw_builtin_print_to(f, &so1.head));
        T("print int", cw_builtin_print_to(f, &iobj.head));
        rewind(f);
        char line[64];
        T("string line", fgets(line, sizeof(line), f) != NULL
          && strcmp(line, "hello\n") == 0);
        T("int line", fgets(line, sizeof(line), f) != NULL
          && strcmp(line, "42\n") == 0);
        close_tmp_file(f, "cwbuiltin_print_test.txt");
    }

    printf("\n - builtin symbol table\n");
    T("table non-empty", cw_builtin_count() == 26);
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
    cwvec_destroy(&vec);
    cwvec_destroy(&inner);
    cwvec_destroy(&empty_vec);
    cwtuple_destroy(&tup);
    cwmap_destroy(&map);
    cwmap_destroy(&selfmap);
    cwset_destroy(&set);
    CWMemCenterStats_t ms;
    cwmc_stats(&ms);
    T("all container memory returned", ms.active_allocs == 0);
    cwmc_shutdown();

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
