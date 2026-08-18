#include "cwind_fix_size_queue.h"
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>


typedef struct {
    int id;
    double val;
} Element;


void init_elem(void *mem, va_list ap) {
    Element *e = (Element*)mem;
    e->id  = va_arg(ap, int);
    e->val = va_arg(ap, double);
}

void init_elem_noargs(void *mem, va_list ap) {
    (void)ap;
    Element *e = (Element*)mem;
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

/* test counters */
static int pass = 0, fail = 0;

#define T(name, cond) do { \
    if (cond) { printf("  [PASS] %s\n", name); pass++; } \
    else      { printf("  [FAIL] %s\n", name); fail++; } \
} while(0)



int main(void) {
    printf("CWFixSizeQ tests:\n\n");

    /* create */
    printf(" - cwfixq_create\n");
    CWFSQueue_t *q = cwfixq_create(sizeof(Element));
    T("create != NULL",     q != NULL);
    T("create size == 0",   cwfixq_size(q) == 0);
    T("create empty true",  cwfixq_empty(q));
    T("create front NULL",  cwfixq_front(q) == NULL);
    T("create back NULL",   cwfixq_back(q) == NULL);

    /* push_copy */
    printf("\n - cwfixq_push_copy\n");
    Element e1 = {1, 1.5};
    void *r1 = cwfixq_push_copy(q, &e1);
    T("push_copy ret != NULL", r1 != NULL);
    T("size == 1",             cwfixq_size(q) == 1);
    T("empty false",           !cwfixq_empty(q));
    T("front == &r1",          cwfixq_front(q) == r1);
    T("back == &r1",           cwfixq_back(q) == r1);
    Element *fe = (Element*)cwfixq_front(q);
    T("front id == 1",         fe->id == 1);
    T("front val == 1.5",      fe->val == 1.5);

    Element e2 = {2, 2.5};
    cwfixq_push_copy(q, &e2);
    T("size == 2", cwfixq_size(q) == 2);
    Element *be = (Element*)cwfixq_back(q);
    T("back id == 2",  be->id == 2);
    T("back val == 2.5", be->val == 2.5);

    /* pop */
    printf("\n - cwfixq_pop\n");
    cwfixq_pop(q);
    T("size == 1 after pop", cwfixq_size(q) == 1);
    Element *f2 = (Element*)cwfixq_front(q);
    T("front id == 2 after pop", f2->id == 2);
    cwfixq_pop(q);
    T("size == 0 after 2 pops", cwfixq_size(q) == 0);
    T("empty true after all pop", cwfixq_empty(q));
    T("front NULL after all pop", cwfixq_front(q) == NULL);
    T("back NULL after all pop",  cwfixq_back(q) == NULL);

    /* pop on empty -- should not crash */
    cwfixq_pop(q);
    T("pop on empty no crash", 1);

    /* pop_copy */
    printf("\n - cwfixq_pop_copy\n");
    Element e3 = {3, 3.5}, e4 = {4, 4.5};
    cwfixq_push_copy(q, &e3);
    cwfixq_push_copy(q, &e4);

    Element out = {0, 0};
    bool ok = cwfixq_pop_copy(q, &out);
    T("pop_copy ok",       ok);
    T("pop_copy id == 3",  out.id == 3);
    T("pop_copy val == 3.5", out.val == 3.5);
    T("size == 1 after pop_copy", cwfixq_size(q) == 1);

    /* pop_copy on empty -- returns false */
    cwfixq_pop(q); /* remove e4 */
    Element out2;
    bool ok2 = cwfixq_pop_copy(q, &out2);
    T("pop_copy on empty returns false", !ok2);

    /* pop_copy with NULL out -- no crash, data discarded */
    cwfixq_push_copy(q, &e1);
    bool ok3 = cwfixq_pop_copy(q, NULL);
    T("pop_copy(NULL) ok", ok3);
    T("size == 0 after pop_copy(NULL)", cwfixq_size(q) == 0);

    /* emplace_back */
    printf("\n - cwfixq_emplace_back\n");
    void *e5_ptr = cwfixq_emplace_back(q, init_elem, 5, 5.5);
    T("emplace_back ret != NULL", e5_ptr != NULL);
    T("size == 1 after emplace",   cwfixq_size(q) == 1);
    Element *e5 = (Element*)e5_ptr;
    T("emplace id == 5",   e5->id == 5);
    T("emplace val == 5.5", e5->val == 5.5);

    void *e6_ptr = cwfixq_emplace_back(q, init_elem, 6, 6.5);
    T("emplace_back 2nd ret != NULL", e6_ptr != NULL);
    T("size == 2 after 2nd emplace",   cwfixq_size(q) == 2);
    Element *e6 = (Element*)e6_ptr;
    T("emplace2 id == 6", e6->id == 6);

    /* emplace_back_0 */
    printf("\n - cwfixq_emplace_back_0\n");
    /* clear first */
    cwfixq_clear(q);
    T("clear -> size 0",  cwfixq_size(q) == 0);
    T("clear -> empty",   cwfixq_empty(q));

    void *e0_ptr = cwfixq_emplace_back_0(q, init_elem_noargs);
    T("emplace_back_0 ret != NULL", e0_ptr != NULL);
    T("size == 1 after emplace_0",   cwfixq_size(q) == 1);
    Element *e0 = (Element*)e0_ptr;
    T("emplace_0 id == 0",  e0->id == 0);
    T("emplace_0 val == 0", e0->val == 0.0);

    /* iterator */
    printf("\n - Iterator\n");
    cwfixq_clear(q);
    for (int i = 0; i < 5; i++) {
        cwfixq_emplace_back(q, init_elem, i, (double)i + 0.5);
    }
    T("size == 5 after batch push", cwfixq_size(q) == 5);

    int idx = 0;
    for (CWFSQIter_t it = cwfixq_begin(q); cwfixq_iter_valid(&it); cwfixq_iter_next(&it)) {
        Element *v = (Element*)cwfixq_iter_value(&it);
        if (!v) { fail++; printf("  [FAIL] iter_value idx=%d\n", idx); continue; }
        /* validate order */
        char lbl[64];
        snprintf(lbl, sizeof(lbl), "iter[%d].id == %d", idx, idx);
        T(lbl, v->id == idx);
        idx++;
    }
    T("iter visited 5 items", idx == 5);

    /* iterator after clear: still holds dangling pointer (no auto-invalidation) */
    CWFSQIter_t eit = cwfixq_begin(q);
    cwfixq_clear(q);
    T("iter after clear: current not NULL (dangling, no auto-invalidation)", eit.current != NULL);

    /* expand pool (stress test) */
    printf("\n - expand pool stress test\n");
    cwfixq_clear(q);
    for (int i = 0; i < 100; i++) {
        void *p = cwfixq_emplace_back(q, init_elem, i, (double)i);
        if (!p) { T("expand did not fail", 0); break; }
    }
    T("100 emplace_back ok", cwfixq_size(q) == 100);
    Element *first = (Element*)cwfixq_front(q);
    Element *last  = (Element*)cwfixq_back(q);
    T("first id == 0 after 100",   first->id == 0);
    T("last  id == 99 after 100",  last->id == 99);

    /* alignment of the element area */
    printf("\n - alignment\n");
    typedef CWFSQueue_MAX_ALIGN_T MA;
    CWFSQueue_t* al = cwfixq_create(sizeof(MA));
    T("align create != NULL", al != NULL);
    int aligned_ok = 1;
    for (int i = 0; i < 100; i++) {
        MA m;
        memset(&m, 0, sizeof(m));
        void* p = cwfixq_push_copy(al, &m);
        if (!p || ((uintptr_t)p & (_Alignof(MA) - 1)) != 0) {
            aligned_ok = 0;
            break;
        }
    }
    T("all element slots aligned", aligned_ok);
    cwfixq_destroy(al);

    /* mmap pool stress: many blocks + recycling */
    printf("\n - pool block expansion stress (5000)\n");
    CWFSQueue_t* st = cwfixq_create(sizeof(Element));
    int ok5000 = 1;
    for (int i = 0; i < 5000; i++) {
        void* p = cwfixq_emplace_back(st, init_elem, i, (double)i);
        if (!p) {
            ok5000 = 0;
            break;
        }
    }
    T("5000 pushes ok", ok5000 && cwfixq_size(st) == 5000);
    T("front id == 0",  ((Element*)cwfixq_front(st))->id == 0);
    T("back  id == 4999", ((Element*)cwfixq_back(st))->id == 4999);
    for (int i = 0; i < 2500; i++) cwfixq_pop(st);
    T("size == 2500 after pops", cwfixq_size(st) == 2500);
    T("front id == 2500", ((Element*)cwfixq_front(st))->id == 2500);
    for (int i = 0; i < 2500; i++) cwfixq_pop(st);
    T("empty after drain", cwfixq_empty(st));
    cwfixq_destroy(st);

    /* safe variants: automatic element destructor */
    printf("\n - safe variants (element destructor)\n");
    CWFSQueue_t* sq = cwfixq_safe_create(sizeof(Owned), owned_dtor);
    T("safe_create != NULL", sq != NULL);
    T("safe_create(NULL dtor) == NULL",
        cwfixq_safe_create(sizeof(Owned), NULL) == NULL);
    for (int i = 0; i < 3; i++) {
        Owned o;
        o.id = i;
        o.buf = (char*)malloc(8);
        if (o.buf) snprintf(o.buf, 8, "buf%d", i);
        cwfixq_push_copy(sq, &o);
    }
    T("size == 3", cwfixq_size(sq) == 3);
    cwfixq_safe_pop(sq);
    T("safe_pop calls dtor (freed == 1)", freed == 1);
    Owned ocopy;
    T("safe_pop_copy ok", cwfixq_safe_pop_copy(sq, &ocopy));
    T("pop_copy transfers ownership (freed == 1)", freed == 1);
    T("pop_copy copy has live buf", ocopy.buf != NULL);
    free(ocopy.buf);
    cwfixq_safe_clear(sq);
    T("safe_clear calls dtor (freed == 2)", freed == 2);
    for (int i = 0; i < 2; i++) {
        Owned o;
        o.id = i;
        o.buf = (char*)malloc(4);
        if (o.buf) memcpy(o.buf, "x", 2);
        cwfixq_push_copy(sq, &o);
    }
    T("safe_pop_copy(NULL) calls dtor (freed == 3)",
        cwfixq_safe_pop_copy(sq, NULL) && freed == 3);
    cwfixq_safe_destroy(sq);
    T("safe_destroy calls dtor (freed == 4)", freed == 4);
    cwfixq_safe_destroy(NULL);
    T("safe_destroy(NULL) no crash", 1);

    /* raw path must stay dtor-free */
    CWFSQueue_t* rawq = cwfixq_create(sizeof(Owned));
    for (int i = 0; i < 2; i++) {
        Owned o;
        o.id = i;
        o.buf = (char*)malloc(4);
        if (o.buf) memcpy(o.buf, "y", 2);
        cwfixq_push_copy(rawq, &o);
    }
    cwfixq_clear(rawq);
    cwfixq_destroy(rawq);
    T("raw container never calls dtor (freed == 4)", freed == 4);

    /* shrink_to_fit */
    printf("\n - cwfixq_shrink_to_fit\n");
    CWFSQueue_t* sh = cwfixq_create(sizeof(Element));
    T("shrink no-op with 1 block", cwfixq_shrink_to_fit(sh));
    for (int i = 0; i < 5000; i++) {
        cwfixq_emplace_back(sh, init_elem, i, (double)i);
    }
    T("size == 5000", cwfixq_size(sh) == 5000);
    for (int i = 0; i < 4000; i++) cwfixq_pop(sh);
    T("size == 1000 after partial drain", cwfixq_size(sh) == 1000);
    void* live_ptr = cwfixq_front(sh);
    int bc_before = 0;
    for (CWFSQBlock_t* b = sh->pool; b; b = b->next) bc_before++;
    T("blocks before == 5", bc_before == 5);
    T("shrink partial ok", cwfixq_shrink_to_fit(sh));
    T("front pointer stable", cwfixq_front(sh) == live_ptr);
    T("front id == 4000", ((Element*)cwfixq_front(sh))->id == 4000);
    int bc_after = 0;
    for (CWFSQBlock_t* b = sh->pool; b; b = b->next) bc_after++;
    T("blocks after == 2", bc_after == 2);
    Element ne = {9999, 0.0};
    cwfixq_push_copy(sh, &ne);
    T("push after shrink ok", cwfixq_size(sh) == 1001);
    T("back id == 9999", ((Element*)cwfixq_back(sh))->id == 9999);
    cwfixq_destroy(sh);

    CWFSQueue_t* sd = cwfixq_create(sizeof(Element));
    for (int i = 0; i < 3000; i++) {
        cwfixq_emplace_back(sd, init_elem, i, 0);
    }
    for (int i = 0; i < 3000; i++) cwfixq_pop(sd);
    T("drained empty", cwfixq_empty(sd));
    T("shrink empty ok", cwfixq_shrink_to_fit(sd));
    int bc2 = 0;
    for (CWFSQBlock_t* b = sd->pool; b; b = b->next) bc2++;
    T("empty shrink keeps 1 block", bc2 == 1);
    cwfixq_push_copy(sd, &ne);
    T("push after empty shrink ok", cwfixq_size(sd) == 1);
    cwfixq_destroy(sd);

    /* destroy */
    printf("\n - cwfixq_destroy\n");
    cwfixq_destroy(q);
    T("destroy completed", 1);

    /* destroy NULL -- should not crash */
    cwfixq_destroy(NULL);
    T("destroy(NULL) no crash", 1);

    printf("\nResults: %d PASS, %d FAIL\n", pass, fail);
    return fail ? 1 : 0;
}
