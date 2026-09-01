/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/rt/cwind_unwind.h
 */

#ifndef CWIND_UNWIND_H
    #define CWIND_UNWIND_H

#include "../object/cwind_object.h"

#include <stdbool.h>
#include <stddef.h>

/*
 * 栈回溯快照 —— 普通函数, 无控制流转移。
 *
 * 生成代码每帧经 cwgc_frame_enter 登记 (todo-155 影子帧栈 = 调用栈
 * 镜像); 本模块只把它读成一份纯 CWind 值, 交给上层展示。不
 * setjmp/longjmp、不改变控制流、不退出进程 —— 退出是 panic 高层
 * (builtins::panic) 自己的决策。CWind 目前不做 try/except, panic
 * 不可捕获, 因此也不需要"回收控制流"。
 *
 * 载荷 schema: Vector<Map<String,String>>, 按序表达帧层级:
 *   frames[0]        = 当前帧 (最内/最深, 触发点)
 *   frames[depth-1]  = 根帧 (最外, main)
 *   每帧 Map:
 *     "index" -> "<深度序号 0..depth-1, 由外到内>"   (定位层级)
 *     "slots" -> "<该帧影子链上的引用载体槽数>"      (定位分配点)
 * 键集即 schema; 多带不多罚, 展示策略归高层 CWind。
 */

    /* 把当前影子帧栈读成 Vector<Map<String,String>> 写进 *out。
     * out 需为可写 CWValue; 成功返回 true。 */
    bool cwunwind_frames(CWValue_t* out);

#endif /* CWIND_UNWIND_H */
