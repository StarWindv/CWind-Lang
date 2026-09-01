/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_unwind.c
 */

#include "../include/rt/cwind_unwind.h"

#include "../include/gc/cwind_gc.h"
#include "../include/object/cwind_container.h"
#include "../include/object/cwind_object.h"
#include "../include/rt/cwind_builtin.h"

#include <stdio.h>
#include <string.h>

/*
 * 栈回溯快照 (普通函数, 无控制流转移)
 *
 * 生成代码纪律 (todo-155 已建立): 每个函数入口 cwgc_frame_enter(&head)
 * 把帧指针压进 rt 的 LIFO 影子帧栈 (frame_stack), 返回点 frame_leave。
 * 影子帧栈 = 生成代码调用栈的精确镜像。本模块只做一件事: 把它读成
 * 一份纯 CWind 值, 交给上层展示 —— 不 setjmp/longjmp, 不改变控制流,
 * 不退出进程。退出是 panic 高层 (builtins::panic) 自己的决策。
 *
 * 载荷 schema (Vector<Map<String,String>>, 按序表达帧层级):
 *   frames[0]                = 当前帧 (最内/最深, 触发点)
 *   frames[depth-1]          = 根帧 (最外, main)
 *   每帧 Map:
 *     "index" -> "<深度序号 0..depth-1, 由外到内>"   (定位层级)
 *     "slots" -> "<该帧影子链上的引用载体槽数>"      (定位分配点)
 * 键集即 schema; 多带不多罚, 展示策略归高层 CWind。
 */

/* 把字符串字面量包成 String 值 (字节流指向 rodata, 进程期存活) */
static void uw_wrap_str(CWValue_t* out, const char* s) {
    cwval_wrap(out, s, (uint64_t)strlen(s));
}

/* 把栈缓冲拷进 arena 再包成 String 值 (snprintf 的 buf 出函数即死) */
static bool uw_wrap_owned(CWValue_t* out, const char* s) {
    const size_t n = strlen(s);
    char* copy = (char*)cwrt_arena_alloc(n + 1);
    if (!copy) return false;
    memcpy(copy, s, n + 1);
    cwval_wrap(out, copy, (uint64_t)n);
    return true;
}

bool cwunwind_frames(CWValue_t* out) {
    if (!out) return false;
    const size_t depth = cwgc_frame_depth();
    if (!cwvec_init(out, CWMap, depth > 0 ? depth : 1)) return false;

    char buf[32];
    for (size_t i = 0; i < depth; i++) {
        CWValue_t fm;
        cwval_none(&fm); /* cwmap_init 要求全新值 (address==0), 栈垃圾会让它拒绝 */
        if (!cwmap_init(&fm, CWString, CWString)) return false;
        CWValue_t k;
        CWValue_t v;

        uw_wrap_str(&k, "index");
        snprintf(buf, sizeof(buf), "%zu", i);
        if (!uw_wrap_owned(&v, buf)) return false;
        cwmap_put(&fm, &k, &v);

        uw_wrap_str(&k, "slots");
        snprintf(buf, sizeof(buf), "%zu", cwgc_frame_slot_count(i));
        if (!uw_wrap_owned(&v, buf)) return false;
        cwmap_put(&fm, &k, &v);

        cwvec_push(out, &fm);
    }
    return true;
}
