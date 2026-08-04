/**
 * Copyright (C) 2026/8/3 CWind-Project
 * License: BSD-3.0
 * First  Author : DeepSeek-V4-Flash[version: 2026/08/03, Official]
 * Second Author : StarWindv[Reviewer, Optimizer]
 * Reference Code:
 *  - [1] StarWindv. cwind_fix_size_queue.h [SourceCode]. CWind-Project(main/a34b76), 2024.
 *  - [2] StarWindv. cwind_ptr_map.h [SourceCode]. CWind-Project(main/a34b76), 2026.
 * Location: src/include/stl/cwind_value_map.h
 */

/**
 * Requirement:
 *  - Value-copy map: keys and values are fixed-size byte arrays
 *    (key_size / value_size fixed per map at create time) and are COPIED into
 *    the node, so the map owns the data and no pointer can dangle. This is
 *    the counterpart of the pointer-based cwind_ptr_map.h.
 *  - C11 or later (-std>=c11).
 *  - Keys are compared with memcmp(key_size) by default; pass a comparator to
 *    the cwvmap_create_cmp*() family for custom ordering.
 *
 * Design:
 *  - Red-black tree. Every node embeds its key bytes + value bytes, so
 *    get/put/remove walk O(log n) nodes and touch one cache line of payload
 *    per visited node. The red-black invariant bounds the height by
 *    2*log2(n+1).
 *  - Node storage comes from large blocks acquired with mmap (POSIX) /
 *    VirtualAlloc (Windows). Compile with -DCWValueMap_USE_MALLOC to fall
 *    back to malloc.
 *  - The pool is lazy: an empty map reserves no memory; the first block
 *    (default 1024 nodes) is acquired on the first insert. Maps that stay
 *    small should use cwvmap_create_ex() with a small nodes_per_block.
 *  - Removed nodes are pushed onto a LIFO free list and recycled by later
 *    inserts. Trailing empty pool blocks are returned to the OS by
 *    cwvmap_shrink_to_fit(); destroy returns everything.
 *  - Keys and values are aligned to _Alignof(max_align_t).
 *  - Automatic cleanup: cwvmap_safe_create*() registers key/value destructors;
 *    they are called whenever an entry is discarded (put overwriting an
 *    existing key, remove, clear, destroy). cwvmap_take() copies the entry
 *    out to the caller and does NOT call the destructors.
 *  - Contract: if a key/value byte area contains pointers to the same owned
 *    object, register only one of the two destructors; content-alias cases
 *    cannot be detected automatically.
 *  - Copy semantics are shallow: the map copies the key/value byte arrays
 *    as-is. If a key or value struct contains pointers to owned memory, the map
 *    shares those pointees with the caller; with a destructor registered, the
 *    map takes ownership on put, so the caller must NOT free its own copy.
 *  - With the default memcmp comparison, struct keys containing padding must
 *    be zero-initialized before put, otherwise the padding bytes make two
 *    logically equal keys compare as different.
 *  - Nesting: store a child map pointer inside a value struct and use a value
 *    destructor that destroys the child map; child maps are then freed
 *    automatically.
 *  - Not thread-safe.
 */


#ifndef CWValueMap_H

    #define CWValueMap_H

    #include <stddef.h>
    #include <stdbool.h>
    #include <string.h>
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
            #define CWValueMap_NEED_MALLOC 1
        #endif
    #endif


    /* nodes per block; block totals stay reasonably large for mmap/VirtualAlloc */
    #ifndef CWValueMap_POOL_NODE_COUNT
        #define CWValueMap_POOL_NODE_COUNT (1024)
    #endif


    /*
     * max_align_t is missing from MSVC's C11 mode, so derive the maximum
     * fundamental alignment from a union of the basic types there.
     */
    #if defined(_MSC_VER)
        typedef union cwvmap_msvc_max_align {
            long double ld;
            long long ll;
            void* p;
            void (*fn)(void);
        } cwvmap_msvc_max_align_t;
        #define CWValueMap_MAX_ALIGN_T cwvmap_msvc_max_align_t
    #else
        #define CWValueMap_MAX_ALIGN_T max_align_t
    #endif


    /* memory provider */

    #if defined(CWValueMap_USE_MALLOC) || defined(CWValueMap_NEED_MALLOC)

        static void* cwvmap_block_alloc(size_t size) {
            return malloc(size);
        }

        static void cwvmap_block_free(void* p, size_t size) {
            (void)size;
            free(p);
        }

    #elif defined(_WIN32)

        static void* cwvmap_block_alloc(size_t size) {
            return (void*)VirtualAlloc(NULL, size,
                                       MEM_RESERVE | MEM_COMMIT,
                                       PAGE_READWRITE);
        }

        static void cwvmap_block_free(void* p, size_t size) {
            (void)size;
            if (p) VirtualFree(p, 0, MEM_RELEASE);
        }

    #else

        static void* cwvmap_block_alloc(size_t size) {
            void* p = mmap(NULL, size, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            return (p == MAP_FAILED) ? NULL : p;
        }

        static void cwvmap_block_free(void* p, size_t size) {
            if (p) munmap(p, size);
        }

    #endif


    /* small helpers */

    static inline size_t cwvmap_align_up(size_t v, size_t a) {
        return (v + a - 1) & ~(a - 1);
    }


    /* tree colors */
    typedef enum CWValueMapColor {
        CWVALUEMAP_RED   = 0,
        CWVALUEMAP_BLACK = 1
    } CWValueMapColor_t;


    /* node embeds key bytes + value bytes; free nodes reuse `parent` as the
       free-list link (never part of the tree while free) */
    typedef struct CWValueMapNode {
        struct CWValueMapNode* parent;
        struct CWValueMapNode* left;
        struct CWValueMapNode* right;
        unsigned char color;
    } CWValueMapNode_t;


    typedef struct CWValueMapBlock {
        struct CWValueMapBlock* next;
        char memory[];
    } CWValueMapBlock_t;


    typedef struct CWValueMap {
        CWValueMapNode_t*  root;
        CWValueMapBlock_t* pool;
        CWValueMapNode_t*  free_list;   /* LIFO recycled nodes */
        size_t count;
        size_t free_count;
        size_t block_count;
        size_t key_size;
        size_t value_size;
        size_t data_offset;             /* payload start inside a node */
        size_t key_stride;              /* aligned key area stride in a node */
        size_t node_stride;             /* aligned stride between two nodes */
        size_t pool_mem_offset;
        size_t pool_block_count;        /* nodes per block */
        int (*compare)(const void* k1, const void* k2); /* NULL => memcmp */
        void (*key_dtor)(void*);        /* optional key destructor (safe mode) */
        void (*value_dtor)(void*);      /* optional value destructor (safe mode) */
    } CWValueMap_t;


    typedef struct CWValueMapIter {
        CWValueMap_t*     map;
        CWValueMapNode_t* current;
    } CWValueMapIter_t;


    /* key / value accessors */

    /*
     * The payload area starts at data_offset (aligned), NOT at the flexible
     * array member: FAM offsets differ between ABIs (e.g. 25 under MSVC vs 32
     * under GNU), so node->data must never be used for payload access.
     */
    static inline void* cwvmap_node_key(CWValueMap_t* m,
                                        CWValueMapNode_t* node) {
        return (char*)node + m->data_offset;
    }


    static inline void* cwvmap_node_value(CWValueMap_t* m,
                                          CWValueMapNode_t* node) {
        return (char*)node + m->data_offset + m->key_stride;
    }


    static inline int cwvmap_compare_keys(CWValueMap_t* m,
                                          const void* a, const void* b) {
        return m->compare ? m->compare(a, b) : memcmp(a, b, m->key_size);
    }


    /* pool management */

    static bool cwvmap_expand_pool(CWValueMap_t* m) {
        const size_t block_total = m->pool_mem_offset
                                 + m->pool_block_count * m->node_stride;
        CWValueMapBlock_t* block =
            cwvmap_block_alloc(block_total);
        if (!block) return false;

        block->next = m->pool;
        m->pool     = block;
        m->block_count++;

        char* ptr = (char*)block + m->pool_mem_offset;
        for (size_t i = 0; i < m->pool_block_count; i++) {
            CWValueMapNode_t* node = (CWValueMapNode_t*)ptr;
            node->parent = m->free_list;
            m->free_list = node;
            ptr += m->node_stride;
        }
        m->free_count += m->pool_block_count;
        return true;
    }


    static CWValueMapNode_t* cwvmap_alloc_node(CWValueMap_t* m) {
        if (!m->free_list && !cwvmap_expand_pool(m)) {
            return NULL;
        }
        CWValueMapNode_t* node = m->free_list;
        m->free_list = node->parent;
        m->free_count--;
        return node;
    }


    static void cwvmap_free_node(CWValueMap_t* m, CWValueMapNode_t* node) {
        node->parent = m->free_list;
        m->free_list = node;
        m->free_count++;
    }


    /* red-black tree core */

    static CWValueMapNode_t* cwvmap_tree_minimum(CWValueMapNode_t* n) {
        while (n && n->left) n = n->left;
        return n;
    }


    static CWValueMapNode_t* cwvmap_tree_maximum(CWValueMapNode_t* n) {
        while (n && n->right) n = n->right;
        return n;
    }


    static void cwvmap_rotate_left(CWValueMap_t* m, CWValueMapNode_t* x) {
        CWValueMapNode_t* y = x->right;
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


    static void cwvmap_rotate_right(CWValueMap_t* m, CWValueMapNode_t* x) {
        CWValueMapNode_t* y = x->left;
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


    static void cwvmap_insert_fixup(CWValueMap_t* m, CWValueMapNode_t* z) {
        while (z->parent && z->parent->color == CWVALUEMAP_RED) {
            if (z->parent == z->parent->parent->left) {
                CWValueMapNode_t* y = z->parent->parent->right;
                if (y && y->color == CWVALUEMAP_RED) {
                    z->parent->color         = CWVALUEMAP_BLACK;
                    y->color                 = CWVALUEMAP_BLACK;
                    z->parent->parent->color = CWVALUEMAP_RED;
                    z = z->parent->parent;
                } else {
                    if (z == z->parent->right) {
                        z = z->parent;
                        cwvmap_rotate_left(m, z);
                    }
                    z->parent->color         = CWVALUEMAP_BLACK;
                    z->parent->parent->color = CWVALUEMAP_RED;
                    cwvmap_rotate_right(m, z->parent->parent);
                }
            } else {
                CWValueMapNode_t* y = z->parent->parent->left;
                if (y && y->color == CWVALUEMAP_RED) {
                    z->parent->color         = CWVALUEMAP_BLACK;
                    y->color                 = CWVALUEMAP_BLACK;
                    z->parent->parent->color = CWVALUEMAP_RED;
                    z = z->parent->parent;
                } else {
                    if (z == z->parent->left) {
                        z = z->parent;
                        cwvmap_rotate_right(m, z);
                    }
                    z->parent->color         = CWVALUEMAP_BLACK;
                    z->parent->parent->color = CWVALUEMAP_RED;
                    cwvmap_rotate_left(m, z->parent->parent);
                }
            }
        }
        m->root->color = CWVALUEMAP_BLACK;
    }


    static void cwvmap_transplant(CWValueMap_t* m,
                                  CWValueMapNode_t* u, CWValueMapNode_t* v) {
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
    static void cwvmap_delete_fixup(CWValueMap_t* m, CWValueMapNode_t* x,
                                    CWValueMapNode_t* x_parent, int x_is_left) {
        while (x != m->root && (x == NULL || x->color == CWVALUEMAP_BLACK)) {
            if (x_is_left) {
                CWValueMapNode_t* w = x_parent->right;
                if (w && w->color == CWVALUEMAP_RED) {
                    w->color        = CWVALUEMAP_BLACK;
                    x_parent->color = CWVALUEMAP_RED;
                    cwvmap_rotate_left(m, x_parent);
                    w = x_parent->right;
                }
                /* a NULL sibling is a black leaf: falls into case 2 */
                if (!w ||
                    ((!w->left  || w->left->color  == CWVALUEMAP_BLACK) &&
                     (!w->right || w->right->color == CWVALUEMAP_BLACK))) {
                    if (w) w->color = CWVALUEMAP_RED;
                    x        = x_parent;
                    x_parent = x->parent;
                    x_is_left = (x_parent != NULL) && (x == x_parent->left);
                } else {
                    if (!w->right || w->right->color == CWVALUEMAP_BLACK) {
                        if (w->left) w->left->color = CWVALUEMAP_BLACK;
                        w->color = CWVALUEMAP_RED;
                        cwvmap_rotate_right(m, w);
                        w = x_parent->right;
                    }
                    w->color        = x_parent->color;
                    x_parent->color = CWVALUEMAP_BLACK;
                    if (w->right) w->right->color = CWVALUEMAP_BLACK;
                    cwvmap_rotate_left(m, x_parent);
                    x = m->root;
                    break;
                }
            } else {
                CWValueMapNode_t* w = x_parent->left;
                if (w && w->color == CWVALUEMAP_RED) {
                    w->color        = CWVALUEMAP_BLACK;
                    x_parent->color = CWVALUEMAP_RED;
                    cwvmap_rotate_right(m, x_parent);
                    w = x_parent->left;
                }
                if (!w ||
                    ((!w->left  || w->left->color  == CWVALUEMAP_BLACK) &&
                     (!w->right || w->right->color == CWVALUEMAP_BLACK))) {
                    if (w) w->color = CWVALUEMAP_RED;
                    x        = x_parent;
                    x_parent = x->parent;
                    x_is_left = (x_parent != NULL) && (x == x_parent->left);
                } else {
                    if (!w->left || w->left->color == CWVALUEMAP_BLACK) {
                        if (w->right) w->right->color = CWVALUEMAP_BLACK;
                        w->color = CWVALUEMAP_RED;
                        cwvmap_rotate_left(m, w);
                        w = x_parent->left;
                    }
                    w->color        = x_parent->color;
                    x_parent->color = CWVALUEMAP_BLACK;
                    if (w->left) w->left->color = CWVALUEMAP_BLACK;
                    cwvmap_rotate_right(m, x_parent);
                    x = m->root;
                    break;
                }
            }
        }
        if (x) x->color = CWVALUEMAP_BLACK;
    }


    /** Remove the tree node z (caller is responsible for dtor + recycle). */
    static void cwvmap_erase_node(CWValueMap_t* m, CWValueMapNode_t* z) {
        CWValueMapNode_t* y = z;
        unsigned char y_color = y->color;
        CWValueMapNode_t* x = NULL;
        CWValueMapNode_t* x_parent = z->parent;
        int x_is_left = 0;

        if (!z->left) {
            x = z->right;
            x_parent = z->parent;
            if (x_parent) x_is_left = (z == x_parent->left);
            cwvmap_transplant(m, z, z->right);
        } else if (!z->right) {
            x = z->left;
            x_parent = z->parent;
            if (x_parent) x_is_left = (z == x_parent->left);
            cwvmap_transplant(m, z, z->left);
        } else {
            y = cwvmap_tree_minimum(z->right);
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
                cwvmap_transplant(m, y, y->right);
                y->right = z->right;
                y->right->parent = y;
            }
            cwvmap_transplant(m, z, y);
            y->left = z->left;
            y->left->parent = y;
            y->color = z->color;
        }

        if (y_color == CWVALUEMAP_BLACK) {
            cwvmap_delete_fixup(m, x, x_parent, x_is_left);
        }
    }


    static CWValueMapNode_t* cwvmap_find_node(CWValueMap_t* m,
                                              const void* key) {
        CWValueMapNode_t* n = m->root;
        while (n) {
            const int c = cwvmap_compare_keys(m, key, cwvmap_node_key(m, n));
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
    static CWValueMapNode_t* cwvmap_locate(CWValueMap_t* m, const void* key,
                                           CWValueMapNode_t** parent_out,
                                           int* cmp_out) {
        CWValueMapNode_t* y = NULL;
        CWValueMapNode_t* x = m->root;
        int last_cmp = 0;

        while (x) {
            y = x;
            last_cmp = cwvmap_compare_keys(m, key, cwvmap_node_key(m, x));
            if (last_cmp == 0) return x;
            x = (last_cmp < 0) ? x->left : x->right;
        }

        if (parent_out) *parent_out = y;
        if (cmp_out)    *cmp_out    = last_cmp;
        return NULL;
    }


    static inline void cwvmap_link_new_node(CWValueMap_t* m,
                                            CWValueMapNode_t* node,
                                            CWValueMapNode_t* parent,
                                            int last_cmp) {
        node->parent = parent;
        node->left   = NULL;
        node->right  = NULL;
        node->color  = CWVALUEMAP_RED;

        if (!parent) {
            m->root = node;
        } else if (last_cmp < 0) {
            parent->left = node;
        } else {
            parent->right = node;
        }

        m->count++;
        cwvmap_insert_fixup(m, node);
    }


    /** Discard one entry's key/value through the registered destructors. */
    static inline void cwvmap_discard_payload(CWValueMap_t* m,
                                              void* key, void* value) {
        if (m->key_dtor)   m->key_dtor(key);
        if (m->value_dtor) m->value_dtor(value);
    }


    static CWValueMapNode_t* cwvmap_successor(CWValueMapNode_t* n) {
        if (n->right) return cwvmap_tree_minimum(n->right);
        CWValueMapNode_t* p = n->parent;
        while (p && n == p->right) {
            n = p;
            p = p->parent;
        }
        return p;
    }


    static CWValueMapNode_t* cwvmap_predecessor(CWValueMapNode_t* n) {
        if (n->left) return cwvmap_tree_maximum(n->left);
        CWValueMapNode_t* p = n->parent;
        while (p && n == p->left) {
            n = p;
            p = p->parent;
        }
        return p;
    }


    /* create / destroy */

    static inline CWValueMap_t* cwvmap_create_cmp_ex(
                                    size_t key_size,
                                    size_t value_size,
                                    size_t nodes_per_block,
                                    int (*compare)(const void*, const void*)) {
        if (key_size == 0) return NULL;

        const size_t align       = _Alignof(CWValueMap_MAX_ALIGN_T);
        const size_t data_offset = cwvmap_align_up(sizeof(CWValueMapNode_t),
                                                  align);
        const size_t key_stride  = cwvmap_align_up(key_size, align);

        if (key_stride > SIZE_MAX - value_size) return NULL;
        const size_t payload = key_stride + value_size;
        if (payload > SIZE_MAX - data_offset) return NULL;
        const size_t total = data_offset + payload;
        if (total > SIZE_MAX - (align - 1)) return NULL;
        const size_t node_stride = cwvmap_align_up(total, align);

        const size_t npb = (nodes_per_block == 0) ? CWValueMap_POOL_NODE_COUNT
                                                  : nodes_per_block;
        if (npb == 0) return NULL; /* requested count wrapped to 0 */

        const size_t mem_offset = cwvmap_align_up(sizeof(CWValueMapBlock_t),
                                                  align);
        if (npb > (SIZE_MAX - mem_offset) / node_stride) return NULL;

        CWValueMap_t* m = (CWValueMap_t*)malloc(sizeof(CWValueMap_t));
        if (!m) return NULL;

        /*
         * Lazy pool: an empty map reserves no memory. The first block is
         * mmap'd/VirtualAlloc'd on the first insert, so tiny maps stay cheap
         * (capacity()/free_count() report 0 until then). For maps that stay
         * small, pass a small nodes_per_block to cwvmap_create_ex().
         */
        m->root             = NULL;
        m->pool             = NULL;
        m->free_list        = NULL;
        m->count            = 0;
        m->free_count       = 0;
        m->block_count      = 0;
        m->key_size         = key_size;
        m->value_size       = value_size;
        m->data_offset      = data_offset;
        m->key_stride       = key_stride;
        m->node_stride      = node_stride;
        m->pool_mem_offset  = mem_offset;
        m->pool_block_count = npb;
        m->compare          = compare;
        m->key_dtor         = NULL;
        m->value_dtor       = NULL;
        return m;
    }


    static inline CWValueMap_t* cwvmap_create_cmp(
                                    size_t key_size,
                                    size_t value_size,
                                    int (*compare)(const void*, const void*)) {
        return cwvmap_create_cmp_ex(key_size, value_size,
                                    CWValueMap_POOL_NODE_COUNT, compare);
    }


    static inline CWValueMap_t* cwvmap_create_ex(size_t key_size,
                                                 size_t value_size,
                                                 size_t nodes_per_block) {
        return cwvmap_create_cmp_ex(key_size, value_size,
                                    nodes_per_block, NULL);
    }


    static inline CWValueMap_t* cwvmap_create(size_t key_size,
                                              size_t value_size) {
        return cwvmap_create_cmp_ex(key_size, value_size,
                                    CWValueMap_POOL_NODE_COUNT, NULL);
    }


    static inline void cwvmap_clear(CWValueMap_t* m) {
        /*
         * Iterative post-order walk. Each node's parent is captured before
         * the node is recycled (free_node overwrites `parent` with the
         * free-list link), and child pointers are cleared so a node is never
         * descended into twice. O(n), no extra storage.
         */
        CWValueMapNode_t* n = m->root;
        while (n) {
            if (n->left) {
                n = n->left;
            } else if (n->right) {
                n = n->right;
            } else {
                CWValueMapNode_t* p = n->parent;
                if (p) {
                    if (p->left == n) {
                        p->left = NULL;
                    } else {
                        p->right = NULL;
                    }
                }
                cwvmap_discard_payload(m, cwvmap_node_key(m, n),
                                       cwvmap_node_value(m, n));
                cwvmap_free_node(m, n);
                n = p;
            }
        }
        m->root  = NULL;
        m->count = 0;
    }


    static inline void cwvmap_destroy(CWValueMap_t* m) {
        if (!m) return;

        cwvmap_clear(m);

        const size_t block_total = m->pool_mem_offset
                                 + m->pool_block_count * m->node_stride;
        CWValueMapBlock_t* block = m->pool;
        while (block) {
            CWValueMapBlock_t* next = block->next;
            cwvmap_block_free(block, block_total);
            block = next;
        }
        free(m);
    }


    /* put / insert / get */

    /**
     * Insert or overwrite. Copies key/value bytes into the node and returns
     * a pointer to the stored value bytes, or NULL on allocation failure.
     * On overwrite the old value is discarded through value_dtor; the old key
     * is discarded through key_dtor only when its bytes differ from the new
     * key's bytes.
     */
    static inline void* cwvmap_put(CWValueMap_t* m,
                                   const void* key, const void* value) {
        CWValueMapNode_t* parent = NULL;
        int last_cmp = 0;
        CWValueMapNode_t* existing = cwvmap_locate(m, key, &parent, &last_cmp);
        if (existing) {
            /* overwrite in place; no node allocation on this hot path */
            void* ok = cwvmap_node_key(m, existing);
            void* ov = cwvmap_node_value(m, existing);
            if (m->key_dtor && memcmp(ok, key, m->key_size) != 0) {
                m->key_dtor(ok);
            }
            if (m->value_dtor) m->value_dtor(ov);
            memcpy(ok, key, m->key_size);
            if (m->value_size) memcpy(ov, value, m->value_size);
            return ov;
        }

        CWValueMapNode_t* node = cwvmap_alloc_node(m);
        if (!node) return NULL;
        memcpy(cwvmap_node_key(m, node), key, m->key_size);
        if (m->value_size) {
            memcpy(cwvmap_node_value(m, node), value, m->value_size);
        }
        cwvmap_link_new_node(m, node, parent, last_cmp);
        return cwvmap_node_value(m, node);
    }


    /** insert-only: returns NULL when the key already exists (no overwrite) */
    static inline void* cwvmap_insert(CWValueMap_t* m,
                                      const void* key, const void* value) {
        CWValueMapNode_t* parent = NULL;
        int last_cmp = 0;
        if (cwvmap_locate(m, key, &parent, &last_cmp)) return NULL;

        CWValueMapNode_t* node = cwvmap_alloc_node(m);
        if (!node) return NULL;
        memcpy(cwvmap_node_key(m, node), key, m->key_size);
        if (m->value_size) {
            memcpy(cwvmap_node_value(m, node), value, m->value_size);
        }
        cwvmap_link_new_node(m, node, parent, last_cmp);
        return cwvmap_node_value(m, node);
    }


    /** pointer to the stored value bytes, or NULL when the key is missing */
    static inline void* cwvmap_get(CWValueMap_t* m, const void* key) {
        CWValueMapNode_t* n = cwvmap_find_node(m, key);
        return n ? cwvmap_node_value(m, n) : NULL;
    }


    static inline bool cwvmap_contains(CWValueMap_t* m, const void* key) {
        return cwvmap_find_node(m, key) != NULL;
    }


    /* remove / take */

    /**
     * Remove the entry and discard it through the registered destructors.
     * Returns false when the key is missing.
     */
    static inline bool cwvmap_remove(CWValueMap_t* m, const void* key) {
        CWValueMapNode_t* z = cwvmap_find_node(m, key);
        if (!z) return false;

        cwvmap_discard_payload(m, cwvmap_node_key(m, z),
                               cwvmap_node_value(m, z));
        cwvmap_erase_node(m, z);
        cwvmap_free_node(m, z);
        m->count--;
        return true;
    }


    /**
     * Remove the entry and copy its key/value bytes to the caller buffers
     * (ownership transfers; destructors are NOT called). Either output may be
     * NULL to discard that side. Returns false when the key is missing.
     */
    static inline bool cwvmap_take(CWValueMap_t* m, const void* key,
                                   void* out_key, void* out_value) {
        CWValueMapNode_t* z = cwvmap_find_node(m, key);
        if (!z) return false;

        if (out_key) {
            memcpy(out_key, cwvmap_node_key(m, z), m->key_size);
        }
        if (out_value && m->value_size) {
            memcpy(out_value, cwvmap_node_value(m, z), m->value_size);
        }

        cwvmap_erase_node(m, z);
        cwvmap_free_node(m, z);
        m->count--;
        return true;
    }


    /* iteration */

    /**
     * After modifying the map, the caller must discard old iterators;
     * otherwise a "use-after-free" logic error may occur (nodes are recycled).
     */
    static inline CWValueMapIter_t cwvmap_begin(CWValueMap_t* m) {
        CWValueMapIter_t it = { m, cwvmap_tree_minimum(m->root) };
        return it;
    }


    static inline CWValueMapIter_t cwvmap_rbegin(CWValueMap_t* m) {
        CWValueMapIter_t it = { m, cwvmap_tree_maximum(m->root) };
        return it;
    }


    static inline CWValueMapIter_t cwvmap_find(CWValueMap_t* m,
                                               const void* key) {
        CWValueMapIter_t it = { m, cwvmap_find_node(m, key) };
        return it;
    }


    static inline bool cwvmap_iter_valid(CWValueMapIter_t* it) {
        return it->current != NULL;
    }


    /** pointer to the key bytes inside the current node */
    static inline void* cwvmap_iter_key(CWValueMapIter_t* it) {
        return it->current ? cwvmap_node_key(it->map, it->current) : NULL;
    }


    /** pointer to the value bytes inside the current node */
    static inline void* cwvmap_iter_value(CWValueMapIter_t* it) {
        return it->current ? cwvmap_node_value(it->map, it->current) : NULL;
    }


    static inline void cwvmap_iter_next(CWValueMapIter_t* it) {
        if (it->current) it->current = cwvmap_successor(it->current);
    }


    static inline void cwvmap_iter_prev(CWValueMapIter_t* it) {
        if (it->current) it->current = cwvmap_predecessor(it->current);
    }


    /* memory reclamation */

    /* block reference used by shrink_to_fit (sorted by node-area base) */
    typedef struct CWValueMapBlockRef {
        size_t chain_idx;       /* position in the block chain (0 = newest) */
        const char* node_base;  /* first node address inside the block */
    } CWValueMapBlockRef_t;


    static int cwvmap_block_ref_cmp(const void* a, const void* b) {
        const uintptr_t x =
            (uintptr_t)((const CWValueMapBlockRef_t*)a)->node_base;
        const uintptr_t y =
            (uintptr_t)((const CWValueMapBlockRef_t*)b)->node_base;
        return (x > y) - (x < y);
    }


    /* index of the block whose node area contains p (binary search, O(log b)) */
    static inline size_t cwvmap_block_index_of(const CWValueMapBlockRef_t* refs,
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
     * O((n + f)*log b + b*log b).
     */
    static inline bool cwvmap_shrink_to_fit(CWValueMap_t* m) {
        if (!m->pool || !m->pool->next) return true; /* 0/1 blocks: no-op */

        const size_t block_total = m->pool_mem_offset
                                 + m->pool_block_count * m->node_stride;

        size_t nb = 0;
        for (CWValueMapBlock_t* b = m->pool; b; b = b->next) nb++;
        CWValueMapBlockRef_t* refs =
            (CWValueMapBlockRef_t*)malloc(nb * sizeof(CWValueMapBlockRef_t));
        if (!refs) return false;
        size_t i = 0;
        for (CWValueMapBlock_t* b = m->pool; b; b = b->next, i++) {
            refs[i].chain_idx = i;
            refs[i].node_base = (const char*)b + m->pool_mem_offset;
        }
        qsort(refs, nb, sizeof(CWValueMapBlockRef_t), cwvmap_block_ref_cmp);

        unsigned char* has_live = (unsigned char*)calloc(1, nb);
        if (!has_live) {
            free(refs);
            return false;
        }
        for (CWValueMapNode_t* n = cwvmap_tree_minimum(m->root);
             n; n = cwvmap_successor(n)) {
            const size_t idx = cwvmap_block_index_of(refs, nb, (const char*)n);
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
        CWValueMapNode_t* fl_head = NULL;
        CWValueMapNode_t* fl_tail = NULL;
        CWValueMapNode_t* cur = m->free_list;
        m->free_count = 0;
        while (cur) {
            CWValueMapNode_t* next = cur->parent; /* free-list link */
            const size_t idx =
                cwvmap_block_index_of(refs, nb, (const char*)cur);
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
        CWValueMapBlock_t* b = m->pool;
        CWValueMapBlock_t* last_kept = NULL;
        while (b && k < keep) {
            last_kept = b;
            b = b->next;
            k++;
        }
        if (last_kept) last_kept->next = NULL;
        while (b) {
            CWValueMapBlock_t* next = b->next;
            cwvmap_block_free(b, block_total);
            b = next;
        }
        m->block_count = keep;
        return true;
    }


    /* safe variants (automatic key/value cleanup) */

    /**
     * safe containers call key_dtor / value_dtor (non-NULL) on every entry
     * they discard (put overwriting an existing key, remove, clear, destroy).
     * cwvmap_take() copies the entry out and does NOT call the destructors.
     * At least one destructor must be non-NULL.
     */
    static inline CWValueMap_t* cwvmap_safe_create_cmp_ex(
                                    size_t key_size,
                                    size_t value_size,
                                    size_t nodes_per_block,
                                    int (*compare)(const void*, const void*),
                                    void (*key_dtor)(void*),
                                    void (*value_dtor)(void*)) {
        if (!key_dtor && !value_dtor) return NULL;
        CWValueMap_t* m = cwvmap_create_cmp_ex(key_size, value_size,
                                               nodes_per_block, compare);
        if (m) {
            m->key_dtor   = key_dtor;
            m->value_dtor = value_dtor;
        }
        return m;
    }


    static inline CWValueMap_t* cwvmap_safe_create_cmp(
                                    size_t key_size,
                                    size_t value_size,
                                    int (*compare)(const void*, const void*),
                                    void (*key_dtor)(void*),
                                    void (*value_dtor)(void*)) {
        return cwvmap_safe_create_cmp_ex(key_size, value_size,
                                         CWValueMap_POOL_NODE_COUNT,
                                         compare, key_dtor, value_dtor);
    }


    static inline CWValueMap_t* cwvmap_safe_create_ex(
                                    size_t key_size,
                                    size_t value_size,
                                    size_t nodes_per_block,
                                    void (*key_dtor)(void*),
                                    void (*value_dtor)(void*)) {
        return cwvmap_safe_create_cmp_ex(key_size, value_size,
                                         nodes_per_block, NULL,
                                         key_dtor, value_dtor);
    }


    static inline CWValueMap_t* cwvmap_safe_create(
                                    size_t key_size,
                                    size_t value_size,
                                    void (*key_dtor)(void*),
                                    void (*value_dtor)(void*)) {
        return cwvmap_safe_create_cmp_ex(key_size, value_size,
                                         CWValueMap_POOL_NODE_COUNT, NULL,
                                         key_dtor, value_dtor);
    }


    /* queries */

    static inline size_t cwvmap_size(CWValueMap_t* m) {
        return m->count;
    }


    static inline bool cwvmap_empty(CWValueMap_t* m) {
        return m->count == 0;
    }


    /* total nodes owned by the pool (live + free) */
    static inline size_t cwvmap_capacity(CWValueMap_t* m) {
        return m->block_count * m->pool_block_count;
    }


    /* nodes currently recycled and waiting for reuse */
    static inline size_t cwvmap_free_count(CWValueMap_t* m) {
        return m->free_count;
    }


    static inline size_t cwvmap_key_size(CWValueMap_t* m) {
        return m->key_size;
    }


    static inline size_t cwvmap_value_size(CWValueMap_t* m) {
        return m->value_size;
    }

#endif // CWValueMap_H
