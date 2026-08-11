/**
 * Copyright (C) 2026 StarWindv
 * License: BSD-3.0
 * Author : StarWindv
 * Location: rt-src/rt/stackframe.c
 */

/* cwind_fix_size_array.h 是 header-only 容器, 未使用的 static 函数会告警 */
#if defined(__GNUC__) || defined(__clang__)
    #pragma GCC diagnostic push
    #pragma GCC diagnostic ignored "-Wunused-function"
#endif

#include "../include/rt/stackframe.h"

#include "../include/memory/cwind_memcenter.h"
#include "../include/object/cwind_object.h"

#include <stdint.h>
#include <string.h>

/*
 * 帧内存来源:
 *  - 帧结构本身: 内存中心 (cwmc_alloc)
 *  - 变量表:     cwfixa (STL, 自己的池)
 *  - 值栈:       VirtualAlloc / mmap + 首页保护 (懒分配, 首次 alloc_value)
 */

#if defined(_WIN32)

    #include <windows.h>

    static size_t cwframe_page_size(void) {
        SYSTEM_INFO si;
        GetSystemInfo(&si);
        return si.dwPageSize;
    }

    static void* cwframe_os_alloc_guard(size_t total, size_t page) {
        void* base = VirtualAlloc(NULL, total,
                                  MEM_RESERVE | MEM_COMMIT,
                                  PAGE_READWRITE);
        if (!base) return NULL;
        DWORD old_protect;
        if (!VirtualProtect(base, page, PAGE_NOACCESS, &old_protect)) {
            VirtualFree(base, 0, MEM_RELEASE);
            return NULL;
        }
        return base;
    }

    static void cwframe_os_free_guard(void* base, size_t total) {
        (void)total;
        if (base) VirtualFree(base, 0, MEM_RELEASE);
    }

#else

    #include <sys/mman.h>
    #include <unistd.h>

    #if !defined(MAP_ANONYMOUS) && defined(MAP_ANON)
        #define MAP_ANONYMOUS MAP_ANON
    #endif

    static size_t cwframe_page_size(void) {
        return (size_t)sysconf(_SC_PAGESIZE);
    }

    static void* cwframe_os_alloc_guard(size_t total, size_t page) {
        void* base = mmap(NULL, total, PROT_READ | PROT_WRITE,
                          MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (base == MAP_FAILED) return NULL;
        if (mprotect(base, page, PROT_NONE) != 0) {
            munmap(base, total);
            return NULL;
        }
        return base;
    }

    static void cwframe_os_free_guard(void* base, size_t total) {
        if (base) munmap(base, total);
    }

#endif

static CWStackFrame_t* cwframe_alloc(void) {
    CWStackFrame_t* f = (CWStackFrame_t*)cwmc_alloc(sizeof(CWStackFrame_t));
    if (!f) return NULL;
    memset(f, 0, sizeof(*f));
    f->stack_vars = cwfixa_create_ex(CWIND_OBJECT_RECORD_SIZE,
                                     CWSTACK_VARS_PER_BLOCK);
    if (!f->stack_vars) {
        cwmc_free(f);
        return NULL;
    }
    return f;
}

static void cwframe_destroy_one(CWStackFrame_t* f) {
    if (!f) return;
    cwfixa_destroy(f->stack_vars);
    if (f->true_beginning) {
        const size_t page = cwframe_page_size();
        void* base = (void*)((uintptr_t)f->true_beginning - page);
        cwframe_os_free_guard(base, f->value_capacity + page);
    }
    cwmc_free(f);
}

CWStackFrame_t* cwframe_create(void) {
    CWStackFrame_t* head = cwframe_alloc();
    if (head) head->tail = head; /* 哨兵帧自己就是栈顶 */
    return head;
}

void cwframe_destroy(CWStackFrame_t* head) {
    CWStackFrame_t* f = head;
    while (f) {
        CWStackFrame_t* next = f->next;
        cwframe_destroy_one(f);
        f = next;
    }
}

CWStackFrame_t* cwframe_push(CWStackFrame_t* head) {
    if (!head || !head->tail) return NULL;
    CWStackFrame_t* f = cwframe_alloc();
    if (!f) return NULL;
    f->pre = head->tail;
    head->tail->next = f;
    head->tail = f;
    return f;
}

bool cwframe_pop(CWStackFrame_t* head) {
    if (!head || head->tail == head) return false;
    CWStackFrame_t* top = head->tail;
    head->tail = top->pre;
    top->pre->next = NULL;
    cwframe_destroy_one(top);
    return true;
}

size_t cwframe_depth(const CWStackFrame_t* head) {
    size_t n = 0;
    for (const CWStackFrame_t* f = head; f; f = f->next) n++;
    return n;
}

size_t cwframe_add_var(CWStackFrame_t* frame, const void* record) {
    if (!frame || !record) return (size_t)-1;
    void* p = cwfixa_push_copy(frame->stack_vars, record);
    if (!p) return (size_t)-1;
    return cwfixa_index_of(frame->stack_vars, p);
}

bool cwframe_get_var(CWStackFrame_t* frame, size_t index, void* out) {
    if (!frame || !out) return false;
    void* p = cwfixa_at(frame->stack_vars, index);
    if (!p) return false;
    if (!cwfixa_occupied(frame->stack_vars, index)) return false;
    memcpy(out, p, CWIND_OBJECT_RECORD_SIZE);
    return true;
}

bool cwframe_set_var(CWStackFrame_t* frame, size_t index,
                     const void* record) {
    if (!frame || !record) return false;
    void* p = cwfixa_at(frame->stack_vars, index);
    if (!p) return false;
    if (!cwfixa_occupied(frame->stack_vars, index)) return false;
    memcpy(p, record, CWIND_OBJECT_RECORD_SIZE);
    return true;
}

size_t cwframe_var_count(const CWStackFrame_t* frame) {
    return frame ? cwfixa_size(frame->stack_vars) : 0;
}

static bool cwframe_ensure_values(CWStackFrame_t* f) {
    if (f->true_beginning) return true;
    const size_t page  = cwframe_page_size();
    const size_t total = CWSTACK_VALUE_STACK_SIZE + page;
    void* base = cwframe_os_alloc_guard(total, page);
    if (!base) return false;
    f->true_beginning = (void*)((uintptr_t)base + page);
    f->value_capacity = CWSTACK_VALUE_STACK_SIZE;
    f->value_cursor   = 0;
    return true;
}

void* cwframe_alloc_value(CWStackFrame_t* frame, size_t size, size_t align) {
    if (!frame || size == 0) return NULL;
    if (!cwframe_ensure_values(frame)) return NULL;

    size_t a = (align == 0) ? 16 : align;
    if ((a & (a - 1)) != 0 || a > 4096) return NULL;

    const size_t off = (frame->value_cursor + a - 1) & ~(a - 1);
    if (off > frame->value_capacity
        || size > frame->value_capacity - off) {
        return NULL;
    }
    frame->value_cursor = off + size;
    return (char*)frame->true_beginning + off;
}

void cwframe_reset_values(CWStackFrame_t* frame) {
    if (frame && frame->true_beginning) frame->value_cursor = 0;
}

size_t cwframe_value_used(const CWStackFrame_t* frame) {
    if (!frame || !frame->true_beginning) return 0;
    return frame->value_cursor;
}

CWStackFrame_t* cwframe_begin(const CWStackFrame_t* head) {
    return (CWStackFrame_t*)head;
}

CWStackFrame_t* cwframe_next(CWStackFrame_t* frame) {
    return frame ? frame->next : NULL;
}

#if defined(__GNUC__) || defined(__clang__)
    #pragma GCC diagnostic pop
#endif
