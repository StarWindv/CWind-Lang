/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: rt-src/rt/cwind_gc.c
 */

#include "../include/gc/cwind_gc.h"

#include "../include/memory/cwind_memcenter.h"
#include "../include/object/cwind_object.h"
#include "../include/object/cwind_container.h"
#include "../include/rt/cwind_safecrt.h"

#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
    #define WIN32_LEAN_AND_MEAN
    #include <windows.h>
#else
    #include <time.h>
#endif

/*
 * 阶段 1 分代标记-清扫 (todo-35 → todo-149):
 *  - 颜色/age 存 memcenter 槽头 reserved (元数据分区, 值不携带);
 *  - 根 = 全局根注册表 + 保守栈扫描 ([SP, 栈底) 区间 word 扫描);
 *  - 标记 = grey 队列; 处理灰槽时按描述符精确走 walker, 无 walker
 *    保守扫载荷字节 (只泄漏不悬垂);
 *  - 分代: 槽头 age 位记存活轮数 (>= CWGC_OLD_AGE 晋升老代);
 *    minor 轮只标年轻代 (老代槽 sweep 时直接视为存活), major 轮
 *    全堆标记。保守面 (栈/全局根) 扫描喂进来的老代地址在 minor 轮
 *    记入 remembered (touched), 供下一轮 minor 把老代里指向年轻代
 *    的引用重新挂根 —— 保守 remember 会误保留 (设计内, todo-155
 *    精确栈图后收敛);
 *  - sweep = 两阶段遍历, white 归还; 之后按水位把连续 N 轮全空的
 *    slab 块 unmap, 大对象 victim 直接 unmap (OS 归还, 149);
 *  - 触发 = cwmc_alloc 分配字节阈值驱动 cwgc_step (Lua 式增量)。
 */

/* ---- 全局状态 ---- */

typedef struct CWGCRoot {
    const void* addr;
    size_t bytes;
} CWGCRoot_t;

typedef struct CWGCCtx {
    bool inited;
    bool enabled;
    bool conservative; /* CWGC_CONSERVATIVE=1: 忽略描述符, 全保守扫描 */
    bool verbose;
    CWGCState_t state;
    CWGCMode_t last_mode;    /* 最近一轮形态 (cwgc_last_mode) */
    size_t minor_run;        /* 连续 minor 轮数 (AUTO 调度: 7:1) */
    bool major_scan;         /* 本轮是否全堆扫描 (major = true) */
    CWGCMode_t pending_mode; /* 下一轮强制形态 (CWGC_MODE_AUTO=调度) */
    size_t step_bytes;
    size_t collected_bytes; /* 当前轮已驱动但未处理的分配字节 */

    CWGCRoot_t* roots;
    size_t root_count;
    size_t root_cap;

    /* grey 队列: 载荷地址数组 */
    void** grey;
    size_t grey_count;
    size_t grey_cap;

    /* remembered 集合 (149): minor 轮保守根扫描命中的老代槽,
     * 下一轮 minor 作为根重新入队 (放行老->新引用) */
    void** remset;
    size_t rem_count;
    size_t rem_cap;

    /* sweep 游标 (阶段 0 sweep 原子完成, 游标留作阶段 2 增量扩展) */
    size_t swept_slots;
    size_t reclaimed_bytes;

    /* 主线程栈底 (cwgc_init 调用点的高地址方向) */
    void* stack_bottom;

    /* OS 归还 (149): 连续全空轮数水位 + 每类保留块数 */
    size_t release_vacant; /* 0 = 关闭 (默认); >=2 = 连续 N 轮全空才还 */
    size_t release_keep;   /* 每类保留的空块数 (默认 1) */
    size_t os_released_bytes;
    size_t blocks_released;
    size_t large_released;

    CWGCStats_t stats;
    jmp_buf regs; /* FINISH 时泼寄存器用 */
    size_t last_pause_ns; /* 最近一轮原子收尾耗时 (cwgc_pause_ns) */
} CWGCCtx_t;

static CWGCCtx_t g_gc;

#define CWGC_DEFAULT_STEP_BYTES ((size_t)64 * 1024)

/* 单调时钟纳秒 (原子收尾计时) */
static size_t cwgc_now_ns(void) {
#ifdef _WIN32
    static LARGE_INTEGER freq = { { 0 } };
    LARGE_INTEGER now;
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&now);
    return (size_t)(now.QuadPart * (uint64_t)1000000000
                    / (uint64_t)freq.QuadPart);
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (size_t)ts.tv_sec * (size_t)1000000000 + (size_t)ts.tv_nsec;
#endif
}

/* ---- 工具 ---- */

static void* cwgc_xgrow(void* p, size_t n, size_t esz) {
    void* np = realloc(p, n * esz);
    return np;
}

/* 日志写文件 (CWGC_VERBOSE=1): 崩溃时 stderr 缓冲会丢, 文件保真 */
static FILE* cwgc_log_file(void) {
    return cw_fopen("cwind_gc.log", "a");
}

static void cwgc_log(const char* fmt, ...) {
    if (!g_gc.verbose) return;
    FILE* f = cwgc_log_file();
    if (!f) return;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

/* ---- 颜色操作 (槽头 reserved, 元数据分区) ---- */

static CWGCColor_t cwgc_color_of(uint64_t* meta) {
    return (CWGCColor_t)(*meta & CWGC_COLOR_MASK);
}

static void cwgc_set_color(uint64_t* meta, CWGCColor_t color) {
    *meta = (*meta & ~CWGC_COLOR_MASK) | (uint64_t)color;
}

/* age (149): 低 4 位以上、desc 以下, 步长 1 轮, 饱和递增 */
static size_t cwgc_age_of(uint64_t* meta) {
    return (size_t)((*meta & CWGC_AGE_MASK) >> CWGC_AGE_SHIFT);
}

static void cwgc_age_inc(uint64_t* meta) {
    uint64_t age = (*meta & CWGC_AGE_MASK) >> CWGC_AGE_SHIFT;
    if (age < (CWGC_AGE_MASK >> CWGC_AGE_SHIFT)) age++;
    *meta = (*meta & ~CWGC_AGE_MASK) | (age << CWGC_AGE_SHIFT);
}

static bool cwgc_is_old(uint64_t* meta) {
    return cwgc_age_of(meta) >= CWGC_OLD_AGE;
}

/* ---- 入队 ---- */

static bool cwgc_grey_push(void* payload) {
    if (g_gc.grey_count == g_gc.grey_cap) {
        const size_t nc = g_gc.grey_cap ? g_gc.grey_cap * 2 : 256;
        void** ng = (void**)cwgc_xgrow(g_gc.grey, nc, sizeof(void*));
        if (!ng) return false;
        g_gc.grey = ng;
        g_gc.grey_cap = nc;
    }
    g_gc.grey[g_gc.grey_count++] = payload;
    return true;
}

static void cwgc_mark_maybe(uintptr_t word);

/* remembered 集合 (149): minor 轮里保守根命中的老代槽 */
static bool cwgc_rem_push(void* payload) {
    if (g_gc.rem_count == g_gc.rem_cap) {
        const size_t nc = g_gc.rem_cap ? g_gc.rem_cap * 2 : 128;
        void** nr = (void**)cwgc_xgrow(g_gc.remset, nc, sizeof(void*));
        if (!nr) return false;
        g_gc.remset = nr;
        g_gc.rem_cap = nc;
    }
    g_gc.remset[g_gc.rem_count++] = payload;
    return true;
}

/* 写屏障 (阶段 0 唯一屏障): MARK 期间被写入堆槽的值要保活。
 * 容器元素写点 (cwvec/cwmap/cwset 的写路径) 调用;
 * 栈/静态上的字段写由 FINISH 的根重扫覆盖, 无需屏障。 */
void cwgc_barrier(const void* value_addr) {
    if (g_gc.state != CWGC_MARK) return;
    cwgc_mark_maybe((uintptr_t)value_addr);
}

/* ---- 横向精确化 (B 组) ---- */

typedef void (*cwgc_walk_fn)(void* base, unsigned size);

typedef struct CWGCWalkEntry {
    uint32_t desc;
    cwgc_walk_fn fn;
} CWGCWalkEntry_t;

static CWGCWalkEntry_t g_walks[16];
static size_t g_walk_count;

void cwgc_set_desc(const void* payload, uint32_t desc) {
    uint64_t* meta = cwmc_gc_meta_of(payload);
    if (!meta) return;
    *meta = (*meta & ~CWGC_DESC_MASK)
        | ((uint64_t)desc << CWGC_DESC_SHIFT);
}

static cwgc_walk_fn cwgc_walk_of(uint32_t desc) {
    if (g_gc.conservative) return NULL;
    for (size_t i = 0; i < g_walk_count; i++) {
        if (g_walks[i].desc == desc) return g_walks[i].fn;
    }
    return NULL;
}

void cwgc_desc_register(uint32_t desc, cwgc_walk_fn fn) {
    if (!fn || g_walk_count >= sizeof(g_walks) / sizeof(g_walks[0])) {
        return;
    }
    g_walks[g_walk_count].desc = desc;
    g_walks[g_walk_count].fn = fn;
    g_walk_count++;
}

void cwgc_mark_ref(const void* addr) {
    cwgc_mark_maybe((uintptr_t)addr);
}

void cwgc_mark_obj(const void* addr) {
    uint64_t* meta = cwmc_gc_meta_of(addr);
    if (!meta) return;
    if (cwgc_color_of(meta) == CWGC_WHITE) {
        cwgc_set_color(meta, CWGC_BLACK);
        g_gc.stats.nodes_marked++;
    }
}

/* 保守染白为灰: 地址命中托管堆且为白 -> 灰并入队。
 * minor 轮 (149): 命中的是老代槽则不直接标 —— 老代默认视为存活,
 * 但该命中说明保守根上可能挂着「老代里引用年轻代」的路径, 记入
 * remembered, 供下一轮 minor 把老代重新作为根入队 (touched 位即
 * 此语义的位标记, 保守 remember 会误保留, 设计内)。 */
static void cwgc_mark_maybe(uintptr_t word) {
    void* p = (void*)(uintptr_t)word;
    uint64_t* meta = cwmc_gc_meta_of(p);
    if (!meta) return;
    /* 命中的是槽头而非载荷? meta_of 只对载荷地址有效, 这里 word 来自
     * 根/载荷扫描, 指向载荷才合法; 槽头地址不产生 meta (view 检查
     * 以载荷为准), 无需额外区分。 */
    if (cwgc_color_of(meta) != CWGC_WHITE) {
        return; /* 已灰/已黑: 共享引用, 非误判 */
    }
    if (!g_gc.major_scan && cwgc_is_old(meta)) {
        /* minor 轮: 老代槽保持白色 (sweep 跳过 = 视为存活), 记入
         * remembered (touched) 供下一轮 minor 重新作根。但本轮仍要
         * 灰队遍历它 —— 否则「老代引用年轻代」的新对象在本轮不可见,
         * 年轻代新槽会被误回收 (声音性关键, gc_edge 实测 AV)。 */
        *meta |= CWGC_TOUCHED_BIT;
        cwgc_rem_push(p);
        cwgc_set_color(meta, CWGC_GREY);
        if (!cwgc_grey_push(p)) {
            cwgc_set_color(meta, CWGC_WHITE);
            return;
        }
        g_gc.stats.nodes_marked++;
        return;
    }
    cwgc_set_color(meta, CWGC_GREY);
    if (!cwgc_grey_push(p)) {
        /* 入队失败 (OOM): 退回白色, 下轮重试 (安全方向: 可能提前回收,
         * 但 OOM 下进程本已接近死亡; 阶段 0 记日志即可) */
        cwgc_set_color(meta, CWGC_WHITE);
        return;
    }
    g_gc.stats.nodes_marked++;
}

/* 分配屏障 (incremental 声音性关键): MARK 期间的新槽立即染灰入队,
 * 使其在本轮被扫描 —— 否则 sweep 会把 mark 期间分配的槽误回收
 * (实测 arena 段在 put 循环中被释放后崩溃)。
 * minor 轮: 新槽属于年轻代, 同样入队 (minor 扫描面包含年轻代全部)。 */
void cwgc_alloc_barrier(void* payload) {
    if (g_gc.state != CWGC_MARK) return;
    uint64_t* meta = cwmc_gc_meta_of(payload);
    if (!meta) return;
    if (cwgc_color_of(meta) != CWGC_WHITE) return;
    cwgc_set_color(meta, CWGC_GREY);
    if (cwgc_grey_push(payload)) g_gc.stats.nodes_marked++;
    else cwgc_set_color(meta, CWGC_WHITE);
}

/* 保守扫描一段内存的对齐 word */
static void cwgc_scan_range(const void* addr, size_t bytes) {
    if (!addr || bytes < sizeof(uintptr_t)) return;
    uintptr_t base = ((uintptr_t)addr + sizeof(uintptr_t) - 1)
        & ~(uintptr_t)(sizeof(uintptr_t) - 1);
    uintptr_t end = (uintptr_t)addr + bytes;
    for (; base + sizeof(uintptr_t) <= end; base += sizeof(uintptr_t)) {
        g_gc.stats.words_scanned++;
        cwgc_mark_maybe(*(const uintptr_t*)base);
    }
}

/* grey 队列处理: 每节点扫描其载荷 */
static void cwgc_grey_process(size_t budget) {
    while (g_gc.grey_count > 0 && budget > 0) {
        void* payload = g_gc.grey[--g_gc.grey_count];
        uint64_t* meta = cwmc_gc_meta_of(payload);
        if (meta && cwgc_color_of(meta) == CWGC_GREY) {
            cwgc_set_color(meta, CWGC_BLACK);
            const uint32_t desc =
                (uint32_t)((*meta & CWGC_DESC_MASK) >> CWGC_DESC_SHIFT);
            const cwgc_walk_fn fn = cwgc_walk_of(desc);
            if (g_gc.verbose) {
                FILE* tf = cwgc_log_file();
                if (tf) {
                    fprintf(tf,
                            "[grey] node=%p desc=%u fn=%d size=%zu\n",
                            payload, desc, fn != NULL,
                            cwmc_usable_size(payload));
                    fclose(tf);
                }
            }
            if (fn) {
                /* 精确: 描述符 walker 只遍历真实内部引用 */
                fn(payload, (unsigned)cwmc_usable_size(payload));
            } else {
                /* 保守兜底: 扫描全部载荷字节 */
                cwgc_scan_range(payload, cwmc_usable_size(payload));
            }
        }
        budget--;
    }
}

/* ---- 根集合 ---- */

bool cwgc_global_register(const void* addr, size_t bytes) {
    if (!addr || bytes == 0) return false;
    if (g_gc.root_count == g_gc.root_cap) {
        const size_t nc = g_gc.root_cap ? g_gc.root_cap * 2 : 32;
        CWGCRoot_t* nr = (CWGCRoot_t*)cwgc_xgrow(g_gc.roots, nc,
                                                 sizeof(CWGCRoot_t));
        if (!nr) return false;
        g_gc.roots = nr;
        g_gc.root_cap = nc;
    }
    g_gc.roots[g_gc.root_count].addr = addr;
    g_gc.roots[g_gc.root_count].bytes = bytes;
    g_gc.root_count++;
    return true;
}

bool cwgc_global_unregister(const void* addr) {
    for (size_t i = 0; i < g_gc.root_count; i++) {
        if (g_gc.roots[i].addr == addr) {
            memmove(&g_gc.roots[i], &g_gc.roots[i + 1],
                    (g_gc.root_count - i - 1) * sizeof(CWGCRoot_t));
            g_gc.root_count--;
            return true;
        }
    }
    return false;
}

/* 保守栈扫描: [当前 SP, 栈底) */
static void cwgc_scan_stack(void) {
    if (!g_gc.stack_bottom) return;
    int probe = 0;
    void* sp = (void*)&probe;
    if (sp >= g_gc.stack_bottom) return; /* 异常: 非主线程或栈方向不符 */
    cwgc_scan_range(sp, (uintptr_t)g_gc.stack_bottom - (uintptr_t)sp);
    /* 泼寄存器: setjmp 保存的寄存器现场在 jmp_buf 里, 一起扫 */
    cwgc_scan_range(&g_gc.regs, sizeof(g_gc.regs));
}

/* 全部根入队 (MARK 起点或 FINISH 兜底)。
 * minor 轮: 老代槽由 mark_maybe 转 remembered; 根若是堆槽 (arena/
 * 帧) 无论代际直接染黑 (进程期存活, F3)。 */
static void cwgc_mark_roots(void) {
    cwgc_scan_stack();
    for (size_t i = 0; i < g_gc.root_count; i++) {
        /* 根若本身是托管堆槽 (arena 段 / rt 帧), 先染黑 ——
         * 否则 sweep 视其为白色误回收 (实测 arena 段被释放后崩溃) */
        uint64_t* meta = cwmc_gc_meta_of(g_gc.roots[i].addr);
        if (meta && cwgc_color_of(meta) == CWGC_WHITE) {
            cwgc_set_color(meta, CWGC_BLACK);
            g_gc.stats.nodes_marked++;
        }
        cwgc_scan_range(g_gc.roots[i].addr, g_gc.roots[i].bytes);
    }
}

/* ---- sweep ----
 * 两阶段: 第一遍只收集白色槽清单 (不改结构), 迭代完成后统一释放 ——
 * 消除遍历与释放交错的任何隐患。
 * 分代 (149): minor 轮里老代槽保持白但不回收 (视作存活); 存活槽在
 * major 轮 age++ (满 CWGC_OLD_AGE 晋升老代)。
 * OS 归还 (149): 白色大对象单列, 统一走 cwmc_gc_release_large
 * (解链 + unmap); 空块水位裁定在 sweep 之后。 */

typedef struct CWGCSweepCtx {
    void** victims;          /* 待释放 slab 槽 */
    size_t vn;
    size_t vc;
    void** large;            /* 待释放大对象 (sweep 恒黑者除外) */
    size_t ln;
    size_t lc;
    size_t freed_slots;
    size_t freed_bytes;
    size_t live_slots;
    size_t live_bytes;
    bool major;              /* major 轮才累 age */
} CWGCSweepCtx_t;

/* 判定载荷属于大对象 (无回指块指针) —— meta 探测已保证 magic 合法 */
static bool cwgc_payload_is_large(void* payload) {
    const uint64_t* meta = cwmc_gc_meta_of(payload);
    if (!meta) return false;
    /* 槽头布局: reserved 前一个 8B 是 block 指针; 大对象恒 0。
     * memcenter 提供判定, 避免在 GC 里复述槽头布局。 */
    return cwmc_gc_is_large(payload);
}

static bool cwgc_sweep_cb(void* payload, size_t size, uint64_t* meta,
                          void* ud) {
    CWGCSweepCtx_t* c = (CWGCSweepCtx_t*)ud;
    const CWGCColor_t color = cwgc_color_of(meta);
    if (color == CWGC_WHITE) {
        if (!c->major && cwgc_is_old(meta)) {
            /* minor 轮: 老代保持白但跳过回收 (视为存活) */
            c->live_slots++;
            c->live_bytes += size;
            return true;
        }
        if (cwgc_payload_is_large(payload)) {
            /* 大对象: 单列, 释放走解链 unmap (arena 段是注册根恒黑,
             * 不会进到这里) */
            if (c->ln == c->lc) {
                const size_t nc = c->lc ? c->lc * 2 : 64;
                void** nl = (void**)cwgc_xgrow(c->large, nc, sizeof(void*));
                if (!nl) return true;
                c->large = nl;
                c->lc = nc;
            }
            c->large[c->ln++] = payload;
            return true;
        }
        if (c->vn == c->vc) {
            const size_t nc = c->vc ? c->vc * 2 : 256;
            void** nv = (void**)cwgc_xgrow(c->victims, nc, sizeof(void*));
            if (!nv) return true; /* 记不满: 本轮少收, 下轮再收 */
            c->victims = nv;
            c->vc = nc;
        }
        c->victims[c->vn++] = payload;
        return true;
    }
    /* 存活: 复位为白 (下一轮); major 轮累 age (晋升判定) */
    c->live_slots++;
    c->live_bytes += size;
    if (c->major) cwgc_age_inc(meta);
    cwgc_set_color(meta, CWGC_WHITE);
    return true;
}

/* 空块水位裁定 (149): 连续 N 轮全空的类, 归还其保留配额之外的块。 */

#define CWGC_MAX_CLASSES 32

static size_t g_vacant_rounds[CWGC_MAX_CLASSES];

/* 块遍历回调: 记录各类是否出现过非空块 */
static bool cwgc_block_probe_cb(size_t class_id, size_t used,
                                size_t capacity, void* ud) {
    bool* seen = (bool*)ud;
    if (class_id < CWGC_MAX_CLASSES && used > 0) seen[class_id] = true;
    (void)capacity;
    return true;
}

/* 归还空块: release_block 会改 classes 链, 不能与遍历交错 ——
 * memcenter 提供一次性的「可还块清单」收集 (collect_empty, 链头
 * = 最新块在前), 这里按类保留 keep 个, 其余在水位达标后归还。 */
static void cwgc_release_empty_blocks(void) {
    if (g_gc.release_vacant < 2) return; /* 关闭 */
    if (cw_env_has("CWGC_DBG_RELEASE")) {
        fprintf(stderr, "[rel] vacant=%zu r0=%zu r1=%zu r2=%zu r3=%zu\n",
                g_gc.release_vacant, g_vacant_rounds[0], g_vacant_rounds[1],
                g_vacant_rounds[2], g_vacant_rounds[3]);
    }
    bool seen[CWGC_MAX_CLASSES];
    memset(seen, 0, sizeof(seen));
    cwmc_gc_iter_blocks(cwgc_block_probe_cb, seen);

    void* cand[CWGC_MAX_CLASSES * 4];
    size_t nc = cwmc_gc_collect_empty_blocks(cand,
                                             sizeof(cand) / sizeof(cand[0]));
    if (nc == 0) return;
    if (nc == sizeof(cand) / sizeof(cand[0])) nc--; /* 截断标记, 少还一轮 */

    /* 水位按类累计: 本轮该类无任何在用槽 -> 轮数 +1, 否则清零。
     * 达到阈值后归还该类「保留配额之外」的全部空块 (链头 = 最新块
     * 先保留)。 */
    size_t per_class_empty[CWGC_MAX_CLASSES];
    memset(per_class_empty, 0, sizeof(per_class_empty));
    for (size_t i = 0; i < nc; i++) {
        size_t cls = 0, used = 0, cap = 0;
        const bool ok = cwmc_gc_block_info(cand[i], &cls, &used, &cap);
        if (cw_env_has("CWGC_DBG_RELEASE")) {
            fprintf(stderr, "[rel3] i=%zu ok=%d cls=%zu used=%zu\n",
                    i, (int)ok, cls, used);
        }
        if (!ok) continue;
        if (used != 0 || cls >= CWGC_MAX_CLASSES) continue;
        per_class_empty[cls]++;
    }
    if (cw_env_has("CWGC_DBG_RELEASE")) {
        fprintf(stderr, "[rel2] nc=%zu seen1=%d seen2=%d empty1=%zu empty2=%zu\n",
                nc, (int)seen[1], (int)seen[2], per_class_empty[1],
                per_class_empty[2]);
    }
    for (size_t cls = 0; cls < CWGC_MAX_CLASSES; cls++) {
        if (seen[cls]) {
            g_vacant_rounds[cls] = 0; /* 本轮有分配活动: 清零 */
            continue;
        }
        if (per_class_empty[cls] == 0) continue;
        g_vacant_rounds[cls]++;
        if (g_vacant_rounds[cls] < g_gc.release_vacant) continue;
        /* 达标: 还掉该类空块, 保留 release_keep 个 (链头最新) */
        size_t kept = 0;
        for (size_t i = 0; i < nc; i++) {
            size_t c2 = 0, u2 = 0, cp2 = 0;
            if (!cwmc_gc_block_info(cand[i], &c2, &u2, &cp2)) continue;
            if (c2 != cls || u2 != 0) continue;
            if (kept < g_gc.release_keep) { kept++; continue; }
            if (cwmc_gc_release_block(cand[i])) {
                g_gc.blocks_released++;
                g_gc.os_released_bytes += CWMC_BLOCK_SIZE;
            }
        }
        g_vacant_rounds[cls] = 0;
    }
}

static size_t cwgc_sweep_all(void) {
    CWGCSweepCtx_t c;
    memset(&c, 0, sizeof(c));
    c.major = g_gc.major_scan;
    cwmc_gc_iter_used(cwgc_sweep_cb, &c);
    size_t freed_bytes = 0;
    for (size_t i = 0; i < c.vn; i++) {
        const size_t sz = cwmc_usable_size(c.victims[i]);
        if (cw_env_has("CWGC_TRACE")) {
            FILE* tf = cwgc_log_file();
            if (tf) {
                fprintf(tf, "[gc-sweep] free slot %p size=%zu\n",
                        c.victims[i], sz);
                fclose(tf);
            }
        }
        cwmc_gc_release(c.victims[i]);
        c.freed_slots++;
        freed_bytes += sz;
    }
    for (size_t i = 0; i < c.ln; i++) {
        const size_t sz = cwmc_usable_size(c.large[i]);
        if (cwmc_gc_release_large(c.large[i])) {
            c.freed_slots++;
            freed_bytes += sz;
            g_gc.large_released++;
            g_gc.os_released_bytes += sz;
        }
        /* 释放失败 (非大对象/不在链): 留给下一轮 */
    }
    free(c.large);
    free(c.victims);
    /* OS 归还: 空块水位裁定 (在槽释放全部完成后做, 避免链操作交错) */
    cwgc_release_empty_blocks();
    g_gc.stats.slots_swept += c.freed_slots;
    g_gc.stats.bytes_reclaimed += freed_bytes;
    g_gc.stats.live_slots = c.live_slots;
    g_gc.stats.live_bytes = c.live_bytes;
    return freed_bytes;
}

/* ---- 状态机 ---- */

static void cwgc_begin_cycle(void) {
    g_gc.state = CWGC_MARK;
    /* 分代形态 (149): AUTO 连续 7 轮 minor 后插 1 轮 major */
    if (g_gc.pending_mode == CWGC_MODE_MINOR) {
        g_gc.major_scan = false;
    } else if (g_gc.pending_mode == CWGC_MODE_MAJOR) {
        g_gc.major_scan = true;
    } else {
        g_gc.major_scan = (g_gc.minor_run >= 7);
    }
    /* minor 轮把上一轮 remembered 的老代槽重新作为根 (老->新引用) */
    if (!g_gc.major_scan) {
        for (size_t i = 0; i < g_gc.rem_count; i++) {
            uint64_t* m = cwmc_gc_meta_of(g_gc.remset[i]);
            if (!m) continue;
            *m &= ~CWGC_TOUCHED_BIT;
            if (cwgc_color_of(m) != CWGC_WHITE) continue;
            cwgc_set_color(m, CWGC_GREY);
            if (cwgc_grey_push(g_gc.remset[i])) g_gc.stats.nodes_marked++;
            else cwgc_set_color(m, CWGC_WHITE);
        }
    }
    g_gc.rem_count = 0; /* 保守根扫描会重新收集命中 */
    cwgc_mark_roots();
    if (!g_gc.major_scan) g_gc.minor_run++;
    else g_gc.minor_run = 0;
    g_gc.last_mode = g_gc.major_scan ? CWGC_MODE_MAJOR : CWGC_MODE_MINOR;
    if (g_gc.major_scan) g_gc.stats.major_cycles++;
    else g_gc.stats.minor_cycles++;
    cwgc_log("[gc] cycle begin, mode=%s grey=%zu",
             g_gc.major_scan ? "major" : "minor", g_gc.grey_count);
    if (g_gc.verbose) {
        fprintf(stderr, "[gc-begin] mode=%d marked=%zu grey=%zu\n",
                (int)g_gc.last_mode, g_gc.stats.nodes_marked,
                g_gc.grey_count);
    }
}

static void cwgc_finish_cycle(void) {
    const size_t t0 = cwgc_now_ns();
    /* 原子收尾: 根兜底重扫 + 清空队列 */
    cwgc_mark_roots();
    cwgc_grey_process(SIZE_MAX);
    g_gc.state = CWGC_SWEEP;
    cwgc_sweep_all();
    g_gc.stats.cycles++;
    g_gc.state = CWGC_IDLE;
    g_gc.pending_mode = CWGC_MODE_AUTO;
    g_gc.last_pause_ns = cwgc_now_ns() - t0;
    if (g_gc.verbose) {
        fprintf(stderr, "[gc-done] mode=%d reclaimed=%zu live=%zu slots=%zu\n",
                (int)g_gc.last_mode, g_gc.stats.bytes_reclaimed,
                g_gc.stats.live_bytes, g_gc.stats.live_slots);
    }
    cwgc_log("[gc] cycle done, mode=%d reclaimed=%zu live=%zu",
             (int)g_gc.last_mode, g_gc.stats.bytes_reclaimed,
             g_gc.stats.live_bytes);
}

size_t cwgc_collect_mode(CWGCMode_t mode) {
    if (!g_gc.inited || !g_gc.enabled) return 0;
    const size_t before = g_gc.stats.bytes_reclaimed;
    if (g_gc.state == CWGC_IDLE) {
        cwmc_gc_take_alloc_bytes(cwmc_gc_alloc_bytes());
        g_gc.pending_mode = mode;
        cwgc_begin_cycle();
    }
    cwgc_finish_cycle();
    return g_gc.stats.bytes_reclaimed - before;
}

size_t cwgc_collect(void) {
    return cwgc_collect_mode(CWGC_MODE_MAJOR); /* 全量轮 = major */
}
bool cwgc_step(void) {
    if (!g_gc.inited || !g_gc.enabled) return false;
    g_gc.stats.steps++;
    if (g_gc.verbose) {
        fprintf(stderr, "[gc-step] state=%d pending=%zu grey=%zu\n",
                (int)g_gc.state, cwmc_gc_alloc_bytes(), g_gc.grey_count);
    }

    const size_t pending = cwmc_gc_alloc_bytes();
    if (g_gc.state == CWGC_IDLE) {
        if (pending < g_gc.step_bytes) return false;
        cwmc_gc_take_alloc_bytes(pending);
        cwgc_begin_cycle();
    }
    if (g_gc.state == CWGC_MARK) {
        cwgc_grey_process(g_gc.step_bytes / 8);
        if (g_gc.grey_count == 0) {
            cwgc_finish_cycle();
        }
    }
    return true;
}


/* ---- 生命周期 ---- */

static size_t cwgc_env_size(const char* name, size_t dflt) {
    char v[64];
    if (!cw_env_get(name, v, sizeof(v)) || !*v) return dflt;
    const size_t n = (size_t)strtoull(v, NULL, 0);
    return n ? n : dflt;
}

void cwgc_init(void) {
    if (g_gc.inited) return;
    memset(&g_gc, 0, sizeof(g_gc));
    cwmc_init();

    char dis[16];
    const bool has_dis = cw_env_get("CWGC_DISABLE", dis, sizeof(dis));
    g_gc.enabled = !(has_dis && *dis && strcmp(dis, "0") != 0);
    g_gc.verbose = cw_env_has("CWGC_VERBOSE");
    g_gc.conservative = cw_env_has("CWGC_CONSERVATIVE");
    g_gc.step_bytes = cwgc_env_size("CWGC_STEP_BYTES",
                                    CWGC_DEFAULT_STEP_BYTES);
    /* 空块归还水位 (149): 默认关闭; CWGC_RELEASE_VACANT>=2 开启 */
    g_gc.release_vacant = cwgc_env_size("CWGC_RELEASE_VACANT", 0);
    g_gc.release_keep = 1;

    /* 主线程栈底: init 调用点的栈地址 (栈向下生长) */
    int probe = 0;
    g_gc.stack_bottom = (void*)&probe;

    g_gc.inited = true;

    /* 容器精确 walker 注册 (B 组; 实现见 cwind_container.c) */
    cwgc_desc_register(CWGC_DESC_VECTOR_DATA, cwgc_walk_vector_data);
    cwgc_desc_register(CWGC_DESC_MAP_DATA, cwgc_walk_map_data);
    cwgc_desc_register(CWGC_DESC_MAP_NODE, cwgc_walk_map_node);
    cwgc_desc_register(CWGC_DESC_SET_DATA, cwgc_walk_set_data);
    cwgc_desc_register(CWGC_DESC_SET_NODE, cwgc_walk_set_node);
    cwgc_desc_register(CWGC_DESC_TUPLE_DATA, cwgc_walk_tuple_data);

    cwgc_log("[gc] init, step=%zu enabled=%d", g_gc.step_bytes,
             g_gc.enabled);
}

void cwgc_shutdown(void) {
    if (!g_gc.inited) return;
    free(g_gc.grey);
    free(g_gc.roots);
    memset(&g_gc, 0, sizeof(g_gc));
}

/* ---- 查询 ---- */

CWGCState_t cwgc_state(void) {
    return g_gc.state;
}

bool cwgc_enabled(void) {
    return g_gc.inited && g_gc.enabled;
}

CWGCMode_t cwgc_last_mode(void) {
    return g_gc.last_mode;
}

void cwgc_stats(CWGCStats_t* out) {
    if (!out) return;
    out->cycles = g_gc.stats.cycles;
    out->minor_cycles = g_gc.stats.minor_cycles;
    out->major_cycles = g_gc.stats.major_cycles;
    out->steps = g_gc.stats.steps;
    out->nodes_marked = g_gc.stats.nodes_marked;
    out->slots_swept = g_gc.stats.slots_swept;
    out->bytes_reclaimed = g_gc.stats.bytes_reclaimed;
    out->live_slots = g_gc.stats.live_slots;
    out->live_bytes = g_gc.stats.live_bytes;
    out->words_scanned = g_gc.stats.words_scanned;
    out->false_marks = g_gc.stats.false_marks;
    out->blocks_released = g_gc.blocks_released;
    out->large_released = g_gc.large_released;
    out->os_released_bytes = g_gc.os_released_bytes;
}

void cwgc_set_step_bytes(size_t bytes) {
    g_gc.step_bytes = bytes ? bytes : CWGC_DEFAULT_STEP_BYTES;
}

size_t cwgc_step_bytes(void) {
    return g_gc.step_bytes;
}

/* ---- 控制/观测 (builtins::gc_* 投影) ---- */

void cwgc_set_enabled(bool enabled) {
    if (!g_gc.inited) return;
    g_gc.enabled = enabled;
    cwgc_log("[gc] set_enabled=%d", (int)enabled);
}

size_t cwgc_alloc_bytes(void) {
    return cwmc_alloc_total_bytes();
}

size_t cwgc_live_bytes(void) {
    return g_gc.stats.live_bytes;
}

size_t cwgc_pause_ns(void) {
    return g_gc.last_pause_ns;
}

/* OS 归还观测/控制 (149) */

size_t cwgc_mapped_bytes(void) {
    CWMemCenterStats_t ms;
    if (!cwmc_stats(&ms)) return 0;
    return ms.mapped_bytes;
}

size_t cwgc_os_released_bytes(void) {
    return g_gc.os_released_bytes;
}

void cwgc_set_release_vacant(size_t rounds) {
    g_gc.release_vacant = rounds;
    /* 每类保留块数固定 1: 常用做法是「保留最近使用的若干块」,
     * classes 链头即最新块, collect_empty_blocks 已按此排序 */
    g_gc.release_keep = 1;
}

size_t cwgc_release_vacant(void) {
    return g_gc.release_vacant;
}
