/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/rt/cwind_builtin.h
 */

/**
 * rt 实际实现的内置函数 (builtins::* 与容器通用操作) — ABI v2
 *
 * 设计参照 Rust 的 trait 思路: 通用操作 (length / contains / to_string)
 * 在 rt 内按类型 tag 分派, 不要求编译器展开成多条特化调用。
 * ABI v2 (todo-50): 值 = 24B CWValue (纯数据), 类型元数据分区存放 —
 * 异构入口一律收 (type_id, const CWValue_t*) 或 32B CWCell_t (tag + 值),
 * 类型 tag 由代码生成的调用点静态提供, 值本身不携带任何元数据。
 */

#ifndef CWIND_BUILTIN_H
    #define CWIND_BUILTIN_H

    #include <stdbool.h>
    #include <stddef.h>
    #include <stdint.h>
    #include <stdio.h>

    #include "../object/cwind_object.h"

    /* 值 → 文本 (标量 / 字符串原样 / 容器递归), 失败返回 false */
    bool cwobj_format(int32_t type_id, const CWValue_t* v,
                      char* buf, size_t cap);

    /* builtins::print: 字符串原样输出, 其余走 cwobj_format;
     * 控制台自动转宽字符 (WriteConsoleW), 重定向/文件保持 UTF-8 原样 */
    bool cw_builtin_print_to(FILE* f, int32_t type_id, const CWValue_t* v);
    bool cw_builtin_print(int32_t type_id, const CWValue_t* v);

    /* builtins::type_of: 类型名写入 buf */
    bool cw_builtin_type_of(int32_t type_id, char* buf, size_t cap);

    /* 通用 length: String 字节数 / 容器元素数 */
    bool cw_builtin_length(int32_t type_id, const CWValue_t* v,
                           uint64_t* out);

    /* 通用 contains: String 子串 / Vector 元素 / Set 元素 / Map 键 */
    bool cw_builtin_contains(int32_t container_type, const CWValue_t* c,
                             int32_t item_type, const CWValue_t* item,
                             bool* out);

    /* String -> 数值 (``T::from(s)`` / 带上下文的 ``s.into()``):
     * 按 target_type_id 解析十进制整数/浮点, 结果数值存 arena 单元;
     * 解析失败时值置 0 并返回 false。 */
    bool cw_builtin_parse_owned(const CWValue_t* src,
                                int32_t target_type_id, CWValue_t* out);

    /* Display::to_string (v0 与 cwobj_format 相同, 无插值) */
    bool cw_builtin_to_string(int32_t type_id, const CWValue_t* v,
                              char* buf, size_t cap);

    /* 任意值 -> String (``x.into()``): 格式化结果存 arena 返回 */
    bool cw_builtin_to_string_owned(int32_t type_id, const CWValue_t* v,
                                    CWValue_t* out);

    /* String 拼接 (String + String / +=):
     * 结果字节分配在 rt 的字符串 arena 中, 进程期存活 (GC 落地后替换);
     * out 必须是可写的 String 值, 失败返回 false。 */
    bool cw_builtin_concat(const CWValue_t* a, const CWValue_t* b,
                           CWValue_t* out);

    /* String::format (基础栈机式模板解析):
     * 模板字节按"保留转义的原始文本"传入 (字符串字面量由后端传 raw):
     *  - `{}` 占位符按顺序消费一个参数, 参数经 cwobj_format 格式化;
     *  - `\{` / `\}` 输出字面花括号, 其它转义与字符串字面量一致;
     *  - 非空占位符 / 参数不足 / 花括号不配对视为失败;
     * 参数以 CWCell 数组传入 (tag + 值, 异构边界), 失败时 out 置空串。
     */
    bool cw_builtin_format(const CWValue_t* self,
                           const CWCell_t* args, size_t nargs,
                           CWValue_t* out);

    /* 通用进程期 arena (v0): String 拼接 / 枚举载荷单元 / 容器标量元素
     * 都从这里分配, 段内存来自内存中心 (cwmc_alloc), 指针稳定;
     * 进程期存活 (GC 落地后由 GC 分配取代); 返回 size+1 字节保证可写 NUL。 */
    void* cwrt_arena_alloc(size_t size);

    /* 当前 arena 段数 (每段 = 内存中心一次大对象分配) */
    size_t cwrt_arena_blocks(void);

    /* builtins::type_of 的 String 形态: 类型名拷进 arena 返回 */
    bool cw_builtin_type_of_owned(int32_t type_id, const CWValue_t* v,
                                  CWValue_t* out);

    /* builtins::readline: 从 stdin 读一行 (去换行), 结果存 arena;
     * EOF 且无任何字符时返回 false, 有内容后 EOF 也返回 true */
    bool cw_builtin_readline(CWValue_t* out);

    /* builtins::exit */
    _Noreturn void cw_builtin_exit(int code);

    /* bug-30: 把 C main 的 argc/argv 打包为 Vector<String> 值
     * (程序参数注入, 后端 main 包装调用); argv 字节零拷贝引用,
     * 进程期存活; 成功写入 out 并返回 true。 */
    bool cw_builtin_main_args(int argc, char** argv, CWValue_t* out);

#endif /* CWIND_BUILTIN_H */
