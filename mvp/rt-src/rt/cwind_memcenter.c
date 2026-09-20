/**
 * Copyright (C) 2026 StarWindv
 * License: BSD-3.0
 * Author : StarWindv
 * Location: rt-src/rt/cwind_memcenter.c
 */

#include "../include/memory/cwind_memcenter.h"
#include "../include/gc/cwind_gc.h"

#include <string.h>
#include <stdlib.h>
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
} CwmcCenter_t;

static CwmcCenter_t g_mc;

/* 托管区间 chunk 登记 (todo-237, 实现见下方「GC 协作接口」段); 块拓扑
 * 变化点直接增删条目, 不再整表重建 */
static void cwmc_page_add(uintptr_t start, uintptr_t end, bool slab);
static void cwmc_page_del(uintptr_t start, uintptr_t end, bool slab);
static void cwmc_page_reset(void);

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
    g_mc.mapped_bytes += CWMC_BLOCK_SIZE;
    cwmc_page_add((uintptr_t)block, (uintptr_t)block + CWMC_BLOCK_SIZE,
                  true);
    return block;
}

static void cwmc_free_block(CwmcBlockHdr_t* block) {
    const size_t total = block->total;
    cwmc_page_del((uintptr_t)block, (uintptr_t)block + total, true);
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
    g_mc.mapped_bytes += total;
    cwmc_stats_add_alloc(size);
    cwmc_page_add((uintptr_t)hdr, (uintptr_t)hdr + total, false);
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
        cwmc_page_del((uintptr_t)hdr, (uintptr_t)hdr + total, false);
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

    cwmc_page_reset(); /* chunk 表随内存中心一并清空 (再 init 重新建) */
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
    cwmc_page_del((uintptr_t)hdr, (uintptr_t)hdr + total, false);
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

/* ---- GC 协作接口实现 (todo-35 阶段 0) ---- */

/*
 * 地址判定必须先确认落在托管块区间内, 再 probe 槽头 ——
 * 保守扫描喂进来的 word 可能指向任意未映射内存, 直接读
 * addr-32/48 的 magic 会踩到不可读页 (实测段错误)。
 */

/*
 * 地址判定 (todo-237): 托管区间 chunk 哈希。
 *
 * 保守扫描每个 word 都要判「是否命中托管区间」。旧实现 (有序区间
 * 数组 + 每次拓扑变化整表 qsort + 每 word 二分) 在存活面大、扫描量
 * 高时是热点。现在把每个托管映射按 64 KiB chunk 登记进开放寻址哈希:
 *   key = addr >> 16, value = { 区间 start/end, slab }
 * 查找 O(1) 平均; 拓扑变化只增删本区间的 chunk 条目, 不再 qsort 重建。
 * 相邻/小映射可能共用一个 chunk (POSIX 4K 映射混排), 同 key 可有多条,
 * 用区间包含判定消歧 (Windows 的 VirtualAlloc 按 64K 粒度独占则不会)。
 * magic/block 校验一律保留 (不得放宽: 假命中会误标任意内存)。
 *
 * OOM 兜底: 表扩容失败 -> g_pages_oom, 此后 sweep 遍历直接停止
 * (全部槽视作存活) —— 只误保留, 不悬垂。
 */

#define CWMC_PAGE_SHIFT 16

typedef struct CWMCPageEnt {
    uintptr_t start; /* 区间起点; 0 = 空槽 */
    uintptr_t end;   /* 区间终点 (不含) */
    uintptr_t page;  /* chunk 号 (addr >> CWMC_PAGE_SHIFT) */
    uintptr_t slab;  /* 1 = slab 块, 0 = 大对象 */
} CWMCPageEnt_t;

static CWMCPageEnt_t* g_pages;
static size_t g_page_cap;  /* 2 的幂; 0 = 未分配 */
static unsigned g_page_shift; /* log2(g_page_cap) */
static size_t g_page_used; /* 占用槽数 */
static bool g_pages_oom;   /* 扩容失败: 地址判定不完整, sweep 停摆 */

static void cwmc_page_reset(void) {
    free(g_pages);
    g_pages = NULL;
    g_page_cap = 0;
    g_page_shift = 0;
    g_page_used = 0;
    g_pages_oom = false;
}

static unsigned cwmc_page_log2(size_t v) {
    unsigned s = 0;
    while (((size_t)1 << s) < v) s++;
    return s;
}

/* Fibonacci 哈希取高 shift 位: chunk 号低位对齐到 16 位, 必须把
 * 乘积累在高位的熵用起来, 不能直接 cap-1 掩码取低位 */
static size_t cwmc_page_bucket(uintptr_t page, unsigned shift) {
    const uint64_t h = (uint64_t)page * UINT64_C(0x9E3779B97F4A7C15);
    return (size_t)(h >> (64 - shift));
}

static bool cwmc_page_grow(void) {
    const size_t ncap = g_page_cap ? g_page_cap * 2 : 4096;
    const unsigned nshift = cwmc_page_log2(ncap);
    CWMCPageEnt_t* np = (CWMCPageEnt_t*)malloc(ncap * sizeof(*np));
    if (!np) {
        g_pages_oom = true;
        return false;
    }
    memset(np, 0, ncap * sizeof(*np));
    for (size_t i = 0; i < g_page_cap; i++) {
        if (!g_pages[i].start) continue;
        size_t j = cwmc_page_bucket(g_pages[i].page, nshift);
        while (np[j].start) j = (j + 1) & (ncap - 1);
        np[j] = g_pages[i];
    }
    free(g_pages);
    g_pages = np;
    g_page_cap = ncap;
    g_page_shift = nshift;
    return true;
}

static inline const CWMCPageEnt_t* cwmc_page_find(uintptr_t addr) {
    if (!g_page_cap) return NULL;
    const uintptr_t page = addr >> CWMC_PAGE_SHIFT;
    size_t i = cwmc_page_bucket(page, g_page_shift);
    while (g_pages[i].start) {
        if (g_pages[i].page == page && addr >= g_pages[i].start
            && addr < g_pages[i].end) {
            return &g_pages[i];
        }
        /* 同 chunk 可能叠着多个区间 (小映射混排), 继续探测 */
        i = (i + 1) & (g_page_cap - 1);
    }
    return NULL;
}

/* 删除单个 chunk 条目并平移其后探测链上依赖空槽的条目 (backward-shift) */
static void cwmc_page_erase(uintptr_t page, uintptr_t start, uintptr_t end,
                            uintptr_t slab) {
    if (!g_page_cap) return;
    size_t i = cwmc_page_bucket(page, g_page_shift);
    while (g_pages[i].start
           && !(g_pages[i].page == page && g_pages[i].start == start
                && g_pages[i].end == end && g_pages[i].slab == slab)) {
        i = (i + 1) & (g_page_cap - 1);
    }
    if (!g_pages[i].start) return;
    g_page_used--; /* 只剔除一个条目 (backward-shift 只是搬运, 不计数) */
    size_t j = i;
    for (;;) {
        g_pages[i].start = 0;
        g_pages[i].end = 0;
        g_pages[i].page = 0;
        g_pages[i].slab = 0;
        j = (j + 1) & (g_page_cap - 1);
        while (g_pages[j].start) {
            const size_t k = cwmc_page_bucket(g_pages[j].page, g_page_shift);
            const bool blocked = (i <= j) ? (k <= i || k > j)
                                          : (k <= i && k > j);
            if (blocked) break;
            j = (j + 1) & (g_page_cap - 1);
        }
        if (!g_pages[j].start) return;
        g_pages[i] = g_pages[j];
        i = j;
    }
}

/* 登记区间 [start, end) 覆盖的全部 chunk; 失败 (OOM) 置 g_pages_oom */
static void cwmc_page_add(uintptr_t start, uintptr_t end, bool slab) {
    if (end <= start) return;
    const uintptr_t first = start >> CWMC_PAGE_SHIFT;
    const uintptr_t last = (end - 1) >> CWMC_PAGE_SHIFT;
    const size_t need = (size_t)(last - first) + 1;
    while (!g_pages_oom
           && (g_page_cap == 0 || (g_page_used + need) * 4 >= g_page_cap * 3)) {
        if (!cwmc_page_grow()) break; /* OOM: 本区间未登记, sweep 停摆兜底 */
    }
    if (g_pages_oom) return;
    for (uintptr_t p = first; ; p++) {
        size_t i = cwmc_page_bucket(p, g_page_shift);
        while (g_pages[i].start
               && !(g_pages[i].page == p && g_pages[i].start == start
                    && g_pages[i].end == end
                    && g_pages[i].slab == (uintptr_t)slab)) {
            i = (i + 1) & (g_page_cap - 1);
        }
        if (!g_pages[i].start) {
            g_page_used++;
            g_pages[i].page = p;
            g_pages[i].start = start;
            g_pages[i].end = end;
            g_pages[i].slab = (uintptr_t)slab;
        }
        if (p == last) break;
    }
}

/* 注销区间 [start, end) 覆盖的全部 chunk (unmap 前调用: 旧范围必须失效) */
static void cwmc_page_del(uintptr_t start, uintptr_t end, bool slab) {
    if (!g_page_cap || end <= start) return;
    const uintptr_t first = start >> CWMC_PAGE_SHIFT;
    const uintptr_t last = (end - 1) >> CWMC_PAGE_SHIFT;
    for (uintptr_t p = first; ; p++) {
        cwmc_page_erase(p, start, end, (uintptr_t)slab);
        if (p == last) break;
    }
}

uint64_t* cwmc_gc_meta_of(const void* addr) {
    if (!addr || !g_mc.inited) return NULL;
    const uintptr_t a = (uintptr_t)addr;
    const CWMCPageEnt_t* e = cwmc_page_find(a);
    if (!e) return NULL;
    const uintptr_t start = e->start;
    if (a < start || a >= e->end) return NULL; /* chunk 尾部: 复验包含 */

    if (e->slab) {
        /* 槽头必须在块内: addr >= start+32 才有 addr-32 可读 */
        if (a < start + CWMC_SLOT_HDR_SIZE) return NULL;
        CwmcSlotHdr_t* h = (CwmcSlotHdr_t*)((const char*)addr
                                            - CWMC_SLOT_HDR_SIZE);
        if (h->u.used.magic != CWMC_CHUNK_MAGIC) return NULL;
        if ((CwmcBlockHdr_t*)(uintptr_t)h->u.used.block
            != (CwmcBlockHdr_t*)start) {
            return NULL; /* magic 撞上载荷字节的假命中 */
        }
        return &h->u.used.reserved;
    }

    /* 大对象: 头 48 字节, addr 必须 >= start+48 */
    if (a < start + CWMC_DEDIC_HDR_SIZE) return NULL;
    CwmcDedicatedHdr_t* h = (CwmcDedicatedHdr_t*)((const char*)addr
                                                  - CWMC_DEDIC_HDR_SIZE);
    if (h->magic != CWMC_CHUNK_MAGIC) return NULL;
    return &h->reserved;
}

void cwmc_gc_iter_used(cwmc_gc_used_cb cb, void* ud) {
    if (!cb || !g_mc.inited) return;
    /* chunk 表 OOM: 地址判定不完整, sweep 必须停摆 (全部槽视作存活),
     * 否则未登记区间的白色槽会被误回收 -> 悬垂。 */
    if (g_pages_oom) return;

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
        /* sweep 归还: 只回块空闲链, 空块保留 (OS 归还由
         * 149 的 cwmc_gc_release_block 在 sweep 后按水位统一做) */
        ((CwmcSlotHdr_t*)view)->u.next_free = block->free_head;
        block->free_head = (CwmcSlotHdr_t*)view;
        block->used--;
        cwmc_stats_remove_alloc((size_t)size);
        return true;
    }

    /* 大对象: sweep 路径不直接释放 (arena 段是注册根恒黑,
     * 不会成为 victim; 其余大对象由 GC 走
     * cwmc_gc_release_large 裁定后释放, 那条路径同步解专用链) */
    return false;
}

/* ---- OS 归还 (todo-149) ---- */

bool cwmc_gc_is_large(const void* payload) {
    if (!payload || !g_mc.inited) return false;
    /* 大对象头 48B: ptr-32 读到的是头内 block 字段
     * (恒 0) 而非 magic, 按 dedicated 视图 ptr-48 校验 */
    const CwmcSlotHdr_t* dv =
        (const CwmcSlotHdr_t*)((const char*)payload - CWMC_DEDIC_HDR_SIZE);
    return dv->u.used.magic == CWMC_CHUNK_MAGIC
        && dv->u.used.block == NULL;
}

void cwmc_gc_iter_blocks(cwmc_gc_block_cb cb, void* ud) {
    if (!cb || !g_mc.inited) return;
    for (size_t ci = 0; ci < CWMC_SLAB_CLASS_COUNT; ci++) {
        for (CwmcBlockHdr_t* b = g_mc.classes[ci]; b; b = b->next) {
            if (!cb(ci, b->used, b->capacity, b)) return;
        }
    }
}

bool cwmc_gc_block_info(void* block, size_t* out_class_id,
                        size_t* out_used, size_t* out_capacity) {
    if (!block || !g_mc.inited) return false;
    const CwmcBlockHdr_t* b = (const CwmcBlockHdr_t*)block;
    if (b->magic != CWMC_BLOCK_MAGIC) return false;
    if (out_class_id) *out_class_id = (size_t)b->class_id;
    if (out_used) *out_used = b->used;
    if (out_capacity) *out_capacity = b->capacity;
    return true;
}

size_t cwmc_gc_collect_empty_blocks(void** out_blocks, size_t cap) {
    if (!out_blocks || cap == 0 || !g_mc.inited) return 0;
    size_t n = 0;
    for (size_t ci = 0; ci < CWMC_SLAB_CLASS_COUNT; ci++) {
        for (CwmcBlockHdr_t* b = g_mc.classes[ci]; b; b = b->next) {
            if (b->used != 0) continue;
            if (n < cap) out_blocks[n++] = b;
        }
    }
    return n;
}

bool cwmc_gc_release_block(void* block) {
    if (!block || !g_mc.inited) return false;
    CwmcBlockHdr_t* b = (CwmcBlockHdr_t*)block;
    if (b->magic != CWMC_BLOCK_MAGIC) return false;
    if (b->used != 0) return false;

    /* 从 classes[] 摘链 */
    CwmcBlockHdr_t** link = &g_mc.classes[b->class_id];
    while (*link && *link != b) link = &(*link)->next;
    if (!*link) return false;
    *link = b->next;

    cwmc_free_block(b); /* unmap + 页条目剔除 + blocks-- + mapped-- */
    return true;
}

bool cwmc_gc_release_large(void* payload) {
    if (!payload || !g_mc.inited) return false;
    if (!cwmc_gc_is_large(payload)) return false;

    const CwmcSlotHdr_t* view =
        (const CwmcSlotHdr_t*)((const char*)payload - CWMC_SLOT_HDR_SIZE);
    const uint64_t size = view->u.used.size;

    CwmcDedicatedHdr_t* hdr =
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
    cwmc_page_del((uintptr_t)hdr, (uintptr_t)hdr + total, false);
    cwmc_os_free(hdr, total);
    g_mc.blocks--;
    if (g_mc.mapped_bytes >= total) {
        g_mc.mapped_bytes -= total;
    } else {
        g_mc.mapped_bytes = 0;
    }
    return true;
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
