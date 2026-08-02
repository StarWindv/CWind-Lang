/**
 * Copyright (C) 2026/6/23 CWind-Project
 * License: BSD-3.0
 * Author: StarWindv
 * Location: src/include/stl/ess/cwind_fix_size_queue.h
 */

/**
 * Update At: 2026/07/31
 * Optimizer: DeepSeek-v4-Flash[version: 2026/07/31, Official]
 * Reviewer : StarWindv
 * Overview of Changes:
 *  - malloc -> mmap / VirtualAlloc, Reduce the rate of memory fragmentation
 *  - The starting address of the node area
 *      is changed to be explicitly aligned for calculation,
 *      avoiding potential misalignment of 16-byte aligned
 *      element types under GCC/Clang.
 *  - create adds overflow protection.
 *      When an element is too large, it returns NULL
 *      instead of calculating an overflow
 */


/**
 * Requirement:
 *  - The objects stored therein must be of the same size.
 *
 * Require: -std>=c11
 * After testing,
 *  this design is compatible with
 *  clang and gcc on Windows and Linux,
 *  as well as msvc.
 *
 * Users should take the comments
 *  present in this document seriously.
 *
 * 2026/7/31: pool blocks are now backed by mmap (POSIX) / VirtualAlloc
 *  (Windows), so they live outside the heap and no longer fragment it.
 *  Define CWFSQueue_USE_MALLOC to compile the old malloc-backed path.
 * 2026/8/1: cwfixq_shrink_to_fit() returns trailing blocks that contain no
 *  live element to the OS; live element pointers stay stable.
 *
 * safe variants (cwfixq_safe_*):
 *  - cwfixq_safe_create registers an element destructor; the container then
 *    calls it on every element it discards (pop, pop_copy with out == NULL,
 *    clear, destroy).
 *  - pop_copy with a non-NULL out transfers the (shallow) ownership to the
 *    caller: dtor is NOT called on that element.
 *  - containers created with cwfixq_create have dtor == NULL and behave
 *    exactly like before (raw bytes, zero overhead).
 */


#ifndef CWFixSizeQ_H

    #define CWFixSizeQ_H

    #include <stddef.h>
    #include <stdbool.h>
    #include <string.h>
    #include <stdlib.h>
    #include <stdarg.h>
    #include <stdalign.h>
    #include <stdint.h>

    #if defined(_WIN32)
        #include <windows.h>
    #else
        #include <sys/mman.h>
        #if !defined(MAP_ANONYMOUS) && defined(MAP_ANON)
            #define MAP_ANONYMOUS MAP_ANON
        #endif
        #if !defined(MAP_ANONYMOUS)
            #define CWFSQueue_NEED_MALLOC 1
        #endif
    #endif


    /*
     * max_align_t is missing from MSVC's C11 mode, so derive the maximum
     * fundamental alignment from a union of the basic types there.
     */
    #if defined(_MSC_VER)
        typedef union cwfixq_msvc_max_align {
            long double ld;
            long long ll;
            void* p;
            void (*fn)(void);
        } cwfixq_msvc_max_align_t;
        #define CWFSQueue_MAX_ALIGN_T cwfixq_msvc_max_align_t
    #else
        #define CWFSQueue_MAX_ALIGN_T max_align_t
    #endif


    #if defined(CWFSQueue_USE_MALLOC) || defined(CWFSQueue_NEED_MALLOC)

        static void* cwfixq_block_alloc(size_t size) {
            return malloc(size);
        }

        static void cwfixq_block_free(void* p, size_t size) {
            (void)size;
            free(p);
        }

    #elif defined(_WIN32)

        static void* cwfixq_block_alloc(size_t size) {
            return (void*)VirtualAlloc(NULL, size,
                                       MEM_RESERVE | MEM_COMMIT,
                                       PAGE_READWRITE);
        }

        static void cwfixq_block_free(void* p, size_t size) {
            (void)size;
            if (p) VirtualFree(p, 0, MEM_RELEASE);
        }

    #else

        static void* cwfixq_block_alloc(size_t size) {
            void* p = mmap(NULL, size, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            return (p == MAP_FAILED) ? NULL : p;
        }

        static void cwfixq_block_free(void* p, size_t size) {
            if (p) munmap(p, size);
        }

    #endif


    static inline size_t cwfixq_align_up(size_t v, size_t a) {
        return (v + a - 1) & ~(a - 1);
    }


    /*
     * Nodes per block. Keep it reasonably large: mmap/VirtualAlloc reserve
     * whole pages (64 KiB granularity on Windows), so tiny blocks would
     * waste virtual address space.
     */
    #ifndef CWFixSizeQ_POOL_BLOCK_COUNT
        #define CWFixSizeQ_POOL_BLOCK_COUNT ( 1024 )
    #endif


    typedef struct CWFSQNode {
        struct CWFSQNode* next;
        #if !defined(_MSC_VER)
            _Alignas(CWFSQueue_MAX_ALIGN_T) char data[];
        #else
            char data[]; /* node stride below keeps data aligned */
        #endif
    } CWFSQNode_t;


    typedef struct CWFSQBlock {
        struct CWFSQBlock* next;
        char memory[];
    } CWFSQBlock_t;


    typedef struct fixq {
        CWFSQNode_t* head;
        CWFSQNode_t* tail;
        CWFSQBlock_t* pool;
        CWFSQNode_t* free_list;
        size_t element_size;
        size_t count;
        size_t pool_item_size;
        size_t pool_block_count;
        size_t pool_mem_offset;
        void (*dtor)(void*); /* optional element destructor (safe mode) */
    } CWFSQueue_t;


    typedef struct CWFSQIter {
        CWFSQueue_t* fixq;
        CWFSQNode_t* current;
    } CWFSQIter_t;


    static inline CWFSQueue_t* cwfixq_create(size_t element_size) {
        CWFSQueue_t* fq = (CWFSQueue_t*)malloc(sizeof(CWFSQueue_t));
        if (!fq) return NULL;

        fq->head    = NULL;
        fq->tail    = NULL;
        fq->pool    = NULL;
        fq->free_list = NULL;
        fq->dtor     = NULL;
        fq->element_size = element_size;
        fq->count   = 0;
        fq->pool_block_count = CWFixSizeQ_POOL_BLOCK_COUNT;

        const size_t align = _Alignof(CWFSQueue_MAX_ALIGN_T);
        if (element_size > SIZE_MAX - sizeof(CWFSQNode_t) - (align - 1)) {
            free(fq);
            return NULL;
        }
        const size_t node_size = sizeof(CWFSQNode_t) + element_size;
        fq->pool_item_size  = cwfixq_align_up(node_size, align);
        fq->pool_mem_offset = cwfixq_align_up(sizeof(CWFSQBlock_t), align);
        if (fq->pool_block_count
            > (SIZE_MAX - fq->pool_mem_offset) / fq->pool_item_size) {
            free(fq);
            return NULL;
        }

        const size_t block_total = fq->pool_mem_offset
                                 + fq->pool_block_count * fq->pool_item_size;
        CWFSQBlock_t* block = (CWFSQBlock_t*)cwfixq_block_alloc(block_total);
        if (!block) {
            free(fq);
            return NULL;
        }

        block->next = NULL;
        fq->pool    = block;

        char* ptr = (char*)block + fq->pool_mem_offset;
        for (size_t i = 0; i < fq->pool_block_count; i++) {
            CWFSQNode_t* node = (CWFSQNode_t*)ptr;
            node->next = fq->free_list;
            fq->free_list = node;
            ptr += fq->pool_item_size;
        }

        return fq;
    }


    static bool cwfixq_expand_pool(CWFSQueue_t* fq) {
        const size_t block_total = fq->pool_mem_offset
                                 + fq->pool_block_count * fq->pool_item_size;
        CWFSQBlock_t* block = (CWFSQBlock_t*)cwfixq_block_alloc(block_total);
        if (!block) return false;

        block->next = fq->pool;
        fq->pool    = block;

        char* ptr = (char*)block + fq->pool_mem_offset;
        for (size_t i = 0; i < fq->pool_block_count; i++) {
            CWFSQNode_t* node = (CWFSQNode_t*)ptr;
            node->next = fq->free_list;
            fq->free_list = node;
            ptr += fq->pool_item_size;
        }

        return true;
    }


    static inline CWFSQNode_t* cwfixq_alloc_node(CWFSQueue_t* fq) {
        if (!fq->free_list) {
            if (!cwfixq_expand_pool(fq)) return NULL;
        }
        CWFSQNode_t* node = fq->free_list;
        fq->free_list = node->next;
        return node;
    }


    static inline void cwfixq_free_node(CWFSQueue_t* fq, CWFSQNode_t* node) {
        node->next = fq->free_list;
        fq->free_list = node;
    }


    static inline void cwfixq_discard_node(CWFSQueue_t* fq,
                                           CWFSQNode_t* node) {
        if (fq->dtor) fq->dtor(node->data);
        cwfixq_free_node(fq, node);
    }


    static inline void* cwfixq_emplace_back_impl(CWFSQueue_t* fq,
                                                void (*init_func)(void*, va_list),
                                                ...) {
        CWFSQNode_t* node = cwfixq_alloc_node(fq);
        if (!node) return NULL;

        node->next = NULL;

        va_list args;
        va_start(args, init_func);
        init_func(node->data, args);
        va_end(args);

        if (!fq->tail) {
            fq->head = fq->tail = node;
        } else {
            fq->tail->next = node;
            fq->tail = node;
        }

        fq->count++;
        return node->data;
    }


    #define cwfixq_emplace_back(fq, init_func, ...) \
        cwfixq_emplace_back_impl(fq, (void (*)(void*, va_list))init_func, __VA_ARGS__)
    /**
     * if no args, you should use the macro function `cwfixq_emplace_back_0`
     * e.g.:
     *
     * ```c
     * typedef struct { int x; float y; } MyType;
     *
     * init_func(void* memory, va_list ap) {
     *     MyType* obj = (MyType*)memory;
     *     obj->x = va_arg(ap, int);
     *     obj->y = va_arg(ap, float);
     * }
     * ```
     */


    #define cwfixq_emplace_back_0(fq, init_func) \
        cwfixq_emplace_back_impl(fq, (void (*)(void*, va_list))init_func)


    static inline void* cwfixq_push_copy(CWFSQueue_t* fq, const void* data) {
        CWFSQNode_t* node = cwfixq_alloc_node(fq);
        if (!node) return NULL;

        node->next = NULL;
        memcpy(node->data, data, fq->element_size);

        if (!fq->tail) {
            fq->head = fq->tail = node;
        } else {
            fq->tail->next = node;
            fq->tail = node;
        }

        fq->count++;
        return node->data;
    }


    static inline void cwfixq_pop(CWFSQueue_t* fq) {
        if (!fq->head) return;

        CWFSQNode_t* node = fq->head;
        fq->head = node->next;

        if (!fq->head) {
            fq->tail = NULL;
        }

        fq->count--;
        cwfixq_discard_node(fq, node);
    }


    static inline bool cwfixq_pop_copy(CWFSQueue_t* fq, void* out) {
        if (!fq->head) return false;

        CWFSQNode_t* node = fq->head;
        fq->head = node->next;

        if (!fq->head) {
            fq->tail = NULL;
        }

        if (out) {
            memcpy(out, node->data, fq->element_size);
        } else if (fq->dtor) {
            fq->dtor(node->data);
        }

        fq->count--;
        cwfixq_free_node(fq, node);
        return true;
    }


    static inline void* cwfixq_front(CWFSQueue_t* fq) {
        return fq->head ? fq->head->data : NULL;
    }


    static inline void* cwfixq_back(CWFSQueue_t* fq) {
        return fq->tail ? fq->tail->data : NULL;
    }


    static inline CWFSQIter_t cwfixq_begin(CWFSQueue_t* fq) {
        return (CWFSQIter_t){fq, fq->head};
    }


    /**
     * After modifying the queue,
     *  - the caller must manually discard the old iterator
     *  - otherwise, a "use-after-free" logic error will occur
     *
     * There is no need to waste this much space
     *   to achieve automatic failure.
     * Sacrificing performance for safety is not worth it.
     */
    static inline bool cwfixq_iter_valid(CWFSQIter_t* iter) {
        return iter->current != NULL;
    }


    static inline void* cwfixq_iter_value(CWFSQIter_t* iter) {
        return iter->current ? iter->current->data : NULL;
    }


    static inline void cwfixq_iter_next(CWFSQIter_t* iter) {
        if (iter->current) {
            iter->current = iter->current->next;
        }
    }


    static inline void cwfixq_clear(CWFSQueue_t* fq) {
        while (fq->head) {
            CWFSQNode_t* node = fq->head;
            fq->head = node->next;
            cwfixq_discard_node(fq, node);
        }
        fq->tail = NULL;
        fq->count = 0;
    }


    static inline void cwfixq_destroy(CWFSQueue_t* fq) {
        if (!fq) return;

        if (fq->dtor) {
            for (CWFSQNode_t* node = fq->head; node; node = node->next) {
                fq->dtor(node->data);
            }
        }

        const size_t block_total = fq->pool_mem_offset
                                 + fq->pool_block_count * fq->pool_item_size;
        CWFSQBlock_t* block = fq->pool;
        while (block) {
            CWFSQBlock_t* next = block->next;
            cwfixq_block_free(block, block_total);
            block = next;
        }

        free(fq);
    }


    /**
     * Returns pool blocks that contain no live element back to the OS.
     * The queue links nodes by pointer (no positional index mapping), so
     * any block without live nodes could be reclaimed; for simplicity this
     * reclaims the oldest blocks first and always keeps one base block.
     * Live element pointers and iterators stay valid; free nodes inside
     * reclaimed blocks are dropped from the free list.
     */
    static inline bool cwfixq_shrink_to_fit(CWFSQueue_t* fq) {
        if (!fq->pool || !fq->pool->next) return true; /* 0/1 blocks: no-op */

        const size_t block_total = fq->pool_mem_offset
                                 + fq->pool_block_count * fq->pool_item_size;

        /* snapshot the block chain: blocks[0] = newest ... [nb-1] = oldest */
        size_t nb = 0;
        for (CWFSQBlock_t* b = fq->pool; b; b = b->next) nb++;
        CWFSQBlock_t** blocks =
            (CWFSQBlock_t**)malloc(nb * sizeof(CWFSQBlock_t*));
        if (!blocks) return false;
        size_t i = 0;
        for (CWFSQBlock_t* b = fq->pool; b; b = b->next) blocks[i++] = b;

        /* mark blocks containing at least one live node */
        unsigned char* has_live = (unsigned char*)calloc(1, nb);
        if (!has_live) {
            free(blocks);
            return false;
        }
        for (CWFSQNode_t* n = fq->head; n; n = n->next) {
            for (size_t k = 0; k < nb; k++) {
                const char* base =
                    (const char*)blocks[k] + fq->pool_mem_offset;
                const char* end =
                    base + fq->pool_block_count * fq->pool_item_size;
                if ((const char*)n >= base && (const char*)n < end) {
                    has_live[k] = 1;
                    break;
                }
            }
        }

        /* reclaim the oldest run of live-free blocks, keep >= 1 */
        size_t keep = nb;
        while (keep > 1 && !has_live[keep - 1]) keep--;
        free(has_live);
        if (keep == nb) {
            free(blocks);
            return true;
        }

        /* rebuild the free list without nodes in reclaimed blocks */
        CWFSQNode_t* fl_head = NULL;
        CWFSQNode_t* fl_tail = NULL;
        CWFSQNode_t* cur = fq->free_list;
        while (cur) {
            CWFSQNode_t* next = cur->next;
            int in_reclaimed = 0;
            for (size_t k = keep; k < nb; k++) {
                const char* base =
                    (const char*)blocks[k] + fq->pool_mem_offset;
                const char* end =
                    base + fq->pool_block_count * fq->pool_item_size;
                if ((const char*)cur >= base && (const char*)cur < end) {
                    in_reclaimed = 1;
                    break;
                }
            }
            if (!in_reclaimed) {
                if (!fl_tail) {
                    fl_head = fl_tail = cur;
                } else {
                    fl_tail->next = cur;
                    fl_tail = cur;
                }
            }
            cur = next;
        }
        if (fl_tail) fl_tail->next = NULL;
        fq->free_list = fl_head;

        /* unlink and free the reclaimed tail blocks */
        blocks[keep - 1]->next = NULL;
        for (size_t k = keep; k < nb; k++) {
            cwfixq_block_free(blocks[k], block_total);
        }
        free(blocks);
        return true;
    }


    /* ---- safe variants (automatic element cleanup) ---------------------- */

    /**
     * Creates a "safe" queue: dtor (non-NULL) is called on every element the
     * container discards, so elements owning heap memory do not leak.
     * pop_copy with a non-NULL `out` transfers (shallow) ownership to the
     * caller instead -- dtor is not called on that element.
     */
    static inline CWFSQueue_t* cwfixq_safe_create(size_t element_size,
                                                  void (*dtor)(void*)) {
        if (!dtor) return NULL;
        CWFSQueue_t* fq = cwfixq_create(element_size);
        if (fq) fq->dtor = dtor;
        return fq;
    }


    static inline void cwfixq_safe_pop(CWFSQueue_t* fq) {
        cwfixq_pop(fq);
    }


    static inline bool cwfixq_safe_pop_copy(CWFSQueue_t* fq, void* out) {
        return cwfixq_pop_copy(fq, out);
    }


    static inline void cwfixq_safe_clear(CWFSQueue_t* fq) {
        cwfixq_clear(fq);
    }


    static inline void cwfixq_safe_destroy(CWFSQueue_t* fq) {
        cwfixq_destroy(fq);
    }


    static inline size_t cwfixq_size(CWFSQueue_t* fq) {
        return fq->count;
    }

    
    static inline bool cwfixq_empty(CWFSQueue_t* fq) {
        return fq->count == 0;
    }

#endif // CWFixSizeQ_H
