#include "cwind_value_map.h"
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <time.h>


typedef struct {
    int id;
    double val;
} Value;


/* numeric-order comparator for int keys */
static int cmp_int(const void* a, const void* b) {
    const int x = *(const int*)a;
    const int y = *(const int*)b;
    return (x > y) - (x < y);
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
}


/* key owning heap memory, for key_dtor tests */
typedef struct {
    int id;
    char* name;
} StrKey;

static int key_freed = 0;

void strkey_dtor(void* mem) {
    StrKey* k = (StrKey*)mem;
    if (k->name) {
        free(k->name);
        k->name = NULL;
    }
    key_freed++;
}

static StrKey new_strkey(int id, const char* name) {
    StrKey k;
    /* zero padding: memcmp-based lookup must not see indeterminate bytes */
    memset(&k, 0, sizeof(k));
    k.id = id;
    k.name = (char*)malloc(strlen(name) + 1);
    if (k.name) memcpy(k.name, name, strlen(name) + 1);
    return k;
}

/* comparator over the id field only: keys equal by id may have different
   name bytes, which is what exercises the overwrite key-dtor path */
static int cmp_strkey_id(const void* a, const void* b) {
    const StrKey* x = (const StrKey*)a;
    const StrKey* y = (const StrKey*)b;
    return (x->id > y->id) - (x->id < y->id);
}


/* nesting: value struct carries a child map pointer */
typedef struct {
    int id;
    CWValueMap_t* child;
} Nested;

static int child_freed = 0;

void nested_value_dtor(void* mem) {
    Nested* n = (Nested*)mem;
    if (n->child) {
        cwvmap_destroy(n->child);
        n->child = NULL;
        child_freed++;
    }
}


static int pass = 0, fail = 0;


#define T(name, cond) do { \
    if (cond) { printf("  [PASS] %s\n", name); pass++; } \
    else      { printf("  [FAIL] %s\n", name); fail++; } \
} while (0)


/* red-black tree invariant checker */

static int rb_bad = 0;

static size_t rb_black_height(const CWValueMapNode_t* n) {
    if (!n) return 1;
    if (n->color == CWVALUEMAP_RED) {
        if (n->left  && n->left->color  == CWVALUEMAP_RED) rb_bad = 1;
        if (n->right && n->right->color == CWVALUEMAP_RED) rb_bad = 1;
    }
    if (n->left  && n->left->parent  != n) rb_bad = 1;
    if (n->right && n->right->parent != n) rb_bad = 1;
    const size_t l = rb_black_height(n->left);
    const size_t r = rb_black_height(n->right);
    if (l != r) rb_bad = 1;
    return l + (n->color == CWVALUEMAP_BLACK);
}


/* full check: colors, parent links, black height, order, count */
static int rb_verify(CWValueMap_t* m) {
    rb_bad = 0;
    if (!m->root) return 1;
    if (m->root->color != CWVALUEMAP_BLACK) return 0;
    rb_black_height(m->root);
    if (rb_bad) return 0;

    const void* prev = NULL;
    size_t cnt = 0;
    for (CWValueMapIter_t it = cwvmap_begin(m);
         cwvmap_iter_valid(&it); cwvmap_iter_next(&it)) {
        const void* k = cwvmap_iter_key(&it);
        if (prev && cwvmap_compare_keys(m, prev, k) >= 0) return 0;
        prev = k;
        cnt++;
    }
    if (cnt != cwvmap_size(m)) return 0;
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
static size_t test_slot_of(CWValueMap_t* m, const CWValueMapNode_t* n) {
    size_t idx = 0;
    for (CWValueMapBlock_t* b = m->pool; b; b = b->next, idx++) {
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
 * Deep audit: RB colors/parents/black-height, in-order ordering, count, and
 * free-list consistency (no duplicates, free_count matches, live nodes are
 * never marked free, count + free_count == capacity).
 */
static int test_audit(CWValueMap_t* m) {
    if (m->root && m->root->color != CWVALUEMAP_BLACK) return 0;
    rb_bad = 0;
    rb_black_height(m->root);
    if (rb_bad) return 0;

    const void* prev = NULL;
    size_t cnt = 0;
    for (CWValueMapIter_t it = cwvmap_begin(m);
         cwvmap_iter_valid(&it); cwvmap_iter_next(&it)) {
        const void* k = cwvmap_iter_key(&it);
        if (prev && cwvmap_compare_keys(m, prev, k) >= 0) return 0;
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
    for (CWValueMapNode_t* n = m->free_list; n; n = n->parent) {
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
    for (CWValueMapNode_t* n = cwvmap_tree_minimum(m->root);
         n; n = cwvmap_successor(n)) {
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
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CWValueMap (value-copy red-black map) tests:\n\n");

    /* create */
    printf(" - cwvmap_create\n");
    CWValueMap_t* m = cwvmap_create(sizeof(int), sizeof(Value));
    T("create != NULL",        m != NULL);
    T("size == 0",             cwvmap_size(m) == 0);
    T("empty",                 cwvmap_empty(m));
    T("empty map capacity == 0 (lazy pool)", cwvmap_capacity(m) == 0);
    T("empty map free_count == 0", cwvmap_free_count(m) == 0);
    int zk = 0;
    Value zv = { 0, 0.0 };
    T("first put allocates block",
        cwvmap_put(m, &zk, &zv) != NULL);
    T("capacity == 1024 after first put", cwvmap_capacity(m) == 1024);
    T("free_count == 1023 after first put", cwvmap_free_count(m) == 1023);
    T("key_size/value_size",
        cwvmap_key_size(m) == sizeof(int)
        && cwvmap_value_size(m) == sizeof(Value));
    T("create(0, x) == NULL", cwvmap_create(0, sizeof(Value)) == NULL);

    CWValueMap_t* tiny = cwvmap_create_ex(sizeof(int), sizeof(Value), 16);
    T("create_ex(16) starts empty (cap == 0)",
        tiny != NULL && cwvmap_capacity(tiny) == 0);
    T("create_ex(16) first put -> cap 16",
        tiny != NULL && cwvmap_put(tiny, &zk, &zv) != NULL
        && cwvmap_capacity(tiny) == 16);
    cwvmap_destroy(tiny);

    /* value-copy semantics: stack locals survive their function */
    printf("\n - value-copy semantics\n");
    {
        int k = 42;
        Value v = { 7, 3.5 };
        cwvmap_put(m, &k, &v);
        /* locals go out of scope here */
    }
    {
        int k2 = 42; /* fresh stack address, same bytes */
        Value* vp = (Value*)cwvmap_get(m, &k2);
        T("stack-local entry survives scope (copied)",
          vp != NULL && vp->id == 7 && vp->val == 3.5);
        const char* base = (const char*)m->pool + m->pool_mem_offset;
        const char* end  = base + m->pool_block_count * m->node_stride;
        T("stored value address is inside the pool",
          vp != NULL && (const char*)vp >= base && (const char*)vp < end);
    }

    /* put / get / contains */
    printf("\n - put / get / contains\n");
    for (int i = 0; i < 100; i++) {
        Value v = { i, (double)i * 1.5 };
        T("put ok", cwvmap_put(m, &i, &v) != NULL);
    }
    T("size == 100", cwvmap_size(m) == 100);
    int k50 = 50;
    Value* v50 = (Value*)cwvmap_get(m, &k50);
    T("get(50).id == 50",  v50 && v50->id == 50);
    T("get(50).val == 75", v50 && v50->val == 75.0);
    int kbad = 1000;
    T("get(missing) == NULL", cwvmap_get(m, &kbad) == NULL);
    T("contains(50)",       cwvmap_contains(m, &k50));
    T("!contains(1000)",    !cwvmap_contains(m, &kbad));

    /* overwrite semantics */
    printf("\n - overwrite semantics\n");
    Value nv = { 500, 9.25 };
    void* pv = cwvmap_put(m, &k50, &nv);
    T("put overwrite != NULL", pv != NULL);
    T("size still 100",       cwvmap_size(m) == 100);
    T("value updated",        ((Value*)pv)->id == 500
                              && ((Value*)pv)->val == 9.25);
    T("get returns same addr", cwvmap_get(m, &k50) == pv);

    /* insert-only */
    printf("\n - cwvmap_insert (no overwrite)\n");
    int k999 = 999;
    Value iv = { 999, 0 };
    T("insert new key ok",    cwvmap_insert(m, &k999, &iv) != NULL);
    T("insert existing NULL", cwvmap_insert(m, &k50, &iv) == NULL);
    T("existing value intact", ((Value*)cwvmap_get(m, &k50))->id == 500);
    T("size == 101", cwvmap_size(m) == 101);

    /* remove / take */
    printf("\n - remove / take\n");
    int kr = 10;
    T("remove ok",            cwvmap_remove(m, &kr));
    T("size == 100",          cwvmap_size(m) == 100);
    T("removed gone",         !cwvmap_contains(m, &kr));
    T("remove missing false", !cwvmap_remove(m, &kr));
    int kr2 = 20;
    int out_key = 0;
    Value out_val;
    T("take ok",              cwvmap_take(m, &kr2, &out_key, &out_val));
    T("taken key/value",      out_key == 20 && out_val.id == 20
                              && out_val.val == 30.0);
    T("taken gone",           !cwvmap_contains(m, &kr2));
    T("size == 99",           cwvmap_size(m) == 99);
    T("take missing false",   !cwvmap_take(m, &kr2, &out_key, &out_val));

    /* iteration */
    printf("\n - iteration\n");
    int prev_k = -1, ordered = 1, it_count = 0;
    for (CWValueMapIter_t it = cwvmap_begin(m);
         cwvmap_iter_valid(&it); cwvmap_iter_next(&it)) {
        const int k = *(const int*)cwvmap_iter_key(&it);
        if (k <= prev_k) ordered = 0;
        prev_k = k;
        it_count++;
    }
    T("iter count == 99",  it_count == 99);
    T("ascending order",   ordered);

    int rev_ok = 1, rev_count = 0;
    prev_k = INT32_MAX;
    for (CWValueMapIter_t it = cwvmap_rbegin(m);
         cwvmap_iter_valid(&it); cwvmap_iter_prev(&it)) {
        const int k = *(const int*)cwvmap_iter_key(&it);
        if (k >= prev_k) rev_ok = 0;
        prev_k = k;
        rev_count++;
    }
    T("reverse count == 99", rev_count == 99);
    T("descending order",     rev_ok);

    int kf = 42;
    CWValueMapIter_t fit = cwvmap_find(m, &kf);
    T("find valid",       cwvmap_iter_valid(&fit));
    T("find key matches", cwvmap_iter_valid(&fit)
                          && *(const int*)cwvmap_iter_key(&fit) == 42);
    CWValueMapIter_t fit2 = cwvmap_find(m, &(int){ 777 });
    T("find missing invalid", !cwvmap_iter_valid(&fit2));

    /* RB invariants */
    printf("\n - red-black invariants\n");
    T("rb invariants after mixed ops", rb_verify(m));

    /* clear & reuse */
    printf("\n - clear & reuse\n");
    const size_t cap_before = cwvmap_capacity(m);
    cwvmap_clear(m);
    T("size == 0 after clear", cwvmap_size(m) == 0);
    T("empty after clear",     cwvmap_empty(m));
    T("capacity unchanged",    cwvmap_capacity(m) == cap_before);
    T("free_count == capacity", cwvmap_free_count(m) == cap_before);
    for (int i = 0; i < 100; i++) {
        Value v = { i, (double)i };
        cwvmap_put(m, &i, &v);
    }
    T("re-push ok",            cwvmap_size(m) == 100);
    T("no growth after reuse", cwvmap_capacity(m) == cap_before);
    T("rb ok after reuse",     rb_verify(m));

    /* recycling */
    printf("\n - node recycling\n");
    for (int i = 0; i < 100; i++) {
        cwvmap_remove(m, &i);
    }
    T("drained",                cwvmap_size(m) == 0);
    T("free_count == capacity", cwvmap_free_count(m) == cwvmap_capacity(m));
    for (int i = 0; i < 100; i++) {
        Value v = { i, (double)i };
        cwvmap_put(m, &i, &v);
    }
    T("recycle keeps capacity", cwvmap_capacity(m) == cap_before);
    T("rb ok after recycle",    rb_verify(m));
    cwvmap_destroy(m);

    /* default memcmp comparator with fixed-size string keys */
    printf("\n - default memcmp comparator (string keys)\n");
    CWValueMap_t* sm = cwvmap_create(16, sizeof(int));
    T("string map create", sm != NULL);
    for (int i = 0; i < 200; i++) {
        char key[16] = {0};
        snprintf(key, sizeof(key), "key%04d", i);
        cwvmap_put(sm, key, &i);
    }
    T("string map size == 200", cwvmap_size(sm) == 200);
    char ktest[16] = "key0042";
    T("string get == 42", *(int*)cwvmap_get(sm, ktest) == 42);
    char kbad2[16] = "key9999";
    T("string missing NULL", cwvmap_get(sm, kbad2) == NULL);
    int str_ordered = 1;
    char last[16] = "";
    for (CWValueMapIter_t it = cwvmap_begin(sm);
         cwvmap_iter_valid(&it); cwvmap_iter_next(&it)) {
        if (last[0] && strcmp((char*)cwvmap_iter_key(&it), last) <= 0) {
            str_ordered = 0;
        }
        snprintf(last, sizeof(last), "%s", (char*)cwvmap_iter_key(&it));
    }
    T("string keys ascending", str_ordered);
    cwvmap_destroy(sm);

    /* zero-size values (set-like usage) */
    printf("\n - zero-size value\n");
    CWValueMap_t* zs = cwvmap_create(sizeof(int), 0);
    T("create with value_size 0", zs != NULL);
    int zk2 = 5;
    T("put zero-value",   cwvmap_put(zs, &zk2, NULL) != NULL);
    T("get zero-value non-NULL", cwvmap_get(zs, &zk2) != NULL);
    T("contains",         cwvmap_contains(zs, &zk2));
    T("remove zero-value", cwvmap_remove(zs, &zk2));
    cwvmap_destroy(zs);

    /* alignment */
    printf("\n - alignment\n");
    CWValueMap_t* al = cwvmap_create_ex(sizeof(int), sizeof(Value), 64);
    const size_t need = _Alignof(CWValueMap_MAX_ALIGN_T);
    int aligned_ok = 1;
    for (int i = 0; i < 1000; i++) {
        Value v = { i, (double)i };
        cwvmap_put(al, &i, &v);
    }
    for (CWValueMapIter_t it = cwvmap_begin(al);
         cwvmap_iter_valid(&it); cwvmap_iter_next(&it)) {
        void* kp = cwvmap_iter_key(&it);
        void* vp = cwvmap_iter_value(&it);
        if ((((uintptr_t)kp) & (need - 1)) != 0 ||
            (((uintptr_t)vp) & (need - 1)) != 0) {
            aligned_ok = 0;
            break;
        }
    }
    T("key & value aligned to max_align_t", aligned_ok);
    T("rb ok after 1000 puts", rb_verify(al));
    cwvmap_destroy(al);

    /* safe variants: automatic key/value cleanup */
    printf("\n - safe variants (value destructor)\n");
    CWValueMap_t* sa = cwvmap_safe_create(sizeof(int), sizeof(Owned),
                                          NULL, owned_dtor);
    T("safe_create != NULL", sa != NULL);
    T("safe_create(NULL,NULL) == NULL",
        cwvmap_safe_create(sizeof(int), sizeof(Owned), NULL, NULL) == NULL);
    for (int i = 10; i <= 40; i += 10) {
        Owned o;
        o.id = i;
        o.buf = (char*)malloc(16);
        if (o.buf) snprintf(o.buf, 16, "buf%d", i);
        cwvmap_put(sa, &i, &o);
    }
    T("size == 4", cwvmap_size(sa) == 4);
    int k10 = 10;
    Owned no = { 99, NULL };
    no.buf = (char*)malloc(4);
    if (no.buf) memcpy(no.buf, "new", 4);
    cwvmap_put(sa, &k10, &no);
    T("put overwrite calls dtor (owned_freed == 1)", owned_freed == 1);
    int k20 = 20;
    T("remove calls dtor (owned_freed == 2)",
        cwvmap_remove(sa, &k20) && owned_freed == 2);
    int k30 = 30;
    Owned ocopy;
    T("take ok", cwvmap_take(sa, &k30, NULL, &ocopy));
    T("take transfers (owned_freed == 2)", owned_freed == 2);
    T("taken buf live", ocopy.buf != NULL);
    free(ocopy.buf);
    cwvmap_clear(sa);
    T("clear calls dtor on rest (owned_freed == 4)", owned_freed == 4);
    for (int i = 0; i < 3; i++) {
        Owned o;
        o.id = i;
        o.buf = (char*)malloc(4);
        if (o.buf) memcpy(o.buf, "x", 2);
        cwvmap_put(sa, &i, &o);
    }
    cwvmap_destroy(sa);
    T("destroy frees all (owned_freed == 7)", owned_freed == 7);

    /* key destructor */
    printf("\n - safe variants (key destructor)\n");
    CWValueMap_t* sk = cwvmap_safe_create_cmp(sizeof(StrKey), sizeof(int),
                                              cmp_strkey_id,
                                              strkey_dtor, NULL);
    T("key-safe create != NULL", sk != NULL);
    for (int i = 0; i < 4; i++) {
        StrKey k = new_strkey(i, "key");
        cwvmap_put(sk, &k, &i);
        /* ownership of k.name transfers to the map's embedded copy */
    }
    T("size == 4", cwvmap_size(sk) == 4);
    T("key_dtor not called on fresh puts", key_freed == 0);
    StrKey nk = new_strkey(0, "other"); /* same id, different name bytes */
    cwvmap_put(sk, &nk, &(int){ 9 });
    T("overwrite discards old key bytes (key_freed == 1)", key_freed == 1);
    /* nk.name ownership also transfers to the map; do not free it */
    int k1 = 1;
    T("remove calls key dtor (key_freed == 2)",
        cwvmap_remove(sk, &k1) && key_freed == 2);
    cwvmap_clear(sk);
    T("clear calls key dtor on rest (key_freed == 5)", key_freed == 5);
    cwvmap_destroy(sk);
    T("destroy no-op when empty (key_freed == 5)", key_freed == 5);

    /* raw containers never call dtors */
    CWValueMap_t* raw = cwvmap_create(sizeof(int), sizeof(Owned));
    for (int i = 0; i < 3; i++) {
        Owned o;
        o.id = i;
        o.buf = (char*)malloc(4);
        if (o.buf) memcpy(o.buf, "r", 2);
        cwvmap_put(raw, &i, &o);
    }
    cwvmap_clear(raw);
    cwvmap_destroy(raw);
    T("raw never calls dtor (owned_freed == 7)", owned_freed == 7);

    /* nesting */
    printf("\n - nesting (child maps inside value structs)\n");
    CWValueMap_t* outer = cwvmap_safe_create(sizeof(int), sizeof(Nested),
                                             NULL, nested_value_dtor);
    T("outer create", outer != NULL);
    for (int i = 0; i < 5; i++) {
        CWValueMap_t* child = cwvmap_create(sizeof(int), sizeof(int));
        for (int j = 0; j < 10; j++) {
            cwvmap_put(child, &j, &(int){ i * 100 + j });
        }
        Nested n;
        n.id = i;
        n.child = child;
        cwvmap_put(outer, &i, &n);
    }
    T("outer size == 5", cwvmap_size(outer) == 5);
    int c3 = 3;
    Nested* n3 = (Nested*)cwvmap_get(outer, &c3);
    T("nested get", n3 && n3->child != NULL);
    T("child lookup works", n3 && cwvmap_contains(n3->child, &(int){ 7 }));
    T("child value correct",
        n3 && *(int*)cwvmap_get(n3->child, &(int){ 7 }) == 307);
    T("child_freed == 0", child_freed == 0);
    Nested extracted;
    T("take child", cwvmap_take(outer, &c3, NULL, &extracted));
    T("extracted usable", extracted.child && cwvmap_size(extracted.child) == 10);
    T("take did not free (child_freed == 0)", child_freed == 0);
    cwvmap_destroy(extracted.child);
    T("manual destroy not counted (child_freed == 0)", child_freed == 0);
    cwvmap_destroy(outer);
    T("outer destroy frees rest (child_freed == 4)", child_freed == 4);

    /* shrink_to_fit */
    printf("\n - cwvmap_shrink_to_fit\n");
    CWValueMap_t* sh = cwvmap_create_ex(sizeof(int), sizeof(Value), 16);
    T("shrink no-op on 1 block", cwvmap_shrink_to_fit(sh));
    for (int i = 0; i < 1000; i++) {
        Value v = { i, (double)i };
        cwvmap_put(sh, &i, &v);
    }
    T("blocks grew to 63", sh->block_count == 63);
    for (int i = 0; i < 900; i++) {
        cwvmap_remove(sh, &i);
    }
    T("size == 100", cwvmap_size(sh) == 100);
    T("shrink ok", cwvmap_shrink_to_fit(sh));
    T("blocks after == 7", sh->block_count == 7);
    int shrink_ok = 1;
    for (int i = 900; i < 1000; i++) {
        Value* v = (Value*)cwvmap_get(sh, &i);
        if (!v || v->id != i) shrink_ok = 0;
    }
    T("live entries intact", shrink_ok);
    T("rb ok after shrink", rb_verify(sh));
    for (int i = 1000; i < 1100; i++) {
        Value v = { i, 0 };
        cwvmap_put(sh, &i, &v);
    }
    T("push after shrink ok", cwvmap_size(sh) == 200);
    cwvmap_clear(sh);
    T("shrink empty ok", cwvmap_shrink_to_fit(sh));
    T("empty shrink keeps 1 block", sh->block_count == 1);
    int z = 1234;
    Value zv2 = { 7, 0 };
    cwvmap_put(sh, &z, &zv2);
    T("push after empty shrink ok", cwvmap_size(sh) == 1);
    cwvmap_destroy(sh);

    /* random differential fuzz vs model */
    printf("\n - random fuzz (20000 ops vs model)\n");
    CWValueMap_t* fz = cwvmap_create_cmp(sizeof(int), sizeof(int), cmp_int);
    model_n = 0;
    int fuzz_ok = 1;
    for (int op = 0; op < 20000; op++) {
        const int k = (int)(rng_next() % MODEL_CAP);
        const int t = (int)(rng_next() % 3);
        switch (t) {
        case 0:
            cwvmap_put(fz, &k, &(int){ k * 2 });
            model_insert(k);
            break;
        case 1:
            if (cwvmap_remove(fz, &k)) model_remove(k);
            break;
        default: {
            const int in_model = model_find(k) >= 0;
            if (cwvmap_contains(fz, &k) != (in_model != 0)) fuzz_ok = 0;
            if (in_model) {
                const int* v = (const int*)cwvmap_get(fz, &k);
                if (!v || *v != k * 2) fuzz_ok = 0;
            }
            break;
        }
        }
        if ((op & 511) == 0) {
            if (cwvmap_size(fz) != model_n) fuzz_ok = 0;
            if (!rb_verify(fz)) fuzz_ok = 0;
        }
    }
    T("fuzz size matches model", cwvmap_size(fz) == model_n);
    T("fuzz invariants hold", fuzz_ok && rb_verify(fz));
    int fuzz_iter = 0, fuzz_asc = 1;
    prev_k = -1;
    for (CWValueMapIter_t it = cwvmap_begin(fz);
         cwvmap_iter_valid(&it); cwvmap_iter_next(&it)) {
        const int k = *(const int*)cwvmap_iter_key(&it);
        if (k <= prev_k) fuzz_asc = 0;
        prev_k = k;
        fuzz_iter++;
    }
    T("fuzz iteration matches model", fuzz_iter == (int)model_n);
    T("fuzz ascending order", fuzz_asc);
    cwvmap_destroy(fz);

    /* structured fuzz (3M ops + full audit) */
    printf("\n - structured fuzz (3M ops + full audit)\n");
    CWValueMap_t* sf = cwvmap_create_ex(sizeof(int), sizeof(int), 7);
    model_n = 0;
    int sf_ok = 1;
    for (long op = 0; op < 3000000; op++) {
        const int k = (int)(rng_next() % MODEL_CAP);
        const int t = (int)(rng_next() % 10);
        if (t < 4) {
            model_insert(k);
            cwvmap_put(sf, &k, &(int){ k });
        } else if (t < 8) {
            model_remove(k);
            cwvmap_remove(sf, &k);
        } else if (t == 8) {
            cwvmap_shrink_to_fit(sf);
        } else {
            cwvmap_clear(sf);
            model_n = 0;
        }
        if ((op & 0x3FFF) == 0) {
            if (cwvmap_size(sf) != model_n) {
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
      sf_ok && cwvmap_size(sf) == model_n && test_audit(sf));
    cwvmap_destroy(sf);

    /* big stress */
    printf("\n - big stress (100k)\n");
    CWValueMap_t* big = cwvmap_create_cmp(sizeof(int), sizeof(int), cmp_int);
    int push_ok = 1;
    for (int i = 0; i < 100000; i++) {
        if (!cwvmap_put(big, &i, &i)) {
            push_ok = 0;
            break;
        }
    }
    T("100k puts ok", push_ok && cwvmap_size(big) == 100000);
    T("rb ok after 100k", rb_verify(big));
    int kk = 65535;
    T("spot get(65535)", *(int*)cwvmap_get(big, &kk) == 65535);
    kk = 0;
    T("spot get(0)", *(int*)cwvmap_get(big, &kk) == 0);
    kk = 99999;
    T("spot get(99999)", *(int*)cwvmap_get(big, &kk) == 99999);
    for (int i = 0; i < 100000; i += 2) {
        cwvmap_remove(big, &i);
    }
    T("50k removes ok", cwvmap_size(big) == 50000);
    T("rb ok after removes", rb_verify(big));
    int count = 0;
    for (CWValueMapIter_t it = cwvmap_begin(big);
         cwvmap_iter_valid(&it); cwvmap_iter_next(&it)) {
        count++;
    }
    T("iter == 50000", count == 50000);
    cwvmap_destroy(big);

    /* memory footprint */
    printf("\n - memory footprint\n");
    {
        CWValueMap_t* fm = cwvmap_create(sizeof(int), sizeof(int));
        const size_t block_total =
            fm->pool_mem_offset + fm->pool_block_count * fm->node_stride;
        printf("  sizeof(CWValueMapNode_t)=%zu node_stride=%zu "
               "nodes/block=%zu block_total=%zu bytes\n",
               sizeof(CWValueMapNode_t), fm->node_stride,
               fm->pool_block_count, block_total);
        printf("  after create: block_count=%zu committed=%zu bytes (%.1f KB)\n",
               fm->block_count, fm->block_count * block_total,
               (fm->block_count * block_total) / 1024.0);
        T("empty map commits nothing", fm->block_count == 0);
        int kk2 = 1;
        cwvmap_put(fm, &kk2, &kk2);
        printf("  after 1st put: block_count=%zu committed=%zu bytes (%.1f KB)\n",
               fm->block_count, fm->block_count * block_total,
               (fm->block_count * block_total) / 1024.0);
        T("first put commits one block", fm->block_count == 1);
        cwvmap_destroy(fm);
    }

    /* benchmark (informational; define CWValueMap_TEST_SKIP_BENCH to skip) */
    printf("\n - benchmark (informational, N=1M; "
           "define CWValueMap_TEST_SKIP_BENCH to skip)\n");
#ifndef CWValueMap_TEST_SKIP_BENCH
    {
        enum { BENCH_N = 1000000 };
        CWValueMap_t* bm = cwvmap_create_cmp(sizeof(int), sizeof(int), cmp_int);
        for (int i = 0; i < BENCH_N; i++) {
            cwvmap_put(bm, &i, &i);
        }
        double t0 = now_ms();
        for (int r = 0; r < 10; r++) {
            for (int i = 0; i < BENCH_N; i++) {
                cwvmap_put(bm, &i, &i);
            }
        }
        const double ow_ms = now_ms() - t0;

        t0 = now_ms();
        for (int r = 0; r < 10; r++) {
            for (int i = 0; i < BENCH_N; i++) {
                cwvmap_get(bm, &i);
            }
        }
        const double gt_ms = now_ms() - t0;

        t0 = now_ms();
        for (int r = 0; r < 10; r++) {
            for (int i = 0; i < BENCH_N; i++) {
                cwvmap_remove(bm, &i);
            }
        }
        const double rm_ms = now_ms() - t0;
        printf("  overwrite put: %.1f ms/10M   (get baseline: %.1f ms/10M)\n",
               ow_ms, gt_ms);
        printf("  remove       : %.1f ms/10M\n", rm_ms);
        cwvmap_destroy(bm);

        CWValueMap_t* bs = cwvmap_create_ex(sizeof(int), sizeof(int), 1024);
        for (int i = 0; i < BENCH_N; i++) {
            cwvmap_put(bs, &i, &i);
        }
        for (int i = 0; i < BENCH_N - 1000; i++) {
            cwvmap_remove(bs, &i);
        }
        t0 = now_ms();
        cwvmap_shrink_to_fit(bs);
        printf("  shrink_to_fit: %.1f ms for %zu live nodes in %zu blocks\n",
               now_ms() - t0, cwvmap_size(bs), bs->block_count);
        cwvmap_destroy(bs);
    }
#else
    printf("  skipped\n");
#endif

    /* destroy NULL */
    printf("\n - cwvmap_destroy(NULL)\n");
    cwvmap_destroy(NULL);
    T("destroy(NULL) no crash", 1);

    printf("\nResults: %d PASS, %d FAIL\n", pass, fail);
    return fail ? 1 : 0;
}
