/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/rt/cwind_builtin_table.h
 */

/**
 * builtin 符号表: (接收者类型, 方法名) → 链接符号
 *
 *  - type 为 NULL 表示模块函数 / 通用操作 (builtins::print 等);
 *  - 只登记 rt 实际实现的符号 (CCompiler.md §2);
 *  - 通用操作 (length / contains / to_string) 由 rt 内部分派,
 *    特化操作 (Vector.get 等) 直接登记容器函数符号。
 */

#ifndef CWIND_BUILTIN_TABLE_H
    #define CWIND_BUILTIN_TABLE_H

    #include <stddef.h>

    typedef struct CwBuiltinEntry {
        const char* type;   /* 接收者类型名, NULL = 模块函数 / 通用 */
        const char* name;   /* TypedAST 中的 builtin ref 名 */
        const char* symbol; /* 链接符号 */
    } CwBuiltinEntry_t;

    size_t cw_builtin_count(void);
    const CwBuiltinEntry_t* cw_builtin_entry(size_t i);
    const char* cw_builtin_symbol(const char* type, const char* name);

#endif /* CWIND_BUILTIN_TABLE_H */
