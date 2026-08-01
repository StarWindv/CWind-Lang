/**
 * Copyright (C) 2026/7/29 CWind-Project
 * License: BSD-3.0
 * Author: StarWindv
 * Location: src/include/rt/stackframe.h
 */
#ifndef CWIND_STACKFRAME_H
    #define CWIND_STACKFRAME_H


#include "../stl/ess/cwind_fix_size_array.h"

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
    } CWStackFrame_t;


#endif //CWIND_STACKFRAME_H
