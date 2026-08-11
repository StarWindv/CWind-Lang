/**
 * Copyright (C) 2026 StarWindv
 * License: BSD-3.0
 * Author : StarWindv
 * Location: rt-src/rt/cwind_memcenter.c
 */

#include "../include/memory/cwind_memcenter.h"

#include <string.h>

/*
 * 布局约定:
 *
 * slab 块 (CWMC_BLOCK_SIZE 字节):
 *   [ CwmcBlockHdr | 槽 0 | 槽 1 | ... ]
 *   每个槽 = 32 字节槽头 + 载荷; 槽头分配时存 {magic, size, block},
 *   空闲时首 8 字节作 next_free 指针。
 *
 * 大对象块:
 *   [ CwmcDedicatedHdr | 载荷 ]
 *   header->next 挂全局大对象链表, 供 shutdown 统一回收。
 */

#define CWMC_CHUNK_MAGIC  UINT64_C(0x434D57434D43484B) /* "CMWCMCHK" */
#define CWMC_BLOCK_MAGIC  UINT64_C(0x434D5743424C4B20) /* "CMWCBLK " */

#define CWMC_SLOT_HDR_SIZE ((size_t)32)
#define CWMC_DEDIC_HDR_SIZE ((size_t)48)
#define CWMC_MAP_HDR_ALIGN ((size_t)16)

typedef struct CwmcBlockHdr CwmcBlockHdr_t;
typedef struct CwmcDedicatedHdr CwmcDedicatedHdr_t;

typedef struct CwmcSlotHdr {
    union {
        struct {
            uint64_t magic;   /* CWMC_CHUNK_MAGIC */
            uint64_t size;    /* 用户请求的载荷大小 */
            CwmcBlockHdr_t* block;      /* 所属 slab 块, 大对象为 NULL */
            uint64_t reserved;
        } used;
        struct CwmcSlotHdr* next_free;  /* 空闲链表 */
    } u;
} CwmcSlotHdr_t;

typedef struct CwmcBlockHdr {
    uint64_t magic;        /* CWMC_BLOCK_MAGIC */
    uint64_t total;        /* 整块映射字节数 (== CWMC_BLOCK_SIZE) */
    uint32_t class_id;     /* 大小类下标 */
    uint32_t slot_size;    /* 槽大小 = 32 + 类容量 */
    size_t   capacity;     /* 槽数 */
    size_t   used;         /* 在用槽数 */
    CwmcSlotHdr_t* free_head;
    CwmcBlockHdr_t* next;      /* 同类块链表 */
} CwmcBlockHdr_t;

typedef struct CwmcDedicatedHdr {
    /* 前 32 字节必须与 CwmcSlotHdr.used 布局一致, 便于统一 free 检查 */
    uint64_t magic;        /* CWMC_CHUNK_MAGIC */
    uint64_t size;         /* 用户请求的载荷大小 */
    uint64_t block;        /* 恒为 0, 标记大对象 */
    uint64_t reserved;
    uint64_t total;        /* 整块映射字节数 (含 48 字节头) */
    CwmcDedicatedHdr_t* next;       /* 全局大对象链表 */
} CwmcDedicatedHdr_t;

static const size_t k_slab_caps[] = {
    16, 32, 48, 64, 96, 128, 192, 256,
    384, 512, 768, 1024, 1536, 2048, 3072, 4096
};
#define CWMC_SLAB_CLASS_COUNT (sizeof(k_slab_caps) / sizeof(k_slab_caps[0]))

typedef struct CwmcCenter {
    bool inited;
    CwmcBlockHdr_t* classes[CWMC_SLAB_CLASS_COUNT];
    CwmcDedicatedHdr_t* dedicated;
    size_t blocks;
    size_t active_allocs;
    size_t mapped_bytes;
    size_t used_bytes;
    size_t errors;
} CwmcCenter_t;

static CwmcCenter_t g_mc;

/* ---- OS 内存来源 ---- */

#if defined(_WIN32)

    #include <windows.h>

    static void* cwmc_os_alloc(size_t size) {
        return VirtualAlloc(NULL, size, MEM_RESERVE | MEM_COMMIT,
                            PAGE_READWRITE);
    }

    static void cwmc_os_free(void* p, size_t size) {
        (void)size;
        if (p) VirtualFree(p, 0, MEM_RELEASE);
    }

#else

    #include <sys/mman.h>

    #if !defined(MAP_ANONYMOUS) && defined(MAP_ANON)
        #define MAP_ANONYMOUS MAP_ANON
    #endif

    static void* cwmc_os_alloc(size_t size) {
        void* p = mmap(NULL, size, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        return (p == MAP_FAILED) ? NULL : p;
    }

    static void cwmc_os_free(void* p, size_t size) {
        if (p) munmap(p, size);
    }

#endif

/* ---- 工具 ---- */

static size_t cwmc_align_up(size_t v, size_t a) {
    return (v + a - 1) & ~(a - 1);
}

static bool cwmc_align_ok(size_t align) {
    if (align == 0) return true;
    if (align > CWMC_MAX_ALIGN) return false;
    return (align & (align - 1)) == 0;
}

static size_t cwmc_class_for(size_t size) {
    size_t need = cwmc_align_up(size, CWMC_MAX_ALIGN);
    for (size_t i = 0; i < CWMC_SLAB_CLASS_COUNT; i++) {
        if (k_slab_caps[i] >= need) return i;
    }
    return (size_t)-1;
}

static void cwmc_stats_add_alloc(size_t size) {
    g_mc.active_allocs++;
    g_mc.used_bytes += size;
}

static void cwmc_stats_remove_alloc(size_t size) {
    if (g_mc.active_allocs > 0) g_mc.active_allocs--;
    if (g_mc.used_bytes >= size) g_mc.used_bytes -= size;
    else g_mc.used_bytes = 0;
}

/* ---- slab 块管理 ---- */

static CwmcBlockHdr_t* cwmc_new_block(size_t class_id) {
    const size_t cap     = k_slab_caps[class_id];
    const size_t slot    = CWMC_SLOT_HDR_SIZE + cap;
    const size_t hdr_off = cwmc_align_up(sizeof(CwmcBlockHdr_t),
                                         CWMC_MAP_HDR_ALIGN);
    if (hdr_off >= CWMC_BLOCK_SIZE) return NULL;

    CwmcBlockHdr_t* block = (CwmcBlockHdr_t*)cwmc_os_alloc(CWMC_BLOCK_SIZE);
    if (!block) return NULL;

    block->magic     = CWMC_BLOCK_MAGIC;
    block->total     = CWMC_BLOCK_SIZE;
    block->class_id  = (uint32_t)class_id;
    block->slot_size = (uint32_t)slot;
    block->capacity  = (CWMC_BLOCK_SIZE - hdr_off) / slot;
    block->used      = 0;
    block->free_head = NULL;
    block->next      = NULL;

    char* slot_base = (char*)block + hdr_off;
    for (size_t i = 0; i < block->capacity; i++) {
        CwmcSlotHdr_t* s = (CwmcSlotHdr_t*)(slot_base + i * slot);
        s->u.next_free = block->free_head;
        block->free_head = s;
    }

    g_mc.blocks++;
    g_mc.mapped_bytes += CWMC_BLOCK_SIZE;
    return block;
}

static void cwmc_free_block(CwmcBlockHdr_t* block) {
    const size_t total = block->total;
    cwmc_os_free(block, total);
    if (g_mc.blocks > 0) g_mc.blocks--;
    if (g_mc.mapped_bytes >= total) {
        g_mc.mapped_bytes -= total;
    } else {
        g_mc.mapped_bytes = 0;
    }
}

/* ---- 分配 / 回收 ---- */

static void* cwmc_alloc_impl(size_t size, size_t align) {
    if (!cwmc_align_ok(align)) {
        g_mc.errors++;
        return NULL;
    }
    if (size == 0) return NULL;

    const size_t class_id = cwmc_class_for(size);
    if (class_id != (size_t)-1) {
        CwmcBlockHdr_t* block = g_mc.classes[class_id];
        if (block == NULL) {
            block = cwmc_new_block(class_id);
            if (!block) return NULL;
            g_mc.classes[class_id] = block;
        }
        if (block->free_head == NULL) {
            CwmcBlockHdr_t* fresh = cwmc_new_block(class_id);
            if (!fresh) return NULL;
            /* 新块放链表头: 下次分配直接复用新块, 避免每次新建 */
            fresh->next = g_mc.classes[class_id];
            g_mc.classes[class_id] = fresh;
            block = fresh;
        }

        CwmcSlotHdr_t* slot = block->free_head;
        block->free_head = slot->u.next_free;
        slot->u.used.magic   = CWMC_CHUNK_MAGIC;
        slot->u.used.size    = size;
        slot->u.used.block   = block;
        slot->u.used.reserved = 0;
        block->used++;

        cwmc_stats_add_alloc(size);
        return (char*)slot + CWMC_SLOT_HDR_SIZE;
    }

    /* 大对象: 独立映射 */
    const size_t total = CWMC_DEDIC_HDR_SIZE + cwmc_align_up(size,
                                                              CWMC_MAX_ALIGN);
    CwmcDedicatedHdr_t* hdr = (CwmcDedicatedHdr_t*)cwmc_os_alloc(total);
    if (!hdr) return NULL;

    hdr->magic    = CWMC_CHUNK_MAGIC;
    hdr->size     = size;
    hdr->block    = 0;
    hdr->reserved = 0;
    hdr->total    = total;
    hdr->next     = g_mc.dedicated;
    g_mc.dedicated = hdr;

    g_mc.blocks++;
    g_mc.mapped_bytes += total;
    cwmc_stats_add_alloc(size);
    return (char*)hdr + CWMC_DEDIC_HDR_SIZE;
}

/**
 * 统一头视图:
 *  - slab 槽: 头从 ptr-32 开始 (magic/size/block/reserved)
 *  - 大对象:  头从 ptr-48 开始, 前 32 字节与槽头同布局, block 恒为 0
 */
static const CwmcSlotHdr_t* cwmc_view_of(const void* ptr) {
    const CwmcSlotHdr_t* s =
        (const CwmcSlotHdr_t*)((const char*)ptr - CWMC_SLOT_HDR_SIZE);
    if (s->u.used.magic == CWMC_CHUNK_MAGIC) return s;

    const CwmcSlotHdr_t* d =
        (const CwmcSlotHdr_t*)((const char*)ptr - CWMC_DEDIC_HDR_SIZE);
    if (d->u.used.magic == CWMC_CHUNK_MAGIC && d->u.used.block == 0) {
        return d;
    }
    return NULL;
}

void cwmc_init(void) {
    if (g_mc.inited) return;
    memset(&g_mc, 0, sizeof(g_mc));
    g_mc.inited = true;
}

void cwmc_shutdown(void) {
    if (!g_mc.inited) return;

    for (size_t i = 0; i < CWMC_SLAB_CLASS_COUNT; i++) {
        CwmcBlockHdr_t* block = g_mc.classes[i];
        while (block) {
            CwmcBlockHdr_t* next = block->next;
            cwmc_free_block(block);
            block = next;
        }
        g_mc.classes[i] = NULL;
    }

    CwmcDedicatedHdr_t* hdr = g_mc.dedicated;
    while (hdr) {
        CwmcDedicatedHdr_t* next = hdr->next;
        const size_t total = (size_t)hdr->total;
        cwmc_os_free(hdr, total);
        g_mc.blocks--;
        if (g_mc.mapped_bytes >= total) {
            g_mc.mapped_bytes -= total;
        } else {
            g_mc.mapped_bytes = 0;
        }
        hdr = next;
    }
    g_mc.dedicated = NULL;

    g_mc.active_allocs = 0;
    g_mc.used_bytes    = 0;
    g_mc.inited        = false;
}

void* cwmc_alloc(size_t size) {
    if (!g_mc.inited) cwmc_init();
    return cwmc_alloc_impl(size, CWMC_MAX_ALIGN);
}

void* cwmc_alloc_aligned(size_t size, size_t align) {
    if (!g_mc.inited) cwmc_init();
    return cwmc_alloc_impl(size, align);
}

void* cwmc_calloc(size_t size) {
    void* p = cwmc_alloc(size);
    if (p) memset(p, 0, size);
    return p;
}

void* cwmc_realloc(void* ptr, size_t new_size) {
    if (!g_mc.inited) cwmc_init();
    if (!ptr) return cwmc_alloc(new_size);
    if (new_size == 0) {
        cwmc_free(ptr);
        return NULL;
    }

    const CwmcSlotHdr_t* view = cwmc_view_of(ptr);
    if (!view) {
        g_mc.errors++;
        return NULL;
    }

    const uint64_t old_size = view->u.used.size;
    if (old_size >= new_size) {
        g_mc.used_bytes -= (size_t)old_size;
        g_mc.used_bytes += new_size;
        ((CwmcSlotHdr_t*)view)->u.used.size = new_size;
        return ptr;
    }

    CwmcBlockHdr_t* block =
        (CwmcBlockHdr_t*)(uintptr_t)view->u.used.block;
    if (block != NULL) {
        const size_t cap = k_slab_caps[block->class_id];
        if (new_size <= cap) {
            g_mc.used_bytes -= (size_t)old_size;
            g_mc.used_bytes += new_size;
            ((CwmcSlotHdr_t*)view)->u.used.size = new_size;
            return ptr;
        }
    } else {
        CwmcDedicatedHdr_t* hdr =
            (CwmcDedicatedHdr_t*)((char*)ptr - CWMC_DEDIC_HDR_SIZE);
        if ((size_t)hdr->total - CWMC_DEDIC_HDR_SIZE >= new_size) {
            g_mc.used_bytes -= (size_t)old_size;
            g_mc.used_bytes += new_size;
            hdr->size = new_size;
            return ptr;
        }
    }

    void* fresh = cwmc_alloc(new_size);
    if (!fresh) return NULL;
    memcpy(fresh, ptr, (size_t)old_size);
    cwmc_free(ptr);
    return fresh;
}

void cwmc_free(void* ptr) {
    if (!ptr) return;
    if (!g_mc.inited) {
        g_mc.errors++;
        return;
    }

    const CwmcSlotHdr_t* view = cwmc_view_of(ptr);
    if (!view) {
        g_mc.errors++;
        return;
    }

    const uint64_t size = view->u.used.size;
    CwmcBlockHdr_t* block =
        (CwmcBlockHdr_t*)(uintptr_t)view->u.used.block;
    if (block != NULL) {
        /* 把槽还给块的空闲链表, 并清掉 magic 以便识别 double-free */
        ((CwmcSlotHdr_t*)view)->u.next_free = block->free_head;
        block->free_head = (CwmcSlotHdr_t*)view;
        block->used--;
        cwmc_stats_remove_alloc((size_t)size);

        /* 空块只保留一个, 其余还给 OS */
        if (block->used == 0 && g_mc.classes[block->class_id] != block) {
            CwmcBlockHdr_t* list = g_mc.classes[block->class_id];
            if (list == block) {
                g_mc.classes[block->class_id] = block->next;
            } else {
                while (list && list->next != block) list = list->next;
                if (list) list->next = block->next;
            }
            cwmc_free_block(block);
        }
        return;
    }

    /* 大对象: 48 字节头, 前 32 字节与槽头同布局 */
    CwmcDedicatedHdr_t* hdr =
        (CwmcDedicatedHdr_t*)((char*)ptr - CWMC_DEDIC_HDR_SIZE);
    CwmcDedicatedHdr_t* prev = NULL;
    CwmcDedicatedHdr_t* cur = g_mc.dedicated;
    while (cur && cur != hdr) {
        prev = cur;
        cur = cur->next;
    }
    if (!cur) {
        g_mc.errors++;
        return;
    }
    if (prev) prev->next = hdr->next;
    else g_mc.dedicated = hdr->next;

    const size_t total = (size_t)hdr->total;
    cwmc_stats_remove_alloc((size_t)size);
    cwmc_os_free(hdr, total);
    g_mc.blocks--;
    if (g_mc.mapped_bytes >= total) {
        g_mc.mapped_bytes -= total;
    } else {
        g_mc.mapped_bytes = 0;
    }
}

bool cwmc_stats(CWMemCenterStats_t* out) {
    if (!out) return false;
    if (!g_mc.inited) cwmc_init();
    out->blocks        = g_mc.blocks;
    out->active_allocs = g_mc.active_allocs;
    out->mapped_bytes  = g_mc.mapped_bytes;
    out->used_bytes    = g_mc.used_bytes;
    out->errors        = g_mc.errors;
    return true;
}

size_t cwmc_usable_size(const void* ptr) {
    if (!ptr) return 0;
    if (!g_mc.inited) return 0;
    const CwmcSlotHdr_t* view = cwmc_view_of(ptr);
    if (!view) return 0;
    return (size_t)view->u.used.size;
}
