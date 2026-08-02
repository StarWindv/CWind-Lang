/**
 * Copyright (C) 2026/7/31 CWind-Project
 * License: BSD-3.0
 * First  Author : DeepSeek-V4-Flash[version: 2026/07/31, Official]
 * Second Author : StarWindv[Reviewer, Optimizer]
 * Reference Code: 
 *  - [1] StarWindv. cwind_fix_size_queue.h [SourceCode]. CWind-Project(main/a34b76), 2024.
 * Location: rt-src/include/stl/ess/cwind_fix_size_array.h
 */

/**
 * Requirement:
 *  - All elements stored must have the same byte size.
 *  - C11 or later (-std>=c11).
 *
 * Design:
 *  - Storage comes from large blocks acquired with mmap (POSIX) /
 *    VirtualAlloc (Windows). Compile with -DCWFSArray_USE_MALLOC
 *    to fall back to malloc instead.
 *  - A dense block-pointer table makes index -> address O(1):
 *        block = map[index >> shift]
 *        addr  = block + mem_offset + (index & mask) * item_size
 *  - Removed slots are recorded in a free list and recycled by later
 *    pushes (LIFO, same pool idea as the fix-size FIFO queue).
 *  - An occupancy bitmap gives O(1) liveness checks and supports
 *    iteration over live slots only.
 *  - Growth only appends blocks and never moves elements, so element
 *    pointers stay stable for the whole container lifetime.
 *  - Slot holes are recycled by later pushes; mmap'd blocks are returned
 *    to the OS by cwfixa_shrink_to_fit() (trailing empty blocks only) or
 *    by destroy.
 *  - Every slot is aligned to _Alignof(max_align_t). Element types that
 *    require stronger alignment are not supported.
 *  - Not thread-safe.
 *
 * safe variants (cwfixa_safe_*):
 *  - cwfixa_safe_create / cwfixa_safe_create_ex register an element
 *    destructor; the container then calls it on every element it discards
 *    (remove_at / pop with out == NULL, clear, destroy).
 *  - remove_at / pop with a non-NULL out transfers the (shallow) ownership
 *    to the caller: dtor is NOT called on that element.
 *  - containers created with cwfixa_create / cwfixa_create_ex have
 *    dtor == NULL and behave exactly like before (raw bytes, zero overhead).
 */


#ifndef CWFSArray_H

    #define CWFSArray_H

    #include <stddef.h>
    #include <stdbool.h>
    #include <string.h>
    #include <stdlib.h>
    #include <stdarg.h>
    #include <stdint.h>

    #if defined(_WIN32)
        #include <windows.h>
    #else
        #include <sys/mman.h>
        #if !defined(MAP_ANONYMOUS) && defined(MAP_ANON)
            #define MAP_ANONYMOUS MAP_ANON
        #endif
        #if !defined(MAP_ANONYMOUS)
            #define CWFSArray_NEED_MALLOC 1
        #endif
    #endif


    /* elements per block; must stay a power of two for fast index math */
    /* mmap -> 1 * [memory] -> [ 4096 * ele ] */
    #ifndef CWFSArray_POOL_ELEM_COUNT
        #define CWFSArray_POOL_ELEM_COUNT (4096)
    #endif


    /*
     * max_align_t is missing from MSVC's C11 mode, so derive the maximum
     * fundamental alignment from a union of the basic types there.
    */
    #if defined(_MSC_VER)
        typedef union cwfixa_msvc_max_align {
            long double ld;
            long long ll;
            void* p;
            void (*fn)(void);
        } cwfixa_msvc_max_align_t;
        #define CWFSArray_MAX_ALIGN_T cwfixa_msvc_max_align_t
    #else
        #define CWFSArray_MAX_ALIGN_T max_align_t
    #endif


    typedef struct CWFSABlock {
        struct CWFSABlock* next;
        #if !defined(_MSC_VER)
            _Alignas(CWFSArray_MAX_ALIGN_T) char memory[];
        #else
            char memory[]; /* mem_offset is aligned explicitly below */
        #endif
    } CWFSABlock_t;


    typedef struct fixarr {
        CWFSABlock_t** block_map;  /* dense block pointer table (O(1) lookup) */
        size_t         block_count;
        unsigned char* used_bits;  /* occupancy bitmap */
        size_t         bits_bytes;
        size_t*        free_list;  /* recycled slot indices */
        size_t         free_count; /* slots currently available for reuse */
        size_t         free_cap;
        size_t         element_size;    /* user element size */
        size_t         item_size;       /* aligned slot size */
        size_t         mem_offset;      /* block bytes before element area */
        size_t         elems_per_block; /* power of two */
        size_t         block_shift;
        size_t         block_mask;
        size_t         tail;       /* handed-out slots (occupied + free) */
        size_t         capacity;   /* total slots in all blocks */
        size_t         count;      /* live elements */
        void (*dtor)(void*);       /* optional element destructor (safe mode) */
    } CWFSArray_t;


    /* forward declaration: defined in the "O(1) index access" section */
    static inline void* cwfixa_at(CWFSArray_t* arr, size_t index);


    /* memory provider */

    #if defined(CWFSArray_USE_MALLOC) || defined(CWFSArray_NEED_MALLOC)

        static void* cwfixa_block_alloc(size_t size) {
            return malloc(size);
        }

        static void cwfixa_block_free(void* p, size_t size) {
            (void)size;
            free(p);
        }

    #elif defined(_WIN32)

        static void* cwfixa_block_alloc(size_t size) {
            return (void*)VirtualAlloc(NULL, size,
                                       MEM_RESERVE | MEM_COMMIT,
                                       PAGE_READWRITE);
        }

        static void cwfixa_block_free(void* p, size_t size) {
            (void)size;
            if (p) VirtualFree(p, 0, MEM_RELEASE);
        }

    #else

        static void* cwfixa_block_alloc(size_t size) {
            void* p = mmap(NULL, size, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            return (p == MAP_FAILED) ? NULL : p;
        }

        static void cwfixa_block_free(void* p, size_t size) {
            if (p) munmap(p, size);
        }

    #endif


    /* small helpers */

    static inline size_t cwfixa_align_up(size_t v, size_t a) {
        return (v + a - 1) & ~(a - 1);
    }


    static inline size_t cwfixa_next_pow2(size_t v) {
        if (v <= 1) return 1;
        v--;
        v |= v >> 1;
        v |= v >> 2;
        v |= v >> 4;
        v |= v >> 8;
        v |= v >> 16;
        #if SIZE_MAX > 0xFFFFFFFFu
            v |= v >> 32;
        #endif
        return v + 1;
    }


    static inline bool cwfixa_bit_get(const unsigned char* bits, size_t i) {
        return (bits[i >> 3] >> (i & 7)) & 1u;
    }


    static inline void cwfixa_bit_set(unsigned char* bits, size_t i) {
        bits[i >> 3] |= (unsigned char)(1u << (i & 7));
    }


    static inline void cwfixa_bit_clear(unsigned char* bits, size_t i) {
        bits[i >> 3] &= (unsigned char)~(1u << (i & 7));
    }


    /* create / destroy */

    static inline CWFSArray_t* cwfixa_create_ex(size_t element_size,
                                                size_t elems_per_block) {
        if (element_size == 0) return NULL;

        const size_t align = _Alignof(CWFSArray_MAX_ALIGN_T);
        if (element_size > SIZE_MAX - (align - 1)) return NULL;
        const size_t item_size = cwfixa_align_up(element_size, align);

        const size_t epb = cwfixa_next_pow2(elems_per_block);
        if (epb == 0) return NULL; /* elems_per_block too large */

        const size_t mem_offset = cwfixa_align_up(sizeof(CWFSABlock_t), align);
        if (epb > (SIZE_MAX - mem_offset) / item_size) return NULL;
        const size_t block_total = mem_offset + epb * item_size;

        CWFSArray_t* arr = (CWFSArray_t*)malloc(sizeof(CWFSArray_t));
        if (!arr) return NULL;

        CWFSABlock_t* block = (CWFSABlock_t*)cwfixa_block_alloc(block_total);
        if (!block) {
            free(arr);
            return NULL;
        }
        block->next = NULL;

        CWFSABlock_t** map =
            (CWFSABlock_t**)malloc(sizeof(CWFSABlock_t*));
        unsigned char* bits =
            (unsigned char*)calloc(1, (epb + 7) / 8);
        if (!map || !bits) {
            free(map);
            free(bits);
            cwfixa_block_free(block, block_total);
            free(arr);
            return NULL;
        }
        map[0] = block;

        size_t shift = 0, e = epb;
        while (e > 1) {
            e >>= 1;
            shift++;
        }

        arr->block_map       = map;
        arr->block_count     = 1;
        arr->used_bits       = bits;
        arr->bits_bytes      = (epb + 7) / 8;
        arr->free_list       = NULL;
        arr->free_count      = 0;
        arr->free_cap        = 0;
        arr->element_size    = element_size;
        arr->item_size       = item_size;
        arr->mem_offset      = mem_offset;
        arr->elems_per_block = epb;
        arr->block_shift     = shift;
        arr->block_mask      = epb - 1;
        arr->tail            = 0;
        arr->capacity        = epb;
        arr->count           = 0;
        arr->dtor            = NULL;
        return arr;
    }


    static inline CWFSArray_t* cwfixa_create(size_t element_size) {
        return cwfixa_create_ex(element_size, CWFSArray_POOL_ELEM_COUNT);
    }


    static inline void cwfixa_destroy(CWFSArray_t* arr) {
        if (!arr) return;

        if (arr->dtor) {
            for (size_t i = 0; i < arr->tail; i++) {
                if (cwfixa_bit_get(arr->used_bits, i)) {
                    arr->dtor(cwfixa_at(arr, i));
                }
            }
        }

        const size_t block_total = arr->mem_offset
                                 + arr->elems_per_block * arr->item_size;
        for (size_t i = 0; i < arr->block_count; i++) {
            cwfixa_block_free(arr->block_map[i], block_total);
        }
        free(arr->block_map);
        free(arr->used_bits);
        free(arr->free_list);
        free(arr);
    }


    /* O(1) index access -- */

    /**
     * O(1): block = map[index >> shift], then slot inside the block.
     * Valid for every index below capacity (including reserved slots);
     * combine with cwfixa_occupied() to test liveness.
    */
    static inline void* cwfixa_at(CWFSArray_t* arr, size_t index) {
        if (index >= arr->capacity) return NULL;

        const size_t bi  = index >> arr->block_shift;
        const size_t off = (index & arr->block_mask) * arr->item_size;
        return (char*)arr->block_map[bi] + arr->mem_offset + off;
    }


    static inline bool cwfixa_occupied(CWFSArray_t* arr, size_t index) {
        if (index >= arr->tail) return false;
        return cwfixa_bit_get(arr->used_bits, index);
    }


    /* convenience: slot index for a pointer returned by push/at (not O(1)) */
    static inline size_t cwfixa_index_of(CWFSArray_t* arr, const void* ptr) {
        const char* p = (const char*)ptr;
        for (size_t b = 0; b < arr->block_count; b++) {
            const char* base = (const char*)arr->block_map[b] + arr->mem_offset;
            const size_t span = arr->elems_per_block * arr->item_size;
            if (p >= base && p < base + span) {
                return (b << arr->block_shift)
                     | (size_t)((p - base) / arr->item_size);
            }
        }
        return (size_t)-1;
    }


    /* growth / reserve --- */

    static bool cwfixa_grow_to(CWFSArray_t* arr, size_t n) {
        if (n <= arr->capacity) return true;

        const size_t nb = (n - arr->capacity + arr->elems_per_block - 1)
                        / arr->elems_per_block;
        if (nb > (SIZE_MAX - arr->capacity) / arr->elems_per_block) return false;
        if (nb > SIZE_MAX / sizeof(CWFSABlock_t*)) return false;

        const size_t block_total = arr->mem_offset
                                 + arr->elems_per_block * arr->item_size;
        CWFSABlock_t** blocks =
            (CWFSABlock_t**)malloc(nb * sizeof(CWFSABlock_t*));
        if (!blocks) return false;

        size_t got = 0;
        for (; got < nb; got++) {
            blocks[got] = (CWFSABlock_t*)cwfixa_block_alloc(block_total);
            if (!blocks[got]) break;
        }
        if (got < nb) {
            for (size_t i = 0; i < got; i++) {
                cwfixa_block_free(blocks[i], block_total);
            }
            free(blocks);
            return false;
        }

        const size_t new_bc = arr->block_count + nb;
        CWFSABlock_t** new_map =
            (CWFSABlock_t**)malloc(new_bc * sizeof(CWFSABlock_t*));
        if (!new_map) {
            for (size_t i = 0; i < nb; i++) {
                cwfixa_block_free(blocks[i], block_total);
            }
            free(blocks);
            return false;
        }
        memcpy(new_map, arr->block_map,
               arr->block_count * sizeof(CWFSABlock_t*));

        const size_t new_cap  = arr->capacity + nb * arr->elems_per_block;
        const size_t new_bits = (new_cap + 7) / 8;
        unsigned char* new_bits_ptr = (unsigned char*)malloc(new_bits);
        if (!new_bits_ptr) {
            free(new_map);
            for (size_t i = 0; i < nb; i++) {
                cwfixa_block_free(blocks[i], block_total);
            }
            free(blocks);
            return false;
        }
        memcpy(new_bits_ptr, arr->used_bits, arr->bits_bytes);
        memset(new_bits_ptr + arr->bits_bytes, 0,
               new_bits - arr->bits_bytes);

        for (size_t i = 0; i < nb; i++) {
            blocks[i]->next =
                (i == 0) ? new_map[arr->block_count - 1] : blocks[i - 1];
            new_map[arr->block_count + i] = blocks[i];
        }

        free(arr->block_map);
        free(arr->used_bits);
        arr->block_map   = new_map;
        arr->used_bits   = new_bits_ptr;
        arr->bits_bytes  = new_bits;
        arr->block_count = new_bc;
        arr->capacity    = new_cap;
        free(blocks);
        return true;
    }


    /* pre-allocate slot space; reserved slots stay free and count = 0 */
    static inline bool cwfixa_reserve(CWFSArray_t* arr, size_t n) {
        return cwfixa_grow_to(arr, n);
    }


    /**
     * Returns trailing blocks that hold no live element back to the OS.
     * Only the tail can be reclaimed safely: index -> block is a positional
     * mapping (map[index >> shift]), so dropping a middle block would break
     * every element index above it. Live element indices/pointers are
     * untouched; capacity shrinks to the block of the highest live element
     * (or a single base block when empty).
     */
    static inline bool cwfixa_shrink_to_fit(CWFSArray_t* arr) {
        if (arr->block_count <= 1) return true; /* base block always kept */

        size_t hi = 0; /* highest occupied slot index */
        if (arr->count > 0) {
            size_t i = arr->tail;
            while (i-- > 0) {
                if (cwfixa_bit_get(arr->used_bits, i)) {
                    hi = i;
                    break;
                }
            }
        }

        const size_t keep_bc =
            (arr->count == 0) ? 1 : (hi >> arr->block_shift) + 1;
        if (keep_bc >= arr->block_count) return true;

        const size_t new_cap  = keep_bc * arr->elems_per_block;
        const size_t new_bits = (new_cap + 7) / 8;

        unsigned char* new_bits_ptr = (unsigned char*)malloc(new_bits);
        if (!new_bits_ptr) return false;
        memcpy(new_bits_ptr, arr->used_bits, new_bits);

        CWFSABlock_t** new_map =
            (CWFSABlock_t**)malloc(keep_bc * sizeof(CWFSABlock_t*));
        if (!new_map) {
            free(new_bits_ptr);
            return false;
        }
        memcpy(new_map, arr->block_map, keep_bc * sizeof(CWFSABlock_t*));

        /* drop free-list entries pointing into the reclaimed region */
        size_t nf = 0;
        for (size_t i = 0; i < arr->free_count; i++) {
            if (arr->free_list[i] < new_cap) {
                arr->free_list[nf++] = arr->free_list[i];
            }
        }

        const size_t block_total = arr->mem_offset
                                 + arr->elems_per_block * arr->item_size;
        for (size_t b = keep_bc; b < arr->block_count; b++) {
            cwfixa_block_free(arr->block_map[b], block_total);
        }

        free(arr->block_map);
        free(arr->used_bits);
        arr->block_map   = new_map;
        arr->used_bits   = new_bits_ptr;
        arr->bits_bytes  = new_bits;
        arr->block_count = keep_bc;
        arr->capacity    = new_cap;
        arr->free_count  = nf;
        arr->tail        = arr->count + nf;
        return true;
    }


    /* free-list management */

    static bool cwfixa_free_list_reserve(CWFSArray_t* arr, size_t min_cap) {
        if (min_cap <= arr->free_cap) return true;

        size_t nc = arr->free_cap ? arr->free_cap : 16;
        while (nc < min_cap) {
            if (nc > SIZE_MAX / 2) {
                nc = min_cap;
                break;
            }
            nc *= 2;
        }
        if (nc > SIZE_MAX / sizeof(size_t)) return false;

        size_t* nf = (size_t*)malloc(nc * sizeof(size_t));
        if (!nf) return false;
        memcpy(nf, arr->free_list, arr->free_count * sizeof(size_t));
        free(arr->free_list);
        arr->free_list = nf;
        arr->free_cap  = nc;
        return true;
    }


    /* push */

    static inline void* cwfixa_alloc_slot(CWFSArray_t* arr) {
        /* recycle a removed slot first */
        if (arr->free_count > 0) {
            const size_t idx = arr->free_list[--arr->free_count];
            cwfixa_bit_set(arr->used_bits, idx);
            arr->count++;
            return cwfixa_at(arr, idx);
        }

        /* otherwise take the next never-used slot */
        if (arr->tail == arr->capacity) {
            if (arr->capacity > SIZE_MAX - arr->elems_per_block) return NULL;
            if (!cwfixa_grow_to(arr, arr->capacity + arr->elems_per_block)) {
                return NULL;
            }
        }
        const size_t idx = arr->tail++;
        cwfixa_bit_set(arr->used_bits, idx);
        arr->count++;
        return cwfixa_at(arr, idx);
    }


    static inline void* cwfixa_push_copy(CWFSArray_t* arr, const void* data) {
        void* mem = cwfixa_alloc_slot(arr);
        if (!mem) return NULL;
        memcpy(mem, data, arr->element_size);
        return mem;
    }


    static inline void* cwfixa_push_impl(CWFSArray_t* arr,
                                         void (*init_func)(void*, va_list),
                                         ...) {
        void* mem = cwfixa_alloc_slot(arr);
        if (!mem) return NULL;

        va_list args;
        va_start(args, init_func);
        init_func(mem, args);
        va_end(args);
        return mem;
    }


    #define cwfixa_push(arr, init_func, ...) \
        cwfixa_push_impl(arr, (void (*)(void*, va_list))init_func, __VA_ARGS__)
    /**
     * if no args, use the macro `cwfixa_push_0`
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

    #define cwfixa_push_0(arr, init_func) \
        cwfixa_push_impl(arr, (void (*)(void*, va_list))init_func)


    /* remove */

    /**
     * Remove the live element at `index`; optionally copy it to `out`.
     * The slot becomes recyclable for the next push.
     * Returns false on out-of-range / not-live / allocation failure
     * (the element is left untouched in that case).
    */
    static inline bool cwfixa_remove_at(CWFSArray_t* arr, size_t index,
                                        void* out) {
        if (index >= arr->tail
            || !cwfixa_bit_get(arr->used_bits, index)) {
            return false;
        }
        if (!cwfixa_free_list_reserve(arr, arr->free_count + 1)) {
            return false;
        }
        if (out) {
            memcpy(out, cwfixa_at(arr, index), arr->element_size);
        } else if (arr->dtor) {
            arr->dtor(cwfixa_at(arr, index));
        }
        cwfixa_bit_clear(arr->used_bits, index);
        arr->free_list[arr->free_count++] = index;
        arr->count--;
        return true;
    }


    /* highest-index live element, or NULL */
    static inline void* cwfixa_back(CWFSArray_t* arr) {
        if (arr->count == 0) return NULL;
        size_t i = arr->tail;
        while (i-- > 0) {
            if (cwfixa_bit_get(arr->used_bits, i)) return cwfixa_at(arr, i);
        }
        return NULL;
    }


    /* pop the highest-index live element (O(tail) worst case) */
    static inline bool cwfixa_pop(CWFSArray_t* arr, void* out) {
        if (arr->count == 0) return false;
        size_t i = arr->tail;
        while (i-- > 0) {
            if (cwfixa_bit_get(arr->used_bits, i)) {
                return cwfixa_remove_at(arr, i, out);
            }
        }
        return false;
    }


    /* clear */

    /**
     * Marks every slot free again (memory stays with the container and is
     * recycled by later pushes). If the recycle list cannot grow, slots are
     * dropped instead (no crash, but that memory is no longer reused).
     */
    static inline void cwfixa_clear(CWFSArray_t* arr) {
        if (arr->count == 0) return;

        if (!cwfixa_free_list_reserve(arr, arr->tail)) {
            if (arr->dtor) {
                for (size_t i = 0; i < arr->tail; i++) {
                    if (cwfixa_bit_get(arr->used_bits, i)) {
                        arr->dtor(cwfixa_at(arr, i));
                    }
                }
            }
            memset(arr->used_bits, 0, arr->bits_bytes);
            arr->count      = 0;
            arr->free_count = 0;
            return;
        }

        for (size_t i = 0; i < arr->tail; i++) {
            if (cwfixa_bit_get(arr->used_bits, i)) {
                if (arr->dtor) arr->dtor(cwfixa_at(arr, i));
                cwfixa_bit_clear(arr->used_bits, i);
                arr->free_list[arr->free_count++] = i;
            }
        }
        arr->count = 0;
    }


    /* ---- safe variants (automatic element cleanup) ---------------------- */

    /**
     * safe containers call dtor (non-NULL) on every element they discard
     * (remove_at / pop with out == NULL, clear, destroy). When `out` is
     * non-NULL the (shallow) ownership transfers to the caller and dtor is
     * not called on that element.
     */
    static inline CWFSArray_t* cwfixa_safe_create_ex(size_t element_size,
                                                     size_t elems_per_block,
                                                     void (*dtor)(void*)) {
        if (!dtor) return NULL;
        CWFSArray_t* arr = cwfixa_create_ex(element_size, elems_per_block);
        if (arr) arr->dtor = dtor;
        return arr;
    }


    static inline CWFSArray_t* cwfixa_safe_create(size_t element_size,
                                                  void (*dtor)(void*)) {
        return cwfixa_safe_create_ex(element_size, CWFSArray_POOL_ELEM_COUNT,
                                     dtor);
    }


    static inline bool cwfixa_safe_remove_at(CWFSArray_t* arr, size_t index,
                                             void* out) {
        return cwfixa_remove_at(arr, index, out);
    }


    static inline bool cwfixa_safe_pop(CWFSArray_t* arr, void* out) {
        return cwfixa_pop(arr, out);
    }


    static inline void cwfixa_safe_clear(CWFSArray_t* arr) {
        cwfixa_clear(arr);
    }


    static inline void cwfixa_safe_destroy(CWFSArray_t* arr) {
        cwfixa_destroy(arr);
    }


    /* iteration (live slots only) -- */

    /**
     * After modifying the array, the caller must discard old iterators;
     * otherwise a "use-after-free" logic error may occur.
     */
    typedef struct CWFSArrayIter {
        CWFSArray_t* arr;
        size_t index;   /* next slot to examine */
        size_t visited; /* live elements visited so far */
    } CWFSArrayIter_t;


    static inline void cwfixa_iter_skip_holes(CWFSArrayIter_t* it) {
        while (it->index < it->arr->tail
            && !cwfixa_bit_get(it->arr->used_bits, it->index)) {
            it->index++;
        }
    }


    static inline CWFSArrayIter_t cwfixa_begin(CWFSArray_t* arr) {
        CWFSArrayIter_t it = { arr, 0, 0 };
        cwfixa_iter_skip_holes(&it);
        return it;
    }


    static inline bool cwfixa_iter_valid(CWFSArrayIter_t* it) {
        return it->index < it->arr->tail;
    }


    static inline void* cwfixa_iter_value(CWFSArrayIter_t* it) {
        return it->index < it->arr->tail ? cwfixa_at(it->arr, it->index) : NULL;
    }


    static inline void cwfixa_iter_next(CWFSArrayIter_t* it) {
        if (it->index < it->arr->tail) {
            it->visited++;
            it->index++;
            cwfixa_iter_skip_holes(it);
        }
    }


    /* queries */

    static inline size_t cwfixa_size(CWFSArray_t* arr) {
        return arr->count;
    }


    static inline bool cwfixa_empty(CWFSArray_t* arr) {
        return arr->count == 0;
    }


    static inline size_t cwfixa_capacity(CWFSArray_t* arr) {
        return arr->capacity;
    }


    /* slots currently recycled and waiting for reuse */
    static inline size_t cwfixa_free_count(CWFSArray_t* arr) {
        return arr->free_count;
    }

#endif // CWFSArray_H
