/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: test-c/test-rtl/ffi_math.c
 *
 * CFFI 端到端测试用静态库 (todo-48/49):
 * 提供 cwind 侧 extern "C" 声明引用的简单算术函数,
 * 由 CMake 编成 cwindmath 静态库, 经 #[link(path=...)] 链接。
 */

#include <stdint.h>

int32_t ffi_add(int32_t a, int32_t b) {
    return a + b;
}

int64_t ffi_mul3(int64_t x) {
    return x * 3;
}

uint32_t ffi_max_u32(uint32_t a, uint32_t b) {
    return a > b ? a : b;
}

/* 通过指针写出结果, 测试指针参数与 void 返回 */
void ffi_store_i32(int32_t value, int32_t *out) {
    if (out) {
        *out = value;
    }
}
