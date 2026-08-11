/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/rt/stackframe.h
 */

#ifndef CWIND_STACKFRAME_H
    #define CWIND_STACKFRAME_H


#include "../stl/ess/cwind_fix_size_array.h"

    #include <stdbool.h>
    #include <stddef.h>

    #ifndef CWSTACK_VALUE_STACK_SIZE
        #define CWSTACK_VALUE_STACK_SIZE ((size_t)2 * 1024 * 1024)
    #endif

    #ifndef CWSTACK_VARS_PER_BLOCK
        #define CWSTACK_VARS_PER_BLOCK ((size_t)256)
    #endif

    typedef struct CWStackFrame {
        struct CWStackFrame* next; // if is_tail->((void *)0)
        struct CWStackFrame* pre;  // if is_head->((void *)0)
        struct CWStackFrame* head; // if is_head->((void *)0)
        struct CWStackFrame* tail; // if is_tail->((void *)0)
        // total_memory=2 mb

        /**
         * 指向非保护栈起始地址,
         * ```c
         * // Linux
         * mprotect(
         *     total_memory, // **must** mmap
         *     4096,
         *     PROT_NONE
         * )
         * sf_obj->true_beginning = (void*)((uintptr_t)total_memory + 0x1000);
         * ```
         * ```c
         * // Windows
         * DWORD useless_reserved_old_state;
         * SYSTEM_INFO s_info; // <sysinfoapi.h>
         * GetSystemInfo(&s_info);
         * DWORD page_size = s_info.dwPageSize;
         * LPVOID raw = VirtualAlloc(
         *     NULL,
         *     1024 * 1024 * 2 + page_size,
         *     MEM_RESERVE | MEM_TOP_DOWN | MEM_COMMIT,
         *     PAGE_READWRITE
         * );
         * MEMORY_BASIC_INFORMATION page_info; // winnt.h
         * VirtualQuery(
         *     raw,
         *     &page_info,
         *     sizeof(page_info)
         * );
         * void* aligned = page_info.BaseAddress;
         * VirtualProtect(
         *    aligned,
         *    page_size,
         *    PAGE_NOACCESS,
         *    &useless_reserved_old_state
         * ); // 反正每次保护都是整页保护, 那就直接定位到分配页的页头保护一下, 然后从下一页开始写数据
         *
         * sf_obj->true_beginning = (void*)((uintptr_t)page_info.BaseAddress + 0x1000);
         * ```
         */
        void* true_beginning;    //  栈内存, 存具体值 ←--+
        CWFSArray_t *stack_vars; // 存 CWindObject_t   ↑

        /* 值栈 bump 分配器状态 (首次 alloc_value 时懒分配值栈) */
        size_t value_cursor;     // 已用字节 (相对 true_beginning)
        size_t value_capacity;   // 可写字节数 (不含保护页)
    } CWStackFrame_t;

    /*
     * 帧链语义: cwframe_create 创建的 head 是哨兵帧 (也是 main 帧)。
     *  - head.pre / head.head 恒为 NULL;
     *  - head.tail 始终指向当前栈顶帧; 只有哨兵维护 tail;
     *  - 其余帧的 head / tail 字段保留为 NULL (未使用);
     *  - next / pre 组成双向链, 栈顶帧 next 为 NULL。
     */

    /* 生命周期 */
    CWStackFrame_t* cwframe_create(void);
    void cwframe_destroy(CWStackFrame_t* head);

    /* 压帧 / 弹帧 */
    CWStackFrame_t* cwframe_push(CWStackFrame_t* head);
    bool cwframe_pop(CWStackFrame_t* head);
    size_t cwframe_depth(const CWStackFrame_t* head);

    /* 变量记录 (stack_vars, 元素为完整对象记录) */
    size_t cwframe_add_var(CWStackFrame_t* frame, const void* record);
    bool   cwframe_get_var(CWStackFrame_t* frame, size_t index, void* out);
    bool   cwframe_set_var(CWStackFrame_t* frame, size_t index,
                           const void* record);
    size_t cwframe_var_count(const CWStackFrame_t* frame);

    /* 值栈 (bump 分配, 用于标量 / 字节流存储) */
    void*  cwframe_alloc_value(CWStackFrame_t* frame,
                               size_t size, size_t align);
    void   cwframe_reset_values(CWStackFrame_t* frame);
    size_t cwframe_value_used(const CWStackFrame_t* frame);

    /* GC 根遍历: head -> ... -> NULL */
    CWStackFrame_t* cwframe_begin(const CWStackFrame_t* head);
    CWStackFrame_t* cwframe_next(CWStackFrame_t* frame);


#endif //CWIND_STACKFRAME_H
