#include "cwind_fix_size_array.h"
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>


typedef struct {
    int id;
    double val;
} Element;


void init_elem(void* mem, va_list ap) {
    Element* e = (Element*)mem;
    e->id  = va_arg(ap, int);
    e->val = va_arg(ap, double);
}


void init_elem_noargs(void* mem, va_list ap) {
    (void)ap;
    Element* e = (Element*)mem;
    e->id  = 0;
    e->val = 0.0;
}


/* element owning heap memory, for safe-variant tests */
typedef struct {
    int id;
    char* buf;
} Owned;

static int freed = 0;

void owned_dtor(void* mem) {
    Owned* o = (Owned*)mem;
    if (o->buf) {
        free(o->buf);
        o->buf = NULL;
    }
    freed++;
}


static int pass = 0, fail = 0;


#define T(name, cond) do { \
    if (cond) { printf("  [PASS] %s\n", name); pass++; } \
    else      { printf("  [FAIL] %s\n", name); fail++; } \
} while (0)



int main(void) {
    printf("CWFSArray tests:\n\n");

    /* create */
    printf(" - cwfixa_create\n");
    CWFSArray_t* a = cwfixa_create(sizeof(Element));
    T("create != NULL",               a != NULL);
    T("create size == 0",             cwfixa_size(a) == 0);
    T("create empty",                 cwfixa_empty(a));
    T("create free_count == 0",       cwfixa_free_count(a) == 0);
    T("create capacity >= 4096",      cwfixa_capacity(a) >= 4096);
    T("at(0) != NULL (reserved)",     cwfixa_at(a, 0) != NULL);
    T("at(0) not occupied",           !cwfixa_occupied(a, 0));
    T("at(capacity) == NULL",         cwfixa_at(a, cwfixa_capacity(a)) == NULL);
    T("occupied(0) false",            !cwfixa_occupied(a, 0));
    T("create(0) == NULL",            cwfixa_create(0) == NULL);

    CWFSArray_t* tiny = cwfixa_create_ex(sizeof(Element), 16);
    T("create_ex(16) capacity == 16", tiny != NULL && cwfixa_capacity(tiny) == 16);
    cwfixa_destroy(tiny);

    /* push_copy + at + index_of */
    printf("\n - cwfixa_push_copy / at / index_of\n");
    Element e1 = {1, 1.5}, e2 = {2, 2.5}, e3 = {3, 3.5};
    void* p1 = cwfixa_push_copy(a, &e1);
    T("push_copy != NULL",            p1 != NULL);
    T("push_copy returns at(0)",      p1 == cwfixa_at(a, 0));
    T("index_of(p1) == 0",            cwfixa_index_of(a, p1) == 0);
    T("size == 1",                    cwfixa_size(a) == 1);
    T("occupied(0)",                  cwfixa_occupied(a, 0));

    void* p2 = cwfixa_push_copy(a, &e2);
    void* p3 = cwfixa_push_copy(a, &e3);
    T("index_of(p2) == 1",            cwfixa_index_of(a, p2) == 1);
    T("index_of(p3) == 2",            cwfixa_index_of(a, p3) == 2);
    T("at(0).id == 1",                ((Element*)cwfixa_at(a, 0))->id == 1);
    T("at(1).id == 2",                ((Element*)cwfixa_at(a, 1))->id == 2);
    T("at(2).val == 3.5",             ((Element*)cwfixa_at(a, 2))->val == 3.5);
    T("index_of(invalid) == -1",
        cwfixa_index_of(a, (void*)(uintptr_t)0x1234) == (size_t)-1);

    /* block crossing & pointer stability */
    printf("\n - block crossing & pointer stability\n");
    for (int i = 3; i < 5000; i++) {
        Element e = { i, (double)i };
        cwfixa_push_copy(a, &e);
    }
    T("size == 5000",                 cwfixa_size(a) == 5000);
    T("capacity >= 8192 (2 blocks)",  cwfixa_capacity(a) >= 8192);
    T("at(0).id == 1",                ((Element*)cwfixa_at(a, 0))->id == 1);
    T("at(4095).id == 4095",          ((Element*)cwfixa_at(a, 4095))->id == 4095);
    T("at(4096).id == 4096",          ((Element*)cwfixa_at(a, 4096))->id == 4096);
    T("at(4999).id == 4999",          ((Element*)cwfixa_at(a, 4999))->id == 4999);

    void* stable = cwfixa_at(a, 4095);
    for (int i = 5000; i < 20000; i++) {
        Element e = { i, (double)i };
        cwfixa_push_copy(a, &e);
    }
    T("pointer stable across growth", cwfixa_at(a, 4095) == stable);
    T("at(19999).id == 19999",        ((Element*)cwfixa_at(a, 19999))->id == 19999);

    /* emplace */
    printf("\n - cwfixa_push (emplace) / push_0\n");
    void* pe = cwfixa_push(a, init_elem, 100, 100.5);
    T("push ret != NULL",             pe != NULL);
    T("push id == 100",               ((Element*)pe)->id == 100);
    T("push val == 100.5",            ((Element*)pe)->val == 100.5);
    T("push index == 20000",          cwfixa_index_of(a, pe) == 20000);
    void* pe0 = cwfixa_push_0(a, init_elem_noargs);
    T("push_0 id == 0",               ((Element*)pe0)->id == 0);
    T("push_0 val == 0.0",            ((Element*)pe0)->val == 0.0);

    /* remove & recycling */
    printf("\n - cwfixa_remove_at / recycling\n");
    CWFSArray_t* r = cwfixa_create_ex(sizeof(Element), 16);
    for (int i = 0; i < 5; i++) {
        Element e = { i, (double)i };
        cwfixa_push_copy(r, &e);
    }
    Element out;
    T("remove_at(2) ok",              cwfixa_remove_at(r, 2, &out));
    T("removed id == 2",              out.id == 2);
    T("size == 4 after remove",       cwfixa_size(r) == 4);
    T("occupied(2) false",            !cwfixa_occupied(r, 2));
    T("free_count == 1",              cwfixa_free_count(r) == 1);
    T("at(2) address still valid",    cwfixa_at(r, 2) != NULL);
    T("remove_at(hole) false",        !cwfixa_remove_at(r, 2, NULL));
    T("remove_at(oob) false",         !cwfixa_remove_at(r, 999, NULL));

    void* rp = cwfixa_push_copy(r, &e3);
    T("recycled slot index == 2",     cwfixa_index_of(r, rp) == 2);
    T("recycled occupied(2)",         cwfixa_occupied(r, 2));
    T("size == 5 after recycle",      cwfixa_size(r) == 5);
    T("recycled value id == 3",       ((Element*)rp)->id == 3);

    cwfixa_remove_at(r, 1, NULL);
    cwfixa_remove_at(r, 3, NULL);
    void* rp2 = cwfixa_push_copy(r, &e1);
    T("LIFO reuse: push lands at 3",  cwfixa_index_of(r, rp2) == 3);
    void* rp3 = cwfixa_push_copy(r, &e2);
    T("LIFO reuse: next lands at 1",  cwfixa_index_of(r, rp3) == 1);
    cwfixa_destroy(r);

    /* iterator */
    printf("\n - iterator\n");
    CWFSArray_t* itarr = cwfixa_create_ex(sizeof(Element), 16);
    for (int i = 0; i < 8; i++) {
        Element e = { i, (double)i };
        cwfixa_push_copy(itarr, &e);
    }
    cwfixa_remove_at(itarr, 1, NULL);
    cwfixa_remove_at(itarr, 5, NULL);
    cwfixa_remove_at(itarr, 7, NULL);
    int visited = 0;
    const int expected[] = {0, 2, 3, 4, 6};
    for (CWFSArrayIter_t it = cwfixa_begin(itarr);
         cwfixa_iter_valid(&it); cwfixa_iter_next(&it)) {
        Element* v = (Element*)cwfixa_iter_value(&it);
        if (!v || v->id != expected[visited]) {
            printf("  [FAIL] iter[%d]\n", visited);
            fail++;
        } else {
            printf("  [PASS] iter[%d].id == %d\n", visited, v->id);
            pass++;
        }
        visited++;
    }
    T("iter visited 5 live elements", visited == 5);

    CWFSArray_t* empty = cwfixa_create(sizeof(Element));
    CWFSArrayIter_t eit = cwfixa_begin(empty);
    T("empty iter invalid",           !cwfixa_iter_valid(&eit));
    cwfixa_destroy(empty);

    /* back / pop */
    printf("\n - back / pop\n");
    CWFSArray_t* bp = cwfixa_create_ex(sizeof(Element), 16);
    Element be1 = {1, 0}, be2 = {2, 0}, be3 = {3, 0};
    cwfixa_push_copy(bp, &be1);
    cwfixa_push_copy(bp, &be2);
    cwfixa_push_copy(bp, &be3);
    T("back id == 3",                 ((Element*)cwfixa_back(bp))->id == 3);
    Element bout;
    T("pop ok",                       cwfixa_pop(bp, &bout));
    T("pop id == 3",                  bout.id == 3);
    T("size == 2 after pop",          cwfixa_size(bp) == 2);
    T("back id == 2",                 ((Element*)cwfixa_back(bp))->id == 2);
    cwfixa_remove_at(bp, 0, NULL);
    T("back skips hole, id == 2",     ((Element*)cwfixa_back(bp))->id == 2);
    cwfixa_destroy(bp);

    /* clear & reuse */
    printf("\n - clear & reuse\n");
    size_t cap_before = cwfixa_capacity(a);
    size_t tail_est = cwfixa_free_count(a) + cwfixa_size(a);
    cwfixa_clear(a);
    T("size == 0 after clear",        cwfixa_size(a) == 0);
    T("capacity unchanged",           cwfixa_capacity(a) == cap_before);
    T("occupied(0) false after clear", !cwfixa_occupied(a, 0));
    T("free_count == tail",           cwfixa_free_count(a) == tail_est);
    for (int i = 0; i < 100; i++) {
        Element e = { i, (double)i };
        cwfixa_push_copy(a, &e);
    }
    T("size == 100 after re-push",    cwfixa_size(a) == 100);
    T("slots reused (no growth)",     cwfixa_capacity(a) == cap_before);
    T("at(0) still a hole",           !cwfixa_occupied(a, 0));
    int cnt = 0;
    for (CWFSArrayIter_t it = cwfixa_begin(a);
         cwfixa_iter_valid(&it); cwfixa_iter_next(&it)) {
        cnt++;
    }
    T("iter after re-push == 100",    cnt == 100);
    cwfixa_destroy(a);

    /* reserve */
    printf("\n - reserve\n");
    CWFSArray_t* rv = cwfixa_create_ex(sizeof(Element), 16);
    T("reserve ok",                   cwfixa_reserve(rv, 100000));
    T("capacity >= 100000",           cwfixa_capacity(rv) >= 100000);
    T("at(100000) == NULL (beyond)",  cwfixa_at(rv, 100000) == NULL);
    T("at(99999) != NULL (reserved)", cwfixa_at(rv, 99999) != NULL);
    T("size still 0",                 cwfixa_size(rv) == 0);
    T("occupied(0) false",            !cwfixa_occupied(rv, 0));
    for (int i = 0; i < 10; i++) {
        Element e = { i, 0 };
        cwfixa_push_copy(rv, &e);
    }
    T("push after reserve ok",        ((Element*)cwfixa_at(rv, 0))->id == 0);
    T("size == 10",                   cwfixa_size(rv) == 10);
    T("free_count == 0 (fresh slots)", cwfixa_free_count(rv) == 0);
    T("occupied(9)",                  cwfixa_occupied(rv, 9));
    cwfixa_destroy(rv);

    /* shrink_to_fit */
    printf("\n - cwfixa_shrink_to_fit\n");
    CWFSArray_t* sh = cwfixa_create_ex(sizeof(Element), 16);
    T("shrink no-op on single block", cwfixa_shrink_to_fit(sh));
    for (int i = 0; i < 1000; i++) {
        Element e = { i, (double)i };
        cwfixa_push_copy(sh, &e);
    }
    T("capacity grew >= 1000", cwfixa_capacity(sh) >= 1000);
    for (int i = 1; i < 1000; i++) {
        cwfixa_remove_at(sh, (size_t)i, NULL);
    }
    T("size == 1", cwfixa_size(sh) == 1);
    T("shrink_to_fit ok", cwfixa_shrink_to_fit(sh));
    T("capacity == 16 after shrink", cwfixa_capacity(sh) == 16);
    T("at(0).id == 0 intact", ((Element*)cwfixa_at(sh, 0))->id == 0);
    Element e9 = {9, 9.5};
    void* shp = cwfixa_push_copy(sh, &e9);
    T("push after shrink ok", shp != NULL && cwfixa_size(sh) == 2);
    T("new element lands in first block", cwfixa_index_of(sh, shp) < 16);
    T("new element value ok", ((Element*)shp)->id == 9);
    for (int i = 0; i < 200; i++) {
        Element e = { i, 0 };
        cwfixa_push_copy(sh, &e);
    }
    cwfixa_clear(sh);
    T("shrink empty after growth ok", cwfixa_shrink_to_fit(sh));
    T("capacity back to 16 when empty", cwfixa_capacity(sh) == 16);
    cwfixa_destroy(sh);

    /* alignment */
    printf("\n - alignment\n");
    typedef CWFSArray_MAX_ALIGN_T MA;
    CWFSArray_t* al = cwfixa_create_ex(sizeof(MA), 64);
    T("align create ok",              al != NULL);
    const size_t need = _Alignof(MA);
    int aligned_ok = 1;
    for (int i = 0; i < 1000; i++) {
        MA m;
        memset(&m, 0, sizeof(m));
        void* p = cwfixa_push_copy(al, &m);
        if (!p || ((uintptr_t)p & (need - 1)) != 0) {
            aligned_ok = 0;
            break;
        }
    }
    T("all slots aligned to max_align_t", aligned_ok);
    cwfixa_destroy(al);

    /* small-block stress */
    printf("\n - small-block stress (16 elems/block)\n");
    CWFSArray_t* s = cwfixa_create_ex(sizeof(Element), 16);
    T("small create ok",              s != NULL);
    for (int i = 0; i < 5000; i++) {
        Element e = { i, (double)i };
        cwfixa_push_copy(s, &e);
    }
    int all_ok = 1;
    for (int i = 0; i < 5000; i++) {
        Element* e = (Element*)cwfixa_at(s, (size_t)i);
        if (!e || e->id != i) {
            all_ok = 0;
            break;
        }
    }
    T("all 5000 slots correct across blocks", all_ok);
    T("index_of(at(4999)) == 4999",
        cwfixa_index_of(s, cwfixa_at(s, 4999)) == 4999);
    for (int i = 0; i < 5000; i += 2) {
        cwfixa_remove_at(s, (size_t)i, NULL);
    }
    T("size == 2500 after removing evens", cwfixa_size(s) == 2500);
    int c2 = 0, odd_ok = 1;
    for (CWFSArrayIter_t it = cwfixa_begin(s);
         cwfixa_iter_valid(&it); cwfixa_iter_next(&it)) {
        Element* e = (Element*)cwfixa_iter_value(&it);
        if (!e || (e->id & 1) == 0) odd_ok = 0;
        c2++;
    }
    T("iter == 2500",                 c2 == 2500);
    T("iter only odd ids",            odd_ok);
    cwfixa_destroy(s);

    /* big stress */
    printf("\n - big stress (100k)\n");
    CWFSArray_t* big = cwfixa_create(sizeof(Element));
    int push_ok = 1;
    for (int i = 0; i < 100000; i++) {
        Element e = { i, (double)i };
        if (!cwfixa_push_copy(big, &e)) {
            push_ok = 0;
            break;
        }
    }
    T("100k pushes ok",               push_ok && cwfixa_size(big) == 100000);
    T("at(0).id == 0",                ((Element*)cwfixa_at(big, 0))->id == 0);
    T("at(65535).id == 65535",        ((Element*)cwfixa_at(big, 65535))->id == 65535);
    T("at(99999).id == 99999",        ((Element*)cwfixa_at(big, 99999))->id == 99999);
    T("index_of roundtrip",
        cwfixa_index_of(big, cwfixa_at(big, 77777)) == 77777);
    cwfixa_clear(big);
    for (int i = 0; i < 50000; i++) {
        Element e = { i * 2, 0 };
        cwfixa_push_copy(big, &e);
    }
    T("50k re-push ok",               cwfixa_size(big) == 50000);
    cwfixa_destroy(big);

    /* safe variants: automatic element destructor */
    printf("\n - safe variants (element destructor)\n");
    CWFSArray_t* sa = cwfixa_safe_create(sizeof(Owned), owned_dtor);
    T("safe_create != NULL", sa != NULL);
    T("safe_create(NULL dtor) == NULL",
        cwfixa_safe_create(sizeof(Owned), NULL) == NULL);
    for (int i = 0; i < 4; i++) {
        Owned o;
        o.id = i;
        o.buf = (char*)malloc(8);
        if (o.buf) snprintf(o.buf, 8, "buf%d", i);
        cwfixa_push_copy(sa, &o);
    }
    T("size == 4", cwfixa_size(sa) == 4);
    T("safe_remove_at(NULL out) ok", cwfixa_safe_remove_at(sa, 1, NULL));
    T("remove_at(NULL out) calls dtor (freed == 1)", freed == 1);
    Owned acopy;
    T("safe_remove_at(&out) ok", cwfixa_safe_remove_at(sa, 2, &acopy));
    T("remove_at(&out) transfers ownership (freed == 1)", freed == 1);
    T("remove_at copy has live buf", acopy.buf != NULL);
    free(acopy.buf);
    cwfixa_safe_clear(sa);
    T("safe_clear calls dtor (freed == 3)", freed == 3);
    for (int i = 0; i < 3; i++) {
        Owned o;
        o.id = i;
        o.buf = (char*)malloc(4);
        if (o.buf) memcpy(o.buf, "z", 2);
        cwfixa_push_copy(sa, &o);
    }
    T("safe_pop(NULL) calls dtor (freed == 4)",
        cwfixa_safe_pop(sa, NULL) && freed == 4);
    Owned pcopy;
    T("safe_pop(&out) ok", cwfixa_safe_pop(sa, &pcopy));
    T("pop(&out) transfers ownership (freed == 4)",
        freed == 4 && pcopy.buf != NULL);
    free(pcopy.buf);
    cwfixa_safe_destroy(sa);
    T("safe_destroy calls dtor (freed == 5)", freed == 5);

    CWFSArray_t* sa2 = cwfixa_safe_create_ex(sizeof(Owned), 16, owned_dtor);
    T("safe_create_ex != NULL", sa2 != NULL);
    for (int i = 0; i < 20; i++) {
        Owned o;
        o.id = i;
        o.buf = (char*)malloc(4);
        if (o.buf) memcpy(o.buf, "w", 2);
        cwfixa_push_copy(sa2, &o);
    }
    cwfixa_safe_clear(sa2);
    cwfixa_safe_destroy(sa2);
    T("safe_create_ex path frees all (freed == 25)", freed == 25);

    /* raw path must stay dtor-free */
    CWFSArray_t* rawarr = cwfixa_create(sizeof(Owned));
    for (int i = 0; i < 3; i++) {
        Owned o;
        o.id = i;
        o.buf = (char*)malloc(4);
        if (o.buf) memcpy(o.buf, "r", 2);
        cwfixa_push_copy(rawarr, &o);
    }
    cwfixa_clear(rawarr);
    cwfixa_destroy(rawarr);
    T("raw container never calls dtor (freed == 25)", freed == 25);

    /* destroy */
    printf("\n - cwfixa_destroy\n");
    cwfixa_destroy(NULL);
    T("destroy(NULL) no crash",       1);

    printf("\nResults: %d PASS, %d FAIL\n", pass, fail);
    return fail ? 1 : 0;
}
