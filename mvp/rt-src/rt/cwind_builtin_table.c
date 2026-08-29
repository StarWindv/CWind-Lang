/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_builtin_table.c
 */

#include "../include/rt/cwind_builtin_table.h"

#include <stdbool.h>
#include <string.h>

static const CwBuiltinEntry_t k_entries[] = {
    /* 模块函数 / 通用操作 */
    { NULL, "print",     "cw_builtin_print" },
    { NULL, "type_of",   "cw_builtin_type_of" },
    { NULL, "exit",      "cw_builtin_exit" },
    { NULL, "to_string", "cw_builtin_to_string" },
    { NULL, "gc_collect", "cwgc_collect" },
    { NULL, "gc_alloc_bytes", "cwgc_alloc_bytes" },
    { NULL, "gc_live_bytes", "cwgc_live_bytes" },
    { NULL, "gc_pause_ns", "cwgc_pause_ns" },
    { NULL, "gc_enable", "cwgc_set_enabled" },
    /* format 由编译器直接降级到 cw_builtin_format (模板在 rt 栈机里扫),
     * 不登记符号表, 避免静默退化成 to_string */

    /* String */
    { "String", "length",   "cw_builtin_length" },
    { "String", "contains", "cw_builtin_contains" },

    /* Vector */
    { "Vector", "length",    "cw_builtin_length" },
    { "Vector", "contains",  "cw_builtin_contains" },
    { "Vector", "new",       "cwvec_init" },
    { "Vector", "push_back", "cwvec_push" },
    { "Vector", "pop_back",  "cwvec_pop" },
    { "Vector", "get",       "cwvec_at" },
    { "Vector", "set",       "cwvec_set" },
    { "Vector", "clear",     "cwvec_clear" },

    /* Map */
    { "Map", "length",   "cw_builtin_length" },
    { "Map", "contains", "cw_builtin_contains" },
    { "Map", "new",      "cwmap_init" },
    { "Map", "get",      "cwmap_get" },
    { "Map", "set",      "cwmap_put" },
    { "Map", "clear",    "cwmap_clear" },

    /* Set */
    { "Set", "length",   "cw_builtin_length" },
    { "Set", "contains", "cw_builtin_contains" },
    { "Set", "new",      "cwset_init" },
    { "Set", "clear",    "cwset_clear" },

    /* Tuple */
    { "Tuple", "length", "cw_builtin_length" },
};

#define CWBUILTIN_TABLE_SIZE \
    (sizeof(k_entries) / sizeof(k_entries[0]))

size_t cw_builtin_count(void) {
    return CWBUILTIN_TABLE_SIZE;
}

const CwBuiltinEntry_t* cw_builtin_entry(size_t i) {
    if (i >= CWBUILTIN_TABLE_SIZE) return NULL;
    return &k_entries[i];
}

const char* cw_builtin_symbol(const char* type, const char* name) {
    if (!name) return NULL;
    for (size_t i = 0; i < CWBUILTIN_TABLE_SIZE; i++) {
        const CwBuiltinEntry_t* e = &k_entries[i];
        const bool type_ok = (e->type == NULL && type == NULL)
            || (e->type != NULL && type != NULL
                && strcmp(e->type, type) == 0);
        if (type_ok && strcmp(e->name, name) == 0) {
            return e->symbol;
        }
    }
    return NULL;
}
