/**
 * 独立测试: CWind 容器对象 (Tuple / Vector / Map / Set) — ABI v2
 * (CWValue cell 元素, 类型元数据存 data 头)
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

/* 标量 cell: 值拷进 arena 风格的稳定存储 (这里用调用方存储) */
static CWValue_t wrap_i16(int16_t* storage, int16_t v) {
    *storage = v;
    CWValue_t r;
    cwval_wrap(&r, storage, 2);
    return r;
}

static CWValue_t wrap_str(const char* s) {
    CWValue_t r;
    cwval_wrap(&r, s, (uint64_t)strlen(s));
    return r;
}

static int16_t cell_i16(const CWValue_t* v) {
    return *(const int16_t*)(uintptr_t)v->address;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    cwmc_init();
    CWMemCenterStats_t ms;
    cwmc_stats(&ms);
    const size_t base = ms.active_allocs;
    printf("CWindContainer tests (ABI v2):\n\n");

    printf(" - Vector (cell 元素 + data 头元素类型)\n");
    CWValue_t vec;
    memset(&vec, 0, sizeof(vec));
    T("vec_init(reserve=4)", cwvec_init(&vec, CWInt16, 4));
    T("vec init cursor == 4", vec.cursor == 4);
    T("vec size 0", cwvec_size(&vec) == 0);
    T("vec length 0", vec.length == 0);
    T("vec elem type", cwvec_elem_type(&vec) == CWInt16);

    int16_t stor[10];
    for (int i = 0; i < 10; i++) {
        CWValue_t r = wrap_i16(&stor[i], (int16_t)(i * 7));
        T("vec push", cwvec_push(&vec, &r));
    }
    T("vec size 10", cwvec_size(&vec) == 10);
    T("vec length 10", vec.length == 10);
    T("vec grew (4 -> 8 -> 16)", vec.cursor >= 16);

    int intact = 1;
    for (int i = 0; i < 10 && intact; i++) {
        CWValue_t r;
        intact = cwvec_at(&vec, (size_t)i, &r)
              && cell_i16(&r) == (int16_t)(i * 7);
    }
    T("vec at: all intact", intact);

    int16_t neg1 = -1;
    CWValue_t rnew = wrap_i16(&neg1, -1);
    T("vec set(3)", cwvec_set(&vec, 3, &rnew));
    CWValue_t r3;
    T("vec set visible", cwvec_at(&vec, 3, &r3) && cell_i16(&r3) == -1);
    T("vec at out of range", !cwvec_at(&vec, 99, &r3));
    T("vec set out of range", !cwvec_set(&vec, 99, &rnew));

    CWValue_t popped;
    T("vec pop", cwvec_pop(&vec, &popped));
    T("vec pop value", cell_i16(&popped) == 9 * 7);
    T("vec size 9", cwvec_size(&vec) == 9);

    cwvec_clear(&vec);
    T("vec clear", cwvec_size(&vec) == 0 && vec.length == 0);
    T("vec push after clear", cwvec_push(&vec, &rnew));

    /* 大量 push: 触发 realloc (cell 数组随 CWValue 搬移, 值字节在外部) */
    int16_t big_stor[2000];
    for (int i = 0; i < 2000; i++) {
        CWValue_t r = wrap_i16(&big_stor[i], (int16_t)i);
        if (!cwvec_push(&vec, &r)) { T("vec push 2000", 0); break; }
    }
    T("vec size 2001", cwvec_size(&vec) == 2001);
    intact = 1;
    for (int i = 1; i <= 2000 && intact; i++) {
        CWValue_t r;
        intact = cwvec_at(&vec, (size_t)i, &r)
              && cell_i16(&r) == (int16_t)(i - 1);
    }
    T("vec 2000 elements intact", intact);

    T("vec NULL rejects", !cwvec_push(NULL, &rnew));

    CWValue_t bogus;
    memset(&bogus, 0, sizeof(bogus));
    bogus.address = (uint64_t)(uintptr_t)&neg1; /* 不是容器 data */
    T("vec rejects non-container value", !cwvec_push(&bogus, &rnew));

    printf("\n - Vector 扩展方法\n");
    CWValue_t vother;
    memset(&vother, 0, sizeof(vother));
    cwvec_init(&vother, CWInt16, 2);
    int16_t o_stor[3];
    for (int i = 0; i < 3; i++) {
        CWValue_t r = wrap_i16(&o_stor[i], (int16_t)(100 + i));
        cwvec_push(&vother, &r);
    }
    CWValue_t vext;
    memset(&vext, 0, sizeof(vext));
    cwvec_init(&vext, CWInt16, 1);
    int16_t e_stor[2] = { 1, 2 };
    for (int i = 0; i < 2; i++) {
        CWValue_t r = wrap_i16(&e_stor[i], e_stor[i]);
        cwvec_push(&vext, &r);
    }
    T("extend_with appends", cwvec_extend_with(&vext, &vother));
    T("extend size 5", cwvec_size(&vext) == 5);
    intact = 1;
    const int16_t expect_ext[5] = { 1, 2, 100, 101, 102 };
    for (int i = 0; i < 5 && intact; i++) {
        CWValue_t r;
        intact = cwvec_at(&vext, (size_t)i, &r)
              && cell_i16(&r) == expect_ext[i];
    }
    T("extend contents intact", intact);
    CWValue_t vempty;
    memset(&vempty, 0, sizeof(vempty));
    cwvec_init(&vempty, CWInt16, 0);
    T("extend empty no-op",
      cwvec_extend_with(&vext, &vempty) && cwvec_size(&vext) == 5);
    T("extend self rejected", !cwvec_extend_with(&vext, &vext));

    CWValue_t vins;
    memset(&vins, 0, sizeof(vins));
    cwvec_init(&vins, CWInt16, 1);
    int16_t i_stor[2] = { 1, 3 };
    for (int i = 0; i < 2; i++) {
        CWValue_t r = wrap_i16(&i_stor[i], i_stor[i]);
        cwvec_push(&vins, &r);
    }
    int16_t mid_v = 2;
    CWValue_t vmid = wrap_i16(&mid_v, 2);
    T("insert_at middle", cwvec_insert_at(&vins, 1, &vmid));
    T("insert_at tail", cwvec_insert_at(&vins, 3, &vmid));
    T("insert_at head", cwvec_insert_at(&vins, 0, &vmid));
    T("insert_at out of range", !cwvec_insert_at(&vins, 99, &vmid));
    T("insert size 5", cwvec_size(&vins) == 5);
    const int16_t expect_ins[5] = { 2, 1, 2, 3, 2 };
    intact = 1;
    for (int i = 0; i < 5 && intact; i++) {
        CWValue_t r;
        intact = cwvec_at(&vins, (size_t)i, &r)
              && cell_i16(&r) == expect_ins[i];
    }
    T("insert contents intact", intact);

    int16_t probe_v3 = 3, probe_v99 = 99, probe_v2 = 2;
    CWValue_t vprobe = wrap_i16(&probe_v3, 3);
    size_t vpos = 0;
    T("index_of found", cwvec_index_of(&vins, &vprobe, &vpos) && vpos == 3);
    vprobe = wrap_i16(&probe_v99, 99);
    T("index_of missing",
      !cwvec_index_of(&vins, &vprobe, &vpos) && vpos == SIZE_MAX);
    T("index_of NULL out", !cwvec_index_of(&vins, &vprobe, NULL));
    vprobe = wrap_i16(&probe_v2, 2);
    T("index_of value compare",
      cwvec_index_of(&vins, &vprobe, &vpos) && vpos == 0);

    CWValue_t vremoved;
    T("remove_at head", cwvec_remove_at(&vins, 0, &vremoved)
      && cell_i16(&vremoved) == 2);
    T("remove_at tail", cwvec_remove_at(&vins, 3, &vremoved));
    T("remove_at out of range", !cwvec_remove_at(&vins, 99, &vremoved));
    T("remove_at NULL out", cwvec_remove_at(&vins, 0, NULL));
    T("remove size 2", cwvec_size(&vins) == 2);
    const int16_t expect_rem[2] = { 2, 3 };
    intact = 1;
    for (int i = 0; i < 2 && intact; i++) {
        CWValue_t r;
        intact = cwvec_at(&vins, (size_t)i, &r)
              && cell_i16(&r) == expect_rem[i];
    }
    T("remove contents intact", intact);

    printf("\n - Tuple (异构 cell + 元素类型表)\n");
    static const char* tstr = "tuple";
    int16_t t1v = 1;
    float t2f = 2.5f;
    CWValue_t cells[3];
    cells[0] = wrap_i16(&t1v, 1);
    cwval_wrap(&cells[1], &t2f, 4);
    cwval_wrap(&cells[2], tstr, strlen(tstr));
    int32_t ttypes[3] = { CWInt16, CWFloat, CWString };
    CWValue_t tup;
    memset(&tup, 0, sizeof(tup));
    T("tuple_init(3)", cwtuple_init(&tup, ttypes, cells, 3));
    T("tuple size 3", cwtuple_size(&tup) == 3);
    T("tuple length 3", tup.length == 3);
    T("tuple elem_type(0)", cwtuple_elem_type(&tup, 0) == CWInt16);
    T("tuple elem_type(2)", cwtuple_elem_type(&tup, 2) == CWString);
    T("tuple elem_type out of range", cwtuple_elem_type(&tup, 3) == 0);
    CWValue_t g;
    T("tuple at(0) int", cwtuple_at(&tup, 0, &g) && cell_i16(&g) == 1);
    T("tuple at(2) string",
      cwtuple_at(&tup, 2, &g)
      && g.length == 5 && memcmp((const void*)(uintptr_t)g.address,
                                 "tuple", 5) == 0);
    T("tuple at out of range", !cwtuple_at(&tup, 3, &g));
    CWValue_t empty;
    memset(&empty, 0, sizeof(empty));
    T("tuple_init(0)", cwtuple_init(&empty, NULL, NULL, 0)
      && cwtuple_size(&empty) == 0);
    T("tuple records NULL with count",
      !cwtuple_init(&empty, NULL, NULL, 1));

    printf("\n - Map (键值 cell, 类型存 data 头)\n");
    CWValue_t map;
    memset(&map, 0, sizeof(map));
    T("map_init", cwmap_init(&map, CWInt16, CWString));
    T("map size 0", cwmap_size(&map) == 0);
    T("map key type", cwmap_key_type(&map) == CWInt16);
    T("map value type", cwmap_value_type(&map) == CWString);

    int16_t ka = 7, kb = 7, kc = 8;
    CWValue_t key_a = wrap_i16(&ka, 7);
    CWValue_t key_b = wrap_i16(&kb, 7);  /* 同值不同存储 */
    CWValue_t key_c = wrap_i16(&kc, 8);
    CWValue_t val_a = wrap_str("one");
    CWValue_t val_b = wrap_str("two");
    T("map put", cwmap_put(&map, &key_a, &val_a));
    T("map put same key (value compare)",
      cwmap_put(&map, &key_b, &val_b));
    T("map size 1 after replace", cwmap_size(&map) == 1);
    T("map length 1", map.length == 1);
    CWValue_t gotv;
    T("map get existing",
      cwmap_get(&map, &key_a, &gotv) && gotv.length == 3
      && memcmp((const void*)(uintptr_t)gotv.address, "two", 3) == 0);
    T("map get missing", !cwmap_get(&map, &key_c, &gotv));
    T("map get existence (NULL out)", cwmap_get(&map, &key_a, NULL));
    T("map remove", cwmap_remove(&map, &key_b));
    T("map size 0 after remove", cwmap_size(&map) == 0);
    T("map remove missing", !cwmap_remove(&map, &key_b));

    int16_t mkeys[100], mvals[100];
    for (int i = 0; i < 100; i++) {
        CWValue_t kr = wrap_i16(&mkeys[i], (int16_t)i);
        CWValue_t vr = wrap_i16(&mvals[i], (int16_t)(i * 3));
        if (!cwmap_put(&map, &kr, &vr)) { T("map put 100", 0); break; }
    }
    T("map size 100", cwmap_size(&map) == 100);
    intact = 1;
    int16_t probe_k;
    for (int i = 0; i < 100 && intact; i++) {
        CWValue_t kk = wrap_i16(&probe_k, (int16_t)i); /* 新存储, 值查找 */
        CWValue_t vv;
        intact = cwmap_get(&map, &kk, &vv) && cell_i16(&vv) == (int16_t)(i * 3);
    }
    T("map 100 entries intact (value keys)", intact);
    for (int i = 0; i < 100; i += 2) {
        CWValue_t kk = wrap_i16(&probe_k, (int16_t)i);
        cwmap_remove(&map, &kk);
    }
    T("map size 50 after remove evens", cwmap_size(&map) == 50);
    CWValue_t kk = wrap_i16(&probe_k, 1);
    T("map odd survives", cwmap_get(&map, &kk, NULL));
    kk = wrap_i16(&probe_k, 0);
    T("map even removed", !cwmap_get(&map, &kk, NULL));

    printf("\n - Set\n");
    CWValue_t set;
    memset(&set, 0, sizeof(set));
    T("set_init", cwset_init(&set, CWInt16));
    T("set elem type", cwset_elem_type(&set) == CWInt16);
    T("set size 0", cwset_size(&set) == 0);
    int16_t sa2 = 5, sb2 = 5;
    CWValue_t i1 = wrap_i16(&sa2, 5);
    CWValue_t i2 = wrap_i16(&sb2, 5);
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
        CWValue_t s = wrap_str(memcpy(sset, "item", 5));
        if (!cwset_add(&set, &s)) { T("set add strings", 0); break; }
    }
    T("set dedups strings", cwset_size(&set) == 1);
    CWValue_t probe = wrap_str(memcpy(sset, "item", 5));
    T("set contains string", cwset_contains(&set, &probe));
    cwset_clear(&set);
    T("set clear", cwset_size(&set) == 0);

    int16_t sstor[100];
    for (int i = 0; i < 100; i++) {
        CWValue_t r = wrap_i16(&sstor[i], (int16_t)i);
        if (!cwset_add(&set, &r)) { T("set add 100", 0); break; }
    }
    T("set size 100", cwset_size(&set) == 100);
    CWValue_t probe_i = wrap_i16(&probe_k, 99);
    T("set contains 99", cwset_contains(&set, &probe_i));
    probe_i = wrap_i16(&probe_k, 100);
    T("set missing 100", !cwset_contains(&set, &probe_i));

    printf("\n - iteration (for-in 基础)\n");
    int16_t istor[5];
    CWValue_t iv;
    memset(&iv, 0, sizeof(iv));
    cwvec_init(&iv, CWInt16, 1);
    for (int i = 0; i < 5; i++) {
        CWValue_t r = wrap_i16(&istor[i], (int16_t)(i + 100));
        cwvec_push(&iv, &r);
    }
    size_t visited = 0;
    long long sum = 0;
    CWindVectorIter_t it;
    cwvec_iter_begin(&iv, &it);
    for (; cwvec_iter_valid(&it); cwvec_iter_next(&it)) {
        CWValue_t r;
        if (cwvec_iter_value(&it, &r)) {
            visited++;
            sum += cell_i16(&r);
        }
    }
    T("vector iteration count", visited == 5);
    T("vector iteration sum", sum == 100 + 101 + 102 + 103 + 104);
    cwvec_destroy(&iv);

    visited = 0;
    CWindTupleIter_t itt;
    cwtuple_iter_begin(&tup, &itt);
    for (; cwtuple_iter_valid(&itt); cwtuple_iter_next(&itt)) {
        visited++;
    }
    T("tuple iteration count", visited == 3);

    visited = 0;
    int odd_ok = 1;
    CWindMapIter_t itm;
    cwmap_iter_begin(&map, &itm);
    for (; cwmap_iter_valid(&itm); cwmap_iter_next(&itm)) {
        CWValue_t k, vv;
        if (cwmap_iter_key(&itm, &k) && cwmap_iter_value(&itm, &vv)) {
            visited++;
            if (cell_i16(&k) % 2 == 0
                || cell_i16(&vv) != cell_i16(&k) * 3) {
                odd_ok = 0;
            }
        }
    }
    T("map iteration count == 50", visited == 50);
    T("map iteration keys odd / values match", odd_ok);

    visited = 0;
    int set_ok = 1;
    CWindSetIter_t its;
    cwset_iter_begin(&set, &its);
    for (; cwset_iter_valid(&its); cwset_iter_next(&its)) {
        CWValue_t item;
        if (cwset_iter_item(&its, &item)) {
            visited++;
            if (!cwset_contains(&set, &item)) set_ok = 0;
        }
    }
    T("set iteration count == 100", visited == 100);
    T("set iteration items all contained", set_ok);

    CWValue_t ev2;
    memset(&ev2, 0, sizeof(ev2));
    cwvec_init(&ev2, CWInt16, 4);
    CWindVectorIter_t vit;
    cwvec_iter_begin(&ev2, &vit);
    T("empty vector iteration invalid", !cwvec_iter_valid(&vit));
    cwvec_destroy(&ev2);
    CWindTupleIter_t tit;
    cwtuple_iter_begin(&empty, &tit);
    T("empty tuple iteration invalid", !cwtuple_iter_valid(&tit));
    CWValue_t emap;
    memset(&emap, 0, sizeof(emap));
    cwmap_init(&emap, CWInt16, CWInt16);
    CWindMapIter_t mit;
    cwmap_iter_begin(&emap, &mit);
    T("empty map iteration invalid", !cwmap_iter_valid(&mit));
    cwmap_destroy(&emap);
    CWValue_t eset;
    memset(&eset, 0, sizeof(eset));
    cwset_init(&eset, CWInt16);
    CWindSetIter_t sit;
    cwset_iter_begin(&eset, &sit);
    T("empty set iteration invalid", !cwset_iter_valid(&sit));
    cwset_destroy(&eset);

    printf("\n - destroy / leak check\n");
    cwmap_clear(&map);
    T("map clear", cwmap_size(&map) == 0 && map.length == 0);
    cwvec_destroy(&vec);
    T("vec destroy zeroes value",
      vec.address == 0 && vec.length == 0);
    T("vec ops after destroy",
      !cwvec_push(&vec, &rnew) && !cwvec_at(&vec, 0, &r3)
      && cwvec_size(&vec) == 0);
    cwtuple_destroy(&tup);
    cwtuple_destroy(&empty);
    cwmap_destroy(&map);
    cwset_destroy(&set);
    cwvec_destroy(&vother);
    cwvec_destroy(&vext);
    cwvec_destroy(&vempty);
    cwvec_destroy(&vins);

    cwmc_stats(&ms);
    T("containers freed: no memcenter leaks", ms.active_allocs == base);
    cwmc_shutdown();

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
