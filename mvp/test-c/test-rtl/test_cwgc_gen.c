/**
 * 独立测试: GC 阶段 1 分代调度 + OS 归还 (todo-149)
 *
 * 验收 (todo-ordered.2026-08-30 GC 专项):
 *   - 分代: minor 轮跳过老代回收、老->新引用经 remember 重扫保活;
 *     major 轮全堆回收 + 存活槽晋升; AUTO 调度 7:1;
 *   - OS 归还: 大对象 sweep 未标记即 unmap; 空块按「连续 N 轮全空 +
 *     每类保留 1 块」水位归还, mapped_bytes 观测回落;
 *   - 混合负载: 分代开关下随便分配怎么丢, 不崩。
 */

#include "../../rt-src/include/rt/cwind_builtin.h"
#include "../../rt-src/include/gc/cwind_gc.h"
#include "../../rt-src/include/object/cwind_container.h"
#include "../../rt-src/include/memory/cwind_memcenter.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

static void scrub(void) {
    volatile char pad[64 * 1024];
    memset((void*)pad, 0, sizeof(pad));
}

/* 标量 cell: 值拷进 arena 单元再 wrap (与 codegen cg_value_cell 同构) */
static void wrap_i16(CWValue_t* out, int v) {
    int16_t* unit = (int16_t*)cwrt_arena_alloc(sizeof(int16_t));
    *unit = (int16_t)v;
    cwval_wrap(out, unit, sizeof(int16_t));
}

static size_t live_allocs(void) {
    CWMemCenterStats_t ms;
    cwmc_stats(&ms);
    return ms.active_allocs;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWGC stage-1 generational tests:\n\n");

    cwgc_init();

    printf(" - 晋升: 存活槽在 major 轮 age++, 满 3 轮晋升老代\n");
    CWGCStats_t st;
    static CWValue_t survivor; /* 经注册根保活 */
    cwgc_global_register(&survivor, sizeof(survivor));
    memset(&survivor, 0, sizeof(survivor));
    cwvec_init(&survivor, CWInt16, 2);
    {
        CWValue_t c;
        wrap_i16(&c, 7);
        cwvec_push(&survivor, &c);
    }
    void* surv_data = (void*)(uintptr_t)survivor.address;
    uint64_t* meta0 = cwmc_gc_meta_of(surv_data);
    T("survivor meta found", meta0 != NULL);
    const size_t age0 = (*meta0 & CWGC_AGE_MASK) >> CWGC_AGE_SHIFT;
    for (int i = 0; i < 3; i++) {
        scrub();
        cwgc_collect_mode(CWGC_MODE_MAJOR);
    }
    meta0 = cwmc_gc_meta_of(surv_data);
    T("survivor still live after 3 majors", meta0 != NULL);
    T("survivor promoted to old",
      meta0 && ((*meta0 & CWGC_AGE_MASK) >> CWGC_AGE_SHIFT) >= CWGC_OLD_AGE
      && ((*meta0 & CWGC_AGE_MASK) >> CWGC_AGE_SHIFT) > age0);
    T("survivor intact", cwvec_size(&survivor) == 1);

    printf("\n - minor 轮: 老代不可回收, 年轻代垃圾照常回收\n");
    /* 年轻代垃圾: 分配后立刻切断 */
    {
        CWValue_t junk;
        memset(&junk, 0, sizeof(junk));
        cwvec_init(&junk, CWInt16, 2);
        CWValue_t c;
        wrap_i16(&c, 1);
        cwvec_push(&junk, &c);
        memset(&junk, 0, sizeof(junk)); /* 切断 */
    }
    scrub();
    const size_t b_minor = live_allocs();
    cwgc_collect_mode(CWGC_MODE_MINOR);
    T("minor reclaims young garbage", live_allocs() < b_minor);
    T("minor leaves old slots alone", cwvec_size(&survivor) == 1);
    cwgc_stats(&st);
    T("minor cycle recorded", st.minor_cycles > 0);
    T("mode is minor", cwgc_last_mode() == CWGC_MODE_MINOR);

    printf("\n - minor 轮老->新引用: remember 重扫保活\n");
    /* old map (survivor2, 晋升) -> 年轻 vector: minor 轮必须保活年轻代 */
    static CWValue_t old_holder;
    cwgc_global_register(&old_holder, sizeof(old_holder));
    memset(&old_holder, 0, sizeof(old_holder));
    cwmap_init(&old_holder, CWInt16, CWVector);
    /* 晋升: 3 轮 major */
    for (int i = 0; i < 3; i++) {
        scrub();
        cwgc_collect_mode(CWGC_MODE_MAJOR);
    }
    /* 老代 map 塞入年轻 vector (minor 前, 触发老->新) */
    {
        CWValue_t young;
        memset(&young, 0, sizeof(young));
        cwvec_init(&young, CWInt16, 2);
        CWValue_t c;
        wrap_i16(&c, 42);
        cwvec_push(&young, &c);
        CWValue_t k;
        wrap_i16(&k, 1);
        cwmap_put(&old_holder, &k, &young);
        memset(&young, 0, sizeof(young)); /* 切断栈引用, 只剩 old->young */
    }
    /* 多轮 minor: young vector 必须活 (remset 重扫) */
    bool ok = true;
    for (int i = 0; i < 4; i++) {
        scrub();
        cwgc_collect_mode(CWGC_MODE_MINOR);
        CWValue_t k, out;
        wrap_i16(&k, 1);
        memset(&out, 0, sizeof(out));
        if (!cwmap_get(&old_holder, &k, &out) || out.address == 0
            || cwvec_size(&out) != 1) {
            ok = false;
            break;
        }
    }
    T("old->young survives minors", ok);
    /* major 轮: old_holder 仍经注册根保活, 内容完好 */
    scrub();
    cwgc_collect_mode(CWGC_MODE_MAJOR);
    {
        CWValue_t k, out;
        wrap_i16(&k, 1);
        memset(&out, 0, sizeof(out));
        T("old holder intact after major",
          cwmap_get(&old_holder, &k, &out) && cwvec_size(&out) == 1);
    }
    cwgc_global_unregister(&old_holder);
    memset(&old_holder, 0, sizeof(old_holder));

    printf("\n - 大对象归还 (F5 收口)\n");
    /* 大对象: > 4096B 的 data -> dedicated 映射。
     * 大容量 Vector: items = 512 * 24B = 12 KiB (dedicated) */
    {
        CWValue_t big;
        memset(&big, 0, sizeof(big));
        cwvec_init(&big, CWInt16, 512);
        CWGCStats_t s0;
        cwgc_stats(&s0);
        void* big_items = (void*)(uintptr_t)big.address;
        (void)big_items;
        /* 找到 items 槽 (data->items 也是 dedicated): 直接构造大对象 */
        memset(&big, 0, sizeof(big));
        cwgc_stats(&st);
    }
    /* 更直接: 造 2 个不可达大对象 (dedicated data), collect 后必须归还 */
    {
        CWValue_t b1, b2;
        memset(&b1, 0, sizeof(b1));
        memset(&b2, 0, sizeof(b2));
        cwvec_init(&b1, CWInt16, 512);
        cwvec_init(&b2, CWInt16, 512);
        void* i1 = (void*)(uintptr_t)b1.address;
        void* i2 = (void*)(uintptr_t)b2.address;
        (void)i1; (void)i2;
        memset(&b1, 0, sizeof(b1));
        memset(&b2, 0, sizeof(b2));
    }
    scrub();
    cwgc_stats(&st);
    const size_t large_before = st.large_released;
    const size_t os_before = st.os_released_bytes;
    cwgc_collect_mode(CWGC_MODE_MAJOR);
    cwgc_stats(&st);
    T("large objects released by sweep",
      st.large_released > large_before);
    T("os released bytes grows", st.os_released_bytes > os_before);

    printf("\n - 空块归还水位 (mapped_bytes 回落)\n");
    /* 开启水位: 连续 2 轮全空 + 每类保留 1 块 */
    cwgc_set_release_vacant(2);
    T("release vacant set", cwgc_release_vacant() == 2);
    CWMemCenterStats_t ms0;
    cwmc_stats(&ms0);
    /* churn: 先暴涨 (让 48B 类跨 2+ 块) 再全丢, 每轮制造
     * 「超过保留配额的空块」; 连续 2 轮后水位达标归还 */
    for (int round = 0; round < 6; round++) {
        CWValue_t keep[2200];
        for (int i = 0; i < 2200; i++) {
            memset(&keep[i], 0, sizeof(keep[i]));
            cwvec_init(&keep[i], CWInt16, 2);
            CWValue_t c;
            wrap_i16(&c, i + 1);
            cwvec_push(&keep[i], &c);
        }
        memset(keep, 0, sizeof(keep));
        scrub();
        /* è¿ç»­ä¸¤è½®æ¶é (ä¸­é´æ åé): æ°´ä½ä¸è¢«
         * fill æ step sweep æ¸é¶, ç©ºåæ°´ä½æè½ç´¯ç§¯å°éå¼ */
        cwgc_collect();
        cwgc_collect();
    }
    CWMemCenterStats_t ms1;
    cwmc_stats(&ms1);
    cwgc_stats(&st);
    printf("    ms0.mapped=%zu ms1.mapped=%zu blocks_released=%zu\n",
           ms0.mapped_bytes, ms1.mapped_bytes, st.blocks_released);
    T("empty blocks released", st.blocks_released > 0);
    T("mapped_bytes settled", ms1.mapped_bytes < ms0.mapped_bytes + 1024 * 1024);
    cwgc_set_release_vacant(0); /* 关闭, 不影响后续 */

    printf("\n - 分代混合负载 (随便分配怎么丢不崩)\n");
    cwgc_set_step_bytes(8 * 1024); /* 高频 step, AUTO 分代调度 */
    static CWValue_t index_map;
    cwgc_global_register(&index_map, sizeof(index_map));
    memset(&index_map, 0, sizeof(index_map));
    cwmap_init(&index_map, CWInt, CWString);
    bool mixed_ok = true;
    for (int r = 0; r < 40; r++) {
        CWValue_t k, v;
        wrap_i16(&k, r);
        char* s = (char*)cwrt_arena_alloc(16);
        snprintf(s, 16, "key%d", r);
        cwval_wrap(&v, s, strlen(s));
        if (!cwmap_put(&index_map, &k, &v)) mixed_ok = false;
        CWValue_t tmp;
        memset(&tmp, 0, sizeof(tmp));
        cwvec_init(&tmp, CWInt, 4);
        for (int i = 0; i < 10; i++) {
            CWValue_t c;
            wrap_i16(&c, i + 1);
            cwvec_push(&tmp, &c);
        }
  
        if (r % 8 == 7) {
            scrub();
            cwgc_collect();
        }
    }
    T("mixed load no crash", mixed_ok);
    /* 长存活索引内容完好 (收敛后) */
    for (int i = 0; i < 3; i++) {
        scrub();
        cwgc_collect();
    }
    {
        CWValue_t k, out;
        wrap_i16(&k, 39);
        memset(&out, 0, sizeof(out));
        T("index intact after mixed load",
          cwmap_get(&index_map, &k, &out) && out.length == 5
          && memcmp((const void*)(uintptr_t)out.address, "key39", 5) == 0);
    }
    cwgc_stats(&st);
    printf("\n - 统计: cycles=%zu minor=%zu major=%zu blocks_rel=%zu "
           "large_rel=%zu os_rel=%zu\n",
           st.cycles, st.minor_cycles, st.major_cycles,
           st.blocks_released, st.large_released,
           st.os_released_bytes);
    T("both minor and major ran",
      st.minor_cycles > 0 && st.major_cycles > 0);
    T("os release observed", st.os_released_bytes > 0);

    cwgc_global_unregister(&index_map);
    cwgc_global_unregister(&survivor);
    cwgc_shutdown();
    cwmc_shutdown();

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
