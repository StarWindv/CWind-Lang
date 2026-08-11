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

static void fixture_path(char* buf, size_t cap, const char* name) {
    const char* f = __FILE__;
    size_t n = strlen(f);
    while (n > 0 && f[n - 1] != '/' && f[n - 1] != '\\') n--;
    snprintf(buf, cap, "%.*sfixtures/%s", (int)n, f, name);
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
    T("Pair<Int> field a",
      l_int && strcmp(l_int->fields[0].name, "a") == 0
      && l_int->fields[0].offset == 0
      && l_int->fields[0].type == int1);
    T("Pair<Int> field b",
      l_int && strcmp(l_int->fields[1].name, "b") == 0
      && l_int->fields[1].offset == CWLAYOUT_SLOT_SIZE
      && l_int->fields[1].type == int1);
    T("Pair<Int> field items (substituted Vector<Int>)",
      l_int && strcmp(l_int->fields[2].name, "items") == 0
      && l_int->fields[2].offset == 2 * CWLAYOUT_SLOT_SIZE
      && cwtype_equal(&types, l_int->fields[2].type, vec_int));
    T("static field excluded",
      l_int && l_int->field_count == 3);

    const CwLayout_t* l_int2 = cwlayout_get(&layouts, m, pair, &int1, 1);
    T("layout cache hit (same pointer)", l_int == l_int2);

    const CwLayout_t* l_str = cwlayout_get(&layouts, m, pair, &str, 1);
    T("Pair<String> distinct layout", l_str != NULL && l_str != l_int);
    T("Pair<String> field types",
      l_str && l_str->fields[0].type == str
      && l_str->fields[1].type == str);

    printf("\n - concrete struct layout (real fixture)\n");
    char fix[1024];
    fixture_path(fix, sizeof(fix), "bindings_sample.json");
    CwModule_t* fm = cwmodule_load_file(fix);
    T("fixture loads", fm != NULL);
    const CwNode_t* box = fm ? cwmodule_node(fm, 6) : NULL;
    const CwLayout_t* l_box = cwlayout_get(&layouts, fm, box, NULL, 0);
    T("Box layout 1 field",
      l_box && l_box->field_count == 1
      && strcmp(l_box->fields[0].name, "value") == 0
      && l_box->fields[0].offset == 0
      && strcmp(cwtype_name(&types, l_box->fields[0].type), "Int") == 0);
    T("non-struct rejected",
      cwlayout_get(&layouts, fm, cwmodule_node(fm, 12), NULL, 0) == NULL);

    printf("\n - cleanup\n");
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
    cwmodule_free(fm);
    T("cleanup ok", 1);

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
