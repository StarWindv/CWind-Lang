/**
 * 独立测试: StackFrame 基础操作 (ABI v2: 变量表元素 = 32B CWCell)
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_stackframe.exe test_stackframe.c
 *       ../../rt-src/rt/stackframe.c
 *       ../../rt-src/rt/cwind_object.c
 *       ../../rt-src/rt/cwind_memcenter.c
 */

/* cwind_fix_size_array.h 是 header-only 容器, 未使用的 static 函数会告警 */
#if defined(__GNUC__) || defined(__clang__)
    #pragma GCC diagnostic push
    #pragma GCC diagnostic ignored "-Wunused-function"
#endif

#include "../../rt-src/include/rt/stackframe.h"

#include "../../rt-src/include/object/cwind_object.h"
#include "../../rt-src/include/memory/cwind_memcenter.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

/* 构造一个指向 storage 的 Int 标量 cell */
static CWCell_t int_cell(int16_t* storage, int16_t v) {
    CWCell_t c;
    c.type_id = CWInt;
    c._pad = 0;
    *storage = v;
    cwval_wrap(&c.value, storage, 2);
    return c;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWStackFrame tests (ABI v2):\n\n");

    printf(" - create\n");
    CWStackFrame_t* head = cwframe_create();
    T("create != NULL", head != NULL);
    T("depth == 1", cwframe_depth(head) == 1);
    T("sentinel pre == NULL", head->pre == NULL);
    T("sentinel head == NULL", head->head == NULL);
    T("sentinel tail == self", head->tail == head);
    T("var_count == 0", cwframe_var_count(head) == 0);
    T("value_used == 0", cwframe_value_used(head) == 0);

    printf("\n - push / pop chain\n");
    CWStackFrame_t* f1 = cwframe_push(head);
    CWStackFrame_t* f2 = cwframe_push(head);
    CWStackFrame_t* f3 = cwframe_push(head);
    T("push x3", f1 && f2 && f3);
    T("depth == 4", cwframe_depth(head) == 4);
    T("tail == f3", head->tail == f3);
    T("f3->next == NULL", f3->next == NULL);
    T("f3->pre == f2", f3->pre == f2);
    T("f2->next == f3", f2->next == f3);
    T("non-sentinel head/tail NULL",
      f3->head == NULL && f3->tail == NULL);
    T("sentinel next == f1", head->next == f1);

    T("pop -> true", cwframe_pop(head));
    T("depth == 3 after pop", cwframe_depth(head) == 3);
    T("tail == f2", head->tail == f2);
    T("f2->next == NULL after pop", f2->next == NULL);
    T("pop x2", cwframe_pop(head) && cwframe_pop(head));
    T("depth == 1", cwframe_depth(head) == 1);
    T("pop sentinel -> false", !cwframe_pop(head));
    T("depth still 1", cwframe_depth(head) == 1);

    printf("\n - many frames\n");
    for (int i = 0; i < 200; i++) {
        if (!cwframe_push(head)) { T("push 200", 0); break; }
    }
    T("depth == 201", cwframe_depth(head) == 201);
    while (cwframe_pop(head)) { /* drain */ }
    T("drain back to 1", cwframe_depth(head) == 1);

    printf("\n - variables (32B CWCell)\n");
    int16_t s1 = 111, s2 = 222;
    CWCell_t rec1 = int_cell(&s1, 111);
    CWCell_t rec2 = int_cell(&s2, 222);

    size_t i0 = cwframe_add_var(head, &rec1);
    size_t i1 = cwframe_add_var(head, &rec2);
    T("add_var indices", i0 == 0 && i1 == 1);
    T("var_count == 2", cwframe_var_count(head) == 2);

    CWCell_t got;
    memset(&got, 0, sizeof(got));
    T("get_var(0)", cwframe_get_var(head, 0, &got));
    T("get_var(0) type", got.type_id == CWInt);
    T("get_var(0) value",
      *(int16_t*)(uintptr_t)got.value.address == 111);

    s1 = -7; /* 经 cell 指向的存储写 */
    T("set_var(0)", cwframe_set_var(head, 0, &rec1));
    T("set_var(0) updated", cwframe_get_var(head, 0, &got)
      && *(int16_t*)(uintptr_t)got.value.address == -7);
    T("get_var(99) false", !cwframe_get_var(head, 99, &got));
    T("get_var(NULL out) false", !cwframe_get_var(head, 0, NULL));
    T("set_var(99) false", !cwframe_set_var(head, 99, &rec1));

    printf("\n - variables across block growth\n");
    enum { NVAR = 1000 };
    for (size_t i = 2; i < NVAR; i++) {
        int16_t v = (int16_t)i;
        int16_t* vs = (int16_t*)cwframe_alloc_value(head, sizeof(int16_t), 2);
        if (!vs) { T("growth alloc value", 0); break; }
        CWCell_t r = int_cell(vs, v);
        cwframe_add_var(head, &r);
    }
    T("var_count == 1000", cwframe_var_count(head) == 1000);
    int ok = 1;
    for (size_t i = 0; i < NVAR && ok; i++) {
        CWCell_t r;
        ok = cwframe_get_var(head, i, &r)
          && *(int16_t*)(uintptr_t)r.value.address
             == (i < 2 ? (i == 0 ? -7 : 222) : (int16_t)i);
    }
    T("all vars intact after growth", ok);

    printf("\n - per-frame isolation\n");
    CWStackFrame_t* inner = cwframe_push(head);
    T("inner var_count == 0", cwframe_var_count(inner) == 0);
    CWCell_t irec = int_cell(&s1, 555);
    size_t inner_i = cwframe_add_var(inner, &irec);
    T("inner add_var", inner_i == 0);
    T("outer count unchanged", cwframe_var_count(head) == 1000);
    cwframe_pop(head);

    printf("\n - value stack (bump allocator)\n");
    cwframe_reset_values(head);
    T("value_used == 0 after reset", cwframe_value_used(head) == 0);
    void* p1 = cwframe_alloc_value(head, 17, 1);
    void* p2 = cwframe_alloc_value(head, 8, 16);
    T("alloc_value x2", p1 != NULL && p2 != NULL);
    T("16-aligned", ((uintptr_t)p2 & 15) == 0);
    T("monotonic", (uintptr_t)p2 > (uintptr_t)p1);
    T("value_used accounting",
      cwframe_value_used(head) == (size_t)((char*)p2 - (char*)p1) + 8);

    void* p3 = cwframe_alloc_value(head, 4096, 4096);
    T("page-aligned alloc", p3 != NULL && ((uintptr_t)p3 & 4095) == 0);
    T("bad align rejected", cwframe_alloc_value(head, 8, 3) == NULL);
    T("zero size rejected", cwframe_alloc_value(head, 0, 1) == NULL);

    T("reset values", (cwframe_reset_values(head), cwframe_value_used(head) == 0));
    void* p4 = cwframe_alloc_value(head, 16, 16);
    T("reuse after reset", p4 == head->true_beginning);

    printf("\n - value stack overflow\n");
    cwframe_reset_values(head);
    T("alloc full stack",
      cwframe_alloc_value(head, CWSTACK_VALUE_STACK_SIZE, 1) != NULL);
    T("alloc beyond -> NULL",
      cwframe_alloc_value(head, 1, 1) == NULL);
    cwframe_reset_values(head);

    printf("\n - variables referencing value stack\n");
    int16_t* vs = (int16_t*)cwframe_alloc_value(head, sizeof(int16_t), 2);
    CWCell_t vrec = int_cell(vs, -321);
    T("int cell in value stack", vs != NULL);
    size_t vi = cwframe_add_var(head, &vrec);
    CWCell_t vgot;
    memset(&vgot, 0, sizeof(vgot));
    T("roundtrip via value stack",
      cwframe_get_var(head, vi, &vgot)
      && *(int16_t*)(uintptr_t)vgot.value.address == -321);

    char* ss = (char*)cwframe_alloc_value(head, 6, 1);
    CWCell_t srec;
    srec.type_id = CWString;
    srec._pad = 0;
    memcpy(ss, "wind", 4);
    ss[4] = '\0';
    cwval_wrap(&srec.value, ss, 4);
    size_t si = cwframe_add_var(head, &srec);
    CWCell_t sgot;
    memset(&sgot, 0, sizeof(sgot));
    T("string roundtrip",
      cwframe_get_var(head, si, &sgot)
      && sgot.type_id == CWString
      && sgot.value.length == 4
      && memcmp((const void*)(uintptr_t)sgot.value.address, "wind", 4) == 0);

    printf("\n - iteration (GC roots)\n");
    CWStackFrame_t* a = cwframe_begin(head);
    CWStackFrame_t* b = cwframe_next(a);
    T("iteration order", a == head && b == head->next);
    size_t visited = 0;
    for (CWStackFrame_t* f = cwframe_begin(head); f;
         f = cwframe_next(f)) {
        visited++;
    }
    T("iteration visits all frames", visited == cwframe_depth(head));

    printf("\n - leak check (frame structs from memcenter)\n");
    CWMemCenterStats_t ms;
    cwmc_stats(&ms);
    T("frames allocated > 0", ms.active_allocs > 0);
    cwframe_destroy(head);
    cwmc_stats(&ms);
    T("destroy frees all frames", ms.active_allocs == 0);
    cwmc_shutdown();

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}

#if defined(__GNUC__) || defined(__clang__)
    #pragma GCC diagnostic pop
#endif
