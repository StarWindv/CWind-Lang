/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_container.c
 */

#include "../include/object/cwind_container.h"

#include "../include/memory/cwind_memcenter.h"
#include "../include/gc/cwind_gc.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*
 * ABI v2 (todo-50: 拆胖对象, 元数据分区存放):
 *  - 容器值 = 24B CWValue, address 指向 data; data 头带自身 kind 与
 *    元素类型 tag (容器级元数据, 不逐元素携带);
 *  - 元素 = 24B CWValue cell; 标量元素值存 arena 单元 (cell.address 指向);
 *  - 内部数据全部来自内存中心:
 *      Vector / Tuple: data + cell 数组 (cwmc_alloc / cwmc_realloc)
 *      Map / Set:      data + 链表节点 (cwmc_alloc / cwmc_free)
 */

typedef struct CWVecData {
    int32_t kind;       /* CWVector */
    int32_t elem_type;  /* 元素类型 tag (元数据分区: 容器级簿记) */
    CWValue_t* items;   /* cell 数组 */
    size_t count;
    size_t capacity;
} CWVecData_t;

typedef struct CWTupleData {
    int32_t kind;       /* CWTuple */
    int32_t _pad;
    size_t count;
    /* 变长尾部 (单次分配):
     *   int32_t   elem_types[count];  偏移 sizeof(CWTupleData_t)
     *   CWValue_t cells[count];       8 对齐后的偏移 */
} CWTupleData_t;

typedef struct CWMapEntry {
    struct CWMapEntry* next;
    CWValue_t key;
    CWValue_t value;
} CWMapEntry_t;

typedef struct CWMapData {
    int32_t kind;       /* CWMap */
    int32_t key_type;
    int32_t value_type;
    int32_t _pad;
    CWMapEntry_t* head;
    size_t count;
} CWMapData_t;

typedef struct CWSetEntry {
    struct CWSetEntry* next;
    CWValue_t item;
} CWSetEntry_t;

typedef struct CWSetData {
    int32_t kind;       /* CWSet */
    int32_t elem_type;
    CWSetEntry_t* head;
    size_t count;
} CWSetData_t;

#define CWCNT_DEFAULT_CAP ((size_t)16)

/* ---- data 指针与 kind 校验 ---- */

static CWVecData_t* cwvec_data_of(const CWValue_t* v) {
    if (!v || v->address == 0) return NULL;
    CWVecData_t* d = (CWVecData_t*)(uintptr_t)v->address;
    return (d->kind == CWVector) ? d : NULL;
}

static CWTupleData_t* cwtuple_data_of(const CWValue_t* v) {
    if (!v || v->address == 0) return NULL;
    CWTupleData_t* d = (CWTupleData_t*)(uintptr_t)v->address;
    return (d->kind == CWTuple) ? d : NULL;
}

static CWMapData_t* cwmap_data_of(const CWValue_t* v) {
    if (!v || v->address == 0) return NULL;
    CWMapData_t* d = (CWMapData_t*)(uintptr_t)v->address;
    return (d->kind == CWMap) ? d : NULL;
}

static CWSetData_t* cwset_data_of(const CWValue_t* v) {
    if (!v || v->address == 0) return NULL;
    CWSetData_t* d = (CWSetData_t*)(uintptr_t)v->address;
    return (d->kind == CWSet) ? d : NULL;
}

/* Tuple 变长尾部偏移 */
static int32_t* cwtuple_types_of(CWTupleData_t* d) {
    return (int32_t*)((char*)d + sizeof(CWTupleData_t));
}

static CWValue_t* cwtuple_cells_of(CWTupleData_t* d) {
    const size_t types_end = sizeof(CWTupleData_t)
        + (size_t)d->count * sizeof(int32_t);
    return (CWValue_t*)(uintptr_t)((char*)d
        + (((types_end + 7u) & ~(size_t)7)));
}

/* ---- Vector ---- */

bool cwvec_init(CWValue_t* v, int32_t elem_type, size_t reserve) {
    if (!v || v->address != 0) return false;

    const size_t cap = (reserve == 0) ? CWCNT_DEFAULT_CAP : reserve;
    CWVecData_t* d = (CWVecData_t*)cwmc_calloc(sizeof(*d));
    if (!d) return false;
    cwgc_set_desc(d, CWGC_DESC_VECTOR_DATA);
    d->kind = CWVector;
    d->elem_type = elem_type;
    d->items = (CWValue_t*)cwmc_calloc(cap * sizeof(CWValue_t));
    if (!d->items) {
        cwmc_free(d);
        return false;
    }
    d->capacity = cap;

    v->address = (uint64_t)(uintptr_t)d;
    v->length  = 0;
    v->cursor  = cap;
    return true;
}

bool cwvec_push(CWValue_t* v, const CWValue_t* cell) {
    if (!v || !cell) return false;
    CWVecData_t* d = cwvec_data_of(v);
    if (!d) return false;

    if (d->count == d->capacity) {
        const size_t nc = d->capacity * 2;
        void* p = cwmc_realloc(d->items, nc * sizeof(CWValue_t));
        if (!p) return false;
        d->items = (CWValue_t*)p;
        d->capacity = nc;
        v->cursor = nc;
    }
    d->items[d->count] = *cell;
    d->count++;
    v->length = d->count;
    cwgc_barrier((const void*)(uintptr_t)cell->address);
    return true;
}

bool cwvec_pop(CWValue_t* v, CWValue_t* out) {
    if (!v) return false;
    CWVecData_t* d = cwvec_data_of(v);
    if (!d || d->count == 0) return false;
    d->count--;
    if (out) *out = d->items[d->count];
    v->length = d->count;
    return true;
}

bool cwvec_at(const CWValue_t* v, size_t index, CWValue_t* out) {
    if (!v || !out) return false;
    const CWVecData_t* d = cwvec_data_of(v);
    if (!d || index >= d->count) return false;
    *out = d->items[index];
    return true;
}

bool cwvec_set(CWValue_t* v, size_t index, const CWValue_t* cell) {
    if (!v || !cell) return false;
    CWVecData_t* d = cwvec_data_of(v);
    if (!d || index >= d->count) return false;
    d->items[index] = *cell;
    cwgc_barrier((const void*)(uintptr_t)cell->address);
    return true;
}

size_t cwvec_size(const CWValue_t* v) {
    const CWVecData_t* d = cwvec_data_of(v);
    return d ? d->count : 0;
}

int32_t cwvec_elem_type(const CWValue_t* v) {
    const CWVecData_t* d = cwvec_data_of(v);
    return d ? d->elem_type : 0;
}

void cwvec_clear(CWValue_t* v) {
    CWVecData_t* d = cwvec_data_of(v);
    if (!d) return;
    d->count = 0;
    if (v) v->length = 0;
}

bool cwvec_extend_with(CWValue_t* v, const CWValue_t* other) {
    if (!v || !other) return false;
    CWVecData_t* d = cwvec_data_of(v);
    const CWVecData_t* od = cwvec_data_of(other);
    if (!d || !od) return false;
    if (d == od) return false; /* v0: 不允许自扩展 */
    if (od->count == 0) return true;
    if (d->count > SIZE_MAX - od->count) return false; /* 溢出 */
    const size_t need = d->count + od->count;
    if (need > d->capacity) {
        size_t nc = d->capacity * 2;
        while (nc < need) nc *= 2;
        void* p = cwmc_realloc(d->items, nc * sizeof(CWValue_t));
        if (!p) return false;
        d->items = (CWValue_t*)p;
        d->capacity = nc;
        v->cursor = nc;
    }
    memcpy(d->items + d->count, od->items,
           od->count * sizeof(CWValue_t));
    d->count = need;
    v->length = need;
    for (size_t i = d->count - od->count; i < d->count; i++) {
        cwgc_barrier((const void*)(uintptr_t)d->items[i].address);
    }
    return true;
}

bool cwvec_insert_at(CWValue_t* v, size_t index, const CWValue_t* cell) {
    if (!v || !cell) return false;
    CWVecData_t* d = cwvec_data_of(v);
    if (!d || index > d->count) return false;
    if (d->count == d->capacity) {
        const size_t nc = d->capacity * 2;
        void* p = cwmc_realloc(d->items, nc * sizeof(CWValue_t));
        if (!p) return false;
        d->items = (CWValue_t*)p;
        d->capacity = nc;
        v->cursor = nc;
    }
    if (index < d->count) {
        memmove(d->items + index + 1, d->items + index,
                (d->count - index) * sizeof(CWValue_t));
    }
    d->items[index] = *cell;
    d->count++;
    v->length = d->count;
    cwgc_barrier((const void*)(uintptr_t)cell->address);
    return true;
}

bool cwvec_index_of(const CWValue_t* v, const CWValue_t* cell,
                    size_t* out_index) {
    if (!v || !cell) return false;
    const CWVecData_t* d = cwvec_data_of(v);
    if (!d) return false;
    for (size_t i = 0; i < d->count; i++) {
        if (cwobj_value_equal(d->elem_type, &d->items[i], cell)) {
            if (out_index) *out_index = i;
            return true;
        }
    }
    if (out_index) *out_index = SIZE_MAX;
    return false;
}

bool cwvec_remove_at(CWValue_t* v, size_t index, CWValue_t* out) {
    if (!v) return false;
    CWVecData_t* d = cwvec_data_of(v);
    if (!d || index >= d->count) return false;
    if (out) *out = d->items[index];
    if (index + 1 < d->count) {
        memmove(d->items + index, d->items + index + 1,
                (d->count - index - 1) * sizeof(CWValue_t));
    }
    d->count--;
    v->length = d->count;
    return true;
}

void cwvec_destroy(CWValue_t* v) {
    CWVecData_t* d = cwvec_data_of(v);
    if (!d) return;
    cwmc_free(d->items);
    cwmc_free(d);
    if (v) {
        v->address = 0;
        v->length  = 0;
        v->cursor  = 0;
    }
}

/* ---- Tuple ---- */

bool cwtuple_init(CWValue_t* v, const int32_t* elem_types,
                  const CWValue_t* cells, size_t count) {
    if (!v || v->address != 0) return false;
    if (count > 0 && (!elem_types || !cells)) return false;
    if (count > SIZE_MAX / sizeof(CWValue_t) - sizeof(CWTupleData_t)) {
        return false;
    }

    const size_t types_bytes = count * sizeof(int32_t);
    const size_t cells_off = (sizeof(CWTupleData_t) + types_bytes + 7u)
        & ~(size_t)7;
    const size_t total = cells_off + count * sizeof(CWValue_t);
    CWTupleData_t* d = (CWTupleData_t*)cwmc_calloc(total);
    if (!d) return false;
    cwgc_set_desc(d, CWGC_DESC_TUPLE_DATA);
    d->kind = CWTuple;
    d->count = count;
    if (count > 0) {
        memcpy(cwtuple_types_of(d), elem_types, types_bytes);
        memcpy(cwtuple_cells_of(d), cells, count * sizeof(CWValue_t));
    }

    v->address = (uint64_t)(uintptr_t)d;
    v->length  = count;
    v->cursor  = 0;
    return true;
}

bool cwtuple_at(const CWValue_t* v, size_t index, CWValue_t* out) {
    if (!v || !out) return false;
    const CWTupleData_t* d = cwtuple_data_of(v);
    if (!d || index >= d->count) return false;
    *out = cwtuple_cells_of((CWTupleData_t*)d)[index];
    return true;
}

int32_t cwtuple_elem_type(const CWValue_t* v, size_t index) {
    const CWTupleData_t* d = cwtuple_data_of(v);
    if (!d || index >= d->count) return 0;
    return cwtuple_types_of((CWTupleData_t*)d)[index];
}

size_t cwtuple_size(const CWValue_t* v) {
    const CWTupleData_t* d = cwtuple_data_of(v);
    return d ? d->count : 0;
}

void cwtuple_destroy(CWValue_t* v) {
    CWTupleData_t* d = cwtuple_data_of(v);
    if (!d) return;
    cwmc_free(d);
    if (v) {
        v->address = 0;
        v->length  = 0;
        v->cursor  = 0;
    }
}

/* ---- Map ---- */

static CWMapEntry_t* cwmap_find(const CWMapData_t* d,
                                int32_t key_type,
                                const CWValue_t* key) {
    for (CWMapEntry_t* e = d->head; e; e = e->next) {
        if (cwobj_value_equal(key_type, &e->key, key)) return e;
    }
    return NULL;
}

bool cwmap_init(CWValue_t* v, int32_t key_type, int32_t value_type) {
    if (!v || v->address != 0) return false;
    CWMapData_t* d = (CWMapData_t*)cwmc_calloc(sizeof(*d));
    if (!d) return false;
    cwgc_set_desc(d, CWGC_DESC_MAP_DATA);
    d->kind = CWMap;
    d->key_type = key_type;
    d->value_type = value_type;

    v->address = (uint64_t)(uintptr_t)d;
    v->length  = 0;
    v->cursor  = 0;
    return true;
}

bool cwmap_put(CWValue_t* v, const CWValue_t* key, const CWValue_t* value) {
    if (!v || !key || !value) return false;
    CWMapData_t* d = cwmap_data_of(v);
    if (!d) return false;

    CWMapEntry_t* e = cwmap_find(d, d->key_type, key);
    if (e) {
        e->value = *value;
        cwgc_barrier((const void*)(uintptr_t)value->address);
        cwgc_barrier((const void*)(uintptr_t)key->address);
        return true;
    }
    e = (CWMapEntry_t*)cwmc_alloc(sizeof(*e));
    if (!e) return false;
    cwgc_set_desc(e, CWGC_DESC_MAP_NODE);
    e->next = d->head;
    e->key = *key;
    e->value = *value;
    d->head = e;
    d->count++;
    v->length = d->count;
    cwgc_barrier((const void*)(uintptr_t)value->address);
    cwgc_barrier((const void*)(uintptr_t)key->address);
    return true;
}

bool cwmap_get(const CWValue_t* v, const CWValue_t* key,
               CWValue_t* out_value) {
    if (!v || !key) return false;
    const CWMapData_t* d = cwmap_data_of(v);
    if (!d) return false;
    CWMapEntry_t* e = cwmap_find(d, d->key_type, key);
    if (!e) return false;
    if (out_value) *out_value = e->value;
    return true;
}

bool cwmap_remove(CWValue_t* v, const CWValue_t* key) {
    if (!v || !key) return false;
    CWMapData_t* d = cwmap_data_of(v);
    if (!d) return false;

    CWMapEntry_t** link = &d->head;
    while (*link) {
        if (cwobj_value_equal(d->key_type, &(*link)->key, key)) {
            CWMapEntry_t* victim = *link;
            *link = victim->next;
            cwmc_free(victim);
            d->count--;
            v->length = d->count;
            return true;
        }
        link = &(*link)->next;
    }
    return false;
}

size_t cwmap_size(const CWValue_t* v) {
    const CWMapData_t* d = cwmap_data_of(v);
    return d ? d->count : 0;
}

int32_t cwmap_key_type(const CWValue_t* v) {
    const CWMapData_t* d = cwmap_data_of(v);
    return d ? d->key_type : 0;
}

int32_t cwmap_value_type(const CWValue_t* v) {
    const CWMapData_t* d = cwmap_data_of(v);
    return d ? d->value_type : 0;
}

void cwmap_clear(CWValue_t* v) {
    CWMapData_t* d = cwmap_data_of(v);
    if (!d) return;
    CWMapEntry_t* e = d->head;
    while (e) {
        CWMapEntry_t* next = e->next;
        cwmc_free(e);
        e = next;
    }
    d->head = NULL;
    d->count = 0;
    if (v) v->length = 0;
}

void cwmap_destroy(CWValue_t* v) {
    CWMapData_t* d = cwmap_data_of(v);
    if (!d) return;
    CWMapEntry_t* e = d->head;
    while (e) {
        CWMapEntry_t* next = e->next;
        cwmc_free(e);
        e = next;
    }
    cwmc_free(d);
    if (v) {
        v->address = 0;
        v->length  = 0;
        v->cursor  = 0;
    }
}

/* ---- Set ---- */

static CWSetEntry_t* cwset_find(const CWSetData_t* d,
                                int32_t elem_type,
                                const CWValue_t* item) {
    for (CWSetEntry_t* e = d->head; e; e = e->next) {
        if (cwobj_value_equal(elem_type, &e->item, item)) return e;
    }
    return NULL;
}

bool cwset_init(CWValue_t* v, int32_t elem_type) {
    if (!v || v->address != 0) return false;
    CWSetData_t* d = (CWSetData_t*)cwmc_calloc(sizeof(*d));
    if (!d) return false;
    cwgc_set_desc(d, CWGC_DESC_SET_DATA);
    d->kind = CWSet;
    d->elem_type = elem_type;

    v->address = (uint64_t)(uintptr_t)d;
    v->length  = 0;
    v->cursor  = 0;
    return true;
}

bool cwset_add(CWValue_t* v, const CWValue_t* item) {
    if (!v || !item) return false;
    CWSetData_t* d = cwset_data_of(v);
    if (!d) return false;

    if (cwset_find(d, d->elem_type, item)) return true; /* 已存在: 幂等 */
    CWSetEntry_t* e = (CWSetEntry_t*)cwmc_alloc(sizeof(*e));
    if (!e) return false;
    cwgc_set_desc(e, CWGC_DESC_SET_NODE);
    e->next = d->head;
    e->item = *item;
    d->head = e;
    d->count++;
    v->length = d->count;
    cwgc_barrier((const void*)(uintptr_t)item->address);
    return true;
}

bool cwset_contains(const CWValue_t* v, const CWValue_t* item) {
    if (!v || !item) return false;
    const CWSetData_t* d = cwset_data_of(v);
    if (!d) return false;
    return cwset_find(d, d->elem_type, item) != NULL;
}

bool cwset_remove(CWValue_t* v, const CWValue_t* item) {
    if (!v || !item) return false;
    CWSetData_t* d = cwset_data_of(v);
    if (!d) return false;

    CWSetEntry_t** link = &d->head;
    while (*link) {
        if (cwobj_value_equal(d->elem_type, &(*link)->item, item)) {
            CWSetEntry_t* victim = *link;
            *link = victim->next;
            cwmc_free(victim);
            d->count--;
            v->length = d->count;
            return true;
        }
        link = &(*link)->next;
    }
    return false;
}

size_t cwset_size(const CWValue_t* v) {
    const CWSetData_t* d = cwset_data_of(v);
    return d ? d->count : 0;
}

int32_t cwset_elem_type(const CWValue_t* v) {
    const CWSetData_t* d = cwset_data_of(v);
    return d ? d->elem_type : 0;
}

void cwset_clear(CWValue_t* v) {
    CWSetData_t* d = cwset_data_of(v);
    if (!d) return;
    CWSetEntry_t* e = d->head;
    while (e) {
        CWSetEntry_t* next = e->next;
        cwmc_free(e);
        e = next;
    }
    d->head = NULL;
    d->count = 0;
    if (v) v->length = 0;
}

void cwset_destroy(CWValue_t* v) {
    CWSetData_t* d = cwset_data_of(v);
    if (!d) return;
    CWSetEntry_t* e = d->head;
    while (e) {
        CWSetEntry_t* next = e->next;
        cwmc_free(e);
        e = next;
    }
    cwmc_free(d);
    if (v) {
        v->address = 0;
        v->length  = 0;
        v->cursor  = 0;
    }
}

/* ---- 迭代 ---- */

void cwvec_iter_begin(const CWValue_t* v, CWindVectorIter_t* out) {
    if (!out) return;
    out->vec = v;
    out->index = 0;
}

bool cwvec_iter_valid(const CWindVectorIter_t* it) {
    if (!it || !it->vec) return false;
    const CWVecData_t* d = cwvec_data_of(it->vec);
    return d && it->index < d->count;
}

bool cwvec_iter_value(const CWindVectorIter_t* it, CWValue_t* out) {
    return it && out && cwvec_at(it->vec, it->index, out);
}

void cwvec_iter_next(CWindVectorIter_t* it) {
    if (it) it->index++;
}

void cwtuple_iter_begin(const CWValue_t* v, CWindTupleIter_t* out) {
    if (!out) return;
    out->tup = v;
    out->index = 0;
}

bool cwtuple_iter_valid(const CWindTupleIter_t* it) {
    if (!it || !it->tup) return false;
    const CWTupleData_t* d = cwtuple_data_of(it->tup);
    return d && it->index < d->count;
}

bool cwtuple_iter_value(const CWindTupleIter_t* it, CWValue_t* out) {
    return it && out && cwtuple_at(it->tup, it->index, out);
}

void cwtuple_iter_next(CWindTupleIter_t* it) {
    if (it) it->index++;
}

void cwmap_iter_begin(const CWValue_t* v, CWindMapIter_t* out) {
    if (!out) return;
    out->map = v;
    out->slot = NULL;
    const CWMapData_t* d = cwmap_data_of(v);
    if (d) out->slot = d->head;
}

bool cwmap_iter_valid(const CWindMapIter_t* it) {
    return it && it->slot != NULL;
}

bool cwmap_iter_key(const CWindMapIter_t* it, CWValue_t* out) {
    if (!it || !out || !it->slot) return false;
    *out = ((const CWMapEntry_t*)it->slot)->key;
    return true;
}

bool cwmap_iter_value(const CWindMapIter_t* it, CWValue_t* out) {
    if (!it || !out || !it->slot) return false;
    *out = ((const CWMapEntry_t*)it->slot)->value;
    return true;
}

void cwmap_iter_next(CWindMapIter_t* it) {
    if (it && it->slot) {
        it->slot = ((const CWMapEntry_t*)it->slot)->next;
    }
}

void cwset_iter_begin(const CWValue_t* v, CWindSetIter_t* out) {
    if (!out) return;
    out->set = v;
    out->slot = NULL;
    const CWSetData_t* d = cwset_data_of(v);
    if (d) out->slot = d->head;
}

bool cwset_iter_valid(const CWindSetIter_t* it) {
    return it && it->slot != NULL;
}

bool cwset_iter_item(const CWindSetIter_t* it, CWValue_t* out) {
    if (!it || !out || !it->slot) return false;
    *out = ((const CWSetEntry_t*)it->slot)->item;
    return true;
}

void cwset_iter_next(CWindSetIter_t* it) {
    if (it && it->slot) {
        it->slot = ((const CWSetEntry_t*)it->slot)->next;
    }
}


/* ---- GC 精确遍历 (todo-35 B 组) ----
 * 语义: walker 在对象被染黑时调用, 遍历"真实存在"的内部引用。
 * 从属槽 (Vector items 数组) 用 cwgc_mark_obj 直接染黑 —— 其内容
 * 由本 walker 精确代管, 不再保守扫描; cell 目标地址经 cwgc_mark_ref
 * 染色 (arena 单元/常量字节不是槽, 自动忽略)。 */

void cwgc_walk_vector_data(void* base, unsigned size) {
    CWVecData_t* d = (CWVecData_t*)base;
    (void)size;
    if (!d || d->kind != CWVector || !d->items) return;
    cwgc_mark_obj(d->items); /* items 槽由本 walker 代管 */
    for (size_t i = 0; i < d->count; i++) {
        cwgc_mark_ref((const void*)(uintptr_t)d->items[i].address);
    }
}

void cwgc_walk_map_data(void* base, unsigned size) {
    CWMapData_t* d = (CWMapData_t*)base;
    (void)size;
    if (!d || d->kind != CWMap) return;
    for (CWMapEntry_t* e = d->head; e; e = e->next) {
        cwgc_mark_ref(e); /* 节点各自成槽, 由节点 walker 继续 */
    }
}

void cwgc_walk_map_node(void* base, unsigned size) {
    CWMapEntry_t* e = (CWMapEntry_t*)base;
    (void)size;
    if (!e) return;
    if (e->next) cwgc_mark_ref(e->next);
    cwgc_mark_ref((const void*)(uintptr_t)e->key.address);
    cwgc_mark_ref((const void*)(uintptr_t)e->value.address);
}

void cwgc_walk_set_data(void* base, unsigned size) {
    CWSetData_t* d = (CWSetData_t*)base;
    (void)size;
    if (!d || d->kind != CWSet) return;
    for (CWSetEntry_t* e = d->head; e; e = e->next) {
        cwgc_mark_ref(e);
    }
}

void cwgc_walk_set_node(void* base, unsigned size) {
    CWSetEntry_t* e = (CWSetEntry_t*)base;
    (void)size;
    if (!e) return;
    if (e->next) cwgc_mark_ref(e->next);
    cwgc_mark_ref((const void*)(uintptr_t)e->item.address);
}

void cwgc_walk_tuple_data(void* base, unsigned size) {
    CWTupleData_t* d = (CWTupleData_t*)base;
    (void)size;
    if (!d || d->kind != CWTuple) return;
    CWValue_t* cells = cwtuple_cells_of(d);
    for (size_t i = 0; i < d->count; i++) {
        cwgc_mark_ref((const void*)(uintptr_t)cells[i].address);
    }
}
