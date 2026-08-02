#include "cwind_map.h"
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <time.h>


/* boxed-int helpers: keys are (void*)(intptr_t)i */
static void* boxed(int v) {
    /* +1 so boxed(0) is not NULL (NULL is reserved for "missing") */
    return (void*)(uintptr_t)((unsigned)v + 1u);
}

static int unbox(const void* p) {
    return (int)(uintptr_t)p - 1;
}

static int cmp_boxed_int(const void* a, const void* b) {
    const int x = unbox(a);
    const int y = unbox(b);
    return (x > y) - (x < y);
}

/* string keys */
static int cmp_str(const void* a, const void* b) {
    return strcmp((const char*)a, (const char*)b);
}

static char* dup_str(const char* s) {
    const size_t n = strlen(s) + 1;
    char* p = (char*)malloc(n);
    if (p) memcpy(p, s, n);
    return p;
}


/* value owning heap memory, for safe-variant tests */
typedef struct {
    int id;
    char* buf;
} Owned;

static int owned_freed = 0;

void owned_dtor(void* mem) {
    Owned* o = (Owned*)mem;
    if (o->buf) {
        free(o->buf);
        o->buf = NULL;
    }
    owned_freed++;
    free(o);
}

static Owned* new_owned(int id, const char* text) {
    Owned* o = (Owned*)malloc(sizeof(Owned));
    if (!o) return NULL;
    o->id = id;
    o->buf = (char*)malloc(strlen(text) + 1);
    if (o->buf) memcpy(o->buf, text, strlen(text) + 1);
    return o;
}


/* nesting: value is a child map */
static int child_freed = 0;

void child_map_dtor(void* mem) {
    CWMap_t* child = (CWMap_t*)mem;
    if (child) {
        cwmap_destroy(child);
        child_freed++;
    }
}


/* set-style: key and value point to the same allocation */
static int shared_freed = 0;

void shared_dtor(void* p) {
    (void)p;
    shared_freed++;
}


static int pass = 0, fail = 0;


#define T(name, cond) do { \
    if (cond) { printf("  [PASS] %s\n", name); pass++; } \
    else      { printf("  [FAIL] %s\n", name); fail++; } \
} while (0)


/* red-black tree invariant checker */

static int rb_bad = 0;

static size_t rb_black_height(const CWMapNode_t* n) {
    if (!n) return 1;
    if (n->color == CWMAP_RED) {
        if (n->left  && n->left->color  == CWMAP_RED) rb_bad = 1;
        if (n->right && n->right->color == CWMAP_RED) rb_bad = 1;
    }
    if (n->left  && n->left->parent  != n) rb_bad = 1;
    if (n->right && n->right->parent != n) rb_bad = 1;
    const size_t l = rb_black_height(n->left);
    const size_t r = rb_black_height(n->right);
    if (l != r) rb_bad = 1;
    return l + (n->color == CWMAP_BLACK);
}


/* full check: colors, parent links, black height, order, count */
static int rb_verify(CWMap_t* m) {
    rb_bad = 0;
    if (!m->root) return 1;
    if (m->root->color != CWMAP_BLACK) return 0;
    rb_black_height(m->root);
    if (rb_bad) return 0;

    const void* prev = NULL;
    size_t cnt = 0;
    for (CWMapIter_t it = cwmap_begin(m);
         cwmap_iter_valid(&it); cwmap_iter_next(&it)) {
        const void* k = cwmap_iter_key(&it);
        if (prev && cwmap_compare_keys(m, prev, k) >= 0) return 0;
        prev = k;
        cnt++;
    }
    if (cnt != cwmap_size(m)) return 0;
    return 1;
}


/* simple model for random differential testing */

#define MODEL_CAP 5000
static int model_keys[MODEL_CAP];
static size_t model_n = 0;

static int model_find(int k) {
    for (size_t i = 0; i < model_n; i++) {
        if (model_keys[i] == k) return (int)i;
    }
    return -1;
}

static void model_insert(int k) {
    if (model_find(k) < 0) model_keys[model_n++] = k;
}

static void model_remove(int k) {
    const int i = model_find(k);
    if (i >= 0) model_keys[i] = model_keys[--model_n];
}

static uint64_t rng_state = 0x9E3779B97F4A7C15ull;

static uint32_t rng_next(void) {
    rng_state = rng_state * 6364136223846793005ull + 1442695040888963407ull;
    return (uint32_t)(rng_state >> 32);
}


/* portable monotonic timer for the informational benchmark */
#if defined(_WIN32)
static double now_ms(void) {
    LARGE_INTEGER freq, cnt;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&cnt);
    return (double)cnt.QuadPart * 1000.0 / (double)freq.QuadPart;
}
#else
static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1e6;
}
#endif


/* slot index of a node inside the pool (for the free-list audit) */
static size_t test_slot_of(CWMap_t* m, const CWMapNode_t* n) {
    size_t idx = 0;
    for (CWMapBlock_t* b = m->pool; b; b = b->next, idx++) {
        const char* base = (const char*)b + m->pool_mem_offset;
        const char* end  = base + m->pool_block_count * m->node_stride;
        if ((const char*)n >= base && (const char*)n < end) {
            return idx * m->pool_block_count
                 + (size_t)((const char*)n - base) / m->node_stride;
        }
    }
    return (size_t)-1;
}


/*
 * Deep audit (merged from the external fuzz tool):
 * RB colors/parents/black-height, in-order ordering, count, and the
 * free-list consistency (no duplicates, free_count matches, live nodes are
 * never marked free, count + free_count == capacity).
 */
static int test_audit(CWMap_t* m) {
    if (m->root && m->root->color != CWMAP_BLACK) return 0;
    rb_bad = 0;
    rb_black_height(m->root);
    if (rb_bad) return 0;

    const void* prev = NULL;
    size_t cnt = 0;
    for (CWMapIter_t it = cwmap_begin(m);
         cwmap_iter_valid(&it); cwmap_iter_next(&it)) {
        const void* k = cwmap_iter_key(&it);
        if (prev && cwmap_compare_keys(m, prev, k) >= 0) return 0;
        prev = k;
        cnt++;
    }
    if (cnt != m->count) return 0;

    const size_t cap = m->block_count * m->pool_block_count;
    if (cap == 0) return m->count == 0 && m->free_count == 0;
    if (m->free_count > cap) return 0;
    if (m->count + m->free_count != cap) return 0;

    unsigned char* mark = (unsigned char*)calloc(1, cap);
    if (!mark) return 0;
    size_t fl = 0;
    for (CWMapNode_t* n = m->free_list; n; n = n->parent) {
        const size_t slot = test_slot_of(m, n);
        if (slot == (size_t)-1 || slot >= cap || mark[slot]) {
            free(mark);
            return 0;
        }
        mark[slot] = 1;
        fl++;
    }
    if (fl != m->free_count) {
        free(mark);
        return 0;
    }
    for (CWMapNode_t* n = cwmap_tree_minimum(m->root);
         n; n = cwmap_successor(n)) {
        const size_t slot = test_slot_of(m, n);
        if (slot == (size_t)-1 || slot >= cap || mark[slot]) {
            free(mark);
            return 0;
        }
    }
    free(mark);
    return 1;
}


int main(void) {
    printf("CWMap (generic red-black map) tests:\n\n");

    /* create */
    printf(" - cwmap_create\n");
    CWMap_t* m = cwmap_create(cmp_boxed_int);
    T("create != NULL",        m != NULL);
    T("size == 0",             cwmap_size(m) == 0);
    T("empty",                 cwmap_empty(m));
    T("empty map capacity == 0 (lazy pool)", cwmap_capacity(m) == 0);
    T("empty map free_count == 0", cwmap_free_count(m) == 0);
    T("first put allocates block",
        cwmap_put(m, boxed(0), boxed(0)) != NULL);
    T("capacity == 1024 after first put", cwmap_capacity(m) == 1024);
    T("free_count == 1023 after first put", cwmap_free_count(m) == 1023);
    CWMap_t* tiny = cwmap_create_ex(cmp_boxed_int, 16);
    T("create_ex(16) starts empty (cap == 0)",
        tiny != NULL && cwmap_capacity(tiny) == 0);
    T("create_ex(16) first put -> cap 16",
        tiny != NULL && cwmap_put(tiny, boxed(1), boxed(2)) != NULL
        && cwmap_capacity(tiny) == 16);
    cwmap_destroy(tiny);

    /* put / get / contains (boxed int keys) */
    printf("\n - put / get / contains\n");
    for (int i = 0; i < 100; i++) {
        T("put ok", cwmap_put(m, boxed(i), boxed(i * 2)) != NULL);
    }
    T("size == 100", cwmap_size(m) == 100);
    T("get(50) == 100",  cwmap_get(m, boxed(50)) == boxed(100));
    T("get(missing) == NULL", cwmap_get(m, boxed(1000)) == NULL);
    T("contains(50)",     cwmap_contains(m, boxed(50)));
    T("!contains(1000)",  !cwmap_contains(m, boxed(1000)));

    /* overwrite semantics */
    printf("\n - overwrite semantics\n");
    T("put overwrite != NULL", cwmap_put(m, boxed(50), boxed(500)) != NULL);
    T("size still 100",        cwmap_size(m) == 100);
    T("value updated",         cwmap_get(m, boxed(50)) == boxed(500));
    T("rb ok",                 rb_verify(m));

    /* insert-only */
    printf("\n - cwmap_insert (no overwrite)\n");
    T("insert new key ok",    cwmap_insert(m, boxed(999), boxed(1)) != NULL);
    T("insert existing NULL", cwmap_insert(m, boxed(50), boxed(2)) == NULL);
    T("existing value intact", cwmap_get(m, boxed(50)) == boxed(500));
    T("size == 101", cwmap_size(m) == 101);

    /* NULL values are storable; contains disambiguates */
    printf("\n - NULL values\n");
    T("put NULL value ok",  cwmap_put(m, boxed(1000), NULL) == NULL);
    T("get NULL value",     cwmap_get(m, boxed(1000)) == NULL);
    T("contains NULL value", cwmap_contains(m, boxed(1000)));

    /* remove / take */
    printf("\n - remove / take\n");
    T("remove ok",            cwmap_remove(m, boxed(10)));
    T("size == 101",          cwmap_size(m) == 101);
    T("removed gone",         !cwmap_contains(m, boxed(10)));
    T("remove missing false", !cwmap_remove(m, boxed(10)));
    void* tk = NULL, *tv = NULL;
    T("take ok",              cwmap_take(m, boxed(20), &tk, &tv));
    T("taken key/value",      unbox(tk) == 20 && tv == boxed(40));
    T("taken gone",           !cwmap_contains(m, boxed(20)));
    T("size == 100",          cwmap_size(m) == 100);
    T("take missing false",   !cwmap_take(m, boxed(20), &tk, &tv));

    /* iteration */
    printf("\n - iteration\n");
    int prev_k = -1, ordered = 1, it_count = 0;
    for (CWMapIter_t it = cwmap_begin(m);
         cwmap_iter_valid(&it); cwmap_iter_next(&it)) {
        const int k = unbox(cwmap_iter_key(&it));
        if (k <= prev_k) ordered = 0;
        prev_k = k;
        it_count++;
    }
    T("iter count == 100", it_count == 100);
    T("ascending order",   ordered);

    int rev_ok = 1, rev_count = 0;
    prev_k = INT32_MAX;
    for (CWMapIter_t it = cwmap_rbegin(m);
         cwmap_iter_valid(&it); cwmap_iter_prev(&it)) {
        const int k = unbox(cwmap_iter_key(&it));
        if (k >= prev_k) rev_ok = 0;
        prev_k = k;
        rev_count++;
    }
    T("reverse count == 100", rev_count == 100);
    T("descending order",     rev_ok);

    CWMapIter_t fit = cwmap_find(m, boxed(42));
    T("find valid",       cwmap_iter_valid(&fit));
    T("find key matches", cwmap_iter_valid(&fit)
                          && unbox(cwmap_iter_key(&fit)) == 42);
    CWMapIter_t fit2 = cwmap_find(m, boxed(777));
    T("find missing invalid", !cwmap_iter_valid(&fit2));

    /* clear & reuse */
    printf("\n - clear & reuse\n");
    const size_t cap_before = cwmap_capacity(m);
    cwmap_clear(m);
    T("size == 0 after clear", cwmap_size(m) == 0);
    T("empty after clear",     cwmap_empty(m));
    T("capacity unchanged",    cwmap_capacity(m) == cap_before);
    T("free_count == capacity", cwmap_free_count(m) == cap_before);
    for (int i = 0; i < 100; i++) {
        cwmap_put(m, boxed(i), boxed(i));
    }
    T("re-push ok",            cwmap_size(m) == 100);
    T("no growth after reuse", cwmap_capacity(m) == cap_before);
    T("rb ok after reuse",     rb_verify(m));

    /* recycling: remove all, re-insert, capacity stays flat */
    printf("\n - node recycling\n");
    for (int i = 0; i < 100; i++) {
        cwmap_remove(m, boxed(i));
    }
    T("drained",               cwmap_size(m) == 0);
    T("free_count == capacity", cwmap_free_count(m) == cwmap_capacity(m));
    for (int i = 0; i < 100; i++) {
        cwmap_put(m, boxed(i), boxed(i));
    }
    T("recycle keeps capacity", cwmap_capacity(m) == cap_before);
    T("rb ok after recycle",    rb_verify(m));
    cwmap_destroy(m);

    /* default (NULL) comparator: pointer-address order */
    printf("\n - default pointer comparator\n");
    CWMap_t* pm = cwmap_create(NULL);
    int a = 1, b = 2, c = 3;
    T("ptr put ok", cwmap_put(pm, &a, &a) != NULL
                    && cwmap_put(pm, &b, &b) != NULL
                    && cwmap_put(pm, &c, &c) != NULL);
    T("ptr get by same addr", cwmap_get(pm, &b) == &b);
    T("ptr distinct addr distinct key", cwmap_get(pm, &a) != cwmap_get(pm, &b));
    int a2 = 1; /* same content, different address */
    T("ptr get by equal-content addr NULL", cwmap_get(pm, &a2) == NULL);
    cwmap_destroy(pm);

    /* string keys + key destructor */
    printf("\n - string keys with key_dtor\n");
    CWMap_t* sm = cwmap_safe_create(cmp_str, free, NULL);
    T("string map create", sm != NULL);
    T("safe_create(NULL,NULL) == NULL",
        cwmap_safe_create(cmp_str, NULL, NULL) == NULL);
    for (int i = 0; i < 50; i++) {
        char key[16];
        snprintf(key, sizeof(key), "key%03d", i);
        cwmap_put(sm, dup_str(key), boxed(i));
    }
    T("string size == 50", cwmap_size(sm) == 50);
    T("string get", unbox(cwmap_get(sm, "key042")) == 42);
    char kbad[] = "key9999";
    T("string missing NULL", cwmap_get(sm, kbad) == NULL);
    int str_ordered = 1;
    char last[16] = "";
    for (CWMapIter_t it = cwmap_begin(sm);
         cwmap_iter_valid(&it); cwmap_iter_next(&it)) {
        if (last[0] && strcmp((char*)cwmap_iter_key(&it), last) <= 0) {
            str_ordered = 0;
        }
        snprintf(last, sizeof(last), "%s", (char*)cwmap_iter_key(&it));
    }
    T("string keys ascending", str_ordered);
    cwmap_destroy(sm);
    T("destroy freed all keys", 1); /* key_dtor = free; no leak check needed */

    /* safe variants: automatic value cleanup */
    printf("\n - safe variants (value destructor)\n");
    CWMap_t* sa = cwmap_safe_create(cmp_boxed_int, NULL, owned_dtor);
    T("safe create != NULL", sa != NULL);
    for (int i = 10; i <= 40; i += 10) {
        cwmap_put(sa, boxed(i), new_owned(i, "value"));
    }
    T("size == 4", cwmap_size(sa) == 4);
    T("overwrite calls dtor (owned_freed == 1)",
        cwmap_put(sa, boxed(10), new_owned(99, "new")) != NULL
        && owned_freed == 1);
    T("remove calls dtor (owned_freed == 2)",
        cwmap_remove(sa, boxed(20)) && owned_freed == 2);
    void* outk = NULL, *outv = NULL;
    T("take transfers ownership (owned_freed == 2)",
        cwmap_take(sa, boxed(30), &outk, &outv) && owned_freed == 2);
    T("taken value usable", outv != NULL
                            && ((Owned*)outv)->id == 30
                            && ((Owned*)outv)->buf != NULL);
    owned_dtor(outv); /* caller frees the transferred value */
    T("clear calls dtor on rest (owned_freed == 5)",
        (cwmap_clear(sa), owned_freed == 5));
    for (int i = 0; i < 3; i++) {
        cwmap_put(sa, boxed(i), new_owned(i, "x"));
    }
    cwmap_destroy(sa);
    T("destroy frees all (owned_freed == 8)", owned_freed == 8);

    /* raw containers never call dtors */
    CWMap_t* raw = cwmap_create(cmp_boxed_int);
    for (int i = 0; i < 3; i++) {
        cwmap_put(raw, boxed(i), new_owned(i, "raw"));
    }
    cwmap_clear(raw);
    cwmap_destroy(raw);
    T("raw never calls dtor (owned_freed == 8)", owned_freed == 8);

    /* set-style shared key/value: the double-free guard */
    printf("\n - set-style shared key/value (double-free guard)\n");
    CWMap_t* set = cwmap_safe_create(cmp_boxed_int, shared_dtor, shared_dtor);
    T("set map create", set != NULL);
    for (int i = 0; i < 5; i++) {
        cwmap_put(set, boxed(i), boxed(i));
    }
    cwmap_remove(set, boxed(2));
    T("remove frees once, not twice", shared_freed == 1);
    cwmap_clear(set);
    T("clear frees once per entry", shared_freed == 5);

    for (int i = 0; i < 3; i++) {
        cwmap_put(set, boxed(i), boxed(i));
    }
    cwmap_put(set, boxed(0), boxed(0));   /* same pointers again: nothing freed */
    T("re-put same shared ptr frees nothing", shared_freed == 5);
    cwmap_put(set, boxed(1), boxed(100)); /* key ptr retained; old shared alloc kept */
    T("overwrite keeps retained ptr", shared_freed == 5);
    cwmap_destroy(set);
    /* entry0 (0,0): 1 dtor; entry1 (1,100): 2 dtors; entry2 (2,2): 1 dtor */
    T("destroy frees once per allocation", shared_freed == 9);

    /* nesting */
    printf("\n - nesting (child maps by pointer)\n");
    CWMap_t* outer = cwmap_safe_create(cmp_boxed_int, NULL, child_map_dtor);
    T("outer create", outer != NULL);
    for (int i = 0; i < 5; i++) {
        CWMap_t* child = cwmap_create(cmp_boxed_int);
        for (int j = 0; j < 10; j++) {
            cwmap_put(child, boxed(j), boxed(i * 100 + j));
        }
        cwmap_put(outer, boxed(i), child);
    }
    T("outer size == 5", cwmap_size(outer) == 5);
    CWMap_t* c3 = (CWMap_t*)cwmap_get(outer, boxed(3));
    T("nested get", c3 != NULL);
    T("child lookup works", c3 && cwmap_contains(c3, boxed(7)));
    T("child value correct", c3 && cwmap_get(c3, boxed(7)) == boxed(307));
    T("child_freed == 0", child_freed == 0);
    CWMap_t* extracted = NULL;
    T("take child", cwmap_take(outer, boxed(3), NULL, (void**)&extracted));
    T("extracted usable", extracted && cwmap_size(extracted) == 10);
    T("take did not free (child_freed == 0)", child_freed == 0);
    cwmap_destroy(extracted);
    T("manual destroy not counted (child_freed == 0)", child_freed == 0);
    cwmap_destroy(outer);
    T("outer destroy frees rest (child_freed == 4)", child_freed == 4);

    /* shrink_to_fit */
    printf("\n - cwmap_shrink_to_fit\n");
    CWMap_t* sh = cwmap_create_ex(cmp_boxed_int, 16);
    T("shrink no-op on 1 block", cwmap_shrink_to_fit(sh));
    for (int i = 0; i < 1000; i++) {
        cwmap_put(sh, boxed(i), boxed(i));
    }
    T("blocks grew to 63", sh->block_count == 63);
    for (int i = 0; i < 900; i++) {
        cwmap_remove(sh, boxed(i));
    }
    T("size == 100", cwmap_size(sh) == 100);
    T("shrink ok", cwmap_shrink_to_fit(sh));
    T("blocks after == 7", sh->block_count == 7);
    int all_ok = 1;
    for (int i = 900; i < 1000; i++) {
        if (cwmap_get(sh, boxed(i)) != boxed(i)) all_ok = 0;
    }
    T("live entries intact", all_ok);
    T("rb ok after shrink", rb_verify(sh));
    for (int i = 1000; i < 1100; i++) {
        cwmap_put(sh, boxed(i), boxed(i));
    }
    T("push after shrink ok", cwmap_size(sh) == 200);
    cwmap_clear(sh);
    T("shrink empty ok", cwmap_shrink_to_fit(sh));
    T("empty shrink keeps 1 block", sh->block_count == 1);
    cwmap_put(sh, boxed(1), boxed(2));
    T("push after empty shrink ok", cwmap_size(sh) == 1);
    cwmap_destroy(sh);

    /* random differential fuzz vs model */
    printf("\n - random fuzz (20000 ops vs model)\n");
    CWMap_t* fz = cwmap_create(cmp_boxed_int);
    model_n = 0;
    int fuzz_ok = 1;
    for (int op = 0; op < 20000; op++) {
        const int k = (int)(rng_next() % MODEL_CAP);
        const int t = (int)(rng_next() % 3);
        switch (t) {
        case 0:
            cwmap_put(fz, boxed(k), boxed(k * 2));
            model_insert(k);
            break;
        case 1:
            if (cwmap_remove(fz, boxed(k))) model_remove(k);
            break;
        default: {
            const int in_model = model_find(k) >= 0;
            if (cwmap_contains(fz, boxed(k)) != (in_model != 0)) fuzz_ok = 0;
            if (in_model && cwmap_get(fz, boxed(k)) != boxed(k * 2)) {
                fuzz_ok = 0;
            }
            break;
        }
        }
        if ((op & 511) == 0) {
            if (cwmap_size(fz) != model_n) fuzz_ok = 0;
            if (!rb_verify(fz)) fuzz_ok = 0;
        }
    }
    T("fuzz size matches model", cwmap_size(fz) == model_n);
    T("fuzz invariants hold", fuzz_ok && rb_verify(fz));
    int fuzz_iter = 0, fuzz_asc = 1;
    prev_k = -1;
    for (CWMapIter_t it = cwmap_begin(fz);
         cwmap_iter_valid(&it); cwmap_iter_next(&it)) {
        const int k = unbox(cwmap_iter_key(&it));
        if (k <= prev_k) fuzz_asc = 0;
        prev_k = k;
        fuzz_iter++;
    }
    T("fuzz iteration matches model", fuzz_iter == (int)model_n);
    T("fuzz ascending order", fuzz_asc);
    cwmap_destroy(fz);

    /* structured fuzz (merged from the external audit tool) */
    printf("\n - structured fuzz (3M ops + full audit)\n");
    CWMap_t* sf = cwmap_create_ex(cmp_boxed_int, 7);
    model_n = 0;
    int sf_ok = 1;
    for (long op = 0; op < 3000000; op++) {
        const int k = (int)(rng_next() % MODEL_CAP);
        const int t = (int)(rng_next() % 10);
        if (t < 4) {
            model_insert(k);
            cwmap_put(sf, boxed(k), boxed(k));
        } else if (t < 8) {
            model_remove(k);
            cwmap_remove(sf, boxed(k));
        } else if (t == 8) {
            cwmap_shrink_to_fit(sf);
        } else {
            cwmap_clear(sf);
            model_n = 0;
        }
        if ((op & 0x3FFF) == 0) {
            if (cwmap_size(sf) != model_n) {
                sf_ok = 0;
                printf("  [INFO] size mismatch at op %ld\n", op);
                break;
            }
            if (!test_audit(sf)) {
                sf_ok = 0;
                printf("  [INFO] audit failed at op %ld\n", op);
                break;
            }
        }
    }
    T("3M-op structured fuzz passes deep audit",
      sf_ok && cwmap_size(sf) == model_n && test_audit(sf));
    cwmap_destroy(sf);

    /* big stress */
    printf("\n - big stress (100k)\n");
    CWMap_t* big = cwmap_create(cmp_boxed_int);
    int push_ok = 1;
    for (int i = 0; i < 100000; i++) {
        if (!cwmap_put(big, boxed(i), boxed(i))) {
            push_ok = 0;
            break;
        }
    }
    T("100k puts ok", push_ok && cwmap_size(big) == 100000);
    T("rb ok after 100k", rb_verify(big));
    T("spot get(65535)", cwmap_get(big, boxed(65535)) == boxed(65535));
    T("spot get(0)",     cwmap_get(big, boxed(0)) == boxed(0));
    T("spot get(99999)", cwmap_get(big, boxed(99999)) == boxed(99999));
    for (int i = 0; i < 100000; i += 2) {
        cwmap_remove(big, boxed(i));
    }
    T("50k removes ok", cwmap_size(big) == 50000);
    T("rb ok after removes", rb_verify(big));
    int count = 0;
    for (CWMapIter_t it = cwmap_begin(big);
         cwmap_iter_valid(&it); cwmap_iter_next(&it)) {
        count++;
    }
    T("iter == 50000", count == 50000);
    cwmap_destroy(big);

    /* memory footprint (merged from the external sizes tool) */
    printf("\n - memory footprint\n");
    {
        CWMap_t* fm = cwmap_create(NULL);
        const size_t block_total =
            fm->pool_mem_offset + fm->pool_block_count * fm->node_stride;
        printf("  sizeof(CWMapNode_t)=%zu node_stride=%zu "
               "nodes/block=%zu block_total=%zu bytes\n",
               sizeof(CWMapNode_t), fm->node_stride,
               fm->pool_block_count, block_total);
        printf("  after create: block_count=%zu committed=%zu bytes (%.1f KB)\n",
               fm->block_count, fm->block_count * block_total,
               (fm->block_count * block_total) / 1024.0);
        T("empty map commits nothing", fm->block_count == 0);
        int kk = 1;
        cwmap_put(fm, &kk, &kk);
        printf("  after 1st put: block_count=%zu committed=%zu bytes (%.1f KB)\n",
               fm->block_count, fm->block_count * block_total,
               (fm->block_count * block_total) / 1024.0);
        T("first put commits one block", fm->block_count == 1);
        cwmap_destroy(fm);
    }

    /* benchmark (merged from the external bench tools; informational only) */
    printf("\n - benchmark (informational, N=1M; "
           "define CWMap_TEST_SKIP_BENCH to skip)\n");
#ifndef CWMap_TEST_SKIP_BENCH
    {
        enum { BENCH_N = 1000000 };
        CWMap_t* bm = cwmap_create(cmp_boxed_int);
        for (int i = 0; i < BENCH_N; i++) {
            cwmap_put(bm, boxed(i), boxed(i));
        }
        double t0 = now_ms();
        for (int r = 0; r < 10; r++) {
            for (int i = 0; i < BENCH_N; i++) {
                cwmap_put(bm, boxed(i), boxed(i + 1));
            }
        }
        const double ow_ms = now_ms() - t0;

        t0 = now_ms();
        for (int r = 0; r < 10; r++) {
            for (int i = 0; i < BENCH_N; i++) {
                cwmap_get(bm, boxed(i));
            }
        }
        const double gt_ms = now_ms() - t0;

        t0 = now_ms();
        for (int r = 0; r < 10; r++) {
            for (int i = 0; i < BENCH_N; i++) {
                cwmap_remove(bm, boxed(i));
            }
        }
        const double rm_ms = now_ms() - t0;
        printf("  overwrite put: %.1f ms/10M   (get baseline: %.1f ms/10M)\n",
               ow_ms, gt_ms);
        printf("  remove       : %.1f ms/10M\n", rm_ms);
        cwmap_destroy(bm);

        CWMap_t* bs = cwmap_create_ex(cmp_boxed_int, 1024);
        for (int i = 0; i < BENCH_N; i++) {
            cwmap_put(bs, boxed(i), boxed(i));
        }
        for (int i = 0; i < BENCH_N - 1000; i++) {
            cwmap_remove(bs, boxed(i));
        }
        t0 = now_ms();
        cwmap_shrink_to_fit(bs);
        printf("  shrink_to_fit: %.1f ms for %zu live nodes in %zu blocks\n",
               now_ms() - t0, cwmap_size(bs), bs->block_count);
        cwmap_destroy(bs);
    }
#else
    printf("  skipped\n");
#endif

    /* destroy NULL */
    printf("\n - cwmap_destroy(NULL)\n");
    cwmap_destroy(NULL);
    T("destroy(NULL) no crash", 1);

    printf("\nResults: %d PASS, %d FAIL\n", pass, fail);
    return fail ? 1 : 0;
}
