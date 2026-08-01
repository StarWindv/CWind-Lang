#ifndef CWIND_WAL_OP_H
    #define CWIND_WAL_OP_H

    #include <stdint.h>
    #include "../object/cwind_obj_handle.h"

    typedef struct CWWalOp {
        CWObjHandle_t old;
        CWObjHandle_t new;
        uint8_t op: 4;
        uint8_t reserved: 4;
        /**
        //* [ 0 0 0 0 ] [ 0 0 0 0 ] 
        //* ..^ M(ove)
        //* ....^ R(eplace)
        //* ......^ U(pdate)
        //* ........^ D(elete)
        //* 四种 op 互斥, 对应位为 1 时代表相应操作, 仅能同时出现一个 op
        //* 后四位保留 
        //* 说着是保留, 其实是不知道怎么用
        //* 该死的计算机没有设计 uint4
        //*
        //* 最后的完整操作码格式:
        //* <[u16] [u16]> <[u16] [u16]> <[u4_high] [u4_low]>
        //*
         */
    } CWWalOp_t;

#endif
