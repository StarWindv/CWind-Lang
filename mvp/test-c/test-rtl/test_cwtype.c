/**
 * 独立测试: 类型表 (interning) + 布局缓存
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwtype.exe test_cwtype.c
 *       ../../compiler/cwmodule.c
 *       ../../compiler/cwtype.c
 *       ../../compiler/cwlayout.c
 */

#include "../../compiler/cwmodule.h"
#include "../../compiler/cwtype.h"
#include "../../compiler/cwlayout.h"
#include "../../rt-src/include/stl/json/cwind_json.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

static CwModule_t* load(const char* json) {
    return cwmodule_load_string(json, strlen(json));
}

/* 生成的 typed-JSON 现在落 build 目录, cmake 用 CWIND_FIXTURE_DIR 指过来
 * (见 mvp/CMakeLists.txt)。宏缺失 (脱离 cmake 手工编译) 时回退到 __FILE__
 * 推导的源码树 fixtures/ —— 那条路径上现在只剩手写文件, 所以这种编译方式
 * 只会让用到 fixture 的那几个断言失败, 不会读到错的 JSON。 */
static void fixture_path(char* buf, size_t cap, const char* name) {
#ifdef CWIND_FIXTURE_DIR
    snprintf(buf, cap, CWIND_FIXTURE_DIR "/%s", name);
#else
    const char* f = __FILE__;
    size_t n = strlen(f);
    while (n > 0 && f[n - 1] != '/' && f[n - 1] != '\\') n--;
    snprintf(buf, cap, "%.*sfixtures/%s", (int)n, f, name);
#endif
}

static const char* k_generic_struct =
    "{\"format\": \"cwind-typed-ast\", \"version\": 1,"
    " \"symbols\": [{\"name\": \"Pair\", \"kind\": \"struct\", \"ref\": 2}],"
    " \"bindings\": [],"
    " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {}, \"items\": ["
    "   {\"kind\": \"StructDecl\", \"id\": 2, \"ann\": {}, \"name\": \"Pair\","
    "    \"params\": ["
    "      {\"kind\": \"TypeParam\", \"id\": 3, \"ann\": {},"
    "       \"name\": \"T\", \"bound\": null}"
    "    ],"
    "    \"fields\": ["
    "      {\"kind\": \"Field\", \"id\": 4, \"ann\": {}, \"name\": \"a\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 5, \"ann\": {},"
    "                 \"name\": \"T\", \"args\": []},"
    "       \"pub\": false, \"static\": false,"
    "       \"validation\": null, \"initializer\": null},"
    "      {\"kind\": \"Field\", \"id\": 6, \"ann\": {}, \"name\": \"b\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 7, \"ann\": {},"
    "                 \"name\": \"T\", \"args\": []},"
    "       \"pub\": false, \"static\": false,"
    "       \"validation\": null, \"initializer\": null},"
    "      {\"kind\": \"Field\", \"id\": 8, \"ann\": {},"
    "       \"name\": \"counter\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 9, \"ann\": {},"
    "                 \"name\": \"Int\", \"args\": []},"
    "       \"pub\": false, \"static\": true,"
    "       \"validation\": null, \"initializer\": null},"
    "      {\"kind\": \"Field\", \"id\": 10, \"ann\": {},"
    "       \"name\": \"items\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 11, \"ann\": {},"
    "                 \"name\": \"Vector\", \"args\": ["
    "                   {\"kind\": \"Type\", \"id\": 12, \"ann\": {},"
    "                    \"name\": \"T\", \"args\": []}"
    "                 ]},"
    "       \"pub\": false, \"static\": false,"
    "       \"validation\": null, \"initializer\": null}"
    "    ]}"
    " ]}}";

/* bug-94: 泛型结构体里的定长数组元素 (标量与结构体两种)。
 *
 * 泛型结构体只声明一次, 字段 ``tag: [T; 2]`` 里的 T 是**类型参数**,
 * 布局必须等单态化 —— cwlayout_subst 把 T 换成实例实参, 步长按
 * **该实例**算 (Rust 同一规则: [T; N] 的 stride = size_of::<T>() 向上
 * 对齐, Cell<T> 自身大小也随实例变)。这里同时钉住:
 *   - Cell<Int32>  tag 步长 4, 整结构 4 + 8 = 12
 *   - Cell<Int64>  tag 步长 8, 整结构 8 + 16 = 24  (同一模板, 步长不同)
 *   - Cell<Int8>   tag 步长 1, 整结构 1 + 2  = 3   (步长写死 4 就串槽)
 *   - Row<Int32>   元素是非泛型结构体 Pt(8B), 步长 8, 整结构 4+16 = 20
 * 步长算错是**静默内存损坏** (编译干净, 读回串槽), 所以这里按字节钉死。 */
static const char* k_gen_array_struct =
    "{\"format\": \"cwind-typed-ast\", \"version\": 1,"
    " \"symbols\": ["
    "   {\"name\": \"Cell\", \"kind\": \"struct\", \"ref\": 2},"
    "   {\"name\": \"Pt\", \"kind\": \"struct\", \"ref\": 8},"
    "   {\"name\": \"Row\", \"kind\": \"struct\", \"ref\": 13}],"
    " \"bindings\": [],"
    " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {}, \"items\": ["
    "   {\"kind\": \"StructDecl\", \"id\": 2, \"ann\": {}, \"name\": \"Cell\","
    "    \"params\": ["
    "      {\"kind\": \"TypeParam\", \"id\": 3, \"ann\": {},"
    "       \"name\": \"T\", \"bound\": null}"
    "    ],"
    "    \"fields\": ["
    "      {\"kind\": \"Field\", \"id\": 4, \"ann\": {}, \"name\": \"v\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 5, \"ann\": {},"
    "                 \"name\": \"T\", \"args\": []},"
    "       \"pub\": false, \"static\": false,"
    "       \"validation\": null, \"initializer\": null},"
    "      {\"kind\": \"Field\", \"id\": 6, \"ann\": {}, \"name\": \"tag\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 7, \"ann\": {},"
    "                 \"name\": \"[T; 2]\", \"args\": []},"
    "       \"pub\": false, \"static\": false,"
    "       \"validation\": null, \"initializer\": null}"
    "    ]},"
    "   {\"kind\": \"StructDecl\", \"id\": 8, \"ann\": {}, \"name\": \"Pt\","
    "    \"params\": ["
    "      {\"kind\": \"TypeParam\", \"id\": 30, \"ann\": {},"
    "       \"name\": \"T\", \"bound\": null}"
    "    ],"
    "    \"fields\": ["
    "      {\"kind\": \"Field\", \"id\": 9, \"ann\": {}, \"name\": \"x\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 10, \"ann\": {},"
    "                 \"name\": \"T\", \"args\": []},"
    "       \"pub\": false, \"static\": false,"
    "       \"validation\": null, \"initializer\": null},"
    "      {\"kind\": \"Field\", \"id\": 11, \"ann\": {}, \"name\": \"y\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 12, \"ann\": {},"
    "                 \"name\": \"T\", \"args\": []},"
    "       \"pub\": false, \"static\": false,"
    "       \"validation\": null, \"initializer\": null}"
    "    ]},"
    "   {\"kind\": \"StructDecl\", \"id\": 13, \"ann\": {}, \"name\": \"Row\","
    "    \"params\": ["
    "      {\"kind\": \"TypeParam\", \"id\": 14, \"ann\": {},"
    "       \"name\": \"P\", \"bound\": null}"
    "    ],"
    "    \"fields\": ["
    "      {\"kind\": \"Field\", \"id\": 15, \"ann\": {}, \"name\": \"head\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 16, \"ann\": {},"
    "                 \"name\": \"P\", \"args\": []},"
    "       \"pub\": false, \"static\": false,"
    "       \"validation\": null, \"initializer\": null},"
    "      {\"kind\": \"Field\", \"id\": 17, \"ann\": {}, \"name\": \"rest\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 18, \"ann\": {},"
    "                 \"name\": \"[P; 2]\", \"args\": []},"
    "       \"pub\": false, \"static\": false,"
    "       \"validation\": null, \"initializer\": null}"
    "    ]}"
    " ]}}";

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CwType / CwLayout tests:\n\n");

    printf(" - type interning\n");
    CwTypeTable_t types;
    cwtype_table_init(&types);
    CwTypeId int1 = cwtype_intern(&types, "Int", NULL, 0);
    CwTypeId int2 = cwtype_intern(&types, "Int", NULL, 0);
    T("intern dedupe", int1 != CW_TYPE_INVALID && int1 == int2);
    CwTypeId str = cwtype_intern(&types, "String", NULL, 0);
    T("different names differ", str != int1);
    CwTypeId vec_int = cwtype_intern(&types, "Vector", &int1, 1);
    CwTypeId vec_int2 = cwtype_intern(&types, "Vector", &int1, 1);
    CwTypeId vec_str = cwtype_intern(&types, "Vector", &str, 1);
    T("nested dedupe", vec_int == vec_int2);
    T("args distinguish", vec_int != vec_str);
    T("name lookup", strcmp(cwtype_name(&types, vec_int), "Vector") == 0);
    T("arg lookup", cwtype_arg(&types, vec_int, 0) == int1);
    T("arg out of range", cwtype_arg(&types, int1, 0) == CW_TYPE_INVALID);
    T("equal", cwtype_equal(&types, vec_int, vec_int2)
      && !cwtype_equal(&types, vec_int, vec_str));
    T("invalid id", cwtype_get(&types, CW_TYPE_INVALID) == NULL);

    printf("\n - type from JSON\n");
    cw_doc* d = cw_parse_cstr("{\"name\": \"T\", \"opaque\": true}");
    CwTypeId t_opaque = cwtype_from_json(&types, d ? cw_doc_root(d) : NULL);
    T("from_json opaque leaf",
      t_opaque != CW_TYPE_INVALID
      && strcmp(cwtype_name(&types, t_opaque), "T") == 0
      && cwtype_is_opaque(&types, t_opaque));
    cw_doc_free(d);
    d = cw_parse_cstr("{\"name\": \"Map\", \"args\": ["
                      "{\"name\": \"String\"}, {\"name\": \"Int\"}]}");
    CwTypeId t_map = cwtype_from_json(&types, d ? cw_doc_root(d) : NULL);
    T("from_json nested",
      t_map != CW_TYPE_INVALID
      && cwtype_arg_count(&types, t_map) == 2
      && cwtype_arg(&types, t_map, 0) == str
      && cwtype_arg(&types, t_map, 1) == int1);
    cw_doc_free(d);
    T("from_json invalid", cwtype_from_json(&types, NULL) == CW_TYPE_INVALID);

    printf("\n - generic struct layout\n");
    CwModule_t* m = load(k_generic_struct);
    T("generic module loads", m != NULL);
    const CwNode_t* pair = m ? cwmodule_node(m, 2) : NULL;
    T("Pair node found", pair != NULL);

    CwLayoutCache_t layouts;
    T("layout cache init", cwlayout_cache_init(&layouts, &types));
    const CwLayout_t* l_int = cwlayout_get(&layouts, m, pair, &int1, 1);
    T("Pair<Int> layout",
      l_int && l_int->field_count == 3);
    /* C-Like-Layout (todo-50): 标量内联 (Int=2B, 对齐 2),
     * 引用型 (Vector<Int>) 收进 24B cell (对齐 8) */
    T("Pair<Int> field a",
      l_int && strcmp(l_int->fields[0].name, "a") == 0
      && l_int->fields[0].offset == 0 && l_int->fields[0].size == 2
      && l_int->fields[0].type == int1);
    T("Pair<Int> field b",
      l_int && strcmp(l_int->fields[1].name, "b") == 0
      && l_int->fields[1].offset == 2 && l_int->fields[1].size == 2
      && l_int->fields[1].type == int1);
    T("Pair<Int> field items (substituted Vector<Int>)",
      l_int && strcmp(l_int->fields[2].name, "items") == 0
      && l_int->fields[2].offset == 8 && l_int->fields[2].size == 24
      && cwtype_equal(&types, l_int->fields[2].type, vec_int));
    T("Pair<Int> blob size (尾补齐到对齐)",
      l_int && l_int->size == 32 && l_int->align == 8);
    T("static field excluded",
      l_int && l_int->field_count == 3);

    const CwLayout_t* l_int2 = cwlayout_get(&layouts, m, pair, &int1, 1);
    T("layout cache hit (same pointer)", l_int == l_int2);

    const CwLayout_t* l_str = cwlayout_get(&layouts, m, pair, &str, 1);
    T("Pair<String> distinct layout", l_str != NULL && l_str != l_int);
    T("Pair<String> field types",
      l_str && l_str->fields[0].type == str
      && l_str->fields[1].type == str);

    /* 扩容后旧指针仍有效 (指针数组缓存) */
    CwTypeId bool_id = cwtype_intern(&types, "Bool", NULL, 0);
    const CwLayout_t* l_bool = cwlayout_get(&layouts, m, pair, &bool_id, 1);
    T("third layout after growth", l_bool != NULL);
    T("old pointers stable after growth",
      l_int && cwlayout_get(&layouts, m, pair, &int1, 1) == l_int);

    /* 空结构体 */
    CwModule_t* em = load(
        "{\"format\": \"cwind-typed-ast\", \"version\": 1,"
        " \"symbols\": [{\"name\": \"Empty\", \"kind\": \"struct\","
        " \"ref\": 2}], \"bindings\": [],"
        " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {},"
        " \"items\": [{\"kind\": \"StructDecl\", \"id\": 2, \"ann\": {},"
        " \"name\": \"Empty\", \"params\": [], \"fields\": []}]}}");
    const CwNode_t* empty_node = em ? cwmodule_node(em, 2) : NULL;
    const CwLayout_t* l_empty = cwlayout_get(&layouts, em, empty_node,
                                             NULL, 0);
    T("empty struct layout", l_empty && l_empty->field_count == 0);
    cwmodule_free(em);
    T("NULL module rejected", cwlayout_get(&layouts, NULL, pair, NULL, 0)
      == NULL);

    printf("\n - concrete struct layout (real fixture)\n");
    char fix[1024];
    fixture_path(fix, sizeof(fix), "bindings_sample.json");
    CwModule_t* fm = cwmodule_load_file(fix);
    T("fixture loads", fm != NULL);
    /* todo-148: fixture 由前端构建期生成, prelude 内容随 stdlib 演化,
     * 按符号名定位 Box, 不钉死节点 id */
    const CwSymbol_t* sbox = fm ? cwmodule_find_symbol(fm, "Box") : NULL;
    const CwNode_t* box = sbox ? cwmodule_node(fm, sbox->ref) : NULL;
    const CwLayout_t* l_box = cwlayout_get(&layouts, fm, box, NULL, 0);
    T("Box layout 1 field",
      l_box && l_box->field_count == 1
      && strcmp(l_box->fields[0].name, "value") == 0
      && l_box->fields[0].offset == 0 && l_box->fields[0].size == 2
      && strcmp(cwtype_name(&types, l_box->fields[0].type), "Int") == 0);
    const CwSymbol_t* smain = fm ? cwmodule_find_symbol(fm, "main") : NULL;
    T("non-struct rejected",
      cwlayout_get(&layouts, fm,
                   smain ? cwmodule_node(fm, smain->ref) : NULL, NULL, 0)
      == NULL);

    printf("\n - bug-94: generic inline-array element layout (stride per instance)\n");
    CwModule_t* am = load(k_gen_array_struct);
    T("bug94: module loads", am != NULL);
    if (am) {
        const CwSymbol_t* scell = cwmodule_find_symbol(am, "Cell");
        const CwSymbol_t* srow  = cwmodule_find_symbol(am, "Row");
        const CwNode_t* cell = scell ? cwmodule_node(am, scell->ref) : NULL;
        const CwNode_t* row  = srow  ? cwmodule_node(am, srow->ref)  : NULL;
        T("bug94: Cell node", cell != NULL);
        T("bug94: Row node", row != NULL);

        CwLayoutCache_t al;
        T("bug94: cache init", cwlayout_cache_init(&al, &types));

        /* 实例实参: 标量三种宽度 + 结构体实例 Pt<Int32> */
        CwTypeId arg_i32 = cwtype_intern(&types, "Int32", NULL, 0);
        CwTypeId arg_i64 = cwtype_intern(&types, "Int64", NULL, 0);
        CwTypeId arg_i8  = cwtype_intern(&types, "Int8", NULL, 0);
        CwTypeId arg_pt  = cwtype_intern(&types, "Pt", &arg_i32, 1);

        /* 标量元素: 步长随实例走, 整结构大小也随之变 */
        const CwLayout_t* c32 = cwlayout_get(&al, am, cell, &arg_i32, 1);
        T("bug94: Cell<Int32> layout exists", c32 != NULL);
        T("bug94: Cell<Int32> v field (i32 @0)",
          c32 && strcmp(c32->fields[0].name, "v") == 0
          && c32->fields[0].offset == 0 && c32->fields[0].size == 4);
        T("bug94: Cell<Int32> tag field = [Int32; 2] @4, 8B",
          c32 && strcmp(c32->fields[1].name, "tag") == 0
          && c32->fields[1].offset == 4 && c32->fields[1].size == 8
          && c32->fields[1].align == 4
          && strcmp(cwtype_name(&types, c32->fields[1].type), "[Int32; 2]")
             == 0);
        T("bug94: Cell<Int32> size 12", c32 && c32->size == 12
          && c32->align == 4);

        const CwLayout_t* c64 = cwlayout_get(&al, am, cell, &arg_i64, 1);
        T("bug94: Cell<Int64> stride 8 (not 4)", c64 && c64->size == 24
          && c64->fields[1].offset == 8 && c64->fields[1].size == 16
          && c64->fields[1].align == 8
          && strcmp(cwtype_name(&types, c64->fields[1].type), "[Int64; 2]")
             == 0);

        const CwLayout_t* c8 = cwlayout_get(&al, am, cell, &arg_i8, 1);
        T("bug94: Cell<Int8> stride 1 (not 4)", c8 && c8->size == 3
          && c8->fields[1].offset == 1 && c8->fields[1].size == 2
          && c8->fields[1].align == 1
          && strcmp(cwtype_name(&types, c8->fields[1].type), "[Int8; 2]")
             == 0);

        /* 结构体元素: 参数替换成实例后按其 C 布局算步长 */
        const CwLayout_t* r32 = cwlayout_get(&al, am, row, &arg_pt, 1);
        T("bug94: Row<Pt<Int32>> layout exists", r32 != NULL);
        T("bug94: Row<Pt<Int32>> head is the instance too (8B @0)",
          r32 && strcmp(r32->fields[0].name, "head") == 0
          && r32->fields[0].offset == 0 && r32->fields[0].size == 8);
        T("bug94: Row<Pt<Int32>> rest = [Pt<Int32>; 2] @8, 16B",
          r32 && strcmp(r32->fields[1].name, "rest") == 0
          && r32->fields[1].offset == 8 && r32->fields[1].size == 16
          && r32->fields[1].align == 4
          && strcmp(cwtype_name(&types, r32->fields[1].type),
                    "[Pt<Int32>; 2]") == 0);
        T("bug94: Row<Pt<Int32>> size 24", r32 && r32->size == 24
          && r32->align == 4);

        /* 决定性对照: 同一 Row 模板, 元素实例换成 Pt<Int8> (元素 2B) ——
         * 步长必须随之从 8 变 2, 否则说明用的是模板尺寸, 那就是静默
         * 串槽 (编译干净, 读回别的槽)。 */
        CwTypeId arg_pt8 = cwtype_intern(&types, "Pt", &arg_i8, 1);
        const CwLayout_t* r8 = cwlayout_get(&al, am, row, &arg_pt8, 1);
        T("bug94: Row<Pt<Int8>> element stride 2 (not 8)",
          r8 && r8->fields[1].size == 4
          && strcmp(cwtype_name(&types, r8->fields[1].type),
                    "[Pt<Int8>; 2]") == 0);
        T("bug94: Row<Pt<Int8>> size 6 (head 2 + rest 4)",
          r8 && r8->size == 6 && r8->fields[1].offset == 2
          && r8->fields[0].size == 2);

        cwlayout_cache_destroy(&al);
        cwmodule_free(am);
    }

    printf("\n - cleanup\n");
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
    cwmodule_free(fm);
    T("cleanup ok", 1);

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
