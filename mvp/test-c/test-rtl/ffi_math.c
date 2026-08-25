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

/* ---- todo-65: 9~16 字节小聚合的传递约定 ----
 * 12 字节同宽标量: Win64 下走内存约定 (>8B 由调用方传指针),
 * SysV 下走寄存器对 (两个 INTEGER 八字节组), 由同一份 IR 按
 * 目标 ABI 自动降级。 */

typedef struct {
    int32_t w;
    int32_t h;
    int32_t d;
} ffi_rect_t;

/* 按值入参: 两维乘积 (d 仅占位, 保证聚合超过 8 字节) */
int32_t ffi_rect_area(ffi_rect_t r) {
    return r.w * r.h + r.d * 0;
}

/* 按值返回: 三维各 +k */
ffi_rect_t ffi_rect_grow(ffi_rect_t r, int32_t k) {
    r.w += k;
    r.h += k;
    r.d += k;
    return r;
}

/* 只测按值返回: 构造一个矩形 */
ffi_rect_t ffi_rect_make(int32_t w, int32_t h, int32_t d) {
    ffi_rect_t r = { w, h, d };
    return r;
}

/* 16 字节混合宽度纯标量 (无数组字段): 验证小聚合约定的
 * 完整 C 布局往返 —— 各字段移位到独立位段, 错位即失配。 */
typedef struct {
    uint16_t family;
    uint16_t port;
    uint32_t flow;
    uint64_t tag64;
} ffi_tag_t;

uint64_t ffi_tag_mix(ffi_tag_t t) {
    return (uint64_t)t.family + ((uint64_t)t.port << 16)
           + ((uint64_t)t.flow << 24) + t.tag64;
}

/* 按值进出: port/flow 各 +1 */
ffi_tag_t ffi_tag_bump(ffi_tag_t t) {
    t.port += 1;
    t.flow += 1;
    return t;
}

/* ---- todo-66: 嵌套 C 布局聚合 ----
 * sockaddr 层级形状: 内嵌结构体字段 + 标量 + 定长数组。
 * C 视图: in@0..8, family@8..10, tail@10..12, 总大小 12 对齐 4。 */

typedef struct {
    uint16_t port;
    uint32_t addr;
} ffi_inner_t;

typedef struct {
    ffi_inner_t in;
    uint16_t family;
    uint8_t tail[2];
} ffi_outer_t;

ffi_outer_t ffi_outer_make(uint16_t family, uint16_t port, uint32_t addr) {
    ffi_outer_t o;
    o.in.port = port;
    o.in.addr = addr;
    o.family = family;
    o.tail[0] = 7;
    o.tail[1] = 9;
    return o;
}

/* 校验嵌套布局: 各层字段求和 */
int32_t ffi_outer_sum(ffi_outer_t o) {
    return (int32_t)o.in.port + (int32_t)o.in.addr
           + (int32_t)o.family + (int32_t)o.tail[0]
           + (int32_t)o.tail[1];
}

/* 嵌套字段修改后按值返回: in.port/family 各 +1 */
ffi_outer_t ffi_outer_touch(ffi_outer_t o) {
    o.in.port += 1;
    o.family += 1;
    return o;
}

/* ---- todo-67: 数组形参的 C 退化语义 ----
 * C 形参 `T arr[N]` 与 `T*` 等价, CWind 侧 [T; N] 实参直传数据地址。 */

int32_t ffi_arr_sum8(const uint8_t buf[8], int32_t n) {
    int32_t s = 0;
    for (int32_t i = 0; i < n && i < 8; i++) {
        s += buf[i];
    }
    return s;
}

int32_t ffi_arr_sum4(const uint8_t buf[4], int32_t n) {
    int32_t s = 0;
    for (int32_t i = 0; i < n && i < 4; i++) {
        s += buf[i];
    }
    return s;
}

/* 出参模式: 经退化指针写回调用方缓冲 */
void ffi_arr_fill(uint8_t buf[4], uint8_t v) {
    for (int i = 0; i < 4; i++) {
        buf[i] = v;
    }
}

/* 宽元素数组: 位宽不同的元素指针互转验证 */
int64_t ffi_u32_widen(const uint32_t xs[3]) {
    return (int64_t)xs[0] * 100000000LL + (int64_t)xs[1] * 10000LL
           + (int64_t)xs[2];
}
