/**
 * 独立测试: CWind ABI 契约 + rt 端到端冒烟
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwabi.exe test_cwabi.c
 *       ../../rt-src/rt/stackframe.c
 *       ../../rt-src/rt/cwind_container.c
 *       ../../rt-src/rt/cwind_object.c
 *       ../../rt-src/rt/cwind_memcenter.c
 */

/* cwind_abi.h 会拉入 cwind_fix_size_array.h (header-only, 有未用 static 函数) */
#if defined(__GNUC__) || defined(__clang__)
    #pragma GCC diagnostic push
    #pragma GCC diagnostic ignored "-Wunused-function"
#endif

#include "../../rt-src/include/rt/cwind_abi.h"
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

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    cwmc_init();
    printf("CWind ABI tests:\n\n");

    printf(" - ABI layout (runtime mirror of _Static_assert)\n");
    T("abi version", CWIND_ABI_VERSION == 1);
    T("head size 8", sizeof(CWindObject_t) == CWIND_ABI_HEAD_SIZE);
    T("handle size 32", sizeof(CWObjHandle_t) == CWIND_ABI_HANDLE_SIZE);
    T("record size 40", CWIND_OBJECT_RECORD_SIZE == CWIND_ABI_OBJECT_RECORD);
    T("head.type_id @0", offsetof(CWindObject_t, type_id) == 0);
    T("head.gc_cnt @4", offsetof(CWindObject_t, gc_cnt) == 4);
    T("handle.object @0", offsetof(CWObjHandle_t, object) == 0);
    T("handle.address @8", offsetof(CWObjHandle_t, address) == 8);
    T("handle.length @16", offsetof(CWObjHandle_t, length) == 16);
    T("handle.cursor @24", offsetof(CWObjHandle_t, cursor) == 24);
    T("type ids pinned",
      CWInt == 1 && CWUInt == 2 && CWFloat == 3 && CWBool == 4
      && CWByte == 5 && CWString == 6 && CWNone == 8 && CWTuple == 9
      && CWVector == 10 && CWMap == 11 && CWSet == 12
      && CWInt8 == 13 && CWUInt8 == 14);
    T("frame config",
      CWSTACK_VALUE_STACK_SIZE == (size_t)2 * 1024 * 1024
      && CWSTACK_VARS_PER_BLOCK >= 16);

    printf("\n - end-to-end: function call simulation\n");
    CWStackFrame_t* main_f = cwframe_create();
    CWStackFrame_t* fn = cwframe_push(main_f);
    T("frames created", main_f != NULL && fn != NULL);

    /* 参数 n = 3, 值存在 fn 值栈 */
    int16_t* n_stor = (int16_t*)cwframe_alloc_value(fn, sizeof(int16_t), 2);
    CWindIntObject_t n_rec = mk_int(n_stor, 3);
    T("param record in value stack",
      n_stor != NULL && cwframe_add_var(fn, &n_rec) == 0);

    /* 局部: Vector<Int>, 元素值也在 fn 值栈 */
    CWindVectorObject_t vec;
    cwobj_container_init(&vec.head, CWVector);
    T("local vector init", cwvec_init(&vec, 2));
    for (int i = 0; i < 3; i++) {
        int16_t* vs = (int16_t*)cwframe_alloc_value(fn,
                                                    sizeof(int16_t), 2);
        CWindIntObject_t r = mk_int(vs, (int16_t)(i * 10));
        if (!cwvec_push(&vec, &r)) { T("vec push in fn", 0); break; }
    }
    T("vector size in fn", cwvec_size(&vec) == 3);
    size_t vec_var = cwframe_add_var(fn, &vec);
    T("vector var added", vec_var == 1);

    /* 函数内读回局部变量 */
    CWindVectorObject_t local;
    CWindIntObject_t elem;
    int16_t ev = 0;
    T("fn reads its vector var",
      cwframe_get_var(fn, vec_var, &local) && cwvec_size(&local) == 3
      && cwvec_at(&local, 2, &elem) && cwobj_get_i16(&elem, &ev)
      && ev == 20);

    /* 返回值: 调用方先准备好 storage, 记录拷贝到 main 帧 */
    int16_t ret_stor = 0;
    CWindIntObject_t ret = mk_int(&ret_stor, 20);
    size_t ret_var = cwframe_add_var(main_f, &ret);
    T("return record in caller frame", ret_var == 0);

    /* 弹帧: fn 的值栈与变量表整体回收 */
    cwvec_destroy(&local);
    T("fn pops", cwframe_pop(main_f));
    T("depth back to 1", cwframe_depth(main_f) == 1);

    /* 调用方读返回值 */
    CWindIntObject_t got;
    int16_t gv = 0;
    T("caller reads return value",
      cwframe_get_var(main_f, ret_var, &got)
      && cwobj_get_i16(&got, &gv) && gv == 20);

    printf("\n - container outlives frame (memcenter ownership)\n");
    CWStackFrame_t* f2 = cwframe_push(main_f);
    int16_t bufs[3];
    CWindVectorObject_t v2;
    cwobj_container_init(&v2.head, CWVector);
    cwvec_init(&v2, 1);
    for (int i = 0; i < 3; i++) {
        CWindIntObject_t r = mk_int(&bufs[i], (int16_t)(i + 1));
        cwvec_push(&v2, &r);
    }
    size_t v2_var = cwframe_add_var(f2, &v2);
    T("vector var in f2", v2_var == 0);

    /* 把记录拷贝到 main 帧, 然后弹帧 */
    size_t v2_main = cwframe_add_var(main_f, &v2);
    T("vector record copied to main", v2_main == 1);
    cwframe_pop(main_f);

    /* 容器数据在内存中心, 不受帧生命周期影响; 元素存储是外部 buffer */
    CWindVectorObject_t v2_after;
    T("vector usable after frame pop",
      cwframe_get_var(main_f, v2_main, &v2_after)
      && cwvec_size(&v2_after) == 3);
    ev = 0;
    T("vector elements intact after pop",
      cwvec_at(&v2_after, 0, &elem) && cwobj_get_i16(&elem, &ev) && ev == 1);
    cwvec_destroy(&v2_after);

    printf("\n - leak check\n");
    cwframe_destroy(main_f);
    CWMemCenterStats_t ms;
    cwmc_stats(&ms);
    T("all rt memory returned", ms.active_allocs == 0 && ms.used_bytes == 0);
    cwmc_shutdown();

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}

#if defined(__GNUC__) || defined(__clang__)
    #pragma GCC diagnostic pop
#endif
