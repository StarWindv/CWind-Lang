/**
 * Copyright (C) 2026/8/2 CWind-Project
 * License: BSD-3.0
 * First  Author : DeepSeek-V4-Flash[version: 2026/08/02, Official]
 * Second Author : StarWindv[Reviewer, Optimizer]
 * Reference Code:
 *  - [1] StarWindv. cwind_fix_size_queue.h [SourceCode]. CWind-Project(main/a34b76), 2024.
 *  - [2] DeepSeek-V4-Flash, StarWindv. cwind_fix_size_array.h [SourceCode]. CWind-Project(main/a34b76), 2026.
 * Location: rt-src/include/stl/cwind_map.h
 */

/**
 * Requirement:
 *  - Generic map: keys and values are opaque void* (any type the caller owns).
 *  - Key ordering is provided by the user via a comparator; NULL falls back to
 *    pointer-address ordering (uintptr_t).
 *  - C11 or later (-std>=c11).
 *
 * Design:
 *  - Red-black tree; every node embeds one key pointer and one value pointer,
 *    so get/put/remove walk O(log n) nodes. The red-black invariant bounds
 *    the height by 2*log2(n+1).
 *  - Node storage comes from large blocks acquired with mmap (POSIX) /
 *    VirtualAlloc (Windows) -- the same pool idea as the fix-size queue and
 *    array. Compile with -DCWMap_USE_MALLOC to fall back to malloc.
 *  - The pool is lazy: an empty map reserves no memory; the first block
 *    (default 1024 nodes, ~48 KB) is acquired on the first insert. Maps that
 *    stay small should use cwmap_create_ex(cmp, n) with a small n (e.g. 16/64).
 *  - Removed nodes are pushed onto a LIFO free list and recycled by later
 *    inserts. Trailing empty pool blocks are returned to the OS by
 *    cwmap_shrink_to_fit(); destroy returns everything.
 *  - Automatic cleanup: cwmap_safe_create*() registers key/value destructors;
 *    they are called whenever an entry is discarded (put overwriting an
 *    existing key, remove, clear, destroy). cwmap_take() transfers ownership
 *    to the caller and does NOT call the destructors.
 *  - Contract: if a key and its value point to the same allocation
 *    (set-style usage), register only one of the two destructors, or keep the
 *    pointers in sync on overwrite; the implementation additionally skips the
 *    second destructor call when both slots hold the same pointer.
 *  - Nesting: store a child map pointer as a value and use a value destructor
 *    that destroys the child map; child maps are then freed automatically.
 *  - Not thread-safe.
 */


#ifndef CWMap_H

    #define CWMap_H

    #include <stddef.h>
    #include <stdbool.h>
    #include <stdlib.h>
    #include <stdint.h>

    #if defined(_WIN32)
        #include <windows.h>
    #else
        #include <sys/mman.h>
        #if !defined(MAP_ANONYMOUS) && defined(MAP_ANON)
            #define MAP_ANONYMOUS MAP_ANON
        #endif
        #if !defined(MAP_ANONYMOUS)
            #define CWMap_NEED_MALLOC 1
        #endif
    #endif


    /* nodes per block; block totals stay reasonably large for mmap/VirtualAlloc */
    #ifndef CWMap_POOL_NODE_COUNT
        #define CWMap_POOL_NODE_COUNT (1024)
    #endif


    /*
     * max_align_t is missing from MSVC's C11 mode, so derive the maximum
     * fundamental alignment from a union of the basic types there.
     */
    #if defined(_MSC_VER)
        typedef union cwmap_msvc_max_align {
            long double ld;
            long long ll;
            void* p;
            void (*fn)(void);
        } cwmap_msvc_max_align_t;
        #define CWMap_MAX_ALIGN_T cwmap_msvc_max_align_t
    #else
        #define CWMap_MAX_ALIGN_T max_align_t
    #endif


    /* memory provider */

    #if defined(CWMap_USE_MALLOC) || defined(CWMap_NEED_MALLOC)

        static void* cwmap_block_alloc(size_t size) {
            return malloc(size);
        }

        static void cwmap_block_free(void* p, size_t size) {
            (void)size;
            free(p);
        }

    #elif defined(_WIN32)

        static void* cwmap_block_alloc(size_t size) {
            return (void*)VirtualAlloc(NULL, size,
                                       MEM_RESERVE | MEM_COMMIT,
                                       PAGE_READWRITE);
        }

        static void cwmap_block_free(void* p, size_t size) {
            (void)size;
            if (p) VirtualFree(p, 0, MEM_RELEASE);
        }

    #else

        static void* cwmap_block_alloc(size_t size) {
            void* p = mmap(NULL, size, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            return (p == MAP_FAILED) ? NULL : p;
        }

        static void cwmap_block_free(void* p, size_t size) {
            if (p) munmap(p, size);
        }

    #endif


    /* small helpers */

    static inline size_t cwmap_align_up(size_t v, size_t a) {
        return (v + a - 1) & ~(a - 1);
    }


    /* tree colors */
    typedef enum CWMapColor {
        CWMAP_RED   = 0,
        CWMAP_BLACK = 1
    } CWMapColor_t;


    /* node embeds one key pointer + one value pointer; free nodes reuse
       `parent` as the free-list link (never part of the tree while free) */
    typedef struct CWMapNode {
        struct CWMapNode* parent;
        struct CWMapNode* left;
        struct CWMapNode* right;
        unsigned char color;
        void* key;
        void* value;
    } CWMapNode_t;


    typedef struct CWMapBlock {
        struct CWMapBlock* next;
        char memory[];
    } CWMapBlock_t;


    typedef struct CWMap {
        CWMapNode_t*  root;
        CWMapBlock_t* pool;
        CWMapNode_t*  free_list;    /* LIFO recycled nodes */
        size_t count;
        size_t free_count;
        size_t block_count;
        size_t node_stride;
        size_t pool_mem_offset;
        size_t pool_block_count;    /* nodes per block */
        int (*compare)(const void* k1, const void* k2); /* NULL => address order */
        void (*key_dtor)(void*);    /* optional key destructor (safe mode) */
        void (*value_dtor)(void*);  /* optional value destructor (safe mode) */
    } CWMap_t;


    typedef struct CWMapIter {
        CWMap_t*     map;
        CWMapNode_t* current;
    } CWMapIter_t;


    /* key comparison */

    static inline int cwmap_compare_keys(CWMap_t* m,
                                         const void* a, const void* b) {
        if (m->compare) return m->compare(a, b);
        const uintptr_t x = (uintptr_t)a;
        const uintptr_t y = (uintptr_t)b;
        return (x > y) - (x < y);
    }


    /* pool management */

    static bool cwmap_expand_pool(CWMap_t* m) {
        const size_t block_total = m->pool_mem_offset
                                 + m->pool_block_count * m->node_stride;
        CWMapBlock_t* block = (CWMapBlock_t*)cwmap_block_alloc(block_total);
        if (!block) return false;

        block->next = m->pool;
        m->pool     = block;
        m->block_count++;

        char* ptr = (char*)block + m->pool_mem_offset;
        for (size_t i = 0; i < m->pool_block_count; i++) {
            CWMapNode_t* node = (CWMapNode_t*)ptr;
            node->parent = m->free_list;
            m->free_list = node;
            ptr += m->node_stride;
        }
        m->free_count += m->pool_block_count;
        return true;
    }


    static inline CWMapNode_t* cwmap_alloc_node(CWMap_t* m) {
        if (!m->free_list) {
            if (!cwmap_expand_pool(m)) return NULL;
        }
        CWMapNode_t* node = m->free_list;
        m->free_list = node->parent;
        m->free_count--;
        return node;
    }


    static inline void cwmap_free_node(CWMap_t* m, CWMapNode_t* node) {
        node->parent = m->free_list;
        m->free_list = node;
        m->free_count++;
    }


    /* red-black tree core */

    static CWMapNode_t* cwmap_tree_minimum(CWMapNode_t* n) {
        while (n && n->left) n = n->left;
        return n;
    }


    static CWMapNode_t* cwmap_tree_maximum(CWMapNode_t* n) {
        while (n && n->right) n = n->right;
        return n;
    }


    static void cwmap_rotate_left(CWMap_t* m, CWMapNode_t* x) {
        CWMapNode_t* y = x->right;
        x->right = y->left;
        if (y->left) y->left->parent = x;
        y->parent = x->parent;
        if (!x->parent) {
            m->root = y;
        } else if (x == x->parent->left) {
            x->parent->left = y;
        } else {
            x->parent->right = y;
        }
        y->left = x;
        x->parent = y;
    }


    static void cwmap_rotate_right(CWMap_t* m, CWMapNode_t* x) {
        CWMapNode_t* y = x->left;
        x->left = y->right;
        if (y->right) y->right->parent = x;
        y->parent = x->parent;
        if (!x->parent) {
            m->root = y;
        } else if (x == x->parent->right) {
            x->parent->right = y;
        } else {
            x->parent->left = y;
        }
        y->right = x;
        x->parent = y;
    }


    static void cwmap_insert_fixup(CWMap_t* m, CWMapNode_t* z) {
        while (z->parent && z->parent->color == CWMAP_RED) {
            if (z->parent == z->parent->parent->left) {
                CWMapNode_t* y = z->parent->parent->right;
                if (y && y->color == CWMAP_RED) {
                    z->parent->color         = CWMAP_BLACK;
                    y->color                 = CWMAP_BLACK;
                    z->parent->parent->color = CWMAP_RED;
                    z = z->parent->parent;
                } else {
                    if (z == z->parent->right) {
                        z = z->parent;
                        cwmap_rotate_left(m, z);
                    }
                    z->parent->color         = CWMAP_BLACK;
                    z->parent->parent->color = CWMAP_RED;
                    cwmap_rotate_right(m, z->parent->parent);
                }
            } else {
                CWMapNode_t* y = z->parent->parent->left;
                if (y && y->color == CWMAP_RED) {
                    z->parent->color         = CWMAP_BLACK;
                    y->color                 = CWMAP_BLACK;
                    z->parent->parent->color = CWMAP_RED;
                    z = z->parent->parent;
                } else {
                    if (z == z->parent->left) {
                        z = z->parent;
                        cwmap_rotate_right(m, z);
                    }
                    z->parent->color         = CWMAP_BLACK;
                    z->parent->parent->color = CWMAP_RED;
                    cwmap_rotate_left(m, z->parent->parent);
                }
            }
        }
        m->root->color = CWMAP_BLACK;
    }


    static void cwmap_transplant(CWMap_t* m,
                                 CWMapNode_t* u, CWMapNode_t* v) {
        if (!u->parent) {
            m->root = v;
        } else if (u == u->parent->left) {
            u->parent->left = v;
        } else {
            u->parent->right = v;
        }
        if (v) v->parent = u->parent;
    }


    /**
     * Deletion with NULL leaves. `x_parent` / `x_is_left` track the position
     * of x even when x is NULL (the sentinel would otherwise be needed).
     */
    static void cwmap_delete_fixup(CWMap_t* m, CWMapNode_t* x,
                                   CWMapNode_t* x_parent, int x_is_left) {
        while (x != m->root && (x == NULL || x->color == CWMAP_BLACK)) {
            if (x_is_left) {
                CWMapNode_t* w = x_parent->right;
                if (w && w->color == CWMAP_RED) {
                    w->color        = CWMAP_BLACK;
                    x_parent->color = CWMAP_RED;
                    cwmap_rotate_left(m, x_parent);
                    w = x_parent->right;
                }
                /* a NULL sibling is a black leaf: falls into case 2 */
                if (!w ||
                    ((!w->left  || w->left->color  == CWMAP_BLACK) &&
                     (!w->right || w->right->color == CWMAP_BLACK))) {
                    if (w) w->color = CWMAP_RED;
                    x        = x_parent;
                    x_parent = x->parent;
                    x_is_left = (x_parent != NULL) && (x == x_parent->left);
                } else {
                    if (!w->right || w->right->color == CWMAP_BLACK) {
                        if (w->left) w->left->color = CWMAP_BLACK;
                        w->color = CWMAP_RED;
                        cwmap_rotate_right(m, w);
                        w = x_parent->right;
                    }
                    w->color        = x_parent->color;
                    x_parent->color = CWMAP_BLACK;
                    if (w->right) w->right->color = CWMAP_BLACK;
                    cwmap_rotate_left(m, x_parent);
                    x = m->root;
                    break;
                }
            } else {
                CWMapNode_t* w = x_parent->left;
                if (w && w->color == CWMAP_RED) {
                    w->color        = CWMAP_BLACK;
                    x_parent->color = CWMAP_RED;
                    cwmap_rotate_right(m, x_parent);
                    w = x_parent->left;
                }
                if (!w ||
                    ((!w->left  || w->left->color  == CWMAP_BLACK) &&
                     (!w->right || w->right->color == CWMAP_BLACK))) {
                    if (w) w->color = CWMAP_RED;
                    x        = x_parent;
                    x_parent = x->parent;
                    x_is_left = (x_parent != NULL) && (x == x_parent->left);
                } else {
                    if (!w->left || w->left->color == CWMAP_BLACK) {
                        if (w->right) w->right->color = CWMAP_BLACK;
                        w->color = CWMAP_RED;
                        cwmap_rotate_left(m, w);
                        w = x_parent->left;
                    }
                    w->color        = x_parent->color;
                    x_parent->color = CWMAP_BLACK;
                    if (w->left) w->left->color = CWMAP_BLACK;
                    cwmap_rotate_right(m, x_parent);
                    x = m->root;
                    break;
                }
            }
        }
        if (x) x->color = CWMAP_BLACK;
    }


    /** Remove the tree node z (caller is responsible for dtor + recycle). */
    static void cwmap_erase_node(CWMap_t* m, CWMapNode_t* z) {
        CWMapNode_t* y = z;
        unsigned char y_color = y->color;
        CWMapNode_t* x = NULL;
        CWMapNode_t* x_parent = z->parent;
        int x_is_left = 0;

        if (!z->left) {
            x = z->right;
            x_parent = z->parent;
            if (x_parent) x_is_left = (z == x_parent->left);
            cwmap_transplant(m, z, z->right);
        } else if (!z->right) {
            x = z->left;
            x_parent = z->parent;
            if (x_parent) x_is_left = (z == x_parent->left);
            cwmap_transplant(m, z, z->left);
        } else {
            y = cwmap_tree_minimum(z->right);
            y_color = y->color;
            x = y->right;
            if (y->parent == z) {
                /* x keeps its position as y's right child */
                x_parent = y;
                x_is_left = 0;
            } else {
                /*
                 * Record y's position BEFORE transplant: transplant replaces
                 * y with y->right in the parent's child slot, so testing
                 * `y == x_parent->left` afterwards would report the wrong side.
                 */
                x_parent = y->parent;
                if (x_parent) x_is_left = (y == x_parent->left);
                cwmap_transplant(m, y, y->right);
                y->right = z->right;
                y->right->parent = y;
            }
            cwmap_transplant(m, z, y);
            y->left = z->left;
            y->left->parent = y;
            y->color = z->color;
        }

        if (y_color == CWMAP_BLACK) {
            cwmap_delete_fixup(m, x, x_parent, x_is_left);
        }
    }


    static CWMapNode_t* cwmap_find_node(CWMap_t* m, const void* key) {
        CWMapNode_t* n = m->root;
        while (n) {
            const int c = cwmap_compare_keys(m, key, n->key);
            if (c == 0) return n;
            n = (c < 0) ? n->left : n->right;
        }
        return NULL;
    }


    /**
     * Single tree walk. Returns the node holding an equal key, or NULL after
     * storing the insertion position into *parent_out / *cmp_out
     * (cmp < 0 means the new node goes on the parent's left).
     */
    static CWMapNode_t* cwmap_locate(CWMap_t* m, const void* key,
                                     CWMapNode_t** parent_out, int* cmp_out) {
        CWMapNode_t* y = NULL;
        CWMapNode_t* x = m->root;
        int last_cmp = 0;

        while (x) {
            y = x;
            last_cmp = cwmap_compare_keys(m, key, x->key);
            if (last_cmp == 0) return x;
            x = (last_cmp < 0) ? x->left : x->right;
        }

        if (parent_out) *parent_out = y;
        if (cmp_out)    *cmp_out    = last_cmp;
        return NULL;
    }


    static inline void cwmap_link_new_node(CWMap_t* m, CWMapNode_t* node,
                                           CWMapNode_t* parent, int last_cmp) {
        node->parent = parent;
        node->left   = NULL;
        node->right  = NULL;
        node->color  = CWMAP_RED;

        if (!parent) {
            m->root = node;
        } else if (last_cmp < 0) {
            parent->left = node;
        } else {
            parent->right = node;
        }

        m->count++;
        cwmap_insert_fixup(m, node);
    }


    /**
     * Discard one entry's key/value through the registered destructors.
     * If both slots point to the same allocation, the destructor runs once
     * (a second call would double-free it).
     */
    static inline void cwmap_discard_payload(CWMap_t* m,
                                             void* key, void* value) {
        if (key == value) {
            if (m->key_dtor) {
                m->key_dtor(key);
            } else if (m->value_dtor) {
                m->value_dtor(value);
            }
        } else {
            if (m->key_dtor)   m->key_dtor(key);
            if (m->value_dtor) m->value_dtor(value);
        }
    }


    static CWMapNode_t* cwmap_successor(CWMapNode_t* n) {
        if (n->right) return cwmap_tree_minimum(n->right);
        CWMapNode_t* p = n->parent;
        while (p && n == p->right) {
            n = p;
            p = p->parent;
        }
        return p;
    }


    static CWMapNode_t* cwmap_predecessor(CWMapNode_t* n) {
        if (n->left) return cwmap_tree_maximum(n->left);
        CWMapNode_t* p = n->parent;
        while (p && n == p->left) {
            n = p;
            p = p->parent;
        }
        return p;
    }


    /* create / destroy */

    static inline CWMap_t* cwmap_create_ex(int (*compare)(const void*, const void*),
                                           size_t nodes_per_block) {
        const size_t align       = _Alignof(CWMap_MAX_ALIGN_T);
        const size_t node_stride = cwmap_align_up(sizeof(CWMapNode_t), align);
        const size_t npb = (nodes_per_block == 0) ? CWMap_POOL_NODE_COUNT
                                                  : nodes_per_block;
        if (npb == 0) return NULL; /* requested count wrapped to 0 */

        const size_t mem_offset = cwmap_align_up(sizeof(CWMapBlock_t), align);
        if (npb > (SIZE_MAX - mem_offset) / node_stride) return NULL;

        CWMap_t* m = (CWMap_t*)malloc(sizeof(CWMap_t));
        if (!m) return NULL;

        /*
         * Lazy pool: an empty map reserves no memory. The first block is
         * mmap'd/VirtualAlloc'd on the first insert, so tiny maps stay cheap
         * (capacity()/free_count() report 0 until then). For maps that stay
         * small, pass a small nodes_per_block to cwmap_create_ex().
         */
        m->root             = NULL;
        m->pool             = NULL;
        m->free_list        = NULL;
        m->count            = 0;
        m->free_count       = 0;
        m->block_count      = 0;
        m->node_stride      = node_stride;
        m->pool_mem_offset  = mem_offset;
        m->pool_block_count = npb;
        m->compare          = compare;
        m->key_dtor         = NULL;
        m->value_dtor       = NULL;
        return m;
    }


    static inline CWMap_t* cwmap_create(int (*compare)(const void*, const void*)) {
        return cwmap_create_ex(compare, CWMap_POOL_NODE_COUNT);
    }


    static inline void cwmap_clear(CWMap_t* m) {
        /*
         * Iterative post-order walk. Each node's parent is captured before
         * the node is recycled (free_node overwrites `parent` with the
         * free-list link), and child pointers are cleared so a node is never
         * descended into twice. O(n), no extra storage.
         */
        CWMapNode_t* n = m->root;
        while (n) {
            if (n->left) {
                n = n->left;
            } else if (n->right) {
                n = n->right;
            } else {
                CWMapNode_t* p = n->parent;
                if (p) {
                    if (p->left == n) {
                        p->left = NULL;
                    } else {
                        p->right = NULL;
                    }
                }
                cwmap_discard_payload(m, n->key, n->value);
                cwmap_free_node(m, n);
                n = p;
            }
        }
        m->root  = NULL;
        m->count = 0;
    }


    static inline void cwmap_destroy(CWMap_t* m) {
        if (!m) return;

        cwmap_clear(m);

        const size_t block_total = m->pool_mem_offset
                                 + m->pool_block_count * m->node_stride;
        CWMapBlock_t* block = m->pool;
        while (block) {
            CWMapBlock_t* next = block->next;
            cwmap_block_free(block, block_total);
            block = next;
        }
        free(m);
    }


    /* put / insert / get */

    /**
     * Insert or overwrite. Returns the stored value (== `value`) on success,
     * or NULL only on allocation failure. On overwrite the old value is
     * discarded through value_dtor; if the old key pointer differs from the
     * new one, the old key is discarded through key_dtor as well. When key
     * and value point to the same allocation (set-style usage), it is
     * discarded at most once even if both destructors are registered.
     */
    static inline void* cwmap_put(CWMap_t* m, void* key, void* value) {
        CWMapNode_t* parent = NULL;
        int last_cmp = 0;
        CWMapNode_t* existing = cwmap_locate(m, key, &parent, &last_cmp);
        if (existing) {
            /* overwrite in place; no node allocation on this hot path */
            if (existing->key == existing->value) {
                /* one allocation backs both slots: free it only if fully
                   replaced (a retained slot still owns the pointer) */
                if (existing->key != key && existing->value != value) {
                    if (m->key_dtor) {
                        m->key_dtor(existing->key);
                    } else if (m->value_dtor) {
                        m->value_dtor(existing->value);
                    }
                }
            } else {
                if (m->key_dtor && existing->key != key) {
                    m->key_dtor(existing->key);
                }
                if (m->value_dtor) m->value_dtor(existing->value);
            }
            existing->key = key;
            existing->value = value;
            return value;
        }

        CWMapNode_t* node = cwmap_alloc_node(m);
        if (!node) return NULL;
        node->key   = key;
        node->value = value;
        cwmap_link_new_node(m, node, parent, last_cmp);
        return value;
    }


    /** insert-only: returns NULL when the key already exists (no overwrite) */
    static inline void* cwmap_insert(CWMap_t* m, void* key, void* value) {
        CWMapNode_t* parent = NULL;
        int last_cmp = 0;
        if (cwmap_locate(m, key, &parent, &last_cmp)) return NULL;

        CWMapNode_t* node = cwmap_alloc_node(m);
        if (!node) return NULL;
        node->key   = key;
        node->value = value;
        cwmap_link_new_node(m, node, parent, last_cmp);
        return value;
    }


    static inline void* cwmap_get(CWMap_t* m, const void* key) {
        CWMapNode_t* n = cwmap_find_node(m, key);
        return n ? n->value : NULL;
    }


    static inline bool cwmap_contains(CWMap_t* m, const void* key) {
        return cwmap_find_node(m, key) != NULL;
    }


    /* remove / take */

    /**
     * Remove the entry and discard it through the registered destructors.
     * Returns false when the key is missing.
     */
    static inline bool cwmap_remove(CWMap_t* m, const void* key) {
        CWMapNode_t* z = cwmap_find_node(m, key);
        if (!z) return false;

        cwmap_discard_payload(m, z->key, z->value);

        cwmap_erase_node(m, z);
        cwmap_free_node(m, z);
        m->count--;
        return true;
    }


    /**
     * Remove the entry and transfer (shallow) ownership to the caller:
     * destructors are NOT called. *out_key / *out_value receive the stored
     * pointers (either may be NULL to discard that side). Returns false when
     * the key is missing.
     */
    static inline bool cwmap_take(CWMap_t* m, const void* key,
                                  void** out_key, void** out_value) {
        CWMapNode_t* z = cwmap_find_node(m, key);
        if (!z) return false;

        if (out_key)   *out_key   = z->key;
        if (out_value) *out_value = z->value;

        cwmap_erase_node(m, z);
        cwmap_free_node(m, z);
        m->count--;
        return true;
    }


    /* iteration */

    /**
     * After modifying the map, the caller must discard old iterators;
     * otherwise a "use-after-free" logic error may occur (nodes are recycled).
     */
    static inline CWMapIter_t cwmap_begin(CWMap_t* m) {
        CWMapIter_t it = { m, cwmap_tree_minimum(m->root) };
        return it;
    }


    static inline CWMapIter_t cwmap_rbegin(CWMap_t* m) {
        CWMapIter_t it = { m, cwmap_tree_maximum(m->root) };
        return it;
    }


    static inline CWMapIter_t cwmap_find(CWMap_t* m, const void* key) {
        CWMapIter_t it = { m, cwmap_find_node(m, key) };
        return it;
    }


    static inline bool cwmap_iter_valid(CWMapIter_t* it) {
        return it->current != NULL;
    }


    static inline void* cwmap_iter_key(CWMapIter_t* it) {
        return it->current ? it->current->key : NULL;
    }


    static inline void* cwmap_iter_value(CWMapIter_t* it) {
        return it->current ? it->current->value : NULL;
    }


    static inline void cwmap_iter_next(CWMapIter_t* it) {
        if (it->current) it->current = cwmap_successor(it->current);
    }


    static inline void cwmap_iter_prev(CWMapIter_t* it) {
        if (it->current) it->current = cwmap_predecessor(it->current);
    }


    /* memory reclamation */

    /* block reference used by shrink_to_fit (sorted by node-area base) */
    typedef struct CWMapBlockRef {
        size_t chain_idx;       /* position in the block chain (0 = newest) */
        const char* node_base;  /* first node address inside the block */
    } CWMapBlockRef_t;


    static int cwmap_block_ref_cmp(const void* a, const void* b) {
        const uintptr_t x = (uintptr_t)((const CWMapBlockRef_t*)a)->node_base;
        const uintptr_t y = (uintptr_t)((const CWMapBlockRef_t*)b)->node_base;
        return (x > y) - (x < y);
    }


    /* index of the block whose node area contains p (binary search, O(log b)) */
    static inline size_t cwmap_block_index_of(const CWMapBlockRef_t* refs,
                                              size_t nb, const char* p) {
        size_t lo = 0, hi = nb;
        while (lo < hi) {
            const size_t mid = lo + (hi - lo) / 2;
            if ((uintptr_t)refs[mid].node_base <= (uintptr_t)p) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return (lo == 0) ? 0 : lo - 1;
    }


    /**
     * Returns pool blocks that contain no live node back to the OS.
     * The tree links nodes by pointer (no positional mapping), so any block
     * without live nodes could be reclaimed; for simplicity this reclaims the
     * oldest run of empty blocks and always keeps one base block. Live entry
     * pointers and iterators stay valid; free nodes inside reclaimed blocks
     * are dropped from the free list.
     *
     * Block bases are sorted once (O(b*log b)) and every node locates its
     * block by binary search (O(log b)), so the whole call is
     * O((n + f)*log b + b*log b) instead of O((n + f)*b).
     */
    static inline bool cwmap_shrink_to_fit(CWMap_t* m) {
        if (!m->pool || !m->pool->next) return true; /* 0/1 blocks: no-op */

        const size_t block_total = m->pool_mem_offset
                                 + m->pool_block_count * m->node_stride;

        size_t nb = 0;
        for (CWMapBlock_t* b = m->pool; b; b = b->next) nb++;
        CWMapBlockRef_t* refs =
            (CWMapBlockRef_t*)malloc(nb * sizeof(CWMapBlockRef_t));
        if (!refs) return false;
        size_t i = 0;
        for (CWMapBlock_t* b = m->pool; b; b = b->next, i++) {
            refs[i].chain_idx = i;
            refs[i].node_base = (const char*)b + m->pool_mem_offset;
        }
        qsort(refs, nb, sizeof(CWMapBlockRef_t), cwmap_block_ref_cmp);

        unsigned char* has_live = (unsigned char*)calloc(1, nb);
        if (!has_live) {
            free(refs);
            return false;
        }
        for (CWMapNode_t* n = cwmap_tree_minimum(m->root);
             n; n = cwmap_successor(n)) {
            const size_t idx = cwmap_block_index_of(refs, nb, (const char*)n);
            has_live[refs[idx].chain_idx] = 1;
        }

        size_t keep = nb;
        while (keep > 1 && !has_live[keep - 1]) keep--;
        free(has_live);
        if (keep == nb) {
            free(refs);
            return true;
        }

        /* rebuild the free list without nodes in reclaimed blocks */
        CWMapNode_t* fl_head = NULL;
        CWMapNode_t* fl_tail = NULL;
        CWMapNode_t* cur = m->free_list;
        m->free_count = 0;
        while (cur) {
            CWMapNode_t* next = cur->parent; /* free-list link */
            const size_t idx = cwmap_block_index_of(refs, nb, (const char*)cur);
            if (refs[idx].chain_idx < keep) {
                if (!fl_tail) {
                    fl_head = fl_tail = cur;
                } else {
                    fl_tail->parent = cur;
                    fl_tail = cur;
                }
                m->free_count++;
            }
            cur = next;
        }
        if (fl_tail) fl_tail->parent = NULL;
        m->free_list = fl_head;
        free(refs);

        /* unlink and free the reclaimed tail blocks (chain order) */
        size_t k = 0;
        CWMapBlock_t* b = m->pool;
        CWMapBlock_t* last_kept = NULL;
        while (b && k < keep) {
            last_kept = b;
            b = b->next;
            k++;
        }
        if (last_kept) last_kept->next = NULL;
        while (b) {
            CWMapBlock_t* next = b->next;
            cwmap_block_free(b, block_total);
            b = next;
        }
        m->block_count = keep;
        return true;
    }


    /* safe variants (automatic key/value cleanup) */

    /**
     * safe containers call key_dtor / value_dtor (non-NULL) on every entry
     * they discard (put overwriting an existing key, remove, clear, destroy).
     * cwmap_take() transfers ownership to the caller and does NOT call the
     * destructors. At least one destructor must be non-NULL.
     */
    static inline CWMap_t* cwmap_safe_create_ex(
                                    int (*compare)(const void*, const void*),
                                    size_t nodes_per_block,
                                    void (*key_dtor)(void*),
                                    void (*value_dtor)(void*)) {
        if (!key_dtor && !value_dtor) return NULL;
        CWMap_t* m = cwmap_create_ex(compare, nodes_per_block);
        if (m) {
            m->key_dtor   = key_dtor;
            m->value_dtor = value_dtor;
        }
        return m;
    }


    static inline CWMap_t* cwmap_safe_create(
                                    int (*compare)(const void*, const void*),
                                    void (*key_dtor)(void*),
                                    void (*value_dtor)(void*)) {
        return cwmap_safe_create_ex(compare, CWMap_POOL_NODE_COUNT,
                                    key_dtor, value_dtor);
    }


    /* queries */

    static inline size_t cwmap_size(CWMap_t* m) {
        return m->count;
    }


    static inline bool cwmap_empty(CWMap_t* m) {
        return m->count == 0;
    }


    /* total nodes owned by the pool (live + free) */
    static inline size_t cwmap_capacity(CWMap_t* m) {
        return m->block_count * m->pool_block_count;
    }


    /* nodes currently recycled and waiting for reuse */
    static inline size_t cwmap_free_count(CWMap_t* m) {
        return m->free_count;
    }

#endif // CWMap_H
