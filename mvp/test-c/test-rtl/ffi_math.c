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

/* ---- todo-51: String <-> char* / const char* 互转 ---- */

#include <string.h>

/* const char* 入参: 按长度统计 (验证 CWind String 直传字节指针) */
uint32_t ffi_count_chars(const char *s) {
    return s ? (uint32_t)strlen(s) : 0u;
}

/* char* 可写入参: 就地转大写并原样返回同一指针,
 * CWind 侧以临时表达式传参 (移动语义), 返回值重新包成 String */
const char *ffi_shout(char *s) {
    for (char *p = s; p && *p; ++p) {
        if (*p >= 'a' && *p <= 'z') {
            *p = (char)(*p - 'a' + 'A');
        }
    }
    return s;
}

/* 返回静态字符串: 验证 char* 返回 -> String 句柄 (strlen 取长) */
const char *ffi_greet(void) {
    return "hello from C";
}

/* ---- todo-56: extern 静态变量绑定 ---- */

/* 被 CWind 侧 `static mut counter: Int32;` 绑定的可写全局 */
int32_t ffi_counter = 0;

/* 只读绑定目标: CWind 侧 `static seed: UInt64;` / `static app_name: String;` */
uint64_t ffi_seed = 4242;
const char *ffi_app_name = "cwind-ffi";

/* 由 C 读取同一符号: 证明 CWind 的写入落在真正的 C 全局上 */
int32_t ffi_counter_get(void) {
    return ffi_counter;
}

/* ---- todo-54: 函数指针互通 (回调) ---- */

/* 接收回调的 C 高阶函数: 对两个整数应用 op 并返回结果 */
int32_t ffi_apply2(int32_t a, int32_t b,
                   int32_t (*op)(int32_t, int32_t)) {
    return op(a, b);
}

/* 被 CWind 侧直接当作回调地址传递的普通 C 函数 (C-to-C 互通),
 * 也用于验证 CWind-ABI thunk 的间接调用 */
int32_t ffi_c_add(int32_t a, int32_t b) {
    return a + b;
}

/* ---- todo-52: 聚合类型 (struct / enum) 按值参数与返回 ---- */

/* 与 CWind 侧 Point { x: Int32, y: Int32 } 对应的普通 C 结构体 */
typedef struct {
    int32_t x;
    int32_t y;
} ffi_point_t;

/* 按值接收结构体并做平移, 再按值返回 */
ffi_point_t ffi_point_shift(ffi_point_t p, int32_t dx, int32_t dy) {
    p.x += dx;
    p.y += dy;
    return p;
}

/* 只测按值入参: 返回两字段之和 */
int32_t ffi_point_sum(ffi_point_t p) {
    return p.x + p.y;
}

/* 探针: 打包两字段便于诊断 (高 32 位 = x, 低 32 位 = y) */
int64_t ffi_point_pack(ffi_point_t p) {
    return ((int64_t)p.x << 32) | (uint32_t)p.y;
}

/* 只测按值返回: 原样构造 */
ffi_point_t ffi_point_echo(int32_t x, int32_t y) {
    ffi_point_t p = { x, y };
    return p;
}

/* 按值返回聚合: 构造一个点 */
ffi_point_t ffi_point_make(int32_t x, int32_t y) {
    ffi_point_t p = { x, y };
    return p;
}

/* 与 CWind 无载荷枚举 Shape { Line, Circle, Square } 对应:
 * 判别值为变体序号 (0/1/2), 由 C 返回下一个形状 */
int32_t ffi_shape_next(int32_t shape) {
    return (shape + 1) % 3;
}

/* ---- todo-62: #[link_name = "..."] 符号重命名 ---- */

/* 被 CWind 侧以别名绑定: `#[link_name = "ffi_secret_add"] fn open_alias` */
int32_t ffi_secret_add(int32_t a, int32_t b) {
    return a + b;
}

/* 与 CWind 关键字同名的 C 符号: CWind 侧无法书写 `fn match(...);`,
 * 只能经 `#[link_name = "match"]` 以合法名 (如 match_) 绑定 */
int32_t match(int32_t x) {
    return x * 10;
}

/* 被 CWind 侧以别名绑定的可写全局: `#[link_name = "ffi_tagged"] static mut TAG` */
int32_t ffi_tagged = 0;

/* 由 C 读取同一符号: 证明经重命名的 CWind 写入落在真正的 C 全局上 */
int32_t ffi_tagged_get(void) {
    return ffi_tagged;
}

/* ---- todo-61: 聚合字段定长数组 (真实 C 布局, sockaddr 风格) ----
 * 与 CWind 侧 SockAddr { family/port/addr/zero } 一一对应:
 * 16 字节聚合在 Win64 下按内存约定传递 (>8B 由调用方拷贝传指针,
 * 返回经隐藏首参写回), 用于验证 byval/sret 端到端互通。 */

#include <string.h>

typedef struct {
    uint16_t family;
    uint16_t port;
    uint8_t addr[4];
    uint8_t zero[8];
} ffi_sock_t;

/* 按值返回 (sret): 构造 127.0.0.1:port */
ffi_sock_t ffi_sock_make(uint16_t port) {
    ffi_sock_t s;
    s.family = 2;
    s.port = port;
    s.addr[0] = 127;
    s.addr[1] = 0;
    s.addr[2] = 0;
    s.addr[3] = 1;
    memset(s.zero, 0, sizeof(s.zero));
    return s;
}

/* 按值进出 (sret + byval): 出参模式模拟 */
ffi_sock_t ffi_sock_touch(ffi_sock_t sa) {
    sa.addr[0] += 1;
    sa.port += 1;
    return sa;
}

/* 按值入参 (byval): 校验字段布局并求和 */
int32_t ffi_sock_sum(ffi_sock_t sa) {
    int32_t acc = (int32_t)sa.family + (int32_t)sa.port;
    for (int i = 0; i < 4; i++) {
        acc += sa.addr[i];
    }
    for (int i = 0; i < 8; i++) {
        acc += sa.zero[i];
    }
    return acc;
}

/* ---- todo-61: sockaddr 原始定义形状 (UInt16 + char[14]) ---- */

typedef struct {
    uint16_t sa_family;
    char sa_data[14];
} ffi_sa_t;

/* 按值返回 (sret): 构造 sa_data[0]=9 的探测结构 */
ffi_sa_t ffi_sa_make(uint16_t family) {
    ffi_sa_t s;
    s.sa_family = family;
    memset(s.sa_data, 0, sizeof(s.sa_data));
    s.sa_data[0] = 9;
    return s;
}

/* 标量首参 + 聚合入参 + 聚合返回 (混合签名回归用例) */
ffi_sa_t ffi_sa_bump(uint16_t port, ffi_sa_t sa) {
    sa.sa_family += port;
    sa.sa_data[13] = 7;
    return sa;
}

/* ---- socket.md 可行性探针: 指针参数 / 出参 / 首字段基址 ---- */

/* 验证 "&struct.first_field == 结构体 C 视图基址":
 * CWind 侧传 family 字段地址, 这里从该地址按字节求和 */
int32_t ffi_probe_sum_from_ptr(const uint16_t *base) {
    const uint8_t *b = (const uint8_t *)base;
    return (int32_t)b[0] + b[1] + b[2] + b[3];
}

/* 验证标量局部变量的 & 借用是可写回的稳定存储 */
void ffi_probe_write_u32(uint32_t *out) {
    *out = 77u;
}
