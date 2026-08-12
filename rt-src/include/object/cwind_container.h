/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/include/object/cwind_container.h
 */

/**
 * CWind 容器对象 (Tuple / Vector / Map / Set)
 *
 * 语义:
 *  - 元素是完整对象记录 (40 字节), 浅拷贝; 标量值的实际存储由调用方
 *    保证存活期 (帧值栈 / 外部), 容器不负责高层变量的内存。
 *  - 容器自身的数组 / 链表节点一律来自内存中心 (cwind_memcenter),
 *    这是内存中心的主要服务对象。
 *  - handle 语义: address -> 容器数据 (memcenter), length -> 元素数,
 *    Vector 的 cursor -> 容量。
 *  - v0 的 Map / Set 是链表 + 线性查找, 键按 cwobj_equal 做值比较
 *    (两个不同存储地址但值相等的标量视为同一个键)。
 *  - destroy 之后对象保持类型, 但句柄清零, 容器操作一律返回 false。
 */

#ifndef CWIND_CONTAINER_H
    #define CWIND_CONTAINER_H

    #include "./cwind_object.h"

    /* ---- Vector: 动态对象记录数组 ---- */

    bool cwvec_init(CWindVectorObject_t* obj, size_t reserve);
    bool cwvec_push(CWindVectorObject_t* obj, const void* record);
    bool cwvec_pop(CWindVectorObject_t* obj, void* out);
    bool cwvec_at(const CWindVectorObject_t* obj, size_t index, void* out);
    bool cwvec_set(CWindVectorObject_t* obj, size_t index,
                   const void* record);
    size_t cwvec_size(const CWindVectorObject_t* obj);
    void cwvec_clear(CWindVectorObject_t* obj);
    void cwvec_destroy(CWindVectorObject_t* obj);

    /* ---- Tuple: 定长对象记录数组 ---- */

    bool cwtuple_init(CWindTupleObject_t* obj, const void* records,
                      size_t count);
    bool cwtuple_at(const CWindTupleObject_t* obj, size_t index, void* out);
    size_t cwtuple_size(const CWindTupleObject_t* obj);
    void cwtuple_destroy(CWindTupleObject_t* obj);

    /* ---- Map: 键值链表 (v0 线性查找) ---- */

    bool cwmap_init(CWindMapObject_t* obj);
    bool cwmap_put(CWindMapObject_t* obj, const void* key,
                   const void* value);
    bool cwmap_get(const CWindMapObject_t* obj, const void* key,
                   void* out_value);
    bool cwmap_remove(CWindMapObject_t* obj, const void* key);
    size_t cwmap_size(const CWindMapObject_t* obj);
    void cwmap_clear(CWindMapObject_t* obj);
    void cwmap_destroy(CWindMapObject_t* obj);

    /* ---- Set: 元素链表 (v0 线性查找) ---- */

    bool cwset_init(CWindSetObject_t* obj);
    bool cwset_add(CWindSetObject_t* obj, const void* item);
    bool cwset_contains(const CWindSetObject_t* obj, const void* item);
    bool cwset_remove(CWindSetObject_t* obj, const void* item);
    size_t cwset_size(const CWindSetObject_t* obj);
    void cwset_clear(CWindSetObject_t* obj);
    void cwset_destroy(CWindSetObject_t* obj);

    /* ---- 迭代 (for-in 基础) ----
     * 修改 / destroy 容器后旧迭代器失效, 调用方必须丢弃。
     */

    typedef struct CWindVectorIter {
        const CWindVectorObject_t* vec;
        size_t index;
    } CWindVectorIter_t;

    void cwvec_iter_begin(const CWindVectorObject_t* obj,
                          CWindVectorIter_t* out);
    bool cwvec_iter_valid(const CWindVectorIter_t* it);
    bool cwvec_iter_value(const CWindVectorIter_t* it, void* out);
    void cwvec_iter_next(CWindVectorIter_t* it);

    typedef struct CWindTupleIter {
        const CWindTupleObject_t* tup;
        size_t index;
    } CWindTupleIter_t;

    void cwtuple_iter_begin(const CWindTupleObject_t* obj,
                            CWindTupleIter_t* out);
    bool cwtuple_iter_valid(const CWindTupleIter_t* it);
    bool cwtuple_iter_value(const CWindTupleIter_t* it, void* out);
    void cwtuple_iter_next(CWindTupleIter_t* it);

    typedef struct CWindMapIter {
        const CWindMapObject_t* map;
        void* slot; /* 当前节点, 实现私有 */
    } CWindMapIter_t;

    void cwmap_iter_begin(const CWindMapObject_t* obj, CWindMapIter_t* out);
    bool cwmap_iter_valid(const CWindMapIter_t* it);
    bool cwmap_iter_key(const CWindMapIter_t* it, void* out);
    bool cwmap_iter_value(const CWindMapIter_t* it, void* out);
    void cwmap_iter_next(CWindMapIter_t* it);

    typedef struct CWindSetIter {
        const CWindSetObject_t* set;
        void* slot; /* 当前节点, 实现私有 */
    } CWindSetIter_t;

    void cwset_iter_begin(const CWindSetObject_t* obj, CWindSetIter_t* out);
    bool cwset_iter_valid(const CWindSetIter_t* it);
    bool cwset_iter_item(const CWindSetIter_t* it, void* out);
    void cwset_iter_next(CWindSetIter_t* it);

#endif /* CWIND_CONTAINER_H */
