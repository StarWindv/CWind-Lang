/**
 * Copyright (C) 2026 StarWindv
 * License: BSD-3.0
 * First  Author : DeepSeek-V4-Flash[version: 2026/07/31, Official]
 * Second Author : StarWindv[Reviewer, Optimizer]
 * Location: rt-src/include/stl/normal/cwind_json.h
 */

/*
 * Require:
 *  - std>=c11
 * Design:
 *   - Self-managed memory: every document owns an arena backed by mmap /
 *     VirtualAlloc blocks. Blocks are recycled when the document is cleared,
 *     so repeated parses reuse mapped memory instead of re-allocating.
 *   - Streaming parse: a SAX-style event parser accepts arbitrary chunk
 *     boundaries, and the DOM parser can be fed incrementally as well.
 *   - Objects are backed by an open-addressing hash table for O(1) key lookup
 *     while preserving insertion order for iteration / serialization.
 */

#ifndef CWIND_JSON_H
#define CWIND_JSON_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CW_VERSION_MAJOR 1
#define CW_VERSION_MINOR 0
#define CW_VERSION_PATCH 0

/* Default arena block size (mmap unit) and default nesting limit. */
#define CW_DEFAULT_BLOCK_SIZE ((size_t)1 << 20)
#define CW_DEFAULT_MAX_DEPTH 512
#define CW_MAX_DEPTH_LIMIT 65536

/* Status / error codes                                                */
typedef enum {
    CW_OK = 0,
    CW_DONE = 1,                 /* a complete top-level value was parsed   */

    CW_ERR_NOMEM = -1,
    CW_ERR_SYNTAX = -2,          /* generic structural error                */
    CW_ERR_INVALID_LITERAL = -3,
    CW_ERR_INVALID_NUMBER = -4,
    CW_ERR_BAD_ESCAPE = -5,
    CW_ERR_CONTROL_CHAR = -6,
    CW_ERR_UNTERMINATED_STRING = -7,
    CW_ERR_SURROGATE = -8,
    CW_ERR_EXPECTED_KEY = -9,
    CW_ERR_EXPECTED_COLON = -10,
    CW_ERR_UNEXPECTED_COMMA = -11,
    CW_ERR_TRAILING_COMMA = -12,
    CW_ERR_DEPTH = -13,
    CW_ERR_TRAILING_DATA = -14,
    CW_ERR_UNEXPECTED_END = -15,
    CW_ERR_IO = -16,
    CW_ERR_TYPE = -17,
    CW_ERR_NONFINITE = -18,
    CW_ERR_ARG = -19
} cw_status;

typedef struct cw_error {
    int code;                    /* cw_status (negative on failure)         */
    size_t offset;               /* 0-based byte offset in the input        */
    size_t line;                 /* 1-based line                            */
    size_t col;                  /* 1-based column                          */
    char message[192];
} cw_error;

/* Value types                                                         */
typedef enum {
    CW_NULL = 0,
    CW_BOOL,
    CW_INT,                      /* int64_t, exact integer                 */
    CW_DOUBLE,                   /* double (fraction / exponent / overflow) */
    CW_STRING,
    CW_ARRAY,
    CW_OBJECT
} cw_type;

typedef struct cw_arena cw_arena;
typedef struct cw_value cw_value;
typedef struct cw_doc cw_doc;
typedef struct cw_sax cw_sax;

/* Arena (self-managed mmap memory pool)                               */
/* Creates an arena. block_size 0 selects the default (1 MiB). Blocks are
 * mapped with mmap / VirtualAlloc and recycled (reset) instead of unmapped. */
cw_arena *cw_arena_new(size_t block_size);
void cw_arena_destroy(cw_arena *a);
void *cw_arena_alloc(cw_arena *a, size_t size);        /* 16-byte aligned   */
void *cw_arena_alloc_zero(cw_arena *a, size_t size);
void cw_arena_reset(cw_arena *a);                      /* recycle all blocks */
size_t cw_arena_mapped(const cw_arena *a);             /* total mapped bytes */
size_t cw_arena_used(const cw_arena *a);               /* bytes in use       */

/* Document                                                            */
/* A document owns an arena. All values created through a document live in
 * its arena and are invalidated by cw_doc_clear / cw_doc_free. */
cw_doc *cw_doc_new(void);
void cw_doc_free(cw_doc *d);
void cw_doc_clear(cw_doc *d);                          /* recycle memory     */

int cw_doc_set_max_depth(cw_doc *d, int max_depth);
int cw_doc_max_depth(const cw_doc *d);

cw_value *cw_doc_root(const cw_doc *d);
void cw_doc_set_root(cw_doc *d, cw_value *v);
const cw_error *cw_doc_error(const cw_doc *d);
void cw_doc_mem_stats(const cw_doc *d, size_t *mapped, size_t *used);

/* Full parse. Clears the document first. Returns CW_OK on success. */
int cw_doc_parse(cw_doc *d, const char *text, size_t len);
int cw_doc_parse_cstr(cw_doc *d, const char *text);
int cw_doc_parse_file(cw_doc *d, const char *path);

/* Streaming DOM parse: feed arbitrary chunks, then finish. */
int cw_doc_parse_begin(cw_doc *d);
int cw_doc_parse_chunk(cw_doc *d, const char *data, size_t len);
int cw_doc_parse_end(cw_doc *d);

/* Convenience one-shots. */
cw_doc *cw_parse(const char *text, size_t len);
cw_doc *cw_parse_cstr(const char *text);
cw_doc *cw_load_file(const char *path);

/* Validity checking (no DOM is built)                                 */
int cw_validate(const char *text, size_t len, cw_error *err);
int cw_validate_file(const char *path, cw_error *err);

/* Value construction (all memory comes from the document's arena)     */
cw_value *cw_new_null(cw_doc *d);
cw_value *cw_new_bool(cw_doc *d, bool b);
cw_value *cw_new_int(cw_doc *d, int64_t i);
cw_value *cw_new_double(cw_doc *d, double x);
cw_value *cw_new_string(cw_doc *d, const char *s, size_t len);
cw_value *cw_new_string_c(cw_doc *d, const char *s);
cw_value *cw_new_array(cw_doc *d);
cw_value *cw_new_object(cw_doc *d);

/* Accessors                                                           */
cw_type cw_typeof(const cw_value *v);
const char *cw_type_name(cw_type t);
bool cw_is_number(const cw_value *v);

int cw_as_bool(const cw_value *v, bool *out);
int cw_as_int(const cw_value *v, int64_t *out);
int cw_as_double(const cw_value *v, double *out);
const char *cw_string_value(const cw_value *v, size_t *len); /* NULL if not a string */
const char *cw_string_cstr(const cw_value *v);               /* NUL-terminated      */
size_t cw_string_len(const cw_value *v);
size_t cw_length(const cw_value *v);                         /* array/object/string */

/* Object operations (hash-backed, insertion order preserved)          */
int cw_object_set(cw_value *obj, const char *key, size_t key_len, cw_value *v);
int cw_object_set_c(cw_value *obj, const char *key, cw_value *v);
cw_value *cw_object_get(const cw_value *obj, const char *key);
cw_value *cw_object_get_len(const cw_value *obj, const char *key, size_t key_len);
bool cw_object_has(const cw_value *obj, const char *key);
int cw_object_remove(cw_value *obj, const char *key);      /* no-op if absent */
size_t cw_object_size(const cw_value *obj);

/* Iteration: start with index = 0; returns 1 while an entry remains. */
int cw_object_iter(const cw_value *obj, size_t *index,
                   const char **key, size_t *key_len, cw_value **value);

/* Array operations                                                    */
size_t cw_array_size(const cw_value *arr);
cw_value *cw_array_get(const cw_value *arr, size_t index);
int cw_array_set(cw_value *arr, size_t index, cw_value *v);
int cw_array_append(cw_value *arr, cw_value *v);
int cw_array_insert(cw_value *arr, size_t index, cw_value *v);
int cw_array_remove(cw_value *arr, size_t index);
void cw_array_clear(cw_value *arr);

/* Generic operations                                                  */
cw_value *cw_value_deep_copy(const cw_value *v, cw_doc *dst);
int cw_value_equal(const cw_value *a, const cw_value *b);

/* Path lookup: "a.b[2].c" -> object key 'a', object key 'b', index 2,
 * object key 'c'. Returns NULL when any step is missing / mistyped. */
cw_value *cw_get_path(const cw_value *root, const char *path);

/* Serialization                                                       */
/* indent is the number of spaces per level (use 4 for 4-space indent,
 * 0 for compact output). */
int cw_dump_file(const cw_value *v, FILE *f, size_t indent);
int cw_dump_path(const cw_value *v, const char *path, size_t indent);

/* Serialize into the document's arena (auto-managed memory). */
char *cw_dump_string(cw_doc *doc, const cw_value *v, size_t indent,
                     size_t *out_len);
/* Serialize into malloc'd memory; the caller must free() it. */
char *cw_dump_malloc(const cw_value *v, size_t indent, size_t *out_len);

/* Streaming SAX parsing                                               */
typedef enum {
    CW_EVT_NULL,
    CW_EVT_BOOL,
    CW_EVT_INT,
    CW_EVT_DOUBLE,
    CW_EVT_STRING,
    CW_EVT_KEY,
    CW_EVT_ARRAY_BEGIN,
    CW_EVT_ARRAY_END,
    CW_EVT_OBJECT_BEGIN,
    CW_EVT_OBJECT_END,
    CW_EVT_DONE
} cw_event_type;

typedef struct cw_event {
    cw_event_type type;
    int depth;                 /* nesting level of the reported value      */
    union {
        bool b;
        int64_t i;
        double d;
        /* For strings/keys the data may point into the current input chunk
         * (when the token needs no unescaping); it is valid until the next
         * event or until cw_sax_feed returns, whichever comes first. Copy it
         * if you need to retain it. */
        struct { const char *data; size_t len; } str;
    } u;
} cw_event;

/* Return 0 to continue, nonzero to abort (the value is returned as-is by
 * cw_sax_feed / cw_sax_finish). */
typedef int (*cw_sax_cb)(void *ctx, const cw_event *ev);

cw_sax *cw_sax_new(cw_sax_cb cb, void *ctx);
void cw_sax_free(cw_sax *s);

/* Feed a chunk. Returns CW_OK (more input expected), CW_DONE (one or more
 * complete top-level values were produced during this call) or a negative
 * error code. Concatenated top-level values are allowed for SAX parsing. */
int cw_sax_feed(cw_sax *s, const char *data, size_t len);
/* Flush end-of-input: finalizes a pending scalar / checks termination. */
int cw_sax_finish(cw_sax *s);
void cw_sax_reset(cw_sax *s);
int cw_sax_set_max_depth(cw_sax *s, int max_depth);
int cw_sax_depth(const cw_sax *s);
int cw_sax_values(const cw_sax *s);      /* number of DONE events emitted */
const cw_error *cw_sax_error(const cw_sax *s);

/* Iteration macros                                                    */
/* cw_array_foreach(arr, idx, val) */
#define CW_ARRAY_FOREACH(arr_, idx_, val_)                                    \
    for (size_t (idx_) = 0;                                                   \
         (idx_) < cw_array_size(arr_) &&                                      \
             ((val_) = cw_array_get((arr_), (idx_)), 1);                      \
         ++(idx_))

/* cw_object_foreach(obj, key, key_len, val) */
#define CW_OBJECT_FOREACH(obj_, key_, key_len_, val_)                         \
    for (size_t _cw_iter_##__LINE__ = 0;                                      \
         cw_object_iter((obj_), &_cw_iter_##__LINE__, &(key_),                \
                        &(key_len_), &(val_));)

#ifdef __cplusplus
}
#endif


/* Define CW_JSON_IMPLEMENTATION in exactly one translation unit       */
/* before including this header:                                       */
/*                                                                     */
/*     #define CW_JSON_IMPLEMENTATION                                  */
/*     #include "cwind_json.h"                                         */
#ifdef CW_JSON_IMPLEMENTATION

    #include <locale.h>
    #include <math.h>
    #include <stdarg.h>
    #include <stdlib.h>
    #include <string.h>

    #if defined(_WIN32)
        #ifndef WIN32_LEAN_AND_MEAN
            #define WIN32_LEAN_AND_MEAN
    #endif
    #include <windows.h>
#else
    #include <sys/mman.h>
    #include <unistd.h>
#endif


static void cw_err_at(cw_error *e, int code, size_t line, size_t col,
                      size_t off, const char *fmt, ...) {
    if (!e) return;
    e->code = code;
    e->line = line;
    e->col = col;
    e->offset = off;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(e->message, sizeof(e->message), fmt, ap);
    va_end(ap);
}


typedef struct cw_block {
    size_t size;
    size_t used;
    struct cw_block *next;
} cw_block;

#define CW_ALIGN16(n) (((n) + 15u) & ~(size_t)15u)
#define CW_BLOCK_HDR CW_ALIGN16(sizeof(cw_block))
#define CW_BLOCK_DATA(b) ((void *)((char *)(b) + CW_BLOCK_HDR))

struct cw_arena {
    cw_block *used;          /* live chain */
    cw_block *free;          /* recycled chain */
    cw_block *cur;           /* current bump block */
    size_t block_size;
    size_t mapped;           /* total mapped bytes */
};

static void *cw_os_map(size_t size) {
#if defined(_WIN32)
    return VirtualAlloc(NULL, size, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
#else
    void *p = mmap(NULL, size, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    return p == MAP_FAILED ? NULL : p;
#endif
}

static void cw_os_unmap(void *p, size_t size) {
#if defined(_WIN32)
    (void)size;
    if (p) VirtualFree(p, 0, MEM_RELEASE);
#else
    if (p) munmap(p, size);
#endif
}

static cw_block *cw_block_new(size_t size) {
    if (size > SIZE_MAX - CW_BLOCK_HDR) return NULL;
    size_t total = CW_BLOCK_HDR + size;
    void *mem = cw_os_map(total);
    if (!mem) return NULL;
    cw_block *b = (cw_block *)mem;
    b->size = size;
    b->used = 0;
    b->next = NULL;
    return b;
}

static void cw_block_list_destroy(cw_block *b) {
    while (b) {
        cw_block *n = b->next;
        cw_os_unmap(b, CW_BLOCK_HDR + b->size);
        b = n;
    }
}

static void cw_arena_init(cw_arena *a, size_t block_size) {
    memset(a, 0, sizeof(*a));
    a->block_size = block_size ? block_size : CW_DEFAULT_BLOCK_SIZE;
    if (a->block_size < 4096) a->block_size = 4096;
}

cw_arena *cw_arena_new(size_t block_size) {
    cw_arena *a = (cw_arena *)malloc(sizeof(cw_arena));
    if (!a) return NULL;
    cw_arena_init(a, block_size);
    return a;
}

static void cw_arena_destroy_blocks(cw_arena *a) {
    if (!a) return;
    cw_block_list_destroy(a->used);
    cw_block_list_destroy(a->free);
    a->used = NULL;
    a->free = NULL;
    a->cur = NULL;
    a->mapped = 0;
}

void cw_arena_destroy(cw_arena *a) {
    if (!a) return;
    cw_arena_destroy_blocks(a);
    free(a);
}

void *cw_arena_alloc(cw_arena *a, size_t size) {
    if (!a) return NULL;
    if (size == 0) size = 16;
    if (size > SIZE_MAX - 15) return NULL; /* CW_ALIGN16 would wrap */
    size = CW_ALIGN16(size);
    cw_block *b = a->cur;
    if (b && size <= b->size - b->used) {
        void *p = (char *)CW_BLOCK_DATA(b) + b->used;
        b->used += size;
        return p;
    }
    if (size > a->block_size) {
        /* oversized allocation gets a dedicated block */
        cw_block *nb = cw_block_new(size);
        if (!nb) return NULL;
        nb->used = size;
        nb->next = a->used;
        a->used = nb;
        a->mapped += CW_BLOCK_HDR + size;
        return CW_BLOCK_DATA(nb);
    }
    /* recycle a suitable block from the free list, if any */
    cw_block **pp = &a->free;
    while (*pp) {
        if ((*pp)->size >= size) {
            cw_block *fb = *pp;
            *pp = fb->next;
            fb->next = a->used;
            a->used = fb;
            fb->used = size;
            a->cur = fb;
            return CW_BLOCK_DATA(fb);
        }
        pp = &(*pp)->next;
    }
    b = cw_block_new(a->block_size);
    if (!b) return NULL;
    b->used = size;
    b->next = a->used;
    a->used = b;
    a->cur = b;
    a->mapped += CW_BLOCK_HDR + b->size;
    return CW_BLOCK_DATA(b);
}

void *cw_arena_alloc_zero(cw_arena *a, size_t size) {
    void *p = cw_arena_alloc(a, size);
    if (p) memset(p, 0, size);
    return p;
}

void cw_arena_reset(cw_arena *a) {
    if (!a) return;
    cw_block *b = a->used;
    while (b) {
        cw_block *n = b->next;
        b->next = a->free;
        a->free = b;
        b->used = 0;
        b = n;
    }
    a->used = NULL;
    a->cur = NULL;
}

size_t cw_arena_mapped(const cw_arena *a) {
    return a ? a->mapped : 0;
}

size_t cw_arena_used(const cw_arena *a) {
    size_t u = 0;
    if (!a) return 0;
    for (cw_block *b = a->used; b; b = b->next) u += b->used;
    return u;
}

static char *cw_arena_strndup(cw_arena *a, const char *s, size_t n) {
    char *p = (char *)cw_arena_alloc(a, n + 1);
    if (!p) return NULL;
    if (n) memcpy(p, s, n);
    p[n] = '\0';
    return p;
}

typedef struct cw_sdom cw_sdom;

struct cw_doc {
    cw_arena arena;
    cw_value *root;
    cw_error err;
    int max_depth;
    cw_sdom *sdom;
};

/* Value layout                                                        */
typedef struct cw_pair {
    char *key;
    size_t key_len;
    cw_value *value;
    uint64_t hash;           /* cached key hash (FNV-1a) */
} cw_pair;

typedef struct cw_object {
    size_t len;
    size_t cap;
    cw_pair *pairs;
    size_t nslots;             /* power of two */
    size_t *slots;             /* pair index + 1, 0 = empty */
} cw_object;

struct cw_value {
    cw_arena *arena;
    cw_type type;
    uint32_t reserved;
    union {
        bool b;
        int64_t i;
        double d;
        struct { size_t len; char *data; } str;
        struct { size_t len, cap; cw_value **items; } arr;
        cw_object obj;
    } u;
};

static cw_value *cw_value_new(cw_doc *d, cw_type t) {
    cw_value *v = (cw_value *)cw_arena_alloc_zero(&d->arena, sizeof(cw_value));
    if (!v) return NULL;
    v->arena = &d->arena;
    v->type = t;
    return v;
}

/* Object hash table (open addressing, insertion order preserved)      */
static uint64_t cw_hash(const char *s, size_t n) {
    uint64_t h = 14695981039346656037ULL;
    for (size_t i = 0; i < n; ++i) {
        h ^= (unsigned char)s[i];
        h *= 1099511628211ULL;
    }
    return h;
}

static int cw_obj_rehash(cw_value *obj, size_t nslots) {
    cw_object *o = &obj->u.obj;
    size_t *slots =
        (size_t *)cw_arena_alloc_zero(obj->arena, nslots * sizeof(size_t));
    if (!slots) return CW_ERR_NOMEM;
    for (size_t k = 0; k < o->len; ++k) {
        cw_pair *pr = &o->pairs[k];
        uint64_t h = pr->hash;
        size_t i = (size_t)h & (nslots - 1);
        while (slots[i]) i = (i + 1) & (nslots - 1);
        slots[i] = k + 1;
    }
    o->slots = slots;
    o->nslots = nslots;
    return CW_OK;
}

static int cw_obj_ensure(cw_value *obj) {
    cw_object *o = &obj->u.obj;
    if (!o->slots) {
        o->nslots = 8;
        o->slots = (size_t *)cw_arena_alloc_zero(obj->arena,
                                                 8 * sizeof(size_t));
        if (!o->slots) return CW_ERR_NOMEM;
    } else if ((o->len + 1) * 10 >= o->nslots * 7) {
        if (o->nslots > ((size_t)1 << 30)) return CW_ERR_NOMEM;
        int r = cw_obj_rehash(obj, o->nslots * 2);
        if (r != CW_OK) return r;
    }
    return CW_OK;
}

static int cw_obj_grow_pairs(cw_value *obj) {
    cw_object *o = &obj->u.obj;
    size_t ncap = o->cap ? o->cap * 2 : 8;
    cw_pair *np =
        (cw_pair *)cw_arena_alloc(obj->arena, ncap * sizeof(cw_pair));
    if (!np) return CW_ERR_NOMEM;
    if (o->len) memcpy(np, o->pairs, o->len * sizeof(cw_pair));
    o->pairs = np;
    o->cap = ncap;
    return CW_OK;
}

int cw_object_set(cw_value *obj, const char *key, size_t key_len,
                  cw_value *v) {
    if (!obj || obj->type != CW_OBJECT) return CW_ERR_TYPE;
    if (!v) return CW_ERR_ARG;
    if (!key) {
        if (key_len) return CW_ERR_ARG;
        key = "";
    }
    cw_object *o = &obj->u.obj;
    int r = cw_obj_ensure(obj);
    if (r != CW_OK) return r;
    uint64_t h = cw_hash(key, key_len);
    size_t i = (size_t)h & (o->nslots - 1);
    for (;;) {
        size_t pi = o->slots[i];
        if (!pi) break;
        cw_pair *pr = &o->pairs[pi - 1];
        if (pr->key_len == key_len && memcmp(pr->key, key, key_len) == 0) {
            pr->value = v;
            return CW_OK;
        }
        i = (i + 1) & (o->nslots - 1);
    }
    if (o->len == o->cap) {
        r = cw_obj_grow_pairs(obj);
        if (r != CW_OK) return r;
    }
    char *k = cw_arena_strndup(obj->arena, key, key_len);
    if (!k) return CW_ERR_NOMEM;
    o->pairs[o->len].key = k;
    o->pairs[o->len].key_len = key_len;
    o->pairs[o->len].value = v;
    o->pairs[o->len].hash = h;
    o->slots[i] = o->len + 1;
    o->len++;
    return CW_OK;
}

int cw_object_set_c(cw_value *obj, const char *key, cw_value *v) {
    if (!key) return CW_ERR_ARG;
    return cw_object_set(obj, key, strlen(key), v);
}

cw_value *cw_object_get_len(const cw_value *obj, const char *key,
                            size_t key_len) {
    if (!obj || obj->type != CW_OBJECT) return NULL;
    const cw_object *o = &obj->u.obj;
    if (!o->slots || o->len == 0) return NULL;
    uint64_t h = cw_hash(key, key_len);
    size_t i = (size_t)h & (o->nslots - 1);
    for (;;) {
        size_t pi = o->slots[i];
        if (!pi) return NULL;
        const cw_pair *pr = &o->pairs[pi - 1];
        if (pr->key_len == key_len && memcmp(pr->key, key, key_len) == 0)
            return pr->value;
        i = (i + 1) & (o->nslots - 1);
    }
}

cw_value *cw_object_get(const cw_value *obj, const char *key) {
    if (!key) return NULL;
    return cw_object_get_len(obj, key, strlen(key));
}

bool cw_object_has(const cw_value *obj, const char *key) {
    return cw_object_get(obj, key) != NULL;
}

static int cw_object_remove_len(cw_value *obj, const char *key,
                                size_t key_len) {
    cw_object *o = &obj->u.obj;
    if (!o->slots || o->len == 0) return CW_OK;
    uint64_t h = cw_hash(key, key_len);
    size_t i = (size_t)h & (o->nslots - 1);
    size_t found = 0;
    for (;;) {
        size_t pi = o->slots[i];
        if (!pi) break;
        cw_pair *pr = &o->pairs[pi - 1];
        if (pr->key_len == key_len && memcmp(pr->key, key, key_len) == 0) {
            found = pi;
            break;
        }
        i = (i + 1) & (o->nslots - 1);
    }
    if (!found) return CW_OK;
    /* Compact the pairs array in place (preserves insertion order) and
     * rebuild the hash slots without allocating new memory. */
    cw_pair *dst = &o->pairs[found - 1];
    if (found < o->len)
        memmove(dst, dst + 1, (o->len - found) * sizeof(cw_pair));
    o->len--;
    memset(o->slots, 0, o->nslots * sizeof(size_t));
    for (size_t k = 0; k < o->len; ++k) {
        cw_pair *pr = &o->pairs[k];
        size_t i = (size_t)pr->hash & (o->nslots - 1);
        while (o->slots[i]) i = (i + 1) & (o->nslots - 1);
        o->slots[i] = k + 1;
    }
    return CW_OK;
}

int cw_object_remove(cw_value *obj, const char *key) {
    if (!obj || obj->type != CW_OBJECT) return CW_ERR_TYPE;
    if (!key) return CW_ERR_ARG;
    return cw_object_remove_len(obj, key, strlen(key));
}

size_t cw_object_size(const cw_value *obj) {
    if (!obj || obj->type != CW_OBJECT) return 0;
    return obj->u.obj.len;
}

int cw_object_iter(const cw_value *obj, size_t *index, const char **key,
                   size_t *key_len, cw_value **value) {
    if (!obj || obj->type != CW_OBJECT || !index) return 0;
    const cw_object *o = &obj->u.obj;
    if (*index >= o->len) return 0;
    const cw_pair *pr = &o->pairs[*index];
    if (key) *key = pr->key;
    if (key_len) *key_len = pr->key_len;
    if (value) *value = pr->value;
    (*index)++;
    return 1;
}

/* Arrays                                                              */
static int cw_array_ensure(cw_value *arr, size_t need) {
    if (need <= arr->u.arr.cap) return CW_OK;
    size_t ncap = arr->u.arr.cap ? arr->u.arr.cap : 8;
    while (ncap < need) ncap *= 2;
    cw_value **ni =
        (cw_value **)cw_arena_alloc(arr->arena, ncap * sizeof(cw_value *));
    if (!ni) return CW_ERR_NOMEM;
    if (arr->u.arr.len) memcpy(ni, arr->u.arr.items,
                                arr->u.arr.len * sizeof(cw_value *));
    arr->u.arr.items = ni;
    arr->u.arr.cap = ncap;
    return CW_OK;
}

size_t cw_array_size(const cw_value *arr) {
    if (!arr || arr->type != CW_ARRAY) return 0;
    return arr->u.arr.len;
}

cw_value *cw_array_get(const cw_value *arr, size_t index) {
    if (!arr || arr->type != CW_ARRAY || index >= arr->u.arr.len) return NULL;
    return arr->u.arr.items[index];
}

int cw_array_set(cw_value *arr, size_t index, cw_value *v) {
    if (!arr || arr->type != CW_ARRAY) return CW_ERR_TYPE;
    if (!v) return CW_ERR_ARG;
    if (index >= arr->u.arr.len) return CW_ERR_ARG;
    arr->u.arr.items[index] = v;
    return CW_OK;
}

int cw_array_append(cw_value *arr, cw_value *v) {
    if (!arr || arr->type != CW_ARRAY) return CW_ERR_TYPE;
    if (!v) return CW_ERR_ARG;
    int r = cw_array_ensure(arr, arr->u.arr.len + 1);
    if (r != CW_OK) return r;
    arr->u.arr.items[arr->u.arr.len++] = v;
    return CW_OK;
}

int cw_array_insert(cw_value *arr, size_t index, cw_value *v) {
    if (!arr || arr->type != CW_ARRAY) return CW_ERR_TYPE;
    if (!v) return CW_ERR_ARG;
    if (index > arr->u.arr.len) return CW_ERR_ARG;
    int r = cw_array_ensure(arr, arr->u.arr.len + 1);
    if (r != CW_OK) return r;
    if (index < arr->u.arr.len)
        memmove(arr->u.arr.items + index + 1, arr->u.arr.items + index,
                (arr->u.arr.len - index) * sizeof(cw_value *));
    arr->u.arr.items[index] = v;
    arr->u.arr.len++;
    return CW_OK;
}

int cw_array_remove(cw_value *arr, size_t index) {
    if (!arr || arr->type != CW_ARRAY) return CW_ERR_TYPE;
    if (index >= arr->u.arr.len) return CW_ERR_ARG;
    if (index + 1 < arr->u.arr.len)
        memmove(arr->u.arr.items + index, arr->u.arr.items + index + 1,
                (arr->u.arr.len - index - 1) * sizeof(cw_value *));
    arr->u.arr.len--;
    return CW_OK;
}

void cw_array_clear(cw_value *arr) {
    if (!arr || arr->type != CW_ARRAY) return;
    arr->u.arr.len = 0;
}

/* Number parsing (locale tolerant)                                    */
static double cw_strtod_local(const char *s, char **end) {
    const char *dp = localeconv()->decimal_point;
    if (dp && dp[0] == '.' && dp[1] == '\0') return strtod(s, end);
    size_t n = strlen(s);
    if (dp && dp[0] && dp[1] == '\0' && n < 256) {
        char tmp[256];
        memcpy(tmp, s, n + 1);
        for (size_t i = 0; i < n; ++i)
            if (tmp[i] == '.') tmp[i] = dp[0];
        return strtod(tmp, end);
    }
    return strtod(s, end);
}

static int cw_number_parse(const char *tok, size_t n, int64_t *iout,
                           double *dout, int *is_int, cw_error *err,
                           size_t line, size_t col, size_t off) {
    size_t k = 0;
    int neg = 0;
    if (k < n && tok[k] == '-') {
        neg = 1;
        ++k;
    }
    if (k >= n) goto bad;
    if (tok[k] == '0') {
        ++k;
        if (k < n && tok[k] >= '0' && tok[k] <= '9') goto bad_leading;
    } else if (tok[k] >= '1' && tok[k] <= '9') {
        while (k < n && tok[k] >= '0' && tok[k] <= '9') ++k;
    } else {
        goto bad;
    }
    int is_double = 0;
    if (k < n && tok[k] == '.') {
        is_double = 1;
        ++k;
        if (k >= n || tok[k] < '0' || tok[k] > '9') goto bad;
        while (k < n && tok[k] >= '0' && tok[k] <= '9') ++k;
    }
    if (k < n && (tok[k] == 'e' || tok[k] == 'E')) {
        is_double = 1;
        ++k;
        if (k < n && (tok[k] == '+' || tok[k] == '-')) ++k;
        if (k >= n || tok[k] < '0' || tok[k] > '9') goto bad;
        while (k < n && tok[k] >= '0' && tok[k] <= '9') ++k;
    }
    if (k != n) goto bad;
    if (!is_double) {
        const char *d = tok + (neg ? 1 : 0);
        size_t dn = n - (neg ? 1 : 0);
        uint64_t v = 0;
        int overflow = 0;
        for (size_t z = 0; z < dn; ++z) {
            unsigned digit = (unsigned)(d[z] - '0');
            if (v > (UINT64_MAX - digit) / 10) {
                overflow = 1;
                break;
            }
            v = v * 10 + digit;
        }
        if (!overflow) {
            if (neg && v == 9223372036854775808ULL) {
                *is_int = 1;
                *iout = INT64_MIN;
                return CW_OK;
            }
            if (!neg && v > 9223372036854775807ULL) {
                is_double = 1;
            } else {
                *is_int = 1;
                *iout = neg ? -(int64_t)v : (int64_t)v;
                return CW_OK;
            }
        } else {
            is_double = 1;
        }
    }
    *is_int = 0;
    *dout = cw_strtod_local(tok, NULL);
    return CW_OK;
bad_leading:
    cw_err_at(err, CW_ERR_INVALID_NUMBER, line, col, off,
              "leading zeros are not allowed");
    return CW_ERR_INVALID_NUMBER;
bad:
    cw_err_at(err, CW_ERR_INVALID_NUMBER, line, col, off, "invalid number");
    return CW_ERR_INVALID_NUMBER;
}

/* Streaming SAX parser (state machine, chunk-safe)                    */
enum { CW_REDO = 2 }; /* private marker: reprocess char structurally */

enum {
    CW_KIND_ARR = 0,
    CW_KIND_OBJ = 1
};

enum {
    CW_EXP_VALUE = 0,     /* fresh array / object after ':'   */
    CW_EXP_VALUE_COMMA,   /* array after ','                  */
    CW_EXP_KEY,           /* fresh object                     */
    CW_EXP_KEY_COMMA,     /* object after ','                 */
    CW_EXP_COLON,
    CW_EXP_COMMA          /* after a value                    */
};

enum {
    SCAN_NONE = 0,
    SCAN_STRING,
    SCAN_NUMBER,
    SCAN_LITERAL
};

enum {
    SSTR_NORMAL = 0,
    SSTR_ESC,
    SSTR_U,        /* accumulating 4 hex digits                 */
    SSTR_U_LOW1,   /* expect '\' after high surrogate           */
    SSTR_U_LOW2,   /* expect 'u'                                */
    SSTR_U_LOW     /* accumulating 4 hex digits of low surrogate */
};

typedef struct cw_frame {
    int kind;
    int expect;
} cw_frame;

struct cw_sax {
    cw_sax_cb cb;
    void *ctx;
    cw_frame *stack;
    int depth;
    int max_depth;

    int scan;
    int sstr;
    char *tok;
    size_t tok_len, tok_cap;
    size_t tok_line, tok_col, tok_off; /* token start position */
    /* Pure (escape-free) strings are served directly from the current input
     * chunk to avoid an intermediate copy; they fall back to tok when an
     * escape appears or the chunk ends mid-token. */
    int str_span_active;
    const char *str_span;      /* start of the pending span in this chunk */
    size_t span_len;           /* characters consumed so far in this token */

    int lit;                 /* 0 true, 1 false, 2 null */
    size_t lit_pos;
    uint32_t hex;
    int hex_rem;
    uint32_t hi_sur;

    int value_count;
    int just_done;

    cw_error err;
    size_t line, col, off;
};

static int cw_sax_err(cw_sax *s, int code, const char *msg) {
    cw_err_at(&s->err, code, s->line, s->col, s->off, "%s", msg);
    return code;
}

static int cw_sax_tok_grow(cw_sax *s, size_t need) {
    if (need == SIZE_MAX) {
        cw_err_at(&s->err, CW_ERR_NOMEM, s->line, s->col, s->off,
                  "out of memory");
        return CW_ERR_NOMEM;
    }
    if (need + 1 <= s->tok_cap) return CW_OK;
    size_t ncap = s->tok_cap ? s->tok_cap : 64;
    while (ncap < need + 1) {
        if (ncap > SIZE_MAX / 2) {
            cw_err_at(&s->err, CW_ERR_NOMEM, s->line, s->col, s->off,
                      "out of memory");
            return CW_ERR_NOMEM;
        }
        ncap *= 2;
    }
    char *np = (char *)realloc(s->tok, ncap);
    if (!np) {
        cw_err_at(&s->err, CW_ERR_NOMEM, s->line, s->col, s->off,
                  "out of memory");
        return CW_ERR_NOMEM;
    }
    s->tok = np;
    s->tok_cap = ncap;
    return CW_OK;
}

static int cw_sax_tok_putc(cw_sax *s, char c) {
    int r = cw_sax_tok_grow(s, s->tok_len + 1);
    if (r != CW_OK) return r;
    s->tok[s->tok_len++] = c;
    return CW_OK;
}

static int cw_sax_tok_append(cw_sax *s, const char *p, size_t n) {
    int r = cw_sax_tok_grow(s, s->tok_len + n);
    if (r != CW_OK) return r;
    if (n) memcpy(s->tok + s->tok_len, p, n);
    s->tok_len += n;
    return CW_OK;
}

static unsigned cw_hexval(char c) {
    if (c >= '0' && c <= '9') return (unsigned)(c - '0');
    if (c >= 'a' && c <= 'f') return (unsigned)(c - 'a' + 10);
    if (c >= 'A' && c <= 'F') return (unsigned)(c - 'A' + 10);
    return 0xFF;
}

static int cw_sax_put_utf8(cw_sax *s, uint32_t cp) {
    char tmp[4];
    int n;
    if (cp < 0x80) {
        tmp[0] = (char)cp;
        n = 1;
    } else if (cp < 0x800) {
        tmp[0] = (char)(0xC0 | (cp >> 6));
        tmp[1] = (char)(0x80 | (cp & 0x3F));
        n = 2;
    } else if (cp < 0x10000) {
        tmp[0] = (char)(0xE0 | (cp >> 12));
        tmp[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        tmp[2] = (char)(0x80 | (cp & 0x3F));
        n = 3;
    } else {
        tmp[0] = (char)(0xF0 | (cp >> 18));
        tmp[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
        tmp[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
        tmp[3] = (char)(0x80 | (cp & 0x3F));
        n = 4;
    }
    return cw_sax_tok_append(s, tmp, (size_t)n);
}

static const char *cw_lit_word(int lit) {
    static const char *const words[3] = {"true", "false", "null"};
    return words[lit];
}

static int cw_is_term(char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\n' ||
           c == ',' || c == ']' || c == '}';
}

static int cw_sax_call(cw_sax *s, const cw_event *ev) {
    if (!s->cb) return CW_OK;
    return s->cb(s->ctx, ev);
}

static int cw_sax_emit_open(cw_sax *s, cw_event_type t, int depth) {
    cw_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = t;
    ev.depth = depth;
    return cw_sax_call(s, &ev);
}

static int cw_sax_emit_string(cw_sax *s, cw_event_type t, int depth) {
    cw_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = t;
    ev.depth = depth;
    if (s->str_span_active) {
        ev.u.str.data = s->str_span;
        ev.u.str.len = s->span_len;
    } else {
        ev.u.str.data = s->tok;
        ev.u.str.len = s->tok_len;
    }
    return cw_sax_call(s, &ev);
}

static int cw_sax_emit_bool(cw_sax *s, bool b) {
    cw_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = CW_EVT_BOOL;
    ev.depth = s->depth;
    ev.u.b = b;
    return cw_sax_call(s, &ev);
}

static int cw_sax_emit_int(cw_sax *s, int64_t i) {
    cw_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = CW_EVT_INT;
    ev.depth = s->depth;
    ev.u.i = i;
    return cw_sax_call(s, &ev);
}

static int cw_sax_emit_double(cw_sax *s, double d) {
    cw_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = CW_EVT_DOUBLE;
    ev.depth = s->depth;
    ev.u.d = d;
    return cw_sax_call(s, &ev);
}

static int cw_sax_emit_null(cw_sax *s) {
    cw_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = CW_EVT_NULL;
    ev.depth = s->depth;
    return cw_sax_call(s, &ev);
}

static int cw_sax_emit_done(cw_sax *s) {
    cw_event ev;
    memset(&ev, 0, sizeof(ev));
    ev.type = CW_EVT_DONE;
    ev.depth = 0;
    int r = cw_sax_call(s, &ev);
    if (r == CW_OK) {
        s->value_count++;
        s->just_done = 1;
    }
    return r;
}

static int cw_sax_scalar_end(cw_sax *s) {
    s->scan = SCAN_NONE;
    s->tok_len = 0;
    if (s->depth == 0) return cw_sax_emit_done(s);
    s->stack[s->depth - 1].expect = CW_EXP_COMMA;
    return CW_OK;
}

static int cw_sax_number_done(cw_sax *s) {
    int r = cw_sax_tok_grow(s, s->tok_len + 1);
    if (r != CW_OK) return r;
    s->tok[s->tok_len] = '\0';
    int64_t i = 0;
    double d = 0;
    int is_int = 0;
    r = cw_number_parse(s->tok, s->tok_len, &i, &d, &is_int, &s->err,
                        s->tok_line, s->tok_col, s->tok_off);
    if (r != CW_OK) return r;
    if (is_int)
        r = cw_sax_emit_int(s, i);
    else
        r = cw_sax_emit_double(s, d);
    if (r != CW_OK) return r;
    return cw_sax_scalar_end(s);
}

static int cw_sax_literal_done(cw_sax *s) {
    int r;
    if (s->lit == 0)
        r = cw_sax_emit_bool(s, true);
    else if (s->lit == 1)
        r = cw_sax_emit_bool(s, false);
    else
        r = cw_sax_emit_null(s);
    if (r != CW_OK) return r;
    return cw_sax_scalar_end(s);
}

static int cw_sax_string_done(cw_sax *s) {
    int r;
    if (!s->str_span_active) {
        r = cw_sax_tok_grow(s, s->tok_len + 1);
        if (r != CW_OK) return r;
        s->tok[s->tok_len] = '\0';
    }
    if (s->depth == 0) {
        r = cw_sax_emit_string(s, CW_EVT_STRING, 0);
        if (r != CW_OK) return r;
        s->str_span_active = 0;
        s->span_len = 0;
        return cw_sax_scalar_end(s);
    }
    cw_frame *f = &s->stack[s->depth - 1];
    if (f->kind == CW_KIND_OBJ &&
        (f->expect == CW_EXP_KEY || f->expect == CW_EXP_KEY_COMMA)) {
        r = cw_sax_emit_string(s, CW_EVT_KEY, s->depth);
        if (r != CW_OK) return r;
        f->expect = CW_EXP_COLON;
        s->scan = SCAN_NONE;
        s->tok_len = 0;
        s->str_span_active = 0;
        s->span_len = 0;
        return CW_OK;
    }
    if (f->expect != CW_EXP_VALUE && f->expect != CW_EXP_VALUE_COMMA)
        return cw_sax_err(s, CW_ERR_SYNTAX, "unexpected string");
    r = cw_sax_emit_string(s, CW_EVT_STRING, s->depth);
    if (r != CW_OK) return r;
    s->str_span_active = 0;
    s->span_len = 0;
    return cw_sax_scalar_end(s);
}

static int cw_sax_string_char(cw_sax *s, char c) {
    switch (s->sstr) {
    case SSTR_NORMAL:
        if (c == '"') return cw_sax_string_done(s);
        if (c == '\\') {
            s->sstr = SSTR_ESC;
            return CW_OK;
        }
        if ((unsigned char)c < 0x20)
            return cw_sax_err(s, CW_ERR_CONTROL_CHAR,
                              "control character in string");
        return cw_sax_tok_putc(s, c);
    case SSTR_ESC: {
        char out;
        switch (c) {
        case '"': out = '"'; break;
        case '\\': out = '\\'; break;
        case '/': out = '/'; break;
        case 'b': out = '\b'; break;
        case 'f': out = '\f'; break;
        case 'n': out = '\n'; break;
        case 'r': out = '\r'; break;
        case 't': out = '\t'; break;
        case 'u':
            s->sstr = SSTR_U;
            s->hex_rem = 4;
            s->hex = 0;
            return CW_OK;
        default:
            return cw_sax_err(s, CW_ERR_BAD_ESCAPE,
                              "invalid escape sequence");
        }
        int r = cw_sax_tok_putc(s, out);
        if (r != CW_OK) return r;
        s->sstr = SSTR_NORMAL;
        return CW_OK;
    }
    case SSTR_U: {
        unsigned h = cw_hexval(c);
        if (h == 0xFF)
            return cw_sax_err(s, CW_ERR_BAD_ESCAPE, "invalid unicode escape");
        s->hex = (s->hex << 4) | h;
        if (--s->hex_rem == 0) {
            if (s->hex >= 0xD800 && s->hex <= 0xDBFF) {
                s->hi_sur = s->hex;
                s->sstr = SSTR_U_LOW1;
                return CW_OK;
            }
            if (s->hex >= 0xDC00 && s->hex <= 0xDFFF)
                return cw_sax_err(s, CW_ERR_SURROGATE,
                                  "unexpected low surrogate");
            int r = cw_sax_put_utf8(s, s->hex);
            if (r != CW_OK) return r;
            s->sstr = SSTR_NORMAL;
        }
        return CW_OK;
    }
    case SSTR_U_LOW1:
        if (c != '\\')
            return cw_sax_err(s, CW_ERR_SURROGATE,
                              "high surrogate must be followed by \\u");
        s->sstr = SSTR_U_LOW2;
        return CW_OK;
    case SSTR_U_LOW2:
        if (c != 'u')
            return cw_sax_err(s, CW_ERR_SURROGATE,
                              "expected 'u' after '\\'");
        s->sstr = SSTR_U_LOW;
        s->hex_rem = 4;
        s->hex = 0;
        return CW_OK;
    case SSTR_U_LOW: {
        unsigned h = cw_hexval(c);
        if (h == 0xFF)
            return cw_sax_err(s, CW_ERR_BAD_ESCAPE, "invalid unicode escape");
        s->hex = (s->hex << 4) | h;
        if (--s->hex_rem == 0) {
            if (s->hex < 0xDC00 || s->hex > 0xDFFF)
                return cw_sax_err(s, CW_ERR_SURROGATE,
                                  "invalid low surrogate");
            uint32_t cp =
                0x10000 + ((s->hi_sur - 0xD800) << 10) + (s->hex - 0xDC00);
            int r = cw_sax_put_utf8(s, cp);
            if (r != CW_OK) return r;
            s->hi_sur = 0;
            s->sstr = SSTR_NORMAL;
        }
        return CW_OK;
    }
    }
    return CW_OK;
}

static int cw_sax_start_scan(cw_sax *s, int scan, char first) {
    s->scan = scan;
    s->tok_len = 0;
    s->tok_line = s->line;
    s->tok_col = s->col;
    s->tok_off = s->off;
    if (scan == SCAN_STRING) {
        s->sstr = SSTR_NORMAL;
        s->str_span_active = 0;
        s->span_len = 0;
        return CW_OK;
    }
    return cw_sax_tok_putc(s, first);
}

static int cw_sax_push(cw_sax *s, int kind, int expect) {
    if (s->depth >= s->max_depth)
        return cw_sax_err(s, CW_ERR_DEPTH,
                          "maximum nesting depth exceeded");
    cw_frame *f = &s->stack[s->depth];
    f->kind = kind;
    f->expect = expect;
    s->depth++;
    return CW_OK;
}

static int cw_sax_close(cw_sax *s, int kind) {
    cw_frame *f = &s->stack[s->depth - 1];
    if (f->kind != kind)
        return cw_sax_err(s, CW_ERR_SYNTAX, "mismatched container");
    int r = cw_sax_emit_open(
        s, kind == CW_KIND_OBJ ? CW_EVT_OBJECT_END : CW_EVT_ARRAY_END,
        s->depth - 1);
    if (r != CW_OK) return r;
    s->depth--;
    if (s->depth == 0) return cw_sax_emit_done(s);
    return CW_OK;
}

static int cw_sax_scan(cw_sax *s, char c) {
    switch (s->scan) {
    case SCAN_STRING:
        return cw_sax_string_char(s, c);
    case SCAN_NUMBER:
        if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' ||
            c == '+' || c == '-')
            return cw_sax_tok_putc(s, c);
        if (cw_is_term(c)) {
            int r = cw_sax_number_done(s);
            if (r != CW_OK) return r;
            return CW_REDO;
        }
        return cw_sax_err(s, CW_ERR_INVALID_NUMBER,
                          "invalid character in number");
    case SCAN_LITERAL: {
        const char *w = cw_lit_word(s->lit);
        size_t wl = strlen(w);
        if (s->lit_pos < wl) {
            if (c == w[s->lit_pos]) {
                s->lit_pos++;
                return CW_OK;
            }
            return cw_sax_err(s, CW_ERR_INVALID_LITERAL, "invalid literal");
        }
        if (cw_is_term(c)) {
            int r = cw_sax_literal_done(s);
            if (r != CW_OK) return r;
            return CW_REDO;
        }
        return cw_sax_err(s, CW_ERR_INVALID_LITERAL,
                          "invalid character after literal");
    }
    default:
        return CW_OK;
    }
}

static int cw_sax_struct(cw_sax *s, char c) {
    if (c == ' ' || c == '\t' || c == '\r' || c == '\n') return CW_OK;
    if (s->depth == 0) {
        switch (c) {
        case '{': {
            int r = cw_sax_emit_open(s, CW_EVT_OBJECT_BEGIN, 0);
            if (r != CW_OK) return r;
            return cw_sax_push(s, CW_KIND_OBJ, CW_EXP_KEY);
        }
        case '[': {
            int r = cw_sax_emit_open(s, CW_EVT_ARRAY_BEGIN, 0);
            if (r != CW_OK) return r;
            return cw_sax_push(s, CW_KIND_ARR, CW_EXP_VALUE);
        }
        case '"':
            return cw_sax_start_scan(s, SCAN_STRING, 0);
        case 't':
        case 'f':
        case 'n': {
            s->lit = (c == 't') ? 0 : (c == 'f') ? 1 : 2;
            s->lit_pos = 1;
            return cw_sax_start_scan(s, SCAN_LITERAL, c);
        }
        default:
            if ((c >= '0' && c <= '9') || c == '-')
                return cw_sax_start_scan(s, SCAN_NUMBER, c);
            return cw_sax_err(s, CW_ERR_SYNTAX, "expected a JSON value");
        }
    }
    cw_frame *f = &s->stack[s->depth - 1];
    int is_obj = (f->kind == CW_KIND_OBJ);
    switch (c) {
    case '{':
        if (f->expect != CW_EXP_VALUE && f->expect != CW_EXP_VALUE_COMMA)
            return cw_sax_err(s, CW_ERR_SYNTAX, "unexpected '{'");
        {
            int r = cw_sax_emit_open(s, CW_EVT_OBJECT_BEGIN, s->depth);
            if (r != CW_OK) return r;
            f->expect = CW_EXP_COMMA; /* parent now holds one value */
            return cw_sax_push(s, CW_KIND_OBJ, CW_EXP_KEY);
        }
    case '[':
        if (f->expect != CW_EXP_VALUE && f->expect != CW_EXP_VALUE_COMMA)
            return cw_sax_err(s, CW_ERR_SYNTAX, "unexpected '['");
        {
            int r = cw_sax_emit_open(s, CW_EVT_ARRAY_BEGIN, s->depth);
            if (r != CW_OK) return r;
            f->expect = CW_EXP_COMMA;
            return cw_sax_push(s, CW_KIND_ARR, CW_EXP_VALUE);
        }
    case '"':
        if (is_obj && (f->expect == CW_EXP_KEY || f->expect == CW_EXP_KEY_COMMA))
            return cw_sax_start_scan(s, SCAN_STRING, 0);
        if (f->expect == CW_EXP_VALUE || f->expect == CW_EXP_VALUE_COMMA)
            return cw_sax_start_scan(s, SCAN_STRING, 0);
        if (is_obj && f->expect == CW_EXP_COLON)
            return cw_sax_err(s, CW_ERR_SYNTAX, "expected a value after ':'");
        if (is_obj)
            return cw_sax_err(s, CW_ERR_EXPECTED_KEY,
                              "expected an object key");
        return cw_sax_err(s, CW_ERR_SYNTAX, "unexpected string");
    case 't':
    case 'f':
    case 'n': {
        if (f->expect != CW_EXP_VALUE && f->expect != CW_EXP_VALUE_COMMA)
            return cw_sax_err(s, CW_ERR_SYNTAX, "expected a value");
        s->lit = (c == 't') ? 0 : (c == 'f') ? 1 : 2;
        s->lit_pos = 1;
        return cw_sax_start_scan(s, SCAN_LITERAL, c);
    }
    default:
        if ((c >= '0' && c <= '9') || c == '-') {
            if (f->expect != CW_EXP_VALUE && f->expect != CW_EXP_VALUE_COMMA)
                return cw_sax_err(s, CW_ERR_SYNTAX, "expected a value");
            return cw_sax_start_scan(s, SCAN_NUMBER, c);
        }
        if (c == ':') {
            if (!is_obj)
                return cw_sax_err(s, CW_ERR_SYNTAX, "unexpected ':'");
            if (f->expect != CW_EXP_COLON)
                return cw_sax_err(s, CW_ERR_EXPECTED_COLON, "expected ':'");
            f->expect = CW_EXP_VALUE;
            return CW_OK;
        }
        if (c == ',') {
            if (f->expect != CW_EXP_COMMA)
                return cw_sax_err(s, CW_ERR_UNEXPECTED_COMMA,
                                  "unexpected ','");
            f->expect = is_obj ? CW_EXP_KEY_COMMA : CW_EXP_VALUE_COMMA;
            return CW_OK;
        }
        if (c == '}') {
            if (!is_obj)
                return cw_sax_err(s, CW_ERR_SYNTAX, "unexpected '}'");
            if (f->expect != CW_EXP_KEY && f->expect != CW_EXP_COMMA)
                return cw_sax_err(
                    s, f->expect == CW_EXP_KEY_COMMA ? CW_ERR_TRAILING_COMMA
                                                     : CW_ERR_SYNTAX,
                    "unexpected '}'");
            return cw_sax_close(s, CW_KIND_OBJ);
        }
        if (c == ']') {
            if (is_obj)
                return cw_sax_err(s, CW_ERR_SYNTAX, "unexpected ']'");
            if (f->expect != CW_EXP_VALUE && f->expect != CW_EXP_COMMA)
                return cw_sax_err(
                    s,
                    f->expect == CW_EXP_VALUE_COMMA ? CW_ERR_TRAILING_COMMA
                                                    : CW_ERR_SYNTAX,
                    "unexpected ']'");
            return cw_sax_close(s, CW_KIND_ARR);
        }
        return cw_sax_err(s, CW_ERR_SYNTAX, "invalid character");
    }
}

static int cw_sax_string_run(cw_sax *s, const char *data, size_t len,
                             size_t *i) {
    size_t start = *i;
    size_t rem = len - start;
    const char *p = data + start;
    /* Find the closing quote first, then look for backslashes only within
     * the string span. This keeps the total scan linear even for chunks
     * containing many short strings without escapes. */
    const char *q1 = (const char *)memchr(p, '"', rem);
    size_t lim = q1 ? (size_t)(q1 - p) : rem;
    const char *q2 = (const char *)memchr(p, '\\', lim);
    size_t run = q1 ? (size_t)(q1 - p) : rem;
    if (q2 && (size_t)(q2 - p) < run) run = (size_t)(q2 - p);
    for (size_t k = 0; k < run; ++k) {
        if ((unsigned char)p[k] < 0x20) {
            cw_err_at(&s->err, CW_ERR_CONTROL_CHAR, s->line, s->col + k + 1,
                      s->off + k + 1, "control character in string");
            return CW_ERR_CONTROL_CHAR;
        }
    }
    if (run) {
        if (s->str_span_active) {
            s->span_len += run;
        } else {
            int r = cw_sax_tok_append(s, p, run);
            if (r != CW_OK) return r;
        }
        *i += run;
    }
    return CW_OK;
}

int cw_sax_feed(cw_sax *s, const char *data, size_t len) {
    if (!s) return CW_ERR_ARG;
    if (s->err.code) return s->err.code;
    if (!data && len) return cw_sax_err(s, CW_ERR_ARG, "NULL input");
    size_t i = 0;
    int completed = 0;
    while (i < len) {
        /* Bulk-skip whitespace between tokens (common in pretty-printed
         * input) instead of dispatching one character at a time. */
        if (s->scan == SCAN_NONE) {
            while (i < len) {
                char w = data[i];
                if (w != ' ' && w != '\t' && w != '\r' && w != '\n') break;
                i++;
                s->off++;
                if (w == '\n') {
                    s->line++;
                    s->col = 0;
                } else {
                    s->col++;
                }
            }
            if (i >= len) break;
        }
        if (s->scan == SCAN_STRING && s->sstr == SSTR_NORMAL) {
            size_t ni = i;
            int r = cw_sax_string_run(s, data, len, &ni);
            if (r != CW_OK) return r;
            size_t run = ni - i;
            if (run) {
                s->off += run;
                s->col += run;
                i = ni;
                if (i >= len) break;
            }
        }
        char c = data[i++];
        s->off++;
        if (c == '\n') {
            s->line++;
            s->col = 0;
        } else {
            s->col++;
        }
        /* If an escape appears inside a span-backed string, copy the pending
         * span into the token buffer before decoding the escape. */
        if (s->scan == SCAN_STRING && s->sstr == SSTR_NORMAL &&
            s->str_span_active && c == '\\') {
            int r = cw_sax_tok_append(s, s->str_span, s->span_len);
            if (r != CW_OK) return r;
            s->str_span_active = 0;
        }
        for (;;) {
            int r = s->scan == SCAN_NONE ? cw_sax_struct(s, c)
                                         : cw_sax_scan(s, c);
            if (r == CW_REDO) continue;
            if (r != CW_OK) return r;
            break;
        }
        if (s->just_done) {
            completed = 1;
            s->just_done = 0;
        }
        /* A fresh, still-pure string token is tracked as a span into the
         * current input buffer; the DOM consumer copies it directly into the
         * arena, avoiding the intermediate token-buffer copy. */
        if (s->scan == SCAN_STRING && s->sstr == SSTR_NORMAL &&
            !s->str_span_active && s->tok_len == 0) {
            s->str_span_active = 1;
            s->str_span = data + i;
            s->span_len = 0;
        }
    }
    /* Flush a pending span before returning so the caller may reuse or free
     * the input buffer. */
    if (s->str_span_active) {
        int r = cw_sax_tok_append(s, s->str_span, s->span_len);
        if (r != CW_OK) return r;
        s->str_span_active = 0;
    }
    return completed ? CW_DONE : CW_OK;
}

int cw_sax_finish(cw_sax *s) {
    if (!s) return CW_ERR_ARG;
    if (s->err.code) return s->err.code;
    if (s->scan == SCAN_NONE) {
        if (s->depth != 0)
            return cw_sax_err(s, CW_ERR_UNEXPECTED_END,
                              "unexpected end of input (unterminated "
                              "container)");
        return CW_OK;
    }
    if (s->scan == SCAN_STRING)
        return cw_sax_err(s, CW_ERR_UNTERMINATED_STRING,
                          "unterminated string");
    if (s->scan == SCAN_NUMBER) {
        int r = cw_sax_number_done(s);
        if (r != CW_OK) return r;
        if (s->depth != 0)
            return cw_sax_err(s, CW_ERR_UNEXPECTED_END,
                              "unexpected end of input (unterminated "
                              "container)");
        return CW_DONE;
    }
    if (s->scan == SCAN_LITERAL) {
        const char *w = cw_lit_word(s->lit);
        if (s->lit_pos < strlen(w))
            return cw_sax_err(s, CW_ERR_INVALID_LITERAL,
                              "incomplete literal");
        int r = cw_sax_literal_done(s);
        if (r != CW_OK) return r;
        if (s->depth != 0)
            return cw_sax_err(s, CW_ERR_UNEXPECTED_END,
                              "unexpected end of input (unterminated "
                              "container)");
        return CW_DONE;
    }
    return CW_OK;
}

cw_sax *cw_sax_new(cw_sax_cb cb, void *ctx) {
    cw_sax *s = (cw_sax *)calloc(1, sizeof(cw_sax));
    if (!s) return NULL;
    s->cb = cb;
    s->ctx = ctx;
    s->max_depth = CW_DEFAULT_MAX_DEPTH;
    s->stack = (cw_frame *)calloc((size_t)s->max_depth, sizeof(cw_frame));
    if (!s->stack) {
        free(s);
        return NULL;
    }
    s->line = 1;
    s->col = 0;
    return s;
}

void cw_sax_free(cw_sax *s) {
    if (!s) return;
    free(s->tok);
    free(s->stack);
    free(s);
}

void cw_sax_reset(cw_sax *s) {
    if (!s) return;
    s->depth = 0;
    s->scan = SCAN_NONE;
    s->tok_len = 0;
    s->str_span_active = 0;
    s->span_len = 0;
    s->value_count = 0;
    s->just_done = 0;
    s->line = 1;
    s->col = 0;
    s->off = 0;
    memset(&s->err, 0, sizeof(s->err));
}

int cw_sax_set_max_depth(cw_sax *s, int max_depth) {
    if (!s || max_depth < 1 || max_depth > CW_MAX_DEPTH_LIMIT)
        return CW_ERR_ARG;
    cw_frame *ns =
        (cw_frame *)realloc(s->stack, (size_t)max_depth * sizeof(cw_frame));
    if (!ns) return CW_ERR_NOMEM;
    s->stack = ns;
    s->max_depth = max_depth;
    return CW_OK;
}

int cw_sax_depth(const cw_sax *s) {
    return s ? s->depth : 0;
}

int cw_sax_values(const cw_sax *s) {
    return s ? s->value_count : 0;
}

const cw_error *cw_sax_error(const cw_sax *s) {
    return s ? &s->err : NULL;
}

/* Streaming DOM consumer (builds a document from SAX events)          */
struct cw_sdom {
    cw_doc *doc;
    cw_sax *sax;
    cw_value **stack;
    int depth;
    int max_depth;
    char *key;
    size_t key_len;
    int done;
    cw_error err;
};

static int cw_sdom_attach(cw_sdom *sd, cw_value *v) {
    if (!v) {
        cw_err_at(&sd->err, CW_ERR_NOMEM, 0, 0, 0, "out of memory");
        return CW_ERR_NOMEM;
    }
    if (sd->depth == 0) {
        sd->doc->root = v;
        return CW_OK;
    }
    cw_value *parent = sd->stack[sd->depth - 1];
    if (parent->type == CW_ARRAY) return cw_array_append(parent, v);
    if (!sd->key) {
        cw_err_at(&sd->err, CW_ERR_SYNTAX, 0, 0, 0, "missing object key");
        return CW_ERR_SYNTAX;
    }
    int r = cw_object_set(parent, sd->key, sd->key_len, v);
    if (r != CW_OK)
        cw_err_at(&sd->err, r, 0, 0, 0, "object insert failed");
    return r;
}

static int cw_sdom_cb(void *ctx, const cw_event *ev) {
    cw_sdom *sd = (cw_sdom *)ctx;
    if (sd->done) {
        cw_err_at(&sd->err, CW_ERR_TRAILING_DATA, 0, 0, 0,
                  "multiple top-level values");
        return CW_ERR_TRAILING_DATA;
    }
    switch (ev->type) {
    case CW_EVT_NULL:
        return cw_sdom_attach(sd, cw_new_null(sd->doc));
    case CW_EVT_BOOL:
        return cw_sdom_attach(sd, cw_new_bool(sd->doc, ev->u.b));
    case CW_EVT_INT:
        return cw_sdom_attach(sd, cw_new_int(sd->doc, ev->u.i));
    case CW_EVT_DOUBLE:
        return cw_sdom_attach(sd, cw_new_double(sd->doc, ev->u.d));
    case CW_EVT_STRING:
        return cw_sdom_attach(
            sd, cw_new_string(sd->doc, ev->u.str.data, ev->u.str.len));
    case CW_EVT_KEY: {
        sd->key = cw_arena_strndup(&sd->doc->arena, ev->u.str.data,
                                   ev->u.str.len);
        if (!sd->key) {
            cw_err_at(&sd->err, CW_ERR_NOMEM, 0, 0, 0, "out of memory");
            return CW_ERR_NOMEM;
        }
        sd->key_len = ev->u.str.len;
        return CW_OK;
    }
    case CW_EVT_ARRAY_BEGIN:
    case CW_EVT_OBJECT_BEGIN: {
        if (sd->depth >= sd->max_depth) {
            cw_err_at(&sd->err, CW_ERR_DEPTH, 0, 0, 0,
                      "maximum nesting depth exceeded");
            return CW_ERR_DEPTH;
        }
        cw_value *v = (ev->type == CW_EVT_ARRAY_BEGIN)
                          ? cw_new_array(sd->doc)
                          : cw_new_object(sd->doc);
        if (!v) {
            cw_err_at(&sd->err, CW_ERR_NOMEM, 0, 0, 0, "out of memory");
            return CW_ERR_NOMEM;
        }
        if (sd->depth > 0) {
            int r = cw_sdom_attach(sd, v);
            if (r != CW_OK) return r;
        }
        sd->stack[sd->depth++] = v;
        return CW_OK;
    }
    case CW_EVT_ARRAY_END:
    case CW_EVT_OBJECT_END: {
        if (sd->depth == 0) {
            cw_err_at(&sd->err, CW_ERR_SYNTAX, 0, 0, 0,
                      "unexpected container end");
            return CW_ERR_SYNTAX;
        }
        cw_value *top = sd->stack[sd->depth - 1];
        int want_arr = (ev->type == CW_EVT_ARRAY_END);
        if ((top->type == CW_ARRAY) != want_arr) {
            cw_err_at(&sd->err, CW_ERR_SYNTAX, 0, 0, 0,
                      "mismatched container");
            return CW_ERR_SYNTAX;
        }
        sd->depth--;
        if (sd->depth == 0) sd->doc->root = top;
        return CW_OK;
    }
    case CW_EVT_DONE:
        sd->done = 1;
        return CW_OK;
    }
    return CW_ERR_SYNTAX;
}

static cw_sdom *cw_sdom_new(cw_doc *d) {
    cw_sdom *sd = (cw_sdom *)calloc(1, sizeof(cw_sdom));
    if (!sd) return NULL;
    sd->doc = d;
    sd->max_depth = d->max_depth;
    sd->stack =
        (cw_value **)calloc((size_t)d->max_depth, sizeof(cw_value *));
    if (!sd->stack) {
        free(sd);
        return NULL;
    }
    sd->sax = cw_sax_new(cw_sdom_cb, sd);
    if (!sd->sax) {
        free(sd->stack);
        free(sd);
        return NULL;
    }
    cw_sax_set_max_depth(sd->sax, d->max_depth);
    return sd;
}

static void cw_sdom_free(cw_sdom *sd) {
    if (!sd) return;
    if (sd->sax) cw_sax_free(sd->sax);
    free(sd->stack);
    free(sd);
}

static int cw_sdom_feed(cw_sdom *sd, const char *data, size_t len) {
    if (!sd) return CW_ERR_ARG;
    if (sd->err.code) return sd->err.code;
    int r = cw_sax_feed(sd->sax, data, len);
    if (r < 0 && !sd->err.code) sd->err = sd->sax->err;
    return r;
}

static int cw_sdom_finish(cw_sdom *sd) {
    if (!sd) return CW_ERR_ARG;
    if (sd->err.code) return sd->err.code;
    int r = cw_sax_finish(sd->sax);
    if (r < 0) {
        sd->err = sd->sax->err;
        return r;
    }
    if (!sd->doc->root) {
        cw_err_at(&sd->err, CW_ERR_SYNTAX, 0, 0, 0, "empty input");
        return CW_ERR_SYNTAX;
    }
    return CW_OK;
}

/* Document API                                                        */
cw_doc *cw_doc_new(void) {
    cw_doc *d = (cw_doc *)calloc(1, sizeof(cw_doc));
    if (!d) return NULL;
    cw_arena_init(&d->arena, CW_DEFAULT_BLOCK_SIZE);
    d->max_depth = CW_DEFAULT_MAX_DEPTH;
    return d;
}

void cw_doc_free(cw_doc *d) {
    if (!d) return;
    cw_sdom_free(d->sdom);
    cw_arena_destroy_blocks(&d->arena);
    free(d);
}

void cw_doc_clear(cw_doc *d) {
    if (!d) return;
    cw_arena_reset(&d->arena);
    d->root = NULL;
    memset(&d->err, 0, sizeof(d->err));
    cw_sdom_free(d->sdom);
    d->sdom = NULL;
}

int cw_doc_set_max_depth(cw_doc *d, int max_depth) {
    if (!d || max_depth < 1 || max_depth > CW_MAX_DEPTH_LIMIT)
        return CW_ERR_ARG;
    d->max_depth = max_depth;
    return CW_OK;
}

int cw_doc_max_depth(const cw_doc *d) {
    return d ? d->max_depth : 0;
}

cw_value *cw_doc_root(const cw_doc *d) {
    return d ? d->root : NULL;
}

void cw_doc_set_root(cw_doc *d, cw_value *v) {
    if (d) d->root = v;
}

const cw_error *cw_doc_error(const cw_doc *d) {
    return d ? &d->err : NULL;
}

void cw_doc_mem_stats(const cw_doc *d, size_t *mapped, size_t *used) {
    if (!d) return;
    if (mapped) *mapped = cw_arena_mapped(&d->arena);
    if (used) *used = cw_arena_used(&d->arena);
}

int cw_doc_parse_begin(cw_doc *d) {
    if (!d) return CW_ERR_ARG;
    cw_doc_clear(d);
    d->sdom = cw_sdom_new(d);
    if (!d->sdom) {
        cw_err_at(&d->err, CW_ERR_NOMEM, 0, 0, 0, "out of memory");
        return CW_ERR_NOMEM;
    }
    return CW_OK;
}

int cw_doc_parse_chunk(cw_doc *d, const char *data, size_t len) {
    if (!d || !d->sdom) return CW_ERR_ARG;
    int r = cw_sdom_feed(d->sdom, data, len);
    if (r < 0) d->err = d->sdom->err;
    return r;
}

int cw_doc_parse_end(cw_doc *d) {
    if (!d || !d->sdom) return CW_ERR_ARG;
    int r = cw_sdom_finish(d->sdom);
    d->err = d->sdom->err;
    cw_sdom_free(d->sdom);
    d->sdom = NULL;
    return r;
}

static void cw_doc_parse_abort(cw_doc *d) {
    if (!d) return;
    cw_sdom_free(d->sdom);
    d->sdom = NULL;
}

int cw_doc_parse(cw_doc *d, const char *text, size_t len) {
    if (!d) return CW_ERR_ARG;
    int r = cw_doc_parse_begin(d);
    if (r != CW_OK) return r;
    r = cw_doc_parse_chunk(d, text, len);
    if (r < 0) {
        cw_doc_parse_abort(d);
        return r;
    }
    return cw_doc_parse_end(d);
}

int cw_doc_parse_cstr(cw_doc *d, const char *text) {
    if (!text) return CW_ERR_ARG;
    return cw_doc_parse(d, text, strlen(text));
}

int cw_doc_parse_file(cw_doc *d, const char *path) {
    if (!d || !path) return CW_ERR_ARG;
    FILE *f = fopen(path, "rb");
    if (!f) {
        cw_err_at(&d->err, CW_ERR_IO, 0, 0, 0, "cannot open '%s'", path);
        return CW_ERR_IO;
    }
    int r = cw_doc_parse_begin(d);
    if (r == CW_OK) {
        char buf[65536];
        for (;;) {
            size_t n = fread(buf, 1, sizeof(buf), f);
            if (n == 0) break;
            r = cw_doc_parse_chunk(d, buf, n);
            if (r < 0) break;
        }
        if (r >= 0 && ferror(f)) {
            cw_err_at(&d->err, CW_ERR_IO, 0, 0, 0, "read error on '%s'",
                      path);
            r = CW_ERR_IO;
        }
        if (r >= 0)
            r = cw_doc_parse_end(d);
        else
            cw_doc_parse_abort(d);
    }
    fclose(f);
    return r;
}

cw_doc *cw_parse(const char *text, size_t len) {
    if (!text && len) return NULL;
    cw_doc *d = cw_doc_new();
    if (!d) return NULL;
    if (cw_doc_parse(d, text, len) != CW_OK) {
        cw_doc_free(d);
        return NULL;
    }
    return d;
}

cw_doc *cw_parse_cstr(const char *text) {
    return text ? cw_parse(text, strlen(text)) : NULL;
}

cw_doc *cw_load_file(const char *path) {
    if (!path) return NULL;
    cw_doc *d = cw_doc_new();
    if (!d) return NULL;
    if (cw_doc_parse_file(d, path) != CW_OK) {
        cw_doc_free(d);
        return NULL;
    }
    return d;
}

/* Value construction & accessors                                      */
cw_value *cw_new_null(cw_doc *d) {
    return d ? cw_value_new(d, CW_NULL) : NULL;
}

cw_value *cw_new_bool(cw_doc *d, bool b) {
    cw_value *v = d ? cw_value_new(d, CW_BOOL) : NULL;
    if (v) v->u.b = b;
    return v;
}

cw_value *cw_new_int(cw_doc *d, int64_t i) {
    cw_value *v = d ? cw_value_new(d, CW_INT) : NULL;
    if (v) v->u.i = i;
    return v;
}

cw_value *cw_new_double(cw_doc *d, double x) {
    cw_value *v = d ? cw_value_new(d, CW_DOUBLE) : NULL;
    if (v) v->u.d = x;
    return v;
}

cw_value *cw_new_string(cw_doc *d, const char *s, size_t len) {
    if (!d || (!s && len)) return NULL;
    cw_value *v = cw_value_new(d, CW_STRING);
    if (!v) return NULL;
    v->u.str.data = cw_arena_strndup(&d->arena, s ? s : "", len);
    if (!v->u.str.data) return NULL;
    v->u.str.len = len;
    return v;
}

cw_value *cw_new_string_c(cw_doc *d, const char *s) {
    if (!s) return NULL;
    return cw_new_string(d, s, strlen(s));
}

cw_value *cw_new_array(cw_doc *d) {
    return d ? cw_value_new(d, CW_ARRAY) : NULL;
}

cw_value *cw_new_object(cw_doc *d) {
    return d ? cw_value_new(d, CW_OBJECT) : NULL;
}

cw_type cw_typeof(const cw_value *v) {
    return v ? v->type : CW_NULL;
}

const char *cw_type_name(cw_type t) {
    static const char *const names[] = {"null",   "boolean", "integer",
                                        "number", "string",  "array",
                                        "object"};
    if ((int)t >= 0 && (int)t <= 6) return names[(int)t];
    return "unknown";
}

bool cw_is_number(const cw_value *v) {
    return v && (v->type == CW_INT || v->type == CW_DOUBLE);
}

int cw_as_bool(const cw_value *v, bool *out) {
    if (!v || v->type != CW_BOOL || !out) return CW_ERR_TYPE;
    *out = v->u.b;
    return CW_OK;
}

int cw_as_int(const cw_value *v, int64_t *out) {
    if (!v || !out) return CW_ERR_TYPE;
    if (v->type == CW_INT) {
        *out = v->u.i;
        return CW_OK;
    }
    if (v->type == CW_DOUBLE) {
        double d = v->u.d;
        if (isfinite(d) && d >= -9.223372036854775808e18 &&
            d < 9.223372036854775808e18) {
            *out = (int64_t)d;
            return CW_OK;
        }
    }
    return CW_ERR_TYPE;
}

int cw_as_double(const cw_value *v, double *out) {
    if (!v || !out) return CW_ERR_TYPE;
    if (v->type == CW_DOUBLE) {
        *out = v->u.d;
        return CW_OK;
    }
    if (v->type == CW_INT) {
        *out = (double)v->u.i;
        return CW_OK;
    }
    return CW_ERR_TYPE;
}

const char *cw_string_value(const cw_value *v, size_t *len) {
    if (!v || v->type != CW_STRING) return NULL;
    if (len) *len = v->u.str.len;
    return v->u.str.data;
}

const char *cw_string_cstr(const cw_value *v) {
    if (!v || v->type != CW_STRING) return NULL;
    return v->u.str.data;
}

size_t cw_string_len(const cw_value *v) {
    return (v && v->type == CW_STRING) ? v->u.str.len : 0;
}

size_t cw_length(const cw_value *v) {
    if (!v) return 0;
    switch (v->type) {
    case CW_STRING:
        return v->u.str.len;
    case CW_ARRAY:
        return v->u.arr.len;
    case CW_OBJECT:
        return v->u.obj.len;
    default:
        return 0;
    }
}

/* Deep copy, equality, path lookup                                    */
static cw_value *cw_copy_make_node(const cw_value *v, cw_doc *dst) {
    switch (v->type) {
    case CW_NULL:
        return cw_new_null(dst);
    case CW_BOOL:
        return cw_new_bool(dst, v->u.b);
    case CW_INT:
        return cw_new_int(dst, v->u.i);
    case CW_DOUBLE:
        return cw_new_double(dst, v->u.d);
    case CW_STRING:
        return cw_new_string(dst, v->u.str.data, v->u.str.len);
    case CW_ARRAY:
        return cw_new_array(dst);
    case CW_OBJECT:
        return cw_new_object(dst);
    }
    return NULL;
}

typedef struct cw_copy_frame {
    const cw_value *src;   /* source container being copied */
    cw_value *dst;         /* matching destination container */
    size_t idx;
} cw_copy_frame;

static int cw_copy_frame_push(cw_copy_frame **st, size_t *n, size_t *cap,
                              const cw_value *src, cw_value *dst) {
    if (*n == *cap) {
        size_t ncap = *cap ? *cap * 2 : 64;
        cw_copy_frame *ns =
            (cw_copy_frame *)realloc(*st, ncap * sizeof(*ns));
        if (!ns) return CW_ERR_NOMEM;
        *st = ns;
        *cap = ncap;
    }
    (*st)[*n].src = src;
    (*st)[*n].dst = dst;
    (*st)[*n].idx = 0;
    (*n)++;
    return CW_OK;
}

/* Iterative deep copy: supports the full nesting range of the parser
 * (up to CW_MAX_DEPTH_LIMIT) without consuming C stack space. */
cw_value *cw_value_deep_copy(const cw_value *v, cw_doc *dst) {
    if (!v || !dst) return NULL;
    cw_value *root = cw_copy_make_node(v, dst);
    if (!root) return NULL;
    if (v->type != CW_ARRAY && v->type != CW_OBJECT) return root;

    cw_copy_frame *st = NULL;
    size_t n = 0, cap = 0;
    int ok = 1;
    if (cw_copy_frame_push(&st, &n, &cap, v, root) != CW_OK) {
        free(st);
        return NULL;
    }
    while (n > 0 && ok) {
        cw_copy_frame *f = &st[n - 1];
        if (f->src->type == CW_ARRAY) {
            if (f->idx >= f->src->u.arr.len) {
                n--;
                continue;
            }
            const cw_value *child = f->src->u.arr.items[f->idx];
            cw_value *c = cw_copy_make_node(child, dst);
            if (!c || cw_array_append(f->dst, c) != CW_OK) {
                ok = 0;
                break;
            }
            f->idx++;
            if (child->type == CW_ARRAY || child->type == CW_OBJECT) {
                if (cw_copy_frame_push(&st, &n, &cap, child, c) != CW_OK) {
                    ok = 0;
                    break;
                }
            }
        } else {
            if (f->idx >= f->src->u.obj.len) {
                n--;
                continue;
            }
            const cw_pair *pr = &f->src->u.obj.pairs[f->idx];
            cw_value *c = cw_copy_make_node(pr->value, dst);
            if (!c ||
                cw_object_set(f->dst, pr->key, pr->key_len, c) != CW_OK) {
                ok = 0;
                break;
            }
            f->idx++;
            if (pr->value->type == CW_ARRAY || pr->value->type == CW_OBJECT) {
                if (cw_copy_frame_push(&st, &n, &cap, pr->value, c) !=
                    CW_OK) {
                    ok = 0;
                    break;
                }
            }
        }
    }
    free(st);
    return ok ? root : NULL;
}

/* Compares scalar values only; containers return 0 here and are handled by
 * the iterative driver. */
static int cw_eq_scalar(const cw_value *a, const cw_value *b) {
    if (a->type != b->type) {
        if (a->type == CW_INT && b->type == CW_DOUBLE)
            return (double)a->u.i == b->u.d;
        if (a->type == CW_DOUBLE && b->type == CW_INT)
            return a->u.d == (double)b->u.i;
        return 0;
    }
    switch (a->type) {
    case CW_NULL:
        return 1;
    case CW_BOOL:
        return a->u.b == b->u.b;
    case CW_INT:
        return a->u.i == b->u.i;
    case CW_DOUBLE:
        return a->u.d == b->u.d;
    case CW_STRING:
        return a->u.str.len == b->u.str.len &&
               memcmp(a->u.str.data, b->u.str.data, a->u.str.len) == 0;
    default:
        return 0;
    }
}

typedef struct cw_eq_frame {
    const cw_value *a, *b;
    size_t idx;
    int is_obj;
} cw_eq_frame;

static int cw_eq_frame_push(cw_eq_frame **st, size_t *n, size_t *cap,
                            const cw_value *a, const cw_value *b) {
    if (*n == *cap) {
        size_t ncap = *cap ? *cap * 2 : 64;
        cw_eq_frame *ns = (cw_eq_frame *)realloc(*st, ncap * sizeof(*ns));
        if (!ns) return CW_ERR_NOMEM;
        *st = ns;
        *cap = ncap;
    }
    (*st)[*n].a = a;
    (*st)[*n].b = b;
    (*st)[*n].idx = 0;
    (*st)[*n].is_obj = (a->type == CW_OBJECT);
    (*n)++;
    return CW_OK;
}

/* Iterative deep equality: handles any nesting depth without recursion. */
int cw_value_equal(const cw_value *a, const cw_value *b) {
    if (a == b) return 1;
    if (!a || !b) return 0;
    if (a->type == CW_ARRAY || a->type == CW_OBJECT) {
        if (b->type != a->type) return 0;
        size_t alen =
            a->type == CW_ARRAY ? a->u.arr.len : a->u.obj.len;
        size_t blen =
            b->type == CW_ARRAY ? b->u.arr.len : b->u.obj.len;
        if (alen != blen) return 0;
    } else {
        return cw_eq_scalar(a, b);
    }
    cw_eq_frame *st = NULL;
    size_t n = 0, cap = 0;
    int result = 1;
    if (cw_eq_frame_push(&st, &n, &cap, a, b) != CW_OK) {
        free(st);
        return 0;
    }
    while (n > 0 && result) {
        cw_eq_frame *f = &st[n - 1];
        const cw_value *ca, *cb;
        if (!f->is_obj) {
            if (f->idx >= f->a->u.arr.len) {
                n--;
                continue;
            }
            ca = f->a->u.arr.items[f->idx];
            cb = f->b->u.arr.items[f->idx];
            f->idx++;
        } else {
            if (f->idx >= f->a->u.obj.len) {
                n--;
                continue;
            }
            const cw_pair *pa = &f->a->u.obj.pairs[f->idx];
            cb = cw_object_get_len(f->b, pa->key, pa->key_len);
            f->idx++;
            if (!cb) {
                result = 0;
                break;
            }
            ca = pa->value;
        }
        if (ca->type == CW_ARRAY || ca->type == CW_OBJECT) {
            if (cb->type != ca->type) {
                result = 0;
                break;
            }
            size_t calen =
                ca->type == CW_ARRAY ? ca->u.arr.len : ca->u.obj.len;
            size_t cblen =
                cb->type == CW_ARRAY ? cb->u.arr.len : cb->u.obj.len;
            if (calen != cblen) {
                result = 0;
                break;
            }
            if (cw_eq_frame_push(&st, &n, &cap, ca, cb) != CW_OK) {
                result = 0;
                break;
            }
        } else if (!cw_eq_scalar(ca, cb)) {
            result = 0;
            break;
        }
    }
    free(st);
    return result;
}

cw_value *cw_get_path(const cw_value *root, const char *path) {
    if (!root || !path) return NULL;
    cw_value *cur = (cw_value *)root;
    const char *p = path;
    while (*p && cur) {
        if (*p == '.') {
            p++;
            continue;
        }
        if (*p == '[') {
            if (cur->type != CW_ARRAY) return NULL;
            p++;
            size_t idx = 0;
            int any = 0;
            while (*p >= '0' && *p <= '9') {
                idx = idx * 10 + (size_t)(*p - '0');
                any = 1;
                p++;
            }
            if (!any || *p != ']') return NULL;
            p++;
            cur = cw_array_get(cur, idx);
            continue;
        }
        const char *start = p;
        while (*p && *p != '.' && *p != '[') p++;
        if (cur->type != CW_OBJECT) return NULL;
        cur = cw_object_get_len(cur, start, (size_t)(p - start));
    }
    return cur;
}

/* Serialization                                                       */
typedef struct cw_fout cw_fout;
typedef struct cw_bout cw_bout;

typedef struct cw_out {
    int kind;              /* 0 = file, 1 = buffer */
    union {
        cw_fout *fo;
        cw_bout *bo;
    } u;
} cw_out;

typedef struct cw_fout {
    FILE *f;
    char buf[16384];
    size_t n;
    int err;
} cw_fout;

static int cw_fout_flush(cw_fout *o) {
    if (o->err) return CW_ERR_IO;
    if (o->n) {
        if (fwrite(o->buf, 1, o->n, o->f) != o->n) {
            o->err = 1;
            return CW_ERR_IO;
        }
        o->n = 0;
    }
    return CW_OK;
}

static int cw_fout_write(void *ud, const void *data, size_t len) {
    cw_fout *o = (cw_fout *)ud;
    if (o->err) return CW_ERR_IO;
    if (len >= sizeof(o->buf)) {
        int r = cw_fout_flush(o);
        if (r != CW_OK) return r;
        if (fwrite(data, 1, len, o->f) != len) {
            o->err = 1;
            return CW_ERR_IO;
        }
        return CW_OK;
    }
    if (o->n + len > sizeof(o->buf)) {
        int r = cw_fout_flush(o);
        if (r != CW_OK) return r;
    }
    memcpy(o->buf + o->n, data, len);
    o->n += len;
    return CW_OK;
}

typedef struct cw_bout {
    char *p;
    size_t n, cap;
    int oom;
} cw_bout;

static int cw_bout_write(void *ud, const void *data, size_t len) {
    cw_bout *o = (cw_bout *)ud;
    if (o->oom) return CW_ERR_NOMEM;
    if (o->n + len > o->cap) {
        size_t ncap = o->cap ? o->cap : 128;
        while (ncap < o->n + len) ncap *= 2;
        char *np = (char *)realloc(o->p, ncap);
        if (!np) {
            o->oom = 1;
            return CW_ERR_NOMEM;
        }
        o->p = np;
        o->cap = ncap;
    }
    if (len) memcpy(o->p + o->n, data, len);
    o->n += len;
    return CW_OK;
}

static int cw_out_write(cw_out *o, const void *data, size_t len) {
    return o->kind == 0 ? cw_fout_write(o->u.fo, data, len)
                        : cw_bout_write(o->u.bo, data, len);
}

static int cw_dump_indent(cw_out *o, int level, size_t indent) {
    static const char spaces[65] =
        "                                                                ";
    size_t total = (size_t)level * indent;
    while (total) {
        size_t k = total > 64 ? 64 : total;
        int r = cw_out_write(o, spaces, k);
        if (r != CW_OK) return r;
        total -= k;
    }
    return CW_OK;
}

static int cw_dump_escaped(cw_out *o, const char *s, size_t n) {
    static const char hexd[] = "0123456789abcdef";
    int r = cw_out_write(o, "\"", 1);
    if (r != CW_OK) return r;
    size_t i = 0;
    while (i < n) {
        size_t j = i;
        while (j < n && s[j] != '"' && s[j] != '\\' &&
               (unsigned char)s[j] >= 0x20)
            j++;
        if (j > i) {
            r = cw_out_write(o, s + i, j - i);
            if (r != CW_OK) return r;
        }
        if (j == n) break;
        unsigned char c = (unsigned char)s[j];
        char esc[8];
        size_t elen = 0;
        switch (c) {
        case '"':
            esc[elen++] = '\\';
            esc[elen++] = '"';
            break;
        case '\\':
            esc[elen++] = '\\';
            esc[elen++] = '\\';
            break;
        case '\b':
            esc[elen++] = '\\';
            esc[elen++] = 'b';
            break;
        case '\f':
            esc[elen++] = '\\';
            esc[elen++] = 'f';
            break;
        case '\n':
            esc[elen++] = '\\';
            esc[elen++] = 'n';
            break;
        case '\r':
            esc[elen++] = '\\';
            esc[elen++] = 'r';
            break;
        case '\t':
            esc[elen++] = '\\';
            esc[elen++] = 't';
            break;
        default:
            esc[elen++] = '\\';
            esc[elen++] = 'u';
            esc[elen++] = '0';
            esc[elen++] = '0';
            esc[elen++] = hexd[(c >> 4) & 0xF];
            esc[elen++] = hexd[c & 0xF];
            break;
        }
        r = cw_out_write(o, esc, elen);
        if (r != CW_OK) return r;
        i = j + 1;
    }
    return cw_out_write(o, "\"", 1);
}

static int cw_dtoa(double d, char *buf, size_t cap) {
    int n = snprintf(buf, cap, "%.15g", d);
    if (n < 0 || (size_t)n >= cap) return -1;
    if (cw_strtod_local(buf, NULL) != d) {
        n = snprintf(buf, cap, "%.17g", d);
        if (n < 0 || (size_t)n >= cap) return -1;
    }
    return n;
}

static int cw_dump_scalar(cw_out *o, const cw_value *v) {
    switch (v->type) {
    case CW_NULL:
        return cw_out_write(o, "null", 4);
    case CW_BOOL:
        return cw_out_write(o, v->u.b ? "true" : "false",
                            v->u.b ? 4 : 5);
    case CW_INT: {
        char buf[32];
        int n = snprintf(buf, sizeof(buf), "%lld", (long long)v->u.i);
        if (n < 0 || (size_t)n >= sizeof(buf)) return CW_ERR_ARG;
        return cw_out_write(o, buf, (size_t)n);
    }
    case CW_DOUBLE: {
        if (!isfinite(v->u.d)) return CW_ERR_NONFINITE;
        char buf[64];
        int n = cw_dtoa(v->u.d, buf, sizeof(buf));
        if (n < 0) return CW_ERR_NONFINITE;
        return cw_out_write(o, buf, (size_t)n);
    }
    case CW_STRING:
        return cw_dump_escaped(o, v->u.str.data, v->u.str.len);
    default:
        return CW_ERR_TYPE;
    }
}

typedef struct cw_dump_frame {
    const cw_value *v;
    int level;
    size_t idx;
    int state;   /* 0 = opening written, 1 = iterating children */
} cw_dump_frame;

static int cw_dump_frame_push(cw_dump_frame **st, size_t *n, size_t *cap,
                              const cw_value *v, int level) {
    if (*n == *cap) {
        size_t ncap = *cap ? *cap * 2 : 64;
        cw_dump_frame *ns =
            (cw_dump_frame *)realloc(*st, ncap * sizeof(*ns));
        if (!ns) return CW_ERR_NOMEM;
        *st = ns;
        *cap = ncap;
    }
    (*st)[*n].v = v;
    (*st)[*n].level = level;
    (*st)[*n].idx = 0;
    (*st)[*n].state = 0;
    (*n)++;
    return CW_OK;
}

/* Iterative serializer: supports the full nesting range of the parser
 * (up to CW_MAX_DEPTH_LIMIT) without consuming C stack space. */
static int cw_dump_value(cw_out *o, const cw_value *v, size_t indent) {
    if (!v) return CW_ERR_ARG;
    cw_dump_frame *st = NULL;
    size_t n = 0, cap = 0;
    int r = cw_dump_frame_push(&st, &n, &cap, v, 0);
    if (r != CW_OK) {
        free(st);
        return r;
    }
    while (n > 0) {
        cw_dump_frame *f = &st[n - 1];
        if (f->state == 0) {
            if (f->v->type == CW_ARRAY || f->v->type == CW_OBJECT) {
                int is_arr = (f->v->type == CW_ARRAY);
                size_t len = is_arr ? f->v->u.arr.len : f->v->u.obj.len;
                if (len == 0) {
                    r = cw_out_write(o, is_arr ? "[]" : "{}", 2);
                    if (r != CW_OK) goto out;
                    n--;
                    continue;
                }
                r = cw_out_write(o, is_arr ? "[" : "{", 1);
                if (r != CW_OK) goto out;
                if (indent) {
                    r = cw_out_write(o, "\n", 1);
                    if (r != CW_OK) goto out;
                    r = cw_dump_indent(o, f->level + 1, indent);
                    if (r != CW_OK) goto out;
                }
                f->state = 1;
                continue;
            }
            r = cw_dump_scalar(o, f->v);
            if (r != CW_OK) goto out;
            n--;
            continue;
        }
        /* state 1: emit children */
        int is_arr = (f->v->type == CW_ARRAY);
        size_t len = is_arr ? f->v->u.arr.len : f->v->u.obj.len;
        if (f->idx >= len) {
            if (indent) {
                r = cw_out_write(o, "\n", 1);
                if (r != CW_OK) goto out;
                r = cw_dump_indent(o, f->level, indent);
                if (r != CW_OK) goto out;
            }
            r = cw_out_write(o, is_arr ? "]" : "}", 1);
            if (r != CW_OK) goto out;
            n--;
            continue;
        }
        if (f->idx > 0) {
            r = cw_out_write(o, ",", 1);
            if (r != CW_OK) goto out;
            if (indent) {
                r = cw_out_write(o, "\n", 1);
                if (r != CW_OK) goto out;
                r = cw_dump_indent(o, f->level + 1, indent);
                if (r != CW_OK) goto out;
            }
        }
        const cw_value *child;
        if (is_arr) {
            child = f->v->u.arr.items[f->idx];
        } else {
            const cw_pair *pr = &f->v->u.obj.pairs[f->idx];
            r = cw_dump_escaped(o, pr->key, pr->key_len);
            if (r != CW_OK) goto out;
            r = cw_out_write(o, indent ? ": " : ":", indent ? 2 : 1);
            if (r != CW_OK) goto out;
            child = pr->value;
        }
        f->idx++;
        if (child->type == CW_ARRAY || child->type == CW_OBJECT) {
            size_t clen = child->type == CW_ARRAY ? child->u.arr.len
                                                  : child->u.obj.len;
            if (clen == 0) {
                r = cw_out_write(o, child->type == CW_ARRAY ? "[]" : "{}",
                                 2);
                if (r != CW_OK) goto out;
            } else {
                r = cw_dump_frame_push(&st, &n, &cap, child, f->level + 1);
                if (r != CW_OK) goto out;
            }
        } else {
            r = cw_dump_scalar(o, child);
            if (r != CW_OK) goto out;
        }
    }
out:
    free(st);
    return r;
}

int cw_dump_file(const cw_value *v, FILE *f, size_t indent) {
    if (!v || !f) return CW_ERR_ARG;
    cw_fout fo;
    fo.f = f;
    fo.n = 0;
    fo.err = 0;
    cw_out out;
    out.kind = 0;
    out.u.fo = &fo;
    int r = cw_dump_value(&out, v, indent);
    if (r == CW_OK) r = cw_fout_flush(&fo);
    return r;
}

int cw_dump_path(const cw_value *v, const char *path, size_t indent) {
    if (!v || !path) return CW_ERR_ARG;
    FILE *f = fopen(path, "wb");
    if (!f) return CW_ERR_IO;
    int r = cw_dump_file(v, f, indent);
    if (fclose(f) != 0 && r == CW_OK) r = CW_ERR_IO;
    return r;
}

char *cw_dump_string(cw_doc *doc, const cw_value *v, size_t indent,
                     size_t *out_len) {
    if (!doc || !v) return NULL;
    cw_bout bo;
    bo.p = NULL;
    bo.n = 0;
    bo.cap = 0;
    bo.oom = 0;
    cw_out out;
    out.kind = 1;
    out.u.bo = &bo;
    int r = cw_dump_value(&out, v, indent);
    if (r != CW_OK) {
        free(bo.p);
        return NULL;
    }
    char *res = cw_arena_strndup(&doc->arena, bo.p ? bo.p : "", bo.n);
    if (out_len) *out_len = bo.n;
    free(bo.p);
    return res;
}

char *cw_dump_malloc(const cw_value *v, size_t indent, size_t *out_len) {
    if (!v) return NULL;
    cw_bout bo;
    bo.p = NULL;
    bo.n = 0;
    bo.cap = 0;
    bo.oom = 0;
    cw_out out;
    out.kind = 1;
    out.u.bo = &bo;
    int r = cw_dump_value(&out, v, indent);
    if (r == CW_OK) r = cw_bout_write(out.u.bo, "", 1);
    if (r != CW_OK) {
        free(bo.p);
        return NULL;
    }
    if (out_len) *out_len = bo.n - 1;
    return bo.p;
}

/* Validity checking (no DOM built)                                    */
int cw_validate(const char *text, size_t len, cw_error *err) {
    if (!text && len) {
        if (err) cw_err_at(err, CW_ERR_ARG, 0, 0, 0, "NULL input");
        return CW_ERR_ARG;
    }
    cw_sax *s = cw_sax_new(NULL, NULL);
    if (!s) {
        if (err) cw_err_at(err, CW_ERR_NOMEM, 0, 0, 0, "out of memory");
        return CW_ERR_NOMEM;
    }
    int r = cw_sax_feed(s, text, len);
    if (r >= 0) r = cw_sax_finish(s);
    if (r >= 0) {
        if (s->value_count == 0) {
            cw_err_at(&s->err, CW_ERR_SYNTAX, 0, 0, 0, "empty input");
            r = CW_ERR_SYNTAX;
        } else if (s->value_count > 1) {
            cw_err_at(&s->err, CW_ERR_TRAILING_DATA, 0, 0, 0,
                      "multiple top-level values");
            r = CW_ERR_TRAILING_DATA;
        }
    }
    if (err) *err = s->err;
    cw_sax_free(s);
    return r < 0 ? r : CW_OK;
}

int cw_validate_file(const char *path, cw_error *err) {
    if (!path) {
        if (err) cw_err_at(err, CW_ERR_ARG, 0, 0, 0, "NULL path");
        return CW_ERR_ARG;
    }
    FILE *f = fopen(path, "rb");
    if (!f) {
        if (err) cw_err_at(err, CW_ERR_IO, 0, 0, 0, "cannot open '%s'", path);
        return CW_ERR_IO;
    }
    cw_sax *s = cw_sax_new(NULL, NULL);
    if (!s) {
        fclose(f);
        if (err) cw_err_at(err, CW_ERR_NOMEM, 0, 0, 0, "out of memory");
        return CW_ERR_NOMEM;
    }
    char buf[65536];
    int r = CW_OK;
    for (;;) {
        size_t n = fread(buf, 1, sizeof(buf), f);
        if (n == 0) break;
        r = cw_sax_feed(s, buf, n);
        if (r < 0) break;
    }
    if (r >= 0 && ferror(f)) {
        cw_err_at(&s->err, CW_ERR_IO, 0, 0, 0, "read error on '%s'", path);
        r = CW_ERR_IO;
    }
    if (r >= 0) r = cw_sax_finish(s);
    if (r >= 0) {
        if (s->value_count == 0) {
            cw_err_at(&s->err, CW_ERR_SYNTAX, 0, 0, 0, "empty input");
            r = CW_ERR_SYNTAX;
        } else if (s->value_count > 1) {
            cw_err_at(&s->err, CW_ERR_TRAILING_DATA, 0, 0, 0,
                      "multiple top-level values");
            r = CW_ERR_TRAILING_DATA;
        }
    }
    if (err) *err = s->err;
    cw_sax_free(s);
    fclose(f);
    return r < 0 ? r : CW_OK;
}

#endif /* CW_JSON_IMPLEMENTATION */

#endif /* CWIND_JSON_H */
