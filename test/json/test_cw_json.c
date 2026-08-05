#define CW_JSON_IMPLEMENTATION
#include "cwind_json.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_pass = 0;
static int g_fail = 0;

typedef struct {
    cw_event_type type[64];
    int64_t i[64];
    char key[64][32];
    int depth[64];
    int n;
} rec;

typedef struct {
    rec *r;
} ctx_t;

int sax_collect(void *ud, const cw_event *ev) {
    ctx_t *c = (ctx_t *)ud;
    rec *r = c->r;
    if (r->n >= 64) return -1;
    r->type[r->n] = ev->type;
    r->depth[r->n] = ev->depth;
    switch (ev->type) {
    case CW_EVT_INT:
        r->i[r->n] = ev->u.i;
        break;
    case CW_EVT_KEY:
    case CW_EVT_STRING: {
        size_t k = ev->u.str.len < 31 ? ev->u.str.len : 31;
        memcpy(r->key[r->n], ev->u.str.data, k);
        r->key[r->n][k] = '\0';
        break;
    }
    default:
        break;
    }
    r->n++;
    return 0;
}

#define CHECK(cond)                                                           \
    do {                                                                      \
        if (cond) {                                                           \
            g_pass++;                                                         \
        } else {                                                              \
            g_fail++;                                                         \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);            \
        }                                                                     \
    } while (0)

static int str_eq(const char *a, const char *b) {
    return a && b && strcmp(a, b) == 0;
}

/* ------------------------------------------------------------------ */
static void test_parse_basic(void) {
    cw_doc *d = cw_parse_cstr(
        "{\"name\":\"cw\",\"version\":1,\"ok\":true,\"no\":false,"
        "\"pi\":3.14,\"nil\":null,\"list\":[1,2,3],"
        "\"nested\":{\"a\":{\"b\":42}}}");
    CHECK(d != NULL);
    if (!d) return;
    cw_value *root = cw_doc_root(d);
    CHECK(cw_typeof(root) == CW_OBJECT);
    CHECK(str_eq(cw_string_cstr(cw_object_get(root, "name")), "cw"));
    int64_t iv = 0;
    CHECK(cw_as_int(cw_object_get(root, "version"), &iv) == CW_OK && iv == 1);
    bool bv = false;
    CHECK(cw_as_bool(cw_object_get(root, "ok"), &bv) == CW_OK && bv);
    CHECK(cw_as_bool(cw_object_get(root, "no"), &bv) == CW_OK && !bv);
    double dv = 0;
    CHECK(cw_as_double(cw_object_get(root, "pi"), &dv) == CW_OK &&
          fabs(dv - 3.14) < 1e-12);
    CHECK(cw_typeof(cw_object_get(root, "nil")) == CW_NULL);
    cw_value *list = cw_object_get(root, "list");
    CHECK(cw_typeof(list) == CW_ARRAY && cw_array_size(list) == 3);
    CHECK(cw_as_int(cw_array_get(list, 2), &iv) == CW_OK && iv == 3);
    cw_value *nested = cw_object_get(root, "nested");
    CHECK(cw_as_int(cw_object_get(cw_object_get(nested, "a"), "b"), &iv) ==
              CW_OK &&
          iv == 42);
    CHECK(cw_get_path(root, "nested.a.b") == cw_object_get(
                                                 cw_object_get(nested, "a"),
                                                 "b"));
    CHECK(cw_get_path(root, "list[1]") == cw_array_get(list, 1));
    CHECK(cw_get_path(root, "missing.x") == NULL);
    cw_doc_free(d);
}

/* ------------------------------------------------------------------ */
static void test_escapes_unicode(void) {
    cw_doc *d = cw_parse_cstr(
        "{\"s\":\"a\\\"b\\\\c\\/d\\n\\t\\r\\b\\f"
        "\\u0041\\u00e9\\ud83d\\ude00\"}");
    CHECK(d != NULL);
    if (!d) return;
    cw_value *s = cw_object_get(cw_doc_root(d), "s");
    size_t len = 0;
    const char *data = cw_string_value(s, &len);
    CHECK(data != NULL);
    static const char expected[] =
        "a\"b\\c/d\n\t\r\b\fA\xC3\xA9\xF0\x9F\x98\x80";
    CHECK(len == sizeof(expected) - 1 && memcmp(data, expected, len) == 0);
    cw_doc_free(d);

    /* embedded NUL via \u0000 */
    d = cw_parse_cstr("{\"x\":\"a\\u0000b\"}");
    CHECK(d != NULL);
    if (!d) return;
    s = cw_object_get(cw_doc_root(d), "x");
    len = 0;
    data = cw_string_value(s, &len);
    CHECK(len == 3 && data[0] == 'a' && data[1] == '\0' && data[2] == 'b');
    cw_doc_free(d);
}

/* ------------------------------------------------------------------ */
static void test_numbers(void) {
    cw_doc *d = cw_parse_cstr(
        "[0,-0,1,-1,2147483647,9223372036854775807,"
        "-9223372036854775808,9223372036854775808,1.5,-2.25,1e10,"
        "1E-3,0.1,1e999]");
    CHECK(d != NULL);
    if (!d) return;
    cw_value *arr = cw_doc_root(d);
    int64_t iv = 0;
    double dv = 0;
    CHECK(cw_as_int(cw_array_get(arr, 0), &iv) == CW_OK && iv == 0);
    CHECK(cw_as_int(cw_array_get(arr, 1), &iv) == CW_OK && iv == 0);
    CHECK(cw_as_int(cw_array_get(arr, 2), &iv) == CW_OK && iv == 1);
    CHECK(cw_as_int(cw_array_get(arr, 3), &iv) == CW_OK && iv == -1);
    CHECK(cw_as_int(cw_array_get(arr, 4), &iv) == CW_OK && iv == 2147483647);
    CHECK(cw_as_int(cw_array_get(arr, 5), &iv) == CW_OK &&
          iv == 9223372036854775807LL);
    CHECK(cw_as_int(cw_array_get(arr, 6), &iv) == CW_OK &&
          iv == (-9223372036854775807LL - 1));
    CHECK(cw_typeof(cw_array_get(arr, 7)) == CW_DOUBLE);
    CHECK(cw_as_double(cw_array_get(arr, 7), &dv) == CW_OK &&
          dv == 9.223372036854775808e18);
    CHECK(cw_typeof(cw_array_get(arr, 8)) == CW_DOUBLE);
    CHECK(cw_as_double(cw_array_get(arr, 8), &dv) == CW_OK && dv == 1.5);
    CHECK(cw_as_double(cw_array_get(arr, 9), &dv) == CW_OK && dv == -2.25);
    CHECK(cw_as_double(cw_array_get(arr, 10), &dv) == CW_OK && dv == 1e10);
    CHECK(cw_as_double(cw_array_get(arr, 11), &dv) == CW_OK &&
          fabs(dv - 1e-3) < 1e-15);
    CHECK(cw_as_double(cw_array_get(arr, 12), &dv) == CW_OK && dv == 0.1);
    CHECK(cw_as_double(cw_array_get(arr, 13), &dv) == CW_OK && isinf(dv));
    cw_doc_free(d);

    /* shortest round-trip dump */
    d = cw_parse_cstr("[0.1,1.0,1e20,0.30000000000000004]");
    size_t n = 0;
    char *out = cw_dump_string(d, cw_doc_root(d), 0, &n);
    CHECK(str_eq(out, "[0.1,1,1e+20,0.30000000000000004]"));
    cw_doc_free(d);
}

/* ------------------------------------------------------------------ */
static void test_invalid_inputs(void) {
    static const char *const bad[] = {
        "",         "x",        "{",        "[",        "]",
        "}",        "{\"a\"}",  "{\"a\":}", "{\"a\":1,}", "[1,]",
        "[,1]",     "[1 2]",    "{'a':1}",  "{\"a\" 1}", "01",
        "-",        "+1",       "1.",       ".5",       "1e",
        "1e+",      "nan",      "NaN",      "Infinity",  "\"abc",
        "\"\\x\"",  "\"\\u12G4\"",
        "\"\\ud800\"",          /* lone high surrogate */
        "\"\\udc00\"",          /* lone low surrogate */
        "\"a\nb\"",             /* raw newline in string */
        "truex",    "tru",      "[1,2] extra", "{\"a\":1} \"b\"",
        "\x01",                 /* raw control character */
        "1a",       "{} {}",    "{\"a\":1",
    };
    for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); ++i) {
        cw_error err;
        int r = cw_validate(bad[i], strlen(bad[i]), &err);
        CHECK(r != CW_OK);
        if (r == CW_OK)
            printf("    unexpectedly valid: [%s]\n", bad[i]);
        cw_doc *d = cw_parse_cstr(bad[i]);
        CHECK(d == NULL);
    }

    /* error position + code */
    cw_error err;
    int r = cw_validate("[1,]", 4, &err);
    CHECK(r == CW_ERR_TRAILING_COMMA);
    CHECK(err.line == 1 && err.col == 4);
    r = cw_validate("{\"a\":1,}", 8, &err);
    CHECK(r == CW_ERR_TRAILING_COMMA && err.col == 8);
}

/* ------------------------------------------------------------------ */
static void test_streaming_dom(void) {
    const char *json =
        "{\"a\":[1,2,{\"b\":\"hi\"}],\"c\":true,\"s\":\"\\ud83d\\ude00\"}";
    /* reference full parse */
    cw_doc *ref = cw_parse_cstr(json);
    CHECK(ref != NULL);
    if (!ref) return;
    /* feed one byte at a time */
    cw_doc *d = cw_doc_new();
    CHECK(cw_doc_parse_begin(d) == CW_OK);
    size_t len = strlen(json);
    int ok = 1;
    for (size_t i = 0; i < len; ++i) {
        if (cw_doc_parse_chunk(d, json + i, 1) < 0) {
            ok = 0;
            break;
        }
    }
    CHECK(ok);
    CHECK(cw_doc_parse_end(d) == CW_OK);
    CHECK(cw_value_equal(cw_doc_root(d), cw_doc_root(ref)));
    cw_doc_free(d);

    /* feed with a variety of chunk sizes */
    for (int step = 2; step <= 7; ++step) {
        d = cw_doc_new();
        CHECK(cw_doc_parse_begin(d) == CW_OK);
        int r = CW_OK;
        for (size_t i = 0; i < len; i += (size_t)step) {
            size_t n = (size_t)step;
            if (i + n > len) n = len - i;
            r = cw_doc_parse_chunk(d, json + i, n);
            if (r < 0) break;
        }
        if (r >= 0) r = cw_doc_parse_end(d);
        CHECK(r == CW_OK);
        if (r == CW_OK)
            CHECK(cw_value_equal(cw_doc_root(d), cw_doc_root(ref)));
        cw_doc_free(d);
    }

    /* split a number across chunks */
    d = cw_doc_new();
    CHECK(cw_doc_parse_begin(d) == CW_OK);
    CHECK(cw_doc_parse_chunk(d, "[123", 4) >= 0);
    CHECK(cw_doc_parse_chunk(d, "45.67", 5) >= 0);
    CHECK(cw_doc_parse_chunk(d, "]", 1) >= 0);
    CHECK(cw_doc_parse_end(d) == CW_OK);
    double dv = 0;
    CHECK(cw_as_double(cw_array_get(cw_doc_root(d), 0), &dv) == CW_OK &&
          dv == 12345.67);
    cw_doc_free(d);

    /* trailing data must fail */
    d = cw_parse_cstr("{} {}");
    CHECK(d == NULL);
    cw_doc_free(d);
    cw_doc_free(ref);
}

/* ------------------------------------------------------------------ */
static void test_sax(void) {
    rec r;
    memset(&r, 0, sizeof(r));
    ctx_t ctx;
    ctx.r = &r;
    cw_sax *s = cw_sax_new(sax_collect, &ctx);
    CHECK(s != NULL);
    const char *json = "{\"a\":[1,2,{\"b\":\"hi\"}],\"c\":true}";
    size_t len = strlen(json);
    for (size_t i = 0; i < len; ++i) {
        int rc = cw_sax_feed(s, json + i, 1);
        CHECK(rc >= 0);
    }
    int rc = cw_sax_finish(s);
    CHECK(rc >= 0);
    CHECK(cw_sax_values(s) == 1);

    static const cw_event_type expect[] = {
        CW_EVT_OBJECT_BEGIN, CW_EVT_KEY,    CW_EVT_ARRAY_BEGIN,
        CW_EVT_INT,          CW_EVT_INT,    CW_EVT_OBJECT_BEGIN,
        CW_EVT_KEY,          CW_EVT_STRING, CW_EVT_OBJECT_END,
        CW_EVT_ARRAY_END,    CW_EVT_KEY,    CW_EVT_BOOL,
        CW_EVT_OBJECT_END,   CW_EVT_DONE,
    };
    CHECK(r.n == 14);
    for (int i = 0; i < r.n && i < 14; ++i) {
        CHECK(r.type[i] == expect[i]);
    }
    /* spot checks */
    CHECK(r.type[3] == CW_EVT_INT && r.i[3] == 1);
    CHECK(r.type[4] == CW_EVT_INT && r.i[4] == 2);
    CHECK(r.type[12] == CW_EVT_OBJECT_END);
    CHECK(r.depth[0] == 0 && r.depth[2] == 1 && r.depth[3] == 2 &&
          r.depth[6] == 3 && r.depth[12] == 0 && r.depth[13] == 0);
    cw_sax_free(s);

    /* multiple top-level values */
    s = cw_sax_new(NULL, NULL);
    CHECK(cw_sax_feed(s, "1 2 3", 5) == CW_DONE);
    CHECK(cw_sax_finish(s) == CW_DONE);
    CHECK(cw_sax_values(s) == 3);
    cw_sax_free(s);
}

/* ------------------------------------------------------------------ */
static void test_operations(void) {
    cw_doc *d = cw_doc_new();
    cw_value *obj = cw_new_object(d);
    cw_doc_set_root(d, obj);
    CHECK(cw_object_set_c(obj, "a", cw_new_int(d, 1)) == CW_OK);
    CHECK(cw_object_set_c(obj, "b", cw_new_string_c(d, "two")) == CW_OK);
    CHECK(cw_object_set_c(obj, "c", cw_new_bool(d, true)) == CW_OK);
    CHECK(cw_object_size(obj) == 3);
    /* duplicate key replaces in place, size unchanged */
    CHECK(cw_object_set_c(obj, "b", cw_new_int(d, 22)) == CW_OK);
    CHECK(cw_object_size(obj) == 3);
    int64_t iv = 0;
    CHECK(cw_as_int(cw_object_get(obj, "b"), &iv) == CW_OK && iv == 22);
    /* get with explicit length (non-NUL key slice) */
    char keybuf[8];
    memcpy(keybuf, "abc", 3);
    CHECK(cw_object_get_len(obj, keybuf, 1) == cw_object_get(obj, "a"));
    CHECK(cw_object_remove(obj, "a") == CW_OK);
    CHECK(cw_object_size(obj) == 2 && cw_object_get(obj, "a") == NULL);
    CHECK(cw_object_remove(obj, "missing") == CW_OK);

    /* arrays */
    cw_value *arr = cw_new_array(d);
    CHECK(cw_object_set_c(obj, "arr", arr) == CW_OK);
    for (int i = 0; i < 10; ++i)
        CHECK(cw_array_append(arr, cw_new_int(d, i)) == CW_OK);
    CHECK(cw_array_size(arr) == 10);
    CHECK(cw_array_insert(arr, 0, cw_new_int(d, 100)) == CW_OK);
    CHECK(cw_array_size(arr) == 11);
    CHECK(cw_as_int(cw_array_get(arr, 0), &iv) == CW_OK && iv == 100);
    CHECK(cw_array_set(arr, 5, cw_new_int(d, 55)) == CW_OK);
    CHECK(cw_as_int(cw_array_get(arr, 5), &iv) == CW_OK && iv == 55);
    CHECK(cw_array_remove(arr, 0) == CW_OK);
    CHECK(cw_as_int(cw_array_get(arr, 0), &iv) == CW_OK && iv == 0);
    cw_array_clear(arr);
    CHECK(cw_array_size(arr) == 0);

    /* deep copy is independent */
    cw_doc *d2 = cw_doc_new();
    cw_value *copy = cw_value_deep_copy(obj, d2);
    CHECK(copy != NULL);
    CHECK(cw_value_equal(obj, copy));
    CHECK(cw_object_remove(copy, "arr") == CW_OK);
    CHECK(!cw_value_equal(obj, copy));
    cw_doc_free(d2);
    cw_doc_free(d);
}

/* ------------------------------------------------------------------ */
static void test_dump(void) {
    cw_doc *d = cw_parse_cstr(
        "{\"a\":1,\"b\":[true,null,\"x\"],\"c\":{\"d\":2.5}}");
    CHECK(d != NULL);
    if (!d) return;
    size_t n = 0;
    char *pretty = cw_dump_string(d, cw_doc_root(d), 4, &n);
    static const char expected[] =
        "{\n"
        "    \"a\": 1,\n"
        "    \"b\": [\n"
        "        true,\n"
        "        null,\n"
        "        \"x\"\n"
        "    ],\n"
        "    \"c\": {\n"
        "        \"d\": 2.5\n"
        "    }\n"
        "}";
    CHECK(pretty != NULL && strcmp(pretty, expected) == 0);
    CHECK(n == sizeof(expected) - 1);

    char *compact = cw_dump_string(d, cw_doc_root(d), 0, &n);
    CHECK(str_eq(compact,
                 "{\"a\":1,\"b\":[true,null,\"x\"],\"c\":{\"d\":2.5}}"));

    /* escaping on output */
    cw_doc *e = cw_parse_cstr("{\"s\":\"a\\n\\t\\u0000\"}");
    char *esc = cw_dump_string(e, cw_doc_root(e), 0, &n);
    CHECK(str_eq(esc, "{\"s\":\"a\\n\\t\\u0000\"}"));
    cw_doc_free(e);

    /* non-finite doubles cannot be serialized */
    cw_value *bad = cw_new_double(d, INFINITY);
    CHECK(cw_dump_string(d, bad, 0, &n) == NULL);
    CHECK(cw_dump_malloc(bad, 0, &n) == NULL);

    /* malloc variant */
    char *m = cw_dump_malloc(cw_doc_root(d), 4, &n);
    CHECK(m != NULL && strcmp(m, expected) == 0);
    free(m);
    cw_doc_free(d);
}

/* ------------------------------------------------------------------ */
static void test_file_io(void) {
    const char *path = "test_out.json";
    cw_doc *d = cw_parse_cstr(
        "{\"user\":\"\\u5f20\\u4e09\",\"tags\":[\"a\",\"b\"],"
        "\"n\":[{\"k\":1},{\"k\":2}]}");
    CHECK(d != NULL);
    if (!d) return;
    CHECK(cw_dump_path(cw_doc_root(d), path, 4) == CW_OK);
    CHECK(cw_validate_file(path, NULL) == CW_OK);

    cw_doc *l = cw_load_file(path);
    CHECK(l != NULL);
    if (l) {
        CHECK(cw_value_equal(cw_doc_root(d), cw_doc_root(l)));
        CHECK(str_eq(cw_string_cstr(cw_object_get(cw_doc_root(l), "user")),
                     "\xE5\xBC\xA0\xE4\xB8\x89"));
    }
    cw_doc_free(l);
    cw_doc_free(d);

    /* top-level array file */
    d = cw_parse_cstr("[{\"x\":1},{\"x\":2}]");
    CHECK(cw_dump_path(cw_doc_root(d), path, 4) == CW_OK);
    l = cw_load_file(path);
    CHECK(l != NULL && cw_array_size(cw_doc_root(l)) == 2);
    cw_doc_free(l);
    cw_doc_free(d);
    remove(path);

    /* missing file */
    cw_error err;
    CHECK(cw_validate_file("no_such_file_xyz.json", &err) == CW_ERR_IO);
}

/* ------------------------------------------------------------------ */
static void test_memory_recycle(void) {
    /* build ~150 KB of JSON text */
    size_t cap = 512 * 1024;
    char *buf = (char *)malloc(cap);
    CHECK(buf != NULL);
    if (!buf) return;
    size_t pos = 0;
#define APPENDF(fmt, ...)                                                     \
    do {                                                                      \
        int n = snprintf(buf + pos, cap - pos, fmt, __VA_ARGS__);             \
        pos += (size_t)n;                                                     \
    } while (0)
#define APPENDS(s)                                                            \
    do {                                                                      \
        size_t n = strlen(s);                                                 \
        memcpy(buf + pos, s, n);                                              \
        pos += n;                                                             \
    } while (0)
    APPENDS("[");
    for (int i = 0; i < 2000; ++i) {
        if (i) APPENDS(",");
        APPENDF("{\"id\":%d,\"name\":\"item%d\",\"ok\":true,\"score\":%.1f}",
                i, i, (double)(i % 10) / 2.0);
    }
    APPENDS("]");
#undef APPENDF
#undef APPENDS

    cw_doc *d = cw_doc_new();
    CHECK(cw_doc_parse(d, buf, pos) == CW_OK);
    size_t m1 = 0, u1 = 0;
    cw_doc_mem_stats(d, &m1, &u1);
    CHECK(m1 > 0 && u1 > 0);
    CHECK(cw_array_size(cw_doc_root(d)) == 2000);

    /* repeated parse recycles the same mapped blocks */
    CHECK(cw_doc_parse(d, buf, pos) == CW_OK);
    size_t m2 = 0, u2 = 0;
    cw_doc_mem_stats(d, &m2, &u2);
    CHECK(m2 == m1);
    CHECK(u2 <= u1);

    cw_doc_clear(d);
    size_t m3 = 0, u3 = 0;
    cw_doc_mem_stats(d, &m3, &u3);
    CHECK(m3 == m1 && u3 == 0);

    /* arena API directly */
    cw_arena *a = cw_arena_new(0);
    CHECK(a != NULL);
    void *p1 = cw_arena_alloc(a, 100);
    void *p2 = cw_arena_alloc(a, 64);
    CHECK(p1 && p2 && p1 != p2);
    size_t am = cw_arena_mapped(a);
    CHECK(am >= 4096);
    cw_arena_reset(a);
    CHECK(cw_arena_used(a) == 0 && cw_arena_mapped(a) == am);
    cw_arena_destroy(a);

    cw_doc_free(d);
    free(buf);
}

/* ------------------------------------------------------------------ */
static void test_iteration(void) {
    cw_doc *d =
        cw_parse_cstr("{\"z\":1,\"a\":2,\"m\":3,\"arr\":[10,20,30]}");
    CHECK(d != NULL);
    if (!d) return;
    const char *key;
    size_t klen = 0;
    cw_value *val = NULL;
    size_t idx = 0;
    const char *order[] = {"z", "a", "m", "arr"};
    int n = 0;
    while (cw_object_iter(cw_doc_root(d), &idx, &key, &klen, &val)) {
        CHECK(n < 4);
        if (n < 4) {
            CHECK(klen == strlen(order[n]) &&
                  memcmp(key, order[n], klen) == 0);
            if (n < 3) {
                int64_t iv = 0;
                CHECK(cw_as_int(val, &iv) == CW_OK);
            } else {
                CHECK(cw_typeof(val) == CW_ARRAY);
            }
        }
        ++n;
    }
    CHECK(n == 4);

    cw_value *arr = cw_object_get(cw_doc_root(d), "arr");
    int64_t sum = 0;
    cw_value *item = NULL;
    CW_ARRAY_FOREACH(arr, i, item) {
        int64_t iv = 0;
        CHECK(cw_as_int(item, &iv) == CW_OK);
        sum += iv;
    }
    CHECK(sum == 60);

    cw_doc_free(d);
}

/* ------------------------------------------------------------------ */
static void test_misc(void) {
    /* whitespace / CRLF tolerance, duplicate keys last wins */
    cw_doc *d = cw_parse_cstr("  {\"a\":1,\"a\":2}  \r\n\t");
    CHECK(d != NULL);
    if (!d) return;
    int64_t iv = 0;
    CHECK(cw_as_int(cw_object_get(cw_doc_root(d), "a"), &iv) == CW_OK &&
          iv == 2);
    CHECK(cw_object_size(cw_doc_root(d)) == 1);
    cw_doc_free(d);

    /* empty containers */
    d = cw_parse_cstr("{\"a\":{},\"b\":[]}");
    size_t n = 0;
    char *out = cw_dump_string(d, cw_doc_root(d), 4, &n);
    CHECK(str_eq(out, "{\n    \"a\": {},\n    \"b\": []\n}"));
    cw_doc_free(d);

    /* depth limit */
    char deep[1500];
    memset(deep, '[', 600);
    memset(deep + 600, ']', 600);
    deep[1200] = '\0';
    d = cw_doc_new();
    CHECK(cw_doc_parse(d, deep, 1200) == CW_ERR_DEPTH);
    CHECK(cw_doc_set_max_depth(d, 2048) == CW_OK);
    CHECK(cw_doc_parse(d, deep, 1200) == CW_OK);
    cw_doc_free(d);

    /* scalar roots */
    d = cw_parse_cstr("123");
    CHECK(d && cw_typeof(cw_doc_root(d)) == CW_INT);
    cw_doc_free(d);
    d = cw_parse_cstr("true");
    CHECK(d && cw_typeof(cw_doc_root(d)) == CW_BOOL);
    cw_doc_free(d);
    d = cw_parse_cstr("\"s\"");
    CHECK(d && cw_typeof(cw_doc_root(d)) == CW_STRING);
    cw_doc_free(d);
    d = cw_parse_cstr("null");
    CHECK(d && cw_typeof(cw_doc_root(d)) == CW_NULL);
    cw_doc_free(d);

    /* type names */
    CHECK(str_eq(cw_type_name(CW_NULL), "null"));
    CHECK(str_eq(cw_type_name(CW_OBJECT), "object"));
}

/* ------------------------------------------------------------------ */
static void test_deep_nesting_ops(void) {
    /* 20000 nested arrays: beyond the old 4096 dump/copy recursion cap */
    enum { DEPTH = 20000 };
    char *buf = (char *)malloc(2 * DEPTH + 1);
    CHECK(buf != NULL);
    if (!buf) return;
    memset(buf, '[', DEPTH);
    memset(buf + DEPTH, ']', DEPTH);
    buf[2 * DEPTH] = '\0';

    cw_doc *d = cw_doc_new();
    CHECK(cw_doc_set_max_depth(d, DEPTH + 100) == CW_OK);
    CHECK(cw_doc_parse(d, buf, 2 * DEPTH) == CW_OK);
    cw_value *root = cw_doc_root(d);

    size_t n = 0;
    char *out = cw_dump_string(d, root, 0, &n); /* compact */
    CHECK(out != NULL && n == 2 * DEPTH && memcmp(out, buf, n) == 0);

    cw_doc *d2 = cw_doc_new();
    cw_value *copy = cw_value_deep_copy(root, d2);
    CHECK(copy != NULL);
    CHECK(cw_value_equal(root, copy));
    cw_doc_free(d2);
    cw_doc_free(d);
    free(buf);
}

/* ------------------------------------------------------------------ */
static void test_object_remove_stress(void) {
    enum { N = 200 };
    cw_doc *d = cw_doc_new();
    cw_value *obj = cw_new_object(d);
    char key[16];
    for (int i = 0; i < N; ++i) {
        snprintf(key, sizeof(key), "k%d", i);
        CHECK(cw_object_set_c(obj, key, cw_new_int(d, i)) == CW_OK);
    }
    CHECK(cw_object_size(obj) == N);

    /* remove every third key; in-place compaction + slot rebuild */
    for (int i = 0; i < N; i += 3) {
        snprintf(key, sizeof(key), "k%d", i);
        CHECK(cw_object_remove(obj, key) == CW_OK);
    }
    for (int i = 0; i < N; ++i) {
        snprintf(key, sizeof(key), "k%d", i);
        cw_value *v = cw_object_get(obj, key);
        if (i % 3 == 0) {
            CHECK(v == NULL);
        } else {
            int64_t iv = 0;
            CHECK(v != NULL && cw_as_int(v, &iv) == CW_OK && iv == i);
        }
    }
    CHECK(cw_object_size(obj) == (size_t)(N - (N + 2) / 3));

    /* iteration order preserved for the survivors */
    size_t idx = 0, count = 0;
    const char *k;
    size_t klen;
    cw_value *val;
    int expect = 1;
    while (cw_object_iter(obj, &idx, &k, &klen, &val)) {
        int cur = atoi(k + 1);
        CHECK(cur == expect);
        expect += (expect % 3 == 1) ? 1 : 2;
        count++;
    }
    CHECK(count == cw_object_size(obj));

    /* remove the rest */
    for (int i = 1; i < N; ++i) {
        if (i % 3) {
            snprintf(key, sizeof(key), "k%d", i);
            CHECK(cw_object_remove(obj, key) == CW_OK);
        }
    }
    CHECK(cw_object_size(obj) == 0);
    cw_doc_free(d);
}

/* ------------------------------------------------------------------ */
static void test_string_span_streaming(void) {
    /* long escape-free string fed in odd chunk sizes */
    char json[2048];
    size_t len = 0;
    json[len++] = '"';
    for (int i = 0; i < 1000; ++i)
        json[len++] = (char)('a' + (i % 26));
    json[len++] = '"';
    cw_doc *ref = cw_parse(json, len);
    CHECK(ref != NULL);
    if (!ref) return;

    cw_doc *d = cw_doc_new();
    CHECK(cw_doc_parse_begin(d) == CW_OK);
    size_t i = 0;
    int ok = 1;
    while (i < len) {
        size_t step = 1 + (i * 7) % 13;
        size_t n = len - i < step ? len - i : step;
        if (cw_doc_parse_chunk(d, json + i, n) < 0) {
            ok = 0;
            break;
        }
        i += n;
    }
    if (ok) ok = cw_doc_parse_end(d) == CW_OK;
    CHECK(ok);
    if (ok) CHECK(cw_value_equal(cw_doc_root(d), cw_doc_root(ref)));
    cw_doc_free(d);
    cw_doc_free(ref);

    /* escape in the middle; split the feed right before and right after the
     * backslash to exercise the span->token fallback */
    const char *esc = "\"abcdefghijklmnopqrstuvwxyz\\u00e9xyz\"";
    cw_doc *e = cw_parse_cstr(esc);
    CHECK(e != NULL);
    if (!e) return;
    size_t elen = strlen(esc);
    const char *bs = strchr(esc, '\\');
    size_t p1 = (size_t)(bs - esc);
    for (int split = 0; split < 2; ++split) {
        cw_doc *s = cw_doc_new();
        CHECK(cw_doc_parse_begin(s) == CW_OK);
        size_t cut = split == 0 ? p1 : p1 + 1;
        CHECK(cw_doc_parse_chunk(s, esc, cut) >= 0);
        CHECK(cw_doc_parse_chunk(s, esc + cut, elen - cut) >= 0);
        CHECK(cw_doc_parse_end(s) == CW_OK);
        CHECK(cw_value_equal(cw_doc_root(s), cw_doc_root(e)));
        cw_doc_free(s);
    }
    cw_doc_free(e);
}

/* ------------------------------------------------------------------ */
static void test_number_eof_errors(void) {
    cw_error err;
    CHECK(cw_validate("-", 1, &err) == CW_ERR_INVALID_NUMBER);
    CHECK(cw_validate("12e", 3, &err) == CW_ERR_INVALID_NUMBER);
    CHECK(cw_validate("1.", 2, &err) == CW_ERR_INVALID_NUMBER);
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    test_parse_basic();
    test_escapes_unicode();
    test_numbers();
    test_invalid_inputs();
    test_streaming_dom();
    test_sax();
    test_operations();
    test_dump();
    test_file_io();
    test_memory_recycle();
    test_iteration();
    test_misc();
    test_deep_nesting_ops();
    test_object_remove_stress();
    test_string_span_streaming();
    test_number_eof_errors();

    printf("passed: %d, failed: %d\n", g_pass, g_fail);
    return g_fail ? 1 : 0;
}
