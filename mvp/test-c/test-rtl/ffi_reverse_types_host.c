/*
 * todo-55: reverse-FFI type-mapping host.  Exercised by CTest against
 * the library emitted by `cwindc --emit share` for
 * fixtures/codegen_export.wind.
 *
 * Covers: scalar args/return, String <-> char*, *mut out-params,
 * [T; N] decay, &T (const pointer), PACK aggregate return, and
 * Option<String> nullable returns.
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#if defined(_WIN32)
    #include <windows.h>
    typedef HMODULE cw_lib_t;
    static cw_lib_t cw_lib_open(const char* path) { return LoadLibraryA(path); }
    static void* cw_lib_sym(cw_lib_t h, const char* name) {
        return (void*)(uintptr_t)GetProcAddress(h, name);
    }
    static void cw_lib_close(cw_lib_t h) { FreeLibrary(h); }
#else
    #include <dlfcn.h>
    typedef void* cw_lib_t;
    static cw_lib_t cw_lib_open(const char* path) { return dlopen(path, RTLD_NOW); }
    static void* cw_lib_sym(cw_lib_t h, const char* name) { return dlsym(h, name); }
    static void cw_lib_close(cw_lib_t h) { dlclose(h); }
#endif

typedef int32_t (*cw_add_fn)(int32_t, int32_t);
typedef const char* (*cw_greet_fn)(const char*);
typedef void (*cw_bump_fn)(int32_t*);
typedef int32_t (*cw_sum3_fn)(const int32_t*);
typedef struct P { int32_t x, y; } P;
typedef P (*cw_make_fn)(int32_t, int32_t);
typedef int32_t (*cw_peek_fn)(const int32_t*);
typedef const char* (*cw_some_fn)(const char*);
typedef const char* (*cw_none_fn)(void);
typedef int32_t (*cw_twice_fn)(int32_t);
/* 无载荷枚举 = i32 判别值 (CW 的 Bool 返回是 i8, 用 uint8_t 接) */
typedef uint8_t (*cw_is_green_fn)(int32_t);
typedef int32_t (*cw_next_fn)(int32_t);
/* C 布局结构体指针 (同 C 声明) */
typedef struct Pair { int32_t x, y; } Pair;
typedef int32_t (*cw_pair_sum_fn)(const Pair*);
typedef void (*cw_pair_set_fn)(Pair*, int32_t, int32_t);
/* 带载荷枚举的 C 视图: { int32 tag; <共享载荷字段...> } */
typedef struct Shape { int32_t tag, a, b; } Shape;
typedef int32_t (*cw_shape_area_fn)(Shape);
typedef Shape (*cw_shape_make_fn)(int32_t, int32_t);
/* bug-84: C 视图恰 8B 的枚举 (Win64 单整数寄存器按值) */
typedef struct Shape8 { int32_t tag, v; } Shape8;
typedef int32_t (*cw_shape8_val_fn)(Shape8);
typedef Shape8 (*cw_shape8_make_fn)(int32_t);

#define CW_CHECK(cond, msg)                                             \
    do {                                                                \
        if (!(cond)) {                                                  \
            fprintf(stderr, "FAIL: %s (%s:%d)\n", msg, __FILE__, __LINE__); \
            failures++;                                                 \
        } else {                                                        \
            printf("ok: %s\n", msg);                                    \
        }                                                               \
    } while (0)

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <shared-lib>\n", argv[0]);
        return 2;
    }
    cw_lib_t lib = cw_lib_open(argv[1]);
    if (!lib) {
        fprintf(stderr, "cannot load %s\n", argv[1]);
        return 1;
    }
    cw_add_fn add = (cw_add_fn)cw_lib_sym(lib, "cw_add");
    cw_greet_fn greet = (cw_greet_fn)cw_lib_sym(lib, "cw_greet");
    cw_bump_fn bump = (cw_bump_fn)cw_lib_sym(lib, "cw_bump");
    cw_sum3_fn sum3 = (cw_sum3_fn)cw_lib_sym(lib, "cw_sum3");
    cw_make_fn make = (cw_make_fn)cw_lib_sym(lib, "cw_make");
    cw_peek_fn peek = (cw_peek_fn)cw_lib_sym(lib, "cw_peek");
    cw_some_fn some = (cw_some_fn)cw_lib_sym(lib, "cw_some");
    cw_none_fn none = (cw_none_fn)cw_lib_sym(lib, "cw_none");
    cw_twice_fn twice = (cw_twice_fn)cw_lib_sym(lib, "cw_twice");
    cw_is_green_fn is_green = (cw_is_green_fn)cw_lib_sym(lib, "cw_is_green");
    cw_next_fn next = (cw_next_fn)cw_lib_sym(lib, "cw_next");
    cw_pair_sum_fn pair_sum = (cw_pair_sum_fn)cw_lib_sym(lib, "cw_pair_sum");
    cw_pair_set_fn pair_set = (cw_pair_set_fn)cw_lib_sym(lib, "cw_pair_set");
    cw_shape_area_fn shape_area =
        (cw_shape_area_fn)cw_lib_sym(lib, "cw_shape_area");
    cw_shape_make_fn shape_make =
        (cw_shape_make_fn)cw_lib_sym(lib, "cw_shape_make");
    cw_shape8_val_fn shape8_val =
        (cw_shape8_val_fn)cw_lib_sym(lib, "cw_shape8_val");
    cw_shape8_make_fn shape8_make =
        (cw_shape8_make_fn)cw_lib_sym(lib, "cw_shape8_make");
    if (!add || !greet || !bump || !sum3 || !make || !peek || !some
        || !none || !twice || !is_green || !next || !pair_sum || !pair_set
        || !shape_area || !shape_make || !shape8_val || !shape8_make) {
        fprintf(stderr, "missing one or more expected exports\n");
        cw_lib_close(lib);
        return 1;
    }

    int failures = 0;
    CW_CHECK(add(20, 22) == 42, "scalar: cw_add(20, 22) == 42");

    const char* s = greet("wind");
    CW_CHECK(s && strcmp(s, "hi wind") == 0, "String: cw_greet(\"wind\")");

    int32_t v = 5;
    bump(&v);
    CW_CHECK(v == 6, "*mut out-param: cw_bump(&5) -> 6");

    int32_t xs[3] = {1, 2, 3};
    CW_CHECK(sum3(xs) == 6, "array decay: cw_sum3([1,2,3]) == 6");

    P p = make(3, 4);
    CW_CHECK(p.x == 3 && p.y == 4, "aggregate: cw_make(3, 4) == {3,4}");

    int32_t q = 7;
    CW_CHECK(peek(&q) == 7, "&T: cw_peek(&7) == 7");

    const char* some_s = some("x");
    CW_CHECK(some_s && strcmp(some_s, "x") == 0,
             "Option<String>: Some(\"x\") -> \"x\"");
    CW_CHECK(none() == NULL, "Option<String>: None -> NULL");

    CW_CHECK(twice(21) == 42, "internally-reachable helper: cw_twice(21)");

    CW_CHECK(is_green(1) == 1 && is_green(0) == 0,
             "fieldless enum param: cw_is_green(Green/Red)");
    CW_CHECK(next(0) == 1 && next(1) == 2 && next(2) == 0,
             "fieldless enum return: cw_next(Red/Green/Blue)");

    Pair pr = {3, 4};
    CW_CHECK(pair_sum(&pr) == 7, "struct pointer read: cw_pair_sum({3,4})");
    pair_set(&pr, 10, 20);
    CW_CHECK(pr.x == 10 && pr.y == 20,
             "struct pointer write: cw_pair_set({10,20})");

    Shape dot = {0, 0, 0};
    Shape line = {1, 6, 7};
    CW_CHECK(shape_area(dot) == 0 && shape_area(line) == 42,
             "payload enum param: cw_shape_area(Dot/Line(6,7))");
    Shape made = shape_make(3, 4);
    CW_CHECK(made.tag == 1 && made.a == 3 && made.b == 4,
             "payload enum return: cw_shape_make(3, 4)");

    Shape8 s8_empty = {0, 0};
    Shape8 s8_dot = {1, 7};
    CW_CHECK(shape8_val(s8_empty) == -1 && shape8_val(s8_dot) == 7,
             "8B payload enum param: cw_shape8_val(Empty/Dot(7))");
    Shape8 s8_made = shape8_make(9);
    CW_CHECK(s8_made.tag == 1 && s8_made.v == 9,
             "8B payload enum return: cw_shape8_make(9)");

    cw_lib_close(lib);
    return failures == 0 ? 0 : 1;
}
