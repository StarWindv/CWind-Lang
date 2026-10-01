/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 * Location: compiler/cwlayout.c
 */

#include "cwlayout.h"

#include "../rt-src/include/stl/json/cwind_json.h"

#include <stdlib.h>
#include <string.h>

/*
 * C-Like-Layout (todo-50): 字段按 C 规则自然对齐放置,
 * 嵌套结构体内联展开 (递归, 深度受限), 引用型字段收进 24B CWValue cell。
 * 实例 blob 无头无槽, 整块 memcpy 即深拷贝。
 */

static bool cwlayout_json_bool(
    cw_value* v,
    bool* out
) {
    if (!v || cw_typeof(v) != CW_BOOL) return false;
    return cw_as_bool(v, out) == CW_OK;
}

static const char* cwlayout_json_name(
    cw_value* obj
) {
    if (!obj || cw_typeof(obj) != CW_OBJECT) return NULL;
    cw_value* name = cw_object_get(obj, "name");
    if (!name || cw_typeof(name) != CW_STRING) return NULL;
    return cw_string_cstr(name);
}

/* bug-94: 扁平类型名里的**类型形参 token** 替换。
 *
 * 扁平拼写 (`[T; N]` / `*const T` / `*mut T` / `fn(*mut T)` /
 * `[Box<T>; 2]`) 把实参全塞在一个字符串里, 没有 args 可递归, 结构化
 * 替换够不着 —— 泛型结构体的 ``tag: [T; 2]`` 字段于是把字面 ``T`` 交给
 * cwlayout_field_meta, 被判"无内联布局"而整个实例布局失败 (bug-94)。
 *
 * 扫描口径与 codegen 的 cg_subst_flat_type_name 一致 (bug-90): 按标识符
 * token 边界匹配, ``T`` 不会误命中 ``T2``/``Tx``; 尖括号/分号/括号原样
 * 带走, 所以 ``[Box<T>; 2]`` -> ``[Box<Int32>; 2]`` 不会被拼坏。
 *
 * 返回值同 cg_subst_flat_type_name:
 *   1 已替换 (out 是结果) / 0 无需替换 (out 不动) / -1 放不下 (放弃)。
 * 放不下时调用方保留原名 —— 宁可退回旧报错路径, 也不返回被截断的类型名
 * (截断名与任何东西都匹配不上, 比字面 "T" 更坏)。
 */
#define CWLAYOUT_TSUBST_CAP 512
#define CWLAYOUT_TSUBST_MAX_DEPTH 4

static bool cwlayout_t_ident(int ch) {
    return (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z')
        || (ch >= '0' && ch <= '9') || ch == '_';
}

/* 把类型 id 渲染成**完整拼写** ``Base<Arg1, Arg2>`` (含实参)。
 *
 * bug-94: `cwtype_name` 只给基名, 单态化替换会把实参**丢掉** ——
 * ``[P; 2]`` 且 P = Pt<Int32> 会变成 ``[Pt; 2]``, 元素退回模板尺寸,
 * 正是静默串槽的来源。扁平拼写里的 token 必须换成带实参的完整写法。
 * 无实参时就是基名 (与 cwtype_name 同)。放不下时返回 NULL, 调用方放弃
 * 替换 (宁可退回旧报错路径, 也不写一个会被截断的类型名)。 */
static const char* cwlayout_render_type(
    CwTypeTable_t* t, CwTypeId id, char* buf, size_t cap, int depth
) {
    const CwType_t* ty = cwtype_get(t, id);
    if (!ty || !ty->name || !cap) return NULL;
    if (ty->arg_count == 0 || depth > CWLAYOUT_TSUBST_MAX_DEPTH) {
        if (strlen(ty->name) + 1 > cap) return NULL;
        snprintf(buf, cap, "%s", ty->name);
        return buf;
    }
    size_t o = 0;
    const size_t nl = strlen(ty->name);
    if (nl + 1 > cap) return NULL;
    memcpy(buf, ty->name, nl);
    o = nl;
    if (o + 2 > cap) return NULL;
    buf[o++] = '<';
    for (size_t i = 0; i < ty->arg_count; i++) {
        if (i > 0) {
            if (o + 3 > cap) return NULL;
            buf[o++] = ',';
            buf[o++] = ' ';
        }
        char sub[CWLAYOUT_TSUBST_CAP];
        const char* r = cwlayout_render_type(
            t, ty->args[i], sub, sizeof(sub), depth + 1);
        if (!r) return NULL;
        const size_t rl = strlen(r);
        if (o + rl + 1 > cap) return NULL;
        memcpy(buf + o, r, rl);
        o += rl;
    }
    if (o + 2 > cap) return NULL;
    buf[o++] = '>';
    buf[o] = '\0';
    return buf;
}

static int cwlayout_subst_flat(
    CwTypeTable_t* t,
    const char* in, char* out, size_t cap, int depth,
    const char** params, size_t nparams,
    const CwTypeId* args, size_t nargs
) {
    size_t o = 0;
    int changed = 0;
    const char* p = in;
    while (*p) {
        if (cwlayout_t_ident((unsigned char)*p)) {
            const char* tok = p;
            while (cwlayout_t_ident((unsigned char)*p)) p++;
            const size_t tlen = (size_t)(p - tok);
            const char* rep = NULL;
            char repbuf[CWLAYOUT_TSUBST_CAP];
            for (size_t i = 0; i < nparams; i++) {
                if (params[i] && strlen(params[i]) == tlen
                    && memcmp(tok, params[i], tlen) == 0) {
                    if (i < nargs) {
                        /* 完整拼写 (含实参): 见 cwlayout_render_type */
                        rep = cwlayout_render_type(
                            t, args[i], repbuf, sizeof(repbuf), 0);
                    }
                    break;
                }
            }
            if (rep && *rep && strcmp(rep, tok) != 0) {
                /* 实参本身可能又含形参 (T = U), 深度封顶防环 */
                const char* piece = rep;
                char sub[CWLAYOUT_TSUBST_CAP];
                if (depth < CWLAYOUT_TSUBST_MAX_DEPTH) {
                    if (cwlayout_subst_flat(
                            t, rep, sub, sizeof(sub), depth + 1,
                            params, nparams, args, nargs) == 1) {
                        piece = sub;
                    }
                }
                const size_t plen = strlen(piece);
                if (o + plen + 1 > cap) return -1;
                memcpy(out + o, piece, plen);
                o += plen;
                changed = 1;
            } else {
                if (o + tlen + 1 > cap) return -1;
                memcpy(out + o, tok, tlen);
                o += tlen;
            }
        } else {
            if (o + 2 > cap) return -1;
            out[o++] = *p++;
        }
    }
    out[o] = '\0';
    return changed;
}

/* 把一个**扁平类型名**登记进类型表, 保持 `Base<Args>` 的结构化形态。
 *
 * bug-94: 替换后的拼写 (``[Pt<Int32>; 2]``) 若整串当作一个不透明名字
 * intern, 元素就丢了实参 —— cwlayout_named_layout 只能查到模板 ``Pt``
 * 的布局, 泛型元素的步长退回未实例化值, 那正是**静默串槽**的来源。
 * 这里按顶层逗号 (括号/尖括号/方括号深度感知, 同前端 _split_args) 拆出
 * 实参, 递归登记, 元素因而保有自己的实参。
 *
 * 只有 `Base<...>` 这一种形态被拆开; 数组 / 指针 / 函数指针整体仍作不透明
 * 名字 (它们不是泛型容器, 元素类型才是), 与 cwlayout_field_meta 的读法
 * 一致。拆不开 (无 '<', 或解析失败) 时整串 intern, 行为与从前一致。 */
CwTypeId cwlayout_intern_flat(
    CwTypeTable_t* t, const char* flat
) {
    if (!t || !flat || !*flat) return CW_TYPE_INVALID;
    const char* lt = strchr(flat, '<');
    if (!lt || flat[0] == '[' || flat[0] == '&') {
        return cwtype_intern(t, flat, NULL, 0);
    }
    /* 尾部必须是 '>' 且没有更深的不匹配 (rt/lt 计数) */
    size_t depth = 0;
    const char* close = NULL;
    for (const char* q = flat; *q; q++) {
        if (*q == '<') depth++;
        else if (*q == '>') {
            if (--depth == 0) { close = q; break; }
        }
    }
    if (!close || close[1] != '\0' || lt == flat) {
        return cwtype_intern(t, flat, NULL, 0);
    }
    /* 基名: flat[0..lt) */
    char base[CWLAYOUT_TSUBST_CAP];
    const size_t blen = (size_t)(lt - flat);
    if (blen >= sizeof(base)) return cwtype_intern(t, flat, NULL, 0);
    memcpy(base, flat, blen);
    base[blen] = '\0';

    /* 实参: lt+1 .. close-1, 顶层逗号切分 */
    CwTypeId ids[8];
    size_t n = 0;
    int depth2 = 0;
    const char* start = lt + 1;
    for (const char* q = lt + 1; q <= close; q++) {
        if (q == close || (*q == ',' && depth2 == 0)) {
            char piece[CWLAYOUT_TSUBST_CAP];
            size_t plen = (size_t)(q - start);
            while (plen > 0 && (*start == ' ')) { start++; plen--; }
            const char* pe = start + plen;
            while (plen > 0 && (pe[-1] == ' ')) { pe--; plen--; }
            const size_t tlen = (size_t)(pe - start);
            if (tlen == 0) return cwtype_intern(t, flat, NULL, 0);
            if (tlen >= sizeof(piece)) {
                return cwtype_intern(t, flat, NULL, 0);
            }
            memcpy(piece, start, tlen);
            piece[tlen] = '\0';
            if (n >= sizeof(ids) / sizeof(ids[0])) {
                return cwtype_intern(t, flat, NULL, 0); /* 太宽: 整串退化 */
            }
            ids[n++] = cwlayout_intern_flat(t, piece);
            if (ids[n - 1] == CW_TYPE_INVALID) {
                return cwtype_intern(t, flat, NULL, 0);
            }
            start = q + 1;
            continue;
        }
        if (*q == '<' || *q == '(' || *q == '[') depth2++;
        else if (*q == '>' || *q == ')' || *q == ']') depth2--;
    }
    if (n == 0) return cwtype_intern(t, flat, NULL, 0);
    return cwtype_intern(t, base, ids, n);
}

/* 泛型替换: 叶子名匹配参数名 -> 具体实参; 否则递归替换 args 后 intern */
static CwTypeId cwlayout_subst(
    CwTypeTable_t* t,
    cw_value* type_obj,
    const char** params, size_t nparams,
    const CwTypeId* args, size_t nargs
) {
    if (!t || !type_obj || cw_typeof(type_obj) != CW_OBJECT) {
        return CW_TYPE_INVALID;
    }
    const char* name = cwlayout_json_name(type_obj);
    if (!name) return CW_TYPE_INVALID;

    for (size_t i = 0; i < nparams; i++) {
        if (params[i] && strcmp(name, params[i]) == 0) {
            return (i < nargs) ? args[i] : CW_TYPE_INVALID;
        }
    }

    /* bug-94: 扁平拼写 (`[T; N]` / `*mut T` / `fn(*mut T)`) 的形参替换。
     * 数组/指针/函数指针形如**一个字符串名** (Type.name == "[T; 2]"),
     * 没有结构化 args, 结构化递归够不着, 必须走 token 扫描才能把 T 换成
     * 实例实参 —— 否则泛型实例的布局算不出来 (``unknown struct layout``)。 */
    if (nparams > 0 && nargs > 0
        && (name[0] == '[' || strncmp(name, "fn(", 3) == 0
            || strncmp(name, "*const ", 7) == 0
            || strncmp(name, "*mut ", 5) == 0)) {
        char flat[CWLAYOUT_TSUBST_CAP];
        if (cwlayout_subst_flat(t, name, flat, sizeof(flat), 0,
                                params, nparams, args, nargs) > 0) {
            return cwlayout_intern_flat(t, flat);
        }
    }

    cw_value* args_v = cw_object_get(type_obj, "args");
    const size_t ac = (args_v && cw_typeof(args_v) == CW_ARRAY)
        ? cw_array_size(args_v) : 0;
    CwTypeId* sub = NULL;
    if (ac > 0) {
        sub = (CwTypeId*)malloc(ac * sizeof(CwTypeId));
        if (!sub) return CW_TYPE_INVALID;
        for (size_t i = 0; i < ac; i++) {
            sub[i] = cwlayout_subst(t, cw_array_get(args_v, i),
                                    params, nparams, args, nargs);
            if (sub[i] == CW_TYPE_INVALID) {
                free(sub);
                return CW_TYPE_INVALID;
            }
        }
    }
    CwTypeId id = cwtype_intern(t, name, sub, ac);
    free(sub);
    return id;
}

/* ---- 字段尺寸/对齐 (C 规则) ---- */

static size_t cwlayout_scalar_size(const char* name) {
    if (!name) return 0;
    if (strcmp(name, "Int") == 0 || strcmp(name, "UInt") == 0
        || strcmp(name, "Int16") == 0 || strcmp(name, "UInt16") == 0) {
        return 2;
    }
    if (strcmp(name, "Int8") == 0 || strcmp(name, "UInt8") == 0
        || strcmp(name, "Byte") == 0 || strcmp(name, "Bool") == 0) {
        return 1;
    }
    if (strcmp(name, "Int32") == 0 || strcmp(name, "UInt32") == 0
        || strcmp(name, "Float") == 0) {
        return 4;
    }
    if (strcmp(name, "Int64") == 0 || strcmp(name, "UInt64") == 0
        || strcmp(name, "Float64") == 0) {
        return 8;
    }
    return 0;
}

/* "[T; N]" 解析 */
static bool cwlayout_array_info(
    const char* tname,
    char* elem, size_t elem_cap,
    size_t* out_n
) {
    if (!tname || tname[0] != '[') return false;
    const char* semi = strrchr(tname, ';');
    const size_t len = strlen(tname);
    if (!semi || len < 3 || tname[len - 1] != ']') return false;
    size_t lo = 1;
    size_t hi = (size_t)(semi - tname);
    while (lo < hi && (tname[lo] == ' ' || tname[lo] == '\t')) lo++;
    while (hi > lo && (tname[hi - 1] == ' ' || tname[hi - 1] == '\t')) hi--;
    if (lo >= hi || hi - lo + 1 >= elem_cap) return false;
    memcpy(elem, tname + lo, hi - lo);
    elem[hi - lo] = '\0';
    const char* p = semi + 1;
    while (*p == ' ' || *p == '\t') p++;
    char* end = NULL;
    const unsigned long long v = strtoull(p, &end, 10);
    if (!end || end == p) return false;
    while (*end == ' ' || *end == '\t') end++;
    if (*end != ']') return false;
    if (v == 0 || v > 65536) return false;
    if (out_n) *out_n = (size_t)v;
    return true;
}

static size_t cwlayout_align_up(size_t v, size_t a) {
    return (v + a - 1) & ~(a - 1);
}

/* 按类型名查结构体符号 */
static const CwNode_t* cwlayout_struct_decl(
    const CwLayoutCache_t* c, const CwModule_t* m,
    const char* name
) {
    if (!c || !m || !name) return NULL;
    const CwSymbol_t* sym = cwmodule_find_symbol(m, name);
    if (!sym || strcmp(sym->kind, "struct") != 0 || !sym->ref) return NULL;
    return cwmodule_node(m, sym->ref);
}

/* bug-94: 按**类型 id** 解析内联结构体的实例布局。
 *
 * `cwlayout_name` 只给基名, 所以 ``Pt<Int32>`` 这种实例必须从 id 读出
 * 基名 + 实参再按实例取布局。泛型字段 (``head: P``, P = Pt<Int32>``)
 * 以前只按基名算, 拿到的是**未实例化模板**的尺寸 —— 字段大小错位却
 * 编译干净 (静默内存损坏)。本函数让字段与数组元素走同一条按实例的口径。 */
static const CwLayout_t* cwlayout_id_struct_layout(
    CwLayoutCache_t* c, const CwModule_t* m, CwTypeId id
) {
    if (!c || !c->types || !m || id == CW_TYPE_INVALID) return NULL;
    const CwType_t* t = cwtype_get(c->types, id);
    if (!t || !t->name) return NULL;
    const CwNode_t* decl = cwlayout_struct_decl(c, m, t->name);
    if (!decl) return NULL;
    return cwlayout_get(c, m, decl, t->args, t->arg_count);
}

/* bug-94: 解析一个**已实例化**的结构体类型名 (``Pt<Int32>``) 的布局。
 *
 * cwlayout_struct_decl 只能按**裸名**查符号表, 所以 ``Pt<Int32>`` 这种
 * 拼写查不到 —— 泛型元素 (`[P; 2]` 且 P = Pt<Int32>) 替换后正是这个形状。
 * 这里把拼写登记进类型表拿到 base + args, 再按实例 (name + 实参) 取布局:
 * 步长因此是**该实例**的 C 布局尺寸, 不是未实例化的模板尺寸。
 * 同一函数也修好嵌套泛型结构体字段 (注释里"实参替换后的非泛型名才内联"
 * 那条遗留限制)。返回 NULL = 不是内联结构体 (调用方按 cell 处理)。 */
static const CwLayout_t* cwlayout_named_layout(
    CwLayoutCache_t* c, const CwModule_t* m, const char* tname
) {
    if (!c || !c->types || !m || !tname || !*tname) return NULL;
    /* 快路径: 裸名结构体不碰类型表 (非泛型代码零分配) */
    const CwNode_t* decl = cwlayout_struct_decl(c, m, tname);
    if (decl) return cwlayout_get(c, m, decl, NULL, 0);
    if (!strchr(tname, '<')) return NULL; /* 非泛型拼写且不是结构体 */
    /* 泛型实例: base<args> -> (基名符号, 实参 id 数组) */
    const CwTypeId id = cwlayout_intern_flat(c->types, tname);
    if (id == CW_TYPE_INVALID) return NULL;
    const CwType_t* t = cwtype_get(c->types, id);
    if (!t || !t->name) return NULL;
    decl = cwlayout_struct_decl(c, m, t->name);
    if (!decl) return NULL;
    return cwlayout_get(c, m, decl, t->args, t->arg_count);
}

/* 单个字段的尺寸/对齐; inline_struct 输出是否为内联嵌套结构体。
 * depth 防自包含结构体 (值语义下无解, 深度超限拒绝)。
 *
 * bug-94: 除了拼写 ``fname`` 还收一个**类型 id**, 因为泛型实例的实参
 * 只存在于类型表里 —— `Pt` 与 `Pt<Int32>` 的基名拼写相同而布局不同,
 * 只看拼写会把字段算成未实例化模板的尺寸 (静默错位)。 */
static bool cwlayout_field_meta(
    CwLayoutCache_t* c, const CwModule_t* m,
    const char* fname, CwTypeId fid,
    size_t* size, size_t* align,
    size_t depth
) {
    const size_t sz = cwlayout_scalar_size(fname);
    if (sz > 0) {
        *size = sz;
        *align = sz;
        return true;
    }
    char elem[128];
    size_t n = 0;
    if (cwlayout_array_info(fname, elem, sizeof(elem), &n)) {
        const size_t esz = cwlayout_scalar_size(elem);
        if (esz > 0) {
            *size = esz * n;
            *align = esz;
            return true;
        }
        /* todo-182/bug-94: 结构体元素数组的布局 = n × 元素 C 布局 (blob 即
         * C-Like 镜像)。元素可以是**泛型参数**替换后的实例 (``[P; 2]``
         * 且 P = Pt<Int32>``): 步长取该实例的布局, 不是模板的。 */
        if (depth < CWLAYOUT_MAX_DEPTH) {
            const CwLayout_t* inner = cwlayout_named_layout(c, m, elem);
            if (inner) {
                /* 步长 = 元素布局尺寸 (内含尾补齐, 故已是 size/stride) */
                *size = inner->size * n;
                *align = inner->align;
                return true;
            }
        }
        return false; /* 泛型/未知元素无内联布局 */
    }
    if (fname && (strncmp(fname, "*const ", 7) == 0
                  || strncmp(fname, "*mut ", 5) == 0
                  || strncmp(fname, "fn(", 3) == 0)) {
        *size = 8; /* 地址即值 (rawptr 值的 address 就是地址本身) */
        *align = 8;
        return true;
    }
    /* 嵌套结构体: 内联展开 (递归)。bug-94: 泛型实例 (``Pt<Int32>``) 也
     * 按**该实例**的布局算, 不再退化成未实例化模板 (或 cell)。 */
    if (fname && depth < CWLAYOUT_MAX_DEPTH) {
        const CwLayout_t* inner = cwlayout_named_layout(c, m, fname);
        if (!inner) {
            inner = cwlayout_id_struct_layout(c, m, fid);
        }
        if (inner) {
            *size = inner->size;
            *align = inner->align;
            return true;
        }
    }
    /* 其余引用型: String/Vector/Map/Set/Tuple/枚举/泛型遗留 -> cell */
    *size = CWLAYOUT_CELL_SIZE;
    *align = 8;
    return true;
}

bool cwlayout_cache_init(
    CwLayoutCache_t* c,
    CwTypeTable_t* types
) {
    if (!c || !types) return false;
    memset(c, 0, sizeof(*c));
    c->types = types;
    return true;
}

void cwlayout_cache_destroy(
    CwLayoutCache_t* c
) {
    if (!c) return;
    for (size_t i = 0; i < c->count; i++) {
        free(c->items[i]->fields);
        free(c->items[i]);
    }
    free(c->items);
    memset(c, 0, sizeof(*c));
}

const CwLayout_t* cwlayout_get(
    CwLayoutCache_t* c,
    const CwModule_t* m,
    const CwNode_t* struct_decl,
    const CwTypeId* args,
    size_t arg_count
) {
    if (!c || !m || !struct_decl
        || strcmp(struct_decl->kind, "StructDecl") != 0) {
        return NULL;
    }

    const char* name = cwlayout_json_name(struct_decl->value);
    if (!name) return NULL;
    const CwTypeId inst = cwtype_intern(c->types, name, args, arg_count);
    if (inst == CW_TYPE_INVALID) return NULL;

    for (size_t i = 0; i < c->count; i++) {
        if (c->items[i]->type == inst) return c->items[i];
    }

    /* 收集泛型参数名 */
    cw_value* params_v = cw_object_get(struct_decl->value, "params");
    const size_t nparams = (params_v && cw_typeof(params_v) == CW_ARRAY)
        ? cw_array_size(params_v) : 0;
    const char** params = NULL;
    if (nparams > 0) {
        params = (const char**)malloc(nparams * sizeof(const char*));
        if (!params) return NULL;
        for (size_t i = 0; i < nparams; i++) {
            params[i] = cwlayout_json_name(cw_array_get(params_v, i));
        }
    }

    /* 统计非 static 字段数 */
    cw_value* fields_v = cw_object_get(struct_decl->value, "fields");
    if (!fields_v || cw_typeof(fields_v) != CW_ARRAY) {
        free(params);
        return NULL;
    }
    const size_t nf = cw_array_size(fields_v);
    size_t live = 0;
    for (size_t i = 0; i < nf; i++) {
        cw_value* f = cw_array_get(fields_v, i);
        bool is_static = false;
        cwlayout_json_bool(cw_object_get(f, "static"), &is_static);
        if (!is_static) live++;
    }

    CwLayout_t* L = (CwLayout_t*)malloc(sizeof(CwLayout_t));
    if (!L) {
        free(params);
        return NULL;
    }
    L->type = inst;
    L->size = 0;
    L->align = 1;
    L->field_count = live;
    L->fields = live ? (CwFieldLayout_t*)malloc(live * sizeof(CwFieldLayout_t))
                     : NULL;
    if (live && !L->fields) {
        free(L);
        free(params);
        return NULL;
    }

    /* C-Like-Layout: 偏移按字段对齐累进 (声明序) */
    size_t off = 0;
    size_t idx = 0;
    for (size_t i = 0; i < nf; i++) {
        cw_value* f = cw_array_get(fields_v, i);
        bool is_static = false;
        cwlayout_json_bool(cw_object_get(f, "static"), &is_static);
        if (is_static) continue;

        const char* fname = cwlayout_json_name(f);
        cw_value* ftype = cw_object_get(f, "type");
        /* bug-23/29: 优先采用 SA 解析后的 ann.type (typedef 别名展开/
         * 精化还原); 泛型参数叶在 ann.type 里保留原参数名, 替换不受影响 */
        cw_value* fann = cw_object_get(f, "ann");
        cw_value* resolved = fann ? cw_object_get(fann, "type") : NULL;
        if (resolved && cw_typeof(resolved) == CW_OBJECT) {
            ftype = resolved;
        }
        const CwTypeId tid = cwlayout_subst(c->types, ftype,
                                            params, nparams,
                                            args, arg_count);
        if (!fname || tid == CW_TYPE_INVALID) {
            free(L->fields);
            free(L);
            free(params);
            return NULL;
        }
        const char* rname = cwtype_name(c->types, tid);
        size_t fsz = 0;
        size_t fal = 1;
        if (!cwlayout_field_meta(c, m, rname, tid, &fsz, &fal, 0)) {
            free(L->fields);
            free(L);
            free(params);
            return NULL;
        }
        if (fal > L->align) L->align = fal;
        off = cwlayout_align_up(off, fal);
        L->fields[idx].name   = fname;
        L->fields[idx].offset = off;
        L->fields[idx].size   = fsz;
        L->fields[idx].align  = fal;
        L->fields[idx].type   = tid;
        off += fsz;
        idx++;
    }
    free(params);

    /* 尾部按最大对齐补齐 */
    L->size = cwlayout_align_up(off, L->align);
    if (L->size == 0) L->size = 1; /* 空结构体至少占 1 字节 */

    if (c->count == c->cap) {
        const size_t nc = c->cap ? c->cap * 2 : 16;
        CwLayout_t** ni = (CwLayout_t**)realloc(
            c->items, nc * sizeof(CwLayout_t*));
        if (!ni) {
            free(L->fields);
            free(L);
            return NULL;
        }
        c->items = ni;
        c->cap = nc;
    }
    c->items[c->count++] = L;
    return L;
}
