/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/object/cwind_container.h
 */

/**
 * CWind 容器对象 (Tuple / Vector / Map / Set) — ABI v2
 *
 * 语义:
 *  - 容器值 = 24B CWValue {address -> data, length = 元素数, cursor = 容量};
 *  - 元素是 24B CWValue cell, 类型元数据存 data 头 (元素类型 tag),
 *    不逐元素携带 (元数据分区存放, todo-50);
 *  - 标量元素的实际字节由调用方保证存活期 (arena 单元 / 外部), 容器不负责;
 *  - 容器自身的数组 / 链表节点一律来自内存中心 (cwind_memcenter);
 *  - v0 的 Map / Set 是链表 + 线性查找, 键按 cwobj_value_equal 值比较;
 *  - destroy 之后值地址清零, 容器操作一律返回 false。
 */

#ifndef CWIND_CONTAINER_H
    #define CWIND_CONTAINER_H

    #include "./cwind_object.h"
    #include <stddef.h>

    /* ---- Vector: 动态 CWValue cell 数组 ---- */

    bool cwvec_init(CWValue_t* v, int32_t elem_type, size_t reserve);
    bool cwvec_push(CWValue_t* v, const CWValue_t* cell);
    bool cwvec_pop(CWValue_t* v, CWValue_t* out);
    bool cwvec_at(const CWValue_t* v, size_t index, CWValue_t* out);
    bool cwvec_set(CWValue_t* v, size_t index, const CWValue_t* cell);
    size_t cwvec_size(const CWValue_t* v);
    int32_t cwvec_elem_type(const CWValue_t* v);
    void cwvec_clear(CWValue_t* v);
    bool cwvec_extend_with(CWValue_t* v, const CWValue_t* other);
    bool cwvec_insert_at(CWValue_t* v, size_t index, const CWValue_t* cell);
    bool cwvec_index_of(const CWValue_t* v, const CWValue_t* cell,
                        size_t* out_index);
    bool cwvec_remove_at(CWValue_t* v, size_t index, CWValue_t* out);
    void cwvec_destroy(CWValue_t* v);

    /* ---- Tuple: 定长异构 cell 数组 (元素类型表存 data 头) ---- */

    bool cwtuple_init(CWValue_t* v, const int32_t* elem_types,
                      const CWValue_t* cells, size_t count);
    bool cwtuple_at(const CWValue_t* v, size_t index, CWValue_t* out);
    int32_t cwtuple_elem_type(const CWValue_t* v, size_t index);
    size_t cwtuple_size(const CWValue_t* v);
    void cwtuple_destroy(CWValue_t* v);

    /* ---- Map: 键值链表 (v0 线性查找) ---- */

    bool cwmap_init(CWValue_t* v, int32_t key_type, int32_t value_type);
    bool cwmap_put(CWValue_t* v, const CWValue_t* key,
                   const CWValue_t* value);
    bool cwmap_get(const CWValue_t* v, const CWValue_t* key,
                   CWValue_t* out_value);
    bool cwmap_remove(CWValue_t* v, const CWValue_t* key);
    size_t cwmap_size(const CWValue_t* v);
    int32_t cwmap_key_type(const CWValue_t* v);
    int32_t cwmap_value_type(const CWValue_t* v);
    void cwmap_clear(CWValue_t* v);
    void cwmap_destroy(CWValue_t* v);

    /* ---- Set: 元素链表 (v0 线性查找) ---- */

    bool cwset_init(CWValue_t* v, int32_t elem_type);
    bool cwset_add(CWValue_t* v, const CWValue_t* item);
    bool cwset_contains(const CWValue_t* v, const CWValue_t* item);
    bool cwset_remove(CWValue_t* v, const CWValue_t* item);
    size_t cwset_size(const CWValue_t* v);
    int32_t cwset_elem_type(const CWValue_t* v);
    void cwset_clear(CWValue_t* v);
    void cwset_destroy(CWValue_t* v);

    /* ---- 迭代 (for-in 基础) ----
     * 修改 / destroy 容器后旧迭代器失效, 调用方必须丢弃。
     */

    typedef struct CWindVectorIter {
        const CWValue_t* vec;
        size_t index;
    } CWindVectorIter_t;

    void cwvec_iter_begin(const CWValue_t* v, CWindVectorIter_t* out);
    bool cwvec_iter_valid(const CWindVectorIter_t* it);
    bool cwvec_iter_value(const CWindVectorIter_t* it, CWValue_t* out);
    void cwvec_iter_next(CWindVectorIter_t* it);

    typedef struct CWindTupleIter {
        const CWValue_t* tup;
        size_t index;
    } CWindTupleIter_t;

    void cwtuple_iter_begin(const CWValue_t* v, CWindTupleIter_t* out);
    bool cwtuple_iter_valid(const CWindTupleIter_t* it);
    bool cwtuple_iter_value(const CWindTupleIter_t* it, CWValue_t* out);
    void cwtuple_iter_next(CWindTupleIter_t* it);

    typedef struct CWindMapIter {
        const CWValue_t* map;
        void* slot; /* 当前节点, 实现私有 */
    } CWindMapIter_t;

    void cwmap_iter_begin(const CWValue_t* v, CWindMapIter_t* out);
    bool cwmap_iter_valid(const CWindMapIter_t* it);
    bool cwmap_iter_key(const CWindMapIter_t* it, CWValue_t* out);
    bool cwmap_iter_value(const CWindMapIter_t* it, CWValue_t* out);
    void cwmap_iter_next(CWindMapIter_t* it);

    typedef struct CWindSetIter {
        const CWValue_t* set;
        void* slot; /* 当前节点, 实现私有 */
    } CWindSetIter_t;

    void cwset_iter_begin(const CWValue_t* v, CWindSetIter_t* out);
    bool cwset_iter_valid(const CWindSetIter_t* it);
    bool cwset_iter_item(const CWindSetIter_t* it, CWValue_t* out);
    void cwset_iter_next(CWindSetIter_t* it);

/* ---- GC 精确遍历 (todo-35 B 组): 各容器 data 的内部引用 walker ---- */
void cwgc_walk_vector_data(void* base, unsigned size);
void cwgc_walk_map_data(void* base, unsigned size);
void cwgc_walk_map_node(void* base, unsigned size);
void cwgc_walk_set_data(void* base, unsigned size);
void cwgc_walk_set_node(void* base, unsigned size);
void cwgc_walk_tuple_data(void* base, unsigned size);

#endif /* CWIND_CONTAINER_H */
