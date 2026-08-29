/**
 * 独立测试: CWind ABI v2 契约 + rt 端到端冒烟
 * (todo-50: 24B CWValue 值类型, 32B CWCell 异构单元, 元数据分区)
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

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    cwmc_init();
    printf("CWind ABI v2 tests:\n\n");

    printf(" - ABI layout (runtime mirror of _Static_assert)\n");
    T("abi version", CWIND_ABI_VERSION == 2);
    T("value size 24", sizeof(CWValue_t) == CWIND_ABI_VALUE_SIZE);
    T("cell size 32", sizeof(CWCell_t) == CWIND_ABI_CELL_SIZE);
    T("value.address @0", offsetof(CWValue_t, address) == 0);
    T("value.length @8", offsetof(CWValue_t, length) == 8);
    T("value.cursor @16", offsetof(CWValue_t, cursor) == 16);
    T("cell.type_id @0", offsetof(CWCell_t, type_id) == 0);
    T("cell.value @8", offsetof(CWCell_t, value) == 8);
    T("no type metadata in value",
      sizeof(CWValue_t) == 3 * sizeof(uint64_t));
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

    /* 参数 n = 3, 值存在 fn 值栈; 变量表元素 = 32B CWCell */
    int16_t* n_stor = (int16_t*)cwframe_alloc_value(fn, sizeof(int16_t), 2);
    *n_stor = 3;
    CWCell_t n_cell;
    n_cell.type_id = CWInt;
    n_cell._pad = 0;
    cwval_wrap(&n_cell.value, n_stor, 2);
    T("param cell in frame",
      n_stor != NULL && cwframe_add_var(fn, &n_cell) == 0);

    /* 局部: Vector<Int16>, 元素值也在 fn 值栈 */
    CWValue_t vec;
    memset(&vec, 0, sizeof(vec));
    T("local vector init", cwvec_init(&vec, CWInt16, 2));
    for (int i = 0; i < 3; i++) {
        int16_t* vs = (int16_t*)cwframe_alloc_value(fn,
                                                    sizeof(int16_t), 2);
        *vs = (int16_t)(i * 10);
        CWValue_t r;
        cwval_wrap(&r, vs, 2);
        if (!cwvec_push(&vec, &r)) { T("vec push in fn", 0); break; }
    }
    T("vector size in fn", cwvec_size(&vec) == 3);
    CWCell_t vec_cell;
    vec_cell.type_id = CWVector;
    vec_cell._pad = 0;
    vec_cell.value = vec;
    size_t vec_var = cwframe_add_var(fn, &vec_cell);
    T("vector var added", vec_var == 1);

    /* 函数内读回局部变量 */
    CWCell_t local_cell;
    CWValue_t elem;
    T("fn reads its vector var",
      cwframe_get_var(fn, vec_var, &local_cell)
      && local_cell.type_id == CWVector
      && cwvec_size(&local_cell.value) == 3
      && cwvec_at(&local_cell.value, 2, &elem)
      && *(int16_t*)(uintptr_t)elem.address == 20);

    /* 返回值: 调用方准备好 cell, 拷贝到 main 帧变量表 */
    int16_t ret_stor = 20;
    CWCell_t ret;
    ret.type_id = CWInt;
    ret._pad = 0;
    cwval_wrap(&ret.value, &ret_stor, 2);
    size_t ret_var = cwframe_add_var(main_f, &ret);
    T("return cell in caller frame", ret_var == 0);

    /* 弹帧: fn 的值栈与变量表整体回收 */
    cwvec_destroy(&vec);
    T("fn pops", cwframe_pop(main_f));
    T("depth back to 1", cwframe_depth(main_f) == 1);

    /* 调用方读返回值 */
    CWCell_t got;
    T("caller reads return value",
      cwframe_get_var(main_f, ret_var, &got)
      && got.type_id == CWInt
      && *(int16_t*)(uintptr_t)got.value.address == 20);

    printf("\n - container outlives frame (memcenter ownership)\n");
    CWStackFrame_t* f2 = cwframe_push(main_f);
    int16_t bufs[3];
    CWValue_t v2;
    memset(&v2, 0, sizeof(v2));
    cwvec_init(&v2, CWInt16, 1);
    for (int i = 0; i < 3; i++) {
        CWValue_t tmp;
        bufs[i] = (int16_t)(i + 1);
        cwval_wrap(&tmp, &bufs[i], 2);
        cwvec_push(&v2, &tmp);
    }
    CWCell_t v2_cell;
    v2_cell.type_id = CWVector;
    v2_cell._pad = 0;
    v2_cell.value = v2;
    size_t v2_var = cwframe_add_var(f2, &v2_cell);
    T("vector var in f2", v2_var == 0);

    /* 把 cell 拷贝到 main 帧, 然后弹帧 */
    size_t v2_main = cwframe_add_var(main_f, &v2_cell);
    T("vector cell copied to main", v2_main == 1);
    cwframe_pop(main_f);

    /* 容器数据在内存中心, 不受帧生命周期影响; 元素存储是外部 buffer */
    CWCell_t v2_after;
    T("vector usable after frame pop",
      cwframe_get_var(main_f, v2_main, &v2_after)
      && cwvec_size(&v2_after.value) == 3);
    T("vector elements intact after pop",
      cwvec_at(&v2_after.value, 0, &elem)
      && *(int16_t*)(uintptr_t)elem.address == 1);
    cwvec_destroy(&v2_after.value);

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
