#ifndef CWIND_MEMPAGE_H
    #define CWIND_MEMPAGE_H

    #include <stdint.h>
    #include "./cwind_wal_manager.h"
    #include "../object/cwind_object.h"

    typedef struct CWindPageNode {
        struct CWindPageNode* previous;
        struct CWindPageNode* next;
        struct CWindPageNode* head; // = ((void *)0);
        struct CWindPageNode* tail; // = ((void *)0);
        // 头节点固定为哨兵节点, 不会对头节点进行更换
        // 头节点的 head 为空指针
        // 只有头节点具有 tail, 指向整个链表尾部

        size_t   node_id;
        // 节点顺序编号

        uint8_t  gc_cnt: 4; 
        uint8_t  colors: 4;
        /**
         *    低四位:
         *     - xxxx 1000 -> init,
         *     - xxxx 0001 -> white, 已完成 gc, 等待下一次 gc
         *     - xxxx 0010 -> gray , 正在 gc
         *     - xxxx 0100 -> black, 全黑, 可以整块回收
         *    其实理论上可以用 0001 一起表示 init
         *    但是为了明确检查是否从来没有参与过 gc
         *    所以还是用 1000 表示 init 了
         *    哪怕染色不是块黑色，也可以根据下面的gc_able进行单独回收，不一定要等待块全黑
         *
         *     高四位:
         *     - 0001 xxxx -> 已 gc 1 次
         *     - 0010 xxxx -> 已 gc 2 次
         *     - 0100 xxxx -> 已 gc 3 次
         *     - 1000 xxxx -> 已 gc 4 次
         */
        uint64_t use_state;  // = 0; slot_state, 标注空位
        uint64_t generation; // = 0; 逻辑分代
        uint64_t gc_able;    // = 0; 标注可回收对象位置
        /**
         * 由于模式相似, 故此注释公用于`use_state`, `generation` 和 `gc_able`
         * 内部实现由各部分独立操作
         *
         * bitmap: <
         *  0  0  0  0  0  0  0  0
         *  0  0  0  0  0  0  0  0
         *  0  0  0  0  0  0  0  0
         *  0  0  0  0  0  0  0  0
         *  0  0  0  0  0  0  0  0
         *  0  0  0  0  0  0  0  0
         *  0  0  0  0  0  0  0  0
         *  0  0  0  0  0  0  0  0
         * >
         * 标志位:
         *  - 0 -> free   / young / 不可回收
         *  - 1 -> in_use / elder / 可以回收
         *
         * 针对分代:
         *  - 根据胖指针内部的 gc_cnt,
         * 当年轻代存活过 4 次 gc 后, 晋升老年代, 更新代际 bitmap, 自身 gc_cnt 归零
         * gc 分析器每次对块进行分析时, 块 gc_cnt++, 晋升老年代后, 只要块 gc_cnt 不为 0b1000
         * 就不会分析老年代
         * 也就是晋升后其 gc 分析频率仅跟随块 gc 次数走
         */
        uint8_t head_cursor; // = 0b01000000;
        /**
         * 0 1 0 0 0 0 0 0
         * ^ is_head: 0 false, 1 true
         * ..^ 表示 -1, 也就是还没开始 gc / 已 gc 完成的状态
         * ...[^ ^ ^ ^ ^ ^] 后六位二进制, 正好是 0~63
         */
        
        CWWalManager_t *wal; // SATB
        CWindObject_t (*store)[64];
    } CWindPageNode_t;

#endif
