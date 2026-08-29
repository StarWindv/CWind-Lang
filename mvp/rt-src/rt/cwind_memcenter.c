/**
 * Copyright (C) 2026 StarWindv
 * License: BSD-3.0
 * Author : StarWindv
 * Location: rt-src/rt/cwind_memcenter.c
 */

#include "../include/memory/cwind_memcenter.h"
#include "../include/gc/cwind_gc.h"

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
    size_t gc_alloc_bytes; /* 自上次 cwgc 取走以来的分配字节 */
    size_t total_alloc_bytes; /* 迄今累计分配字节 (单调, 含已回收) */
    uint64_t gc_topo;      /* 块拓扑版本号 (任何块增删都递增) */
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
    g_mc.gc_alloc_bytes += size; /* cwgc step 的字节驱动 */
    g_mc.total_alloc_bytes += size; /* 累计, 单调不减 */
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
    g_mc.gc_topo++; /* 拓扑变化: GC 范围缓存需重建 */
    g_mc.mapped_bytes += CWMC_BLOCK_SIZE;
    return block;
}

static void cwmc_free_block(CwmcBlockHdr_t* block) {
    const size_t total = block->total;
    cwmc_os_free(block, total);
    if (g_mc.blocks > 0) g_mc.blocks--;
    g_mc.gc_topo++; /* 块已 unmap: 旧范围必须从缓存剔除 */
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
            if (!block->free_head) return NULL;
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
    if (size > SIZE_MAX - CWMC_DEDIC_HDR_SIZE - (CWMC_MAX_ALIGN - 1)) {
        g_mc.errors++;
        return NULL;
    }
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
    g_mc.gc_topo++;
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
    /* 分配字节驱动的增量 GC (todo-35): 触发在分配前;
     * MARK 期间的新槽由 alloc_barrier 染灰保活 */
    cwgc_step();
    void* p = cwmc_alloc_impl(size, CWMC_MAX_ALIGN);
    if (p) cwgc_alloc_barrier(p);
    return p;
}

void* cwmc_alloc_aligned(size_t size, size_t align) {
    if (!g_mc.inited) cwmc_init();
    cwgc_step();
    void* p = cwmc_alloc_impl(size, align);
    if (p) cwgc_alloc_barrier(p);
    return p;
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
    g_mc.gc_topo++;
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

/* ---- GC 协作接口实现 (todo-35 阶段 0) ---- */

/*
 * 地址判定必须先确认落在托管块区间内, 再 probe 槽头 ——
 * 保守扫描喂进来的 word 可能指向任意未映射内存, 直接读
 * addr-32/48 的 magic 会踩到不可读页 (实测段错误)。
 */

typedef struct CWMCRange {
    char* start; /* 块起始 (slab 头 / dedicated 头) */
    char* end;   /* start + total */
    bool  slab;  /* true = slab 块, false = 大对象 */
} CWMCRange_t;

static CWMCRange_t* g_gc_ranges;
static size_t g_gc_range_count;
static size_t g_gc_range_cap;
static uint64_t g_gc_range_gen = UINT64_MAX; /* 重建依据: 拓扑版本号 */

static int cwmc_range_cmp(const void* a, const void* b) {
    const CWMCRange_t* ra = (const CWMCRange_t*)a;
    const CWMCRange_t* rb = (const CWMCRange_t*)b;
    return (ra->start < rb->start) ? -1 : (ra->start > rb->start) ? 1 : 0;
}

static void cwmc_gc_rebuild_ranges(void) {
    g_gc_range_count = 0;
    const size_t need = g_mc.blocks;
    if (need > g_gc_range_cap) {
        CWMCRange_t* nr =
            (CWMCRange_t*)realloc(g_gc_ranges, need * sizeof(CWMCRange_t));
        if (!nr) return;
        g_gc_ranges = nr;
        g_gc_range_cap = need;
    }
    for (size_t ci = 0; ci < CWMC_SLAB_CLASS_COUNT; ci++) {
        for (CwmcBlockHdr_t* b = g_mc.classes[ci]; b; b = b->next) {
            if (g_gc_range_count >= g_gc_range_cap) break;
            g_gc_ranges[g_gc_range_count].start = (char*)b;
            g_gc_ranges[g_gc_range_count].end = (char*)b + b->total;
            g_gc_ranges[g_gc_range_count].slab = true;
            g_gc_range_count++;
        }
    }
    for (CwmcDedicatedHdr_t* d = g_mc.dedicated; d; d = d->next) {
        if (g_gc_range_count >= g_gc_range_cap) break;
        g_gc_ranges[g_gc_range_count].start = (char*)d;
        g_gc_ranges[g_gc_range_count].end = (char*)d + d->total;
        g_gc_ranges[g_gc_range_count].slab = false;
        g_gc_range_count++;
    }
    qsort(g_gc_ranges, g_gc_range_count, sizeof(CWMCRange_t),
          cwmc_range_cmp);
}

/* 命中托管块则返回该区间; 否则 NULL (二分, 只读安全) */
static const CWMCRange_t* cwmc_gc_range_of(const void* addr) {
    if (!g_mc.inited) return NULL;
    if (g_gc_range_gen != g_mc.gc_topo) cwmc_gc_rebuild_ranges();
    if (!g_gc_ranges || g_gc_range_count == 0) return NULL;
    uintptr_t a = (uintptr_t)addr;
    size_t lo = 0;
    size_t hi = g_gc_range_count;
    while (lo < hi) {
        const size_t mid = lo + (hi - lo) / 2;
        const uintptr_t s = (uintptr_t)g_gc_ranges[mid].start;
        if (a < s) {
            hi = mid;
        } else if (a >= (uintptr_t)g_gc_ranges[mid].end) {
            lo = mid + 1;
        } else {
            return &g_gc_ranges[mid];
        }
    }
    return NULL;
}

uint64_t* cwmc_gc_meta_of(const void* addr) {
    if (!addr || !g_mc.inited) return NULL;
    const CWMCRange_t* r = cwmc_gc_range_of(addr);
    if (!r) return NULL;

    if (r->slab) {
        /* 槽头必须在块内: addr >= start+32 才有 addr-32 可读 */
        if ((uintptr_t)addr < (uintptr_t)r->start + CWMC_SLOT_HDR_SIZE) {
            return NULL;
        }
        CwmcSlotHdr_t* h = (CwmcSlotHdr_t*)((const char*)addr
                                            - CWMC_SLOT_HDR_SIZE);
        if (h->u.used.magic != CWMC_CHUNK_MAGIC) return NULL;
        if ((CwmcBlockHdr_t*)(uintptr_t)h->u.used.block
            != (CwmcBlockHdr_t*)r->start) {
            return NULL; /* magic 撞上载荷字节的假命中 */
        }
        return &h->u.used.reserved;
    }

    /* 大对象: 头 48 字节, addr 必须 >= start+48 */
    if ((uintptr_t)addr < (uintptr_t)r->start + CWMC_DEDIC_HDR_SIZE) {
        return NULL;
    }
    CwmcDedicatedHdr_t* h = (CwmcDedicatedHdr_t*)((const char*)addr
                                                  - CWMC_DEDIC_HDR_SIZE);
    if (h->magic != CWMC_CHUNK_MAGIC) return NULL;
    return &h->reserved;
}

void cwmc_gc_iter_used(cwmc_gc_used_cb cb, void* ud) {
    if (!cb || !g_mc.inited) return;

    for (size_t ci = 0; ci < CWMC_SLAB_CLASS_COUNT; ci++) {
        for (CwmcBlockHdr_t* b = g_mc.classes[ci]; b; b = b->next) {
            const size_t hdr_off = cwmc_align_up(sizeof(CwmcBlockHdr_t),
                                                 CWMC_MAP_HDR_ALIGN);
            char* slot_base = (char*)b + hdr_off;
            for (size_t i = 0; i < b->capacity; i++) {
                CwmcSlotHdr_t* s = (CwmcSlotHdr_t*)(slot_base + i * b->slot_size);
                if (s->u.used.magic != CWMC_CHUNK_MAGIC) continue; /* 空闲 */
                void* payload = (char*)s + CWMC_SLOT_HDR_SIZE;
                if (!cb(payload, (size_t)s->u.used.size,
                        &s->u.used.reserved, ud)) {
                    return;
                }
            }
        }
    }

    CwmcDedicatedHdr_t* d = g_mc.dedicated;
    while (d) {
        void* payload = (char*)d + CWMC_DEDIC_HDR_SIZE;
        if (!cb(payload, (size_t)d->size, &d->reserved, ud)) return;
        d = d->next;
    }
}

bool cwmc_gc_release(void* payload) {
    if (!payload || !g_mc.inited) return false;
    const CwmcSlotHdr_t* view = cwmc_view_of(payload);
    if (!view) return false;

    const uint64_t size = view->u.used.size;
    CwmcBlockHdr_t* block =
        (CwmcBlockHdr_t*)(uintptr_t)view->u.used.block;
    if (block != NULL) {
        /* sweep 归还: 只回块空闲链, 空块保留 (阶段 0 红线) */
        ((CwmcSlotHdr_t*)view)->u.next_free = block->free_head;
        block->free_head = (CwmcSlotHdr_t*)view;
        block->used--;
        cwmc_stats_remove_alloc((size_t)size);
        return true;
    }

    /* 大对象: 阶段 0 策略不释放 (arena 段进程期存活且是注册根;
     * 释放会破坏 cwmc_gc_iter_used 的专用链遍历) */
    return false;
    { CwmcDedicatedHdr_t* hdr =
        (CwmcDedicatedHdr_t*)((char*)payload - CWMC_DEDIC_HDR_SIZE);
    CwmcDedicatedHdr_t* prev = NULL;
    CwmcDedicatedHdr_t* cur = g_mc.dedicated;
    while (cur && cur != hdr) {
        prev = cur;
        cur = cur->next;
    }
    if (!cur) return false;
    if (prev) prev->next = hdr->next;
    else g_mc.dedicated = hdr->next;

    const size_t total = (size_t)hdr->total;
    cwmc_stats_remove_alloc((size_t)size);
    cwmc_os_free(hdr, total);
    g_mc.blocks--;
    g_mc.gc_topo++;
    if (g_mc.mapped_bytes >= total) {
        g_mc.mapped_bytes -= total;
    } else {
        g_mc.mapped_bytes = 0;
    }
    return true;
    }
}

size_t cwmc_gc_alloc_bytes(void) {
    return g_mc.gc_alloc_bytes;
}

/* 迄今累计分配字节 (单调递增, 含已回收; builtins::gc_allocated_bytes) */
size_t cwmc_alloc_total_bytes(void) {
    return g_mc.total_alloc_bytes;
}

void cwmc_gc_take_alloc_bytes(size_t bytes) {
    if (g_mc.gc_alloc_bytes >= bytes) g_mc.gc_alloc_bytes -= bytes;
    else g_mc.gc_alloc_bytes = 0;
}
