/**
 * Copyright (C) 2026 StarWindv
 * License: BSD-3.0
 * Author : StarWindv
 * Location: rt-src/rt/cwind_container.c
 */

#include "../include/object/cwind_container.h"

#include "../include/memory/cwind_memcenter.h"

#include <string.h>

/*
 * 内部数据全部来自内存中心:
 *  - Vector / Tuple: 对象记录数组 (cwmc_alloc / cwmc_realloc)
 *  - Map / Set: 链表节点 (cwmc_alloc / cwmc_free)
 */

typedef struct CWindVectorData {
    unsigned char* items;   /* CWIND_OBJECT_RECORD_SIZE * capacity */
    size_t count;
    size_t capacity;
} CWindVectorData_t;

typedef struct CWindTupleData {
    unsigned char* items;   /* CWIND_OBJECT_RECORD_SIZE * count */
    size_t count;
} CWindTupleData_t;

/* key / value 是 40 字节记录区, 可存放任意对象记录 */
typedef struct CWindMapEntry {
    struct CWindMapEntry* next;
    CWindIntObject_t key;
    CWindIntObject_t value;
} CWindMapEntry_t;

typedef struct CWindMapData {
    CWindMapEntry_t* head;
    size_t count;
} CWindMapData_t;

typedef struct CWindSetEntry {
    struct CWindSetEntry* next;
    CWindIntObject_t item;  /* 40 字节记录区 */
} CWindSetEntry_t;

typedef struct CWindSetData {
    CWindSetEntry_t* head;
    size_t count;
} CWindSetData_t;

#define CWCNT_DEFAULT_CAP ((size_t)16)

static CWObjHandle_t* cwcnt_handle(CWindObject_t* obj) {
    return (CWObjHandle_t*)((char*)obj + sizeof(CWindObject_t));
}

static const CWObjHandle_t* cwcnt_handle_c(const CWindObject_t* obj) {
    return (const CWObjHandle_t*)((const char*)obj + sizeof(CWindObject_t));
}

/* ---- Vector ---- */

bool cwvec_init(CWindVectorObject_t* obj, size_t reserve) {
    if (!obj || !cwobj_type_is(&obj->head, CWVector)) return false;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address != 0) return false;

    const size_t cap = (reserve == 0) ? CWCNT_DEFAULT_CAP : reserve;
    CWindVectorData_t* d = (CWindVectorData_t*)cwmc_calloc(sizeof(*d));
    if (!d) return false;
    d->items = (unsigned char*)cwmc_calloc(cap * CWIND_OBJECT_RECORD_SIZE);
    if (!d->items) {
        cwmc_free(d);
        return false;
    }
    d->capacity = cap;

    h->address = (uint64_t)(uintptr_t)d;
    h->cursor  = cap;
    return true;
}

bool cwvec_push(CWindVectorObject_t* obj, const void* record) {
    if (!obj || !record || !cwobj_type_is(&obj->head, CWVector)) return false;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return false;
    CWindVectorData_t* d = (CWindVectorData_t*)(uintptr_t)h->address;

    if (d->count == d->capacity) {
        const size_t nc = d->capacity * 2;
        void* p = cwmc_realloc(d->items, nc * CWIND_OBJECT_RECORD_SIZE);
        if (!p) return false;
        d->items = (unsigned char*)p;
        d->capacity = nc;
        h->cursor = nc;
    }
    memcpy(d->items + d->count * CWIND_OBJECT_RECORD_SIZE,
           record, CWIND_OBJECT_RECORD_SIZE);
    d->count++;
    h->length = d->count;
    return true;
}

bool cwvec_pop(CWindVectorObject_t* obj, void* out) {
    if (!obj || !cwobj_type_is(&obj->head, CWVector)) return false;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return false;
    CWindVectorData_t* d = (CWindVectorData_t*)(uintptr_t)h->address;
    if (d->count == 0) return false;
    d->count--;
    if (out) {
        memcpy(out, d->items + d->count * CWIND_OBJECT_RECORD_SIZE,
               CWIND_OBJECT_RECORD_SIZE);
    }
    h->length = d->count;
    return true;
}

bool cwvec_at(const CWindVectorObject_t* obj, size_t index, void* out) {
    if (!obj || !out || !cwobj_type_is(&obj->head, CWVector)) return false;
    const CWObjHandle_t* h = cwcnt_handle_c(&obj->head);
    if (h->address == 0) return false;
    const CWindVectorData_t* d =
        (const CWindVectorData_t*)(uintptr_t)h->address;
    if (index >= d->count) return false;
    memcpy(out, d->items + index * CWIND_OBJECT_RECORD_SIZE,
           CWIND_OBJECT_RECORD_SIZE);
    return true;
}

bool cwvec_set(CWindVectorObject_t* obj, size_t index,
               const void* record) {
    if (!obj || !record || !cwobj_type_is(&obj->head, CWVector)) return false;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return false;
    CWindVectorData_t* d = (CWindVectorData_t*)(uintptr_t)h->address;
    if (index >= d->count) return false;
    memcpy(d->items + index * CWIND_OBJECT_RECORD_SIZE,
           record, CWIND_OBJECT_RECORD_SIZE);
    return true;
}

size_t cwvec_size(const CWindVectorObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWVector)) return 0;
    const CWObjHandle_t* h = cwcnt_handle_c(&obj->head);
    if (h->address == 0) return 0;
    return ((const CWindVectorData_t*)(uintptr_t)h->address)->count;
}

void cwvec_clear(CWindVectorObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWVector)) return;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return;
    ((CWindVectorData_t*)(uintptr_t)h->address)->count = 0;
    h->length = 0;
}

void cwvec_destroy(CWindVectorObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWVector)) return;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return;
    CWindVectorData_t* d = (CWindVectorData_t*)(uintptr_t)h->address;
    cwmc_free(d->items);
    cwmc_free(d);
    h->address = 0;
    h->length  = 0;
    h->cursor  = 0;
}

/* ---- Tuple ---- */

bool cwtuple_init(CWindTupleObject_t* obj, const void* records,
                  size_t count) {
    if (!obj || !cwobj_type_is(&obj->head, CWTuple)) return false;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address != 0) return false;
    if (count > 0 && !records) return false;

    CWindTupleData_t* d = (CWindTupleData_t*)cwmc_alloc(sizeof(*d));
    if (!d) return false;
    d->count = count;
    d->items = NULL;
    if (count > 0) {
        d->items = (unsigned char*)cwmc_alloc(
            count * CWIND_OBJECT_RECORD_SIZE);
        if (!d->items) {
            cwmc_free(d);
            return false;
        }
        memcpy(d->items, records, count * CWIND_OBJECT_RECORD_SIZE);
    }

    h->address = (uint64_t)(uintptr_t)d;
    h->length  = count;
    return true;
}

bool cwtuple_at(const CWindTupleObject_t* obj, size_t index, void* out) {
    if (!obj || !out || !cwobj_type_is(&obj->head, CWTuple)) return false;
    const CWObjHandle_t* h = cwcnt_handle_c(&obj->head);
    if (h->address == 0) return false;
    const CWindTupleData_t* d =
        (const CWindTupleData_t*)(uintptr_t)h->address;
    if (index >= d->count) return false;
    memcpy(out, d->items + index * CWIND_OBJECT_RECORD_SIZE,
           CWIND_OBJECT_RECORD_SIZE);
    return true;
}

size_t cwtuple_size(const CWindTupleObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWTuple)) return 0;
    const CWObjHandle_t* h = cwcnt_handle_c(&obj->head);
    if (h->address == 0) return 0;
    return ((const CWindTupleData_t*)(uintptr_t)h->address)->count;
}

void cwtuple_destroy(CWindTupleObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWTuple)) return;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return;
    CWindTupleData_t* d = (CWindTupleData_t*)(uintptr_t)h->address;
    cwmc_free(d->items);
    cwmc_free(d);
    h->address = 0;
    h->length  = 0;
    h->cursor  = 0;
}

/* ---- Map ---- */

static CWindMapEntry_t* cwmap_find(const CWindMapData_t* d,
                                   const void* key) {
    for (CWindMapEntry_t* e = d->head; e; e = e->next) {
        if (cwobj_equal((const CWindObject_t*)&e->key,
                        (const CWindObject_t*)key)) {
            return e;
        }
    }
    return NULL;
}

bool cwmap_init(CWindMapObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWMap)) return false;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address != 0) return false;
    CWindMapData_t* d = (CWindMapData_t*)cwmc_calloc(sizeof(*d));
    if (!d) return false;
    h->address = (uint64_t)(uintptr_t)d;
    return true;
}

bool cwmap_put(CWindMapObject_t* obj, const void* key,
               const void* value) {
    if (!obj || !key || !value || !cwobj_type_is(&obj->head, CWMap)) {
        return false;
    }
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return false;
    CWindMapData_t* d = (CWindMapData_t*)(uintptr_t)h->address;

    CWindMapEntry_t* e = cwmap_find(d, key);
    if (e) {
        memcpy(&e->value, value, CWIND_OBJECT_RECORD_SIZE);
        return true;
    }
    e = (CWindMapEntry_t*)cwmc_alloc(sizeof(*e));
    if (!e) return false;
    e->next = d->head;
    memcpy(&e->key, key, CWIND_OBJECT_RECORD_SIZE);
    memcpy(&e->value, value, CWIND_OBJECT_RECORD_SIZE);
    d->head = e;
    d->count++;
    h->length = d->count;
    return true;
}

bool cwmap_get(const CWindMapObject_t* obj, const void* key,
               void* out_value) {
    if (!obj || !key || !cwobj_type_is(&obj->head, CWMap)) return false;
    const CWObjHandle_t* h = cwcnt_handle_c(&obj->head);
    if (h->address == 0) return false;
    const CWindMapData_t* d =
        (const CWindMapData_t*)(uintptr_t)h->address;
    CWindMapEntry_t* e = cwmap_find(d, key);
    if (!e) return false;
    if (out_value) {
        memcpy(out_value, &e->value, CWIND_OBJECT_RECORD_SIZE);
    }
    return true;
}

bool cwmap_remove(CWindMapObject_t* obj, const void* key) {
    if (!obj || !key || !cwobj_type_is(&obj->head, CWMap)) return false;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return false;
    CWindMapData_t* d = (CWindMapData_t*)(uintptr_t)h->address;

    CWindMapEntry_t** link = &d->head;
    while (*link) {
        if (cwobj_equal((const CWindObject_t*)&(*link)->key,
                        (const CWindObject_t*)key)) {
            CWindMapEntry_t* victim = *link;
            *link = victim->next;
            cwmc_free(victim);
            d->count--;
            h->length = d->count;
            return true;
        }
        link = &(*link)->next;
    }
    return false;
}

size_t cwmap_size(const CWindMapObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWMap)) return 0;
    const CWObjHandle_t* h = cwcnt_handle_c(&obj->head);
    if (h->address == 0) return 0;
    return ((const CWindMapData_t*)(uintptr_t)h->address)->count;
}

void cwmap_clear(CWindMapObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWMap)) return;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return;
    CWindMapData_t* d = (CWindMapData_t*)(uintptr_t)h->address;
    CWindMapEntry_t* e = d->head;
    while (e) {
        CWindMapEntry_t* next = e->next;
        cwmc_free(e);
        e = next;
    }
    d->head = NULL;
    d->count = 0;
    h->length = 0;
}

void cwmap_destroy(CWindMapObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWMap)) return;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return;
    cwmap_clear(obj);
    cwmc_free((void*)(uintptr_t)h->address);
    h->address = 0;
    h->cursor  = 0;
}

/* ---- Set ---- */

static CWindSetEntry_t* cwset_find(const CWindSetData_t* d,
                                   const void* item) {
    for (CWindSetEntry_t* e = d->head; e; e = e->next) {
        if (cwobj_equal((const CWindObject_t*)&e->item,
                        (const CWindObject_t*)item)) {
            return e;
        }
    }
    return NULL;
}

bool cwset_init(CWindSetObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWSet)) return false;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address != 0) return false;
    CWindSetData_t* d = (CWindSetData_t*)cwmc_calloc(sizeof(*d));
    if (!d) return false;
    h->address = (uint64_t)(uintptr_t)d;
    return true;
}

bool cwset_add(CWindSetObject_t* obj, const void* item) {
    if (!obj || !item || !cwobj_type_is(&obj->head, CWSet)) return false;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return false;
    CWindSetData_t* d = (CWindSetData_t*)(uintptr_t)h->address;

    if (cwset_find(d, item)) return true; /* 已存在: 幂等 */
    CWindSetEntry_t* e = (CWindSetEntry_t*)cwmc_alloc(sizeof(*e));
    if (!e) return false;
    e->next = d->head;
    memcpy(&e->item, item, CWIND_OBJECT_RECORD_SIZE);
    d->head = e;
    d->count++;
    h->length = d->count;
    return true;
}

bool cwset_contains(const CWindSetObject_t* obj, const void* item) {
    if (!obj || !item || !cwobj_type_is(&obj->head, CWSet)) return false;
    const CWObjHandle_t* h = cwcnt_handle_c(&obj->head);
    if (h->address == 0) return false;
    const CWindSetData_t* d =
        (const CWindSetData_t*)(uintptr_t)h->address;
    return cwset_find(d, item) != NULL;
}

bool cwset_remove(CWindSetObject_t* obj, const void* item) {
    if (!obj || !item || !cwobj_type_is(&obj->head, CWSet)) return false;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return false;
    CWindSetData_t* d = (CWindSetData_t*)(uintptr_t)h->address;

    CWindSetEntry_t** link = &d->head;
    while (*link) {
        if (cwobj_equal((const CWindObject_t*)&(*link)->item,
                        (const CWindObject_t*)item)) {
            CWindSetEntry_t* victim = *link;
            *link = victim->next;
            cwmc_free(victim);
            d->count--;
            h->length = d->count;
            return true;
        }
        link = &(*link)->next;
    }
    return false;
}

size_t cwset_size(const CWindSetObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWSet)) return 0;
    const CWObjHandle_t* h = cwcnt_handle_c(&obj->head);
    if (h->address == 0) return 0;
    return ((const CWindSetData_t*)(uintptr_t)h->address)->count;
}

void cwset_clear(CWindSetObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWSet)) return;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return;
    CWindSetData_t* d = (CWindSetData_t*)(uintptr_t)h->address;
    CWindSetEntry_t* e = d->head;
    while (e) {
        CWindSetEntry_t* next = e->next;
        cwmc_free(e);
        e = next;
    }
    d->head = NULL;
    d->count = 0;
    h->length = 0;
}

void cwset_destroy(CWindSetObject_t* obj) {
    if (!obj || !cwobj_type_is(&obj->head, CWSet)) return;
    CWObjHandle_t* h = cwcnt_handle(&obj->head);
    if (h->address == 0) return;
    cwset_clear(obj);
    cwmc_free((void*)(uintptr_t)h->address);
    h->address = 0;
    h->cursor  = 0;
}

/* ---- 迭代 ---- */

CWindVectorIter_t cwvec_iter_begin(const CWindVectorObject_t* obj) {
    CWindVectorIter_t it = { obj, 0 };
    return it;
}

bool cwvec_iter_valid(const CWindVectorIter_t* it) {
    if (!it || !it->vec || !cwobj_type_is(&it->vec->head, CWVector)) {
        return false;
    }
    const CWObjHandle_t* h = cwcnt_handle_c(&it->vec->head);
    if (h->address == 0) return false;
    const CWindVectorData_t* d =
        (const CWindVectorData_t*)(uintptr_t)h->address;
    return it->index < d->count;
}

bool cwvec_iter_value(const CWindVectorIter_t* it, void* out) {
    return it && out && cwvec_at(it->vec, it->index, out);
}

void cwvec_iter_next(CWindVectorIter_t* it) {
    if (it) it->index++;
}

CWindTupleIter_t cwtuple_iter_begin(const CWindTupleObject_t* obj) {
    CWindTupleIter_t it = { obj, 0 };
    return it;
}

bool cwtuple_iter_valid(const CWindTupleIter_t* it) {
    if (!it || !it->tup || !cwobj_type_is(&it->tup->head, CWTuple)) {
        return false;
    }
    const CWObjHandle_t* h = cwcnt_handle_c(&it->tup->head);
    if (h->address == 0) return false;
    const CWindTupleData_t* d =
        (const CWindTupleData_t*)(uintptr_t)h->address;
    return it->index < d->count;
}

bool cwtuple_iter_value(const CWindTupleIter_t* it, void* out) {
    return it && out && cwtuple_at(it->tup, it->index, out);
}

void cwtuple_iter_next(CWindTupleIter_t* it) {
    if (it) it->index++;
}

CWindMapIter_t cwmap_iter_begin(const CWindMapObject_t* obj) {
    CWindMapIter_t it = { obj, NULL };
    if (obj && cwobj_type_is(&obj->head, CWMap)) {
        const CWObjHandle_t* h = cwcnt_handle_c(&obj->head);
        if (h->address != 0) {
            const CWindMapData_t* d =
                (const CWindMapData_t*)(uintptr_t)h->address;
            it.slot = d->head;
        }
    }
    return it;
}

bool cwmap_iter_valid(const CWindMapIter_t* it) {
    return it && it->slot != NULL;
}

bool cwmap_iter_key(const CWindMapIter_t* it, void* out) {
    if (!it || !out || !it->slot) return false;
    const CWindMapEntry_t* e = (const CWindMapEntry_t*)it->slot;
    memcpy(out, &e->key, CWIND_OBJECT_RECORD_SIZE);
    return true;
}

bool cwmap_iter_value(const CWindMapIter_t* it, void* out) {
    if (!it || !out || !it->slot) return false;
    const CWindMapEntry_t* e = (const CWindMapEntry_t*)it->slot;
    memcpy(out, &e->value, CWIND_OBJECT_RECORD_SIZE);
    return true;
}

void cwmap_iter_next(CWindMapIter_t* it) {
    if (it && it->slot) {
        it->slot = ((const CWindMapEntry_t*)it->slot)->next;
    }
}

CWindSetIter_t cwset_iter_begin(const CWindSetObject_t* obj) {
    CWindSetIter_t it = { obj, NULL };
    if (obj && cwobj_type_is(&obj->head, CWSet)) {
        const CWObjHandle_t* h = cwcnt_handle_c(&obj->head);
        if (h->address != 0) {
            const CWindSetData_t* d =
                (const CWindSetData_t*)(uintptr_t)h->address;
            it.slot = d->head;
        }
    }
    return it;
}

bool cwset_iter_valid(const CWindSetIter_t* it) {
    return it && it->slot != NULL;
}

bool cwset_iter_item(const CWindSetIter_t* it, void* out) {
    if (!it || !out || !it->slot) return false;
    const CWindSetEntry_t* e = (const CWindSetEntry_t*)it->slot;
    memcpy(out, &e->item, CWIND_OBJECT_RECORD_SIZE);
    return true;
}

void cwset_iter_next(CWindSetIter_t* it) {
    if (it && it->slot) {
        it->slot = ((const CWindSetEntry_t*)it->slot)->next;
    }
}
