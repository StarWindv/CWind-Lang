/**
 * 独立测试: CWind 容器对象 (Tuple / Vector / Map / Set) + 值语义比较
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwcontainer.exe test_cwcontainer.c
 *       ../../rt-src/rt/cwind_container.c
 *       ../../rt-src/rt/cwind_object.c
 *       ../../rt-src/rt/cwind_memcenter.c
 */

#include "../../rt-src/include/object/cwind_container.h"
#include "../../rt-src/include/memory/cwind_memcenter.h"

#include <stdint.h>
#include <stdio.h>
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

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    cwmc_init();
    CWMemCenterStats_t ms;
    cwmc_stats(&ms);
    const size_t base = ms.active_allocs;
    printf("CWindContainer tests:\n\n");

    printf(" - cwobj_equal / cwobj_hash\n");
    int16_t sa = 42, sb = 42, sc = 43;
    CWindIntObject_t ia = mk_int(&sa, 42);
    CWindIntObject_t ib = mk_int(&sb, 42);
    CWindIntObject_t ic = mk_int(&sc, 43);
    T("equal: same value different storage",
      cwobj_equal(&ia.head, &ib.head));
    T("hash: same value same hash", cwobj_hash(&ia.head) == cwobj_hash(&ib.head));
    T("equal: different value", !cwobj_equal(&ia.head, &ic.head));
    T("equal: NULL safety",
      cwobj_equal(NULL, NULL) && !cwobj_equal(NULL, &ia.head));
    T("equal: cross type", !cwobj_equal(&ia.head, (const CWindObject_t*)&ic));

    char buf1[16] = {0}, buf2[16] = {0}, buf3[16] = {0};
    CWindStringObject_t s1 = mk_str(buf1, "hello");
    CWindStringObject_t s2 = mk_str(buf2, "hello");
    CWindStringObject_t s3 = mk_str(buf3, "world");
    T("equal: string content",
      cwobj_equal(&s1.head, &s2.head));
    T("hash: string content", cwobj_hash(&s1.head) == cwobj_hash(&s2.head));
    T("equal: string differs", !cwobj_equal(&s1.head, &s3.head));

    CWindNoneObject_t n1, n2;
    cwobj_none_new(&n1);
    cwobj_none_new(&n2);
    T("equal: none == none", cwobj_equal(&n1.head, &n2.head));

    printf("\n - Vector\n");
    CWindVectorObject_t vec;
    cwobj_container_init(&vec.head, CWVector);
    T("vec_init(reserve=4)", cwvec_init(&vec, 4));
    T("vec init cursor == 4", vec.handle.cursor == 4);
    T("vec size 0", cwvec_size(&vec) == 0);
    T("vec length 0", vec.handle.length == 0);

    int16_t stor[10];
    for (int i = 0; i < 10; i++) {
        CWindIntObject_t r = mk_int(&stor[i], (int16_t)(i * 7));
        T("vec push", cwvec_push(&vec, &r));
    }
    T("vec size 10", cwvec_size(&vec) == 10);
    T("vec length 10", vec.handle.length == 10);
    T("vec grew (4 -> 8 -> 16)", vec.handle.cursor >= 16);

    int intact = 1;
    for (int i = 0; i < 10 && intact; i++) {
        CWindIntObject_t r;
        int16_t v = 0;
        intact = cwvec_at(&vec, (size_t)i, &r)
              && cwobj_get_i16(&r, &v) && v == (int16_t)(i * 7);
    }
    T("vec at: all intact", intact);

    CWindIntObject_t rnew = mk_int(&sc, -1);
    T("vec set(3)", cwvec_set(&vec, 3, &rnew));
    CWindIntObject_t r3;
    int16_t v3 = 0;
    T("vec set visible",
      cwvec_at(&vec, 3, &r3) && cwobj_get_i16(&r3, &v3) && v3 == -1);
    T("vec at out of range", !cwvec_at(&vec, 99, &r3));
    T("vec set out of range", !cwvec_set(&vec, 99, &rnew));

    CWindIntObject_t popped;
    T("vec pop", cwvec_pop(&vec, &popped));
    int16_t vp = 0;
    T("vec pop value", cwobj_get_i16(&popped, &vp) && vp == 9 * 7);
    T("vec size 9", cwvec_size(&vec) == 9);

    cwvec_clear(&vec);
    T("vec clear", cwvec_size(&vec) == 0 && vec.handle.length == 0);
    T("vec push after clear", cwvec_push(&vec, &ia));

    int16_t big_stor[2000];
    for (int i = 0; i < 2000; i++) {
        CWindIntObject_t r = mk_int(&big_stor[i], (int16_t)i);
        if (!cwvec_push(&vec, &r)) { T("vec push 2000", 0); break; }
    }
    T("vec size 2001", cwvec_size(&vec) == 2001);
    intact = 1;
    for (int i = 1; i <= 2000 && intact; i++) {
        CWindIntObject_t r;
        int16_t v = 0;
        intact = cwvec_at(&vec, (size_t)i, &r)
              && cwobj_get_i16(&r, &v) && v == (int16_t)(i - 1);
    }
    T("vec 2000 elements intact", intact);

    CWindIntObject_t notvec = ia;
    T("vec rejects scalar object",
      !cwvec_push((CWindVectorObject_t*)&notvec, &ia));
    T("vec NULL rejects", !cwvec_push(NULL, &ia));

    printf("\n - Tuple\n");
    char tbuf[8];
    CWindIntObject_t t1 = mk_int(&sa, 1);
    CWindFloatObject_t t2;
    float fs = 2.5f;
    cwobj_float_new(&t2, &fs, 2.5f);
    CWindStringObject_t t3 = mk_str(tbuf, "tuple");
    unsigned char records[3 * CWIND_OBJECT_RECORD_SIZE];
    memcpy(records, &t1, CWIND_OBJECT_RECORD_SIZE);
    memcpy(records + CWIND_OBJECT_RECORD_SIZE, &t2,
           CWIND_OBJECT_RECORD_SIZE);
    memcpy(records + 2 * CWIND_OBJECT_RECORD_SIZE, &t3,
           CWIND_OBJECT_RECORD_SIZE);
    CWindTupleObject_t tup;
    cwobj_container_init(&tup.head, CWTuple);
    T("tuple_init(3)", cwtuple_init(&tup, records, 3));
    T("tuple size 3", cwtuple_size(&tup) == 3);
    T("tuple length 3", tup.handle.length == 3);
    CWindIntObject_t gr;
    int16_t gv = 0;
    T("tuple at(0) int",
      cwtuple_at(&tup, 0, &gr) && cwobj_get_i16(&gr, &gv) && gv == 1);
    CWindStringObject_t gs;
    const char* gd = NULL;
    uint64_t gl = 0;
    T("tuple at(2) string",
      cwtuple_at(&tup, 2, &gs)
      && cwobj_string_get(&gs, &gd, &gl) && gl == 5
      && memcmp(gd, "tuple", 5) == 0);
    T("tuple at out of range", !cwtuple_at(&tup, 3, &gr));
    CWindTupleObject_t empty;
    cwobj_container_init(&empty.head, CWTuple);
    T("tuple_init(0)", cwtuple_init(&empty, NULL, 0) && cwtuple_size(&empty) == 0);
    T("tuple records NULL with count", !cwtuple_init(&empty, NULL, 1));

    printf("\n - Map\n");
    CWindMapObject_t map;
    cwobj_container_init(&map.head, CWMap);
    T("map_init", cwmap_init(&map));
    T("map size 0", cwmap_size(&map) == 0);

    int16_t ka = 7, kb = 7, kc = 8;
    char va[8] = {0}, vb[8] = {0};
    CWindIntObject_t key_a = mk_int(&ka, 7);
    CWindIntObject_t key_b = mk_int(&kb, 7);  /* 同值不同存储 */
    CWindIntObject_t key_c = mk_int(&kc, 8);
    CWindStringObject_t val_a = mk_str(va, "one");
    CWindStringObject_t val_b = mk_str(vb, "two");
    T("map put", cwmap_put(&map, &key_a, &val_a));
    T("map put same key (value compare)",
      cwmap_put(&map, &key_b, &val_b));
    T("map size 1 after replace", cwmap_size(&map) == 1);
    T("map length 1", map.handle.length == 1);
    CWindStringObject_t gotv;
    const char* gs2 = NULL;
    uint64_t gl2 = 0;
    T("map get existing",
      cwmap_get(&map, &key_a, &gotv)
      && cwobj_string_get(&gotv, &gs2, &gl2) && gl2 == 3
      && memcmp(gs2, "two", 3) == 0);
    T("map get missing", !cwmap_get(&map, &key_c, &gotv));
    T("map get existence (NULL out)", cwmap_get(&map, &key_a, NULL));
    T("map remove", cwmap_remove(&map, &key_b));
    T("map size 0 after remove", cwmap_size(&map) == 0);
    T("map remove missing", !cwmap_remove(&map, &key_b));

    int16_t mkeys[100], mvals[100];
    CWindIntObject_t kr, vr;
    for (int i = 0; i < 100; i++) {
        kr = mk_int(&mkeys[i], (int16_t)i);
        vr = mk_int(&mvals[i], (int16_t)(i * 3));
        if (!cwmap_put(&map, &kr, &vr)) { T("map put 100", 0); break; }
    }
    T("map size 100", cwmap_size(&map) == 100);
    intact = 1;
    for (int i = 0; i < 100 && intact; i++) {
        CWindIntObject_t kk = mk_int(&sa, (int16_t)i); /* 新存储, 值查找 */
        CWindIntObject_t vv;
        int16_t v = 0;
        intact = cwmap_get(&map, &kk, &vv) && cwobj_get_i16(&vv, &v)
              && v == (int16_t)(i * 3);
    }
    T("map 100 entries intact (value keys)", intact);
    for (int i = 0; i < 100; i += 2) {
        CWindIntObject_t kk = mk_int(&sa, (int16_t)i);
        cwmap_remove(&map, &kk);
    }
    T("map size 50 after remove evens", cwmap_size(&map) == 50);
    CWindIntObject_t kk = mk_int(&sa, 1);
    T("map odd survives", cwmap_get(&map, &kk, NULL));
    kk = mk_int(&sa, 0);
    T("map even removed", !cwmap_get(&map, &kk, NULL));

    printf("\n - Set\n");
    CWindSetObject_t set;
    cwobj_container_init(&set.head, CWSet);
    T("set_init", cwset_init(&set));
    T("set size 0", cwset_size(&set) == 0);
    int16_t sa2 = 5, sb2 = 5;
    CWindIntObject_t i1 = mk_int(&sa2, 5);
    CWindIntObject_t i2 = mk_int(&sb2, 5);
    T("set add", cwset_add(&set, &i1));
    T("set add duplicate (value compare)", cwset_add(&set, &i2));
    T("set size 1", cwset_size(&set) == 1);
    T("set contains", cwset_contains(&set, &i1));
    T("set contains (new storage)", cwset_contains(&set, &i2));
    T("set remove", cwset_remove(&set, &i2));
    T("set size 0", cwset_size(&set) == 0);
    T("set remove missing", !cwset_remove(&set, &i1));

    char sset[64];
    for (int i = 0; i < 50; i++) {
        CWindStringObject_t s = mk_str(sset, "item");
        if (!cwset_add(&set, &s)) { T("set add strings", 0); break; }
    }
    T("set dedups strings", cwset_size(&set) == 1);
    CWindStringObject_t probe = mk_str(sset, "item");
    T("set contains string", cwset_contains(&set, &probe));
    cwset_clear(&set);
    T("set clear", cwset_size(&set) == 0);

    int16_t sstor[100];
    for (int i = 0; i < 100; i++) {
        CWindIntObject_t r = mk_int(&sstor[i], (int16_t)i);
        if (!cwset_add(&set, &r)) { T("set add 100", 0); break; }
    }
    T("set size 100", cwset_size(&set) == 100);
    CWindIntObject_t probe_i = mk_int(&sa, 99);
    T("set contains 99", cwset_contains(&set, &probe_i));
    probe_i = mk_int(&sa, 100);
    T("set missing 100", !cwset_contains(&set, &probe_i));

    printf("\n - iteration (for-in 基础)\n");
    int16_t istor[5];
    CWindVectorObject_t iv;
    cwobj_container_init(&iv.head, CWVector);
    cwvec_init(&iv, 1);
    for (int i = 0; i < 5; i++) {
        CWindIntObject_t r = mk_int(&istor[i], (int16_t)(i + 100));
        cwvec_push(&iv, &r);
    }
    size_t visited = 0;
    long long sum = 0;
    for (CWindVectorIter_t it = cwvec_iter_begin(&iv);
         cwvec_iter_valid(&it); cwvec_iter_next(&it)) {
        CWindIntObject_t r;
        int16_t v = 0;
        if (cwvec_iter_value(&it, &r) && cwobj_get_i16(&r, &v)) {
            visited++;
            sum += v;
        }
    }
    T("vector iteration count", visited == 5);
    T("vector iteration sum", sum == 100 + 101 + 102 + 103 + 104);
    cwvec_destroy(&iv);

    visited = 0;
    for (CWindTupleIter_t it = cwtuple_iter_begin(&tup);
         cwtuple_iter_valid(&it); cwtuple_iter_next(&it)) {
        visited++;
    }
    T("tuple iteration count", visited == 3);

    visited = 0;
    int odd_ok = 1;
    for (CWindMapIter_t it = cwmap_iter_begin(&map);
         cwmap_iter_valid(&it); cwmap_iter_next(&it)) {
        CWindIntObject_t k, vv;
        int16_t kv = 0, v = 0;
        if (cwmap_iter_key(&it, &k) && cwobj_get_i16(&k, &kv)
            && cwmap_iter_value(&it, &vv) && cwobj_get_i16(&vv, &v)) {
            visited++;
            if (kv % 2 == 0 || v != kv * 3) odd_ok = 0;
        }
    }
    T("map iteration count == 50", visited == 50);
    T("map iteration keys odd / values match", odd_ok);

    visited = 0;
    int set_ok = 1;
    for (CWindSetIter_t it = cwset_iter_begin(&set);
         cwset_iter_valid(&it); cwset_iter_next(&it)) {
        CWindIntObject_t item;
        int16_t v = 0;
        if (cwset_iter_item(&it, &item) && cwobj_get_i16(&item, &v)) {
            visited++;
            CWindIntObject_t probe2 = mk_int(&sa, v);
            if (!cwset_contains(&set, &probe2)) set_ok = 0;
        }
    }
    T("set iteration count == 100", visited == 100);
    T("set iteration items all contained", set_ok);

    CWindVectorObject_t ev2;
    cwobj_container_init(&ev2.head, CWVector);
    cwvec_init(&ev2, 4);
    CWindVectorIter_t vit = cwvec_iter_begin(&ev2);
    T("empty vector iteration invalid", !cwvec_iter_valid(&vit));
    cwvec_destroy(&ev2);
    CWindTupleIter_t tit = cwtuple_iter_begin(&empty);
    T("empty tuple iteration invalid", !cwtuple_iter_valid(&tit));
    CWindMapObject_t emap;
    cwobj_container_init(&emap.head, CWMap);
    cwmap_init(&emap);
    CWindMapIter_t mit = cwmap_iter_begin(&emap);
    T("empty map iteration invalid", !cwmap_iter_valid(&mit));
    cwmap_destroy(&emap);
    CWindSetObject_t eset;
    cwobj_container_init(&eset.head, CWSet);
    cwset_init(&eset);
    CWindSetIter_t sit = cwset_iter_begin(&eset);
    T("empty set iteration invalid", !cwset_iter_valid(&sit));
    cwset_destroy(&eset);

    printf("\n - destroy / leak check\n");
    cwmap_clear(&map);
    T("map clear", cwmap_size(&map) == 0 && map.handle.length == 0);
    cwvec_destroy(&vec);
    T("vec destroy zeroes handle",
      vec.handle.address == 0 && vec.handle.length == 0);
    T("vec ops after destroy",
      !cwvec_push(&vec, &ia) && !cwvec_at(&vec, 0, &ia)
      && cwvec_size(&vec) == 0);
    cwtuple_destroy(&tup);
    cwtuple_destroy(&empty);
    cwmap_destroy(&map);
    cwset_destroy(&set);

    cwmc_stats(&ms);
    T("containers freed: no memcenter leaks", ms.active_allocs == base);
    cwmc_shutdown();

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
