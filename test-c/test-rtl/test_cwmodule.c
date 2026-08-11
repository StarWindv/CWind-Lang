/**
 * 独立测试: CwModule (TypedAST 装载)
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwmodule.exe test_cwmodule.c
 *       ../../compiler/cwmodule.c
 */

#include "../../compiler/cwmodule.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

static const char* k_valid =
    "{"
    " \"format\": \"cwind-typed-ast\","
    " \"version\": 1,"
    " \"symbols\": ["
    "   {\"name\": \"Point\", \"kind\": \"struct\", \"ref\": 2},"
    "   {\"name\": \"main\", \"kind\": \"fn\", \"ref\": 5}"
    " ],"
    " \"bindings\": ["
    "   {\"id\": 1, \"decl_id\": 4, \"owner\": \"Point\","
    "    \"trait\": null, \"fn_id\": 7}"
    " ],"
    " \"ast\": {"
    "   \"kind\": \"Program\", \"id\": 1, \"line\": 1, \"column\": 1,"
    "   \"ann\": {},"
    "   \"items\": ["
    "     {\"kind\": \"StructDecl\", \"id\": 2, \"line\": 1, \"column\": 1,"
    "      \"ann\": {}, \"name\": \"Point\", \"params\": [], \"fields\": ["
    "        {\"kind\": \"Field\", \"id\": 3, \"line\": 1, \"column\": 9,"
    "         \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "         \"name\": \"x\","
    "         \"type\": {\"kind\": \"Type\", \"id\": 6, \"line\": 1,"
    "                   \"column\": 11, \"ann\": {}, \"name\": \"Int\","
    "                   \"args\": []},"
    "         \"pub\": false, \"static\": false, \"validation\": null,"
    "         \"initializer\": null}"
    "      ]},"
    "     {\"kind\": \"ExtraDecl\", \"id\": 4, \"line\": 2, \"column\": 1,"
    "      \"ann\": {}, \"name\": \"Point\", \"methods\": ["
    "        {\"kind\": \"FnDecl\", \"id\": 7, \"line\": 2, \"column\": 9,"
    "         \"ann\": {}, \"name\": \"new\", \"params\": [],"
    "         \"return_type\": null, \"body\": null}"
    "      ],"
    "      \"struct\": {\"kind\": \"Type\", \"id\": 9, \"line\": 2,"
    "                  \"column\": 6, \"ann\": {}, \"name\": \"Point\","
    "                  \"args\": []}"
    "    },"
    "     {\"kind\": \"FnDecl\", \"id\": 5, \"line\": 3, \"column\": 1,"
    "      \"ann\": {}, \"name\": \"main\", \"params\": ["
    "        {\"kind\": \"Param\", \"id\": 8, \"line\": 3, \"column\": 5,"
    "         \"ann\": {}, \"name\": \"p\", \"type\": null, \"self\": false}"
    "      ], \"return_type\": null, \"body\": null}"
    "   ]"
    " }"
    "}";

static CwModule_t* load(const char* json) {
    return cwmodule_load_string(json, strlen(json));
}

static void fixture_path(char* buf, size_t cap, const char* name) {
    const char* f = __FILE__;
    size_t n = strlen(f);
    while (n > 0 && f[n - 1] != '/' && f[n - 1] != '\\') n--;
    snprintf(buf, cap, "%.*sfixtures/%s", (int)n, f, name);
}

static const char* k_impl_extra =
    "{\"format\": \"cwind-typed-ast\", \"version\": 1,"
    " \"symbols\": ["
    "   {\"name\": \"Show\", \"kind\": \"trait\", \"ref\": 2},"
    "   {\"name\": \"Box\", \"kind\": \"struct\", \"ref\": 3}"
    " ],"
    " \"bindings\": ["
    "   {\"id\": 1, \"decl_id\": 4, \"owner\": \"Box\","
    "    \"trait\": \"Show\", \"fn_id\": 6},"
    "   {\"id\": 2, \"decl_id\": 5, \"owner\": \"Box\","
    "    \"trait\": null, \"fn_id\": 7}"
    " ],"
    " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {}, \"items\": ["
    "   {\"kind\": \"TraitDecl\", \"id\": 2, \"ann\": {},"
    "    \"name\": \"Show\", \"methods\": []},"
    "   {\"kind\": \"StructDecl\", \"id\": 3, \"ann\": {},"
    "    \"name\": \"Box\", \"fields\": []},"
    "   {\"kind\": \"ImplDecl\", \"id\": 4, \"ann\": {},"
    "    \"trait\": {\"kind\": \"Type\", \"id\": 8, \"ann\": {},"
    "               \"name\": \"Show\", \"args\": []},"
    "    \"struct\": {\"kind\": \"Type\", \"id\": 9, \"ann\": {},"
    "                \"name\": \"Box\", \"args\": []},"
    "    \"params\": [], \"methods\": ["
    "      {\"kind\": \"FnDecl\", \"id\": 6, \"ann\": {}, \"name\": \"str\","
    "       \"params\": [], \"return_type\": null, \"body\": null}"
    "    ]},"
    "   {\"kind\": \"ExtraDecl\", \"id\": 5, \"ann\": {},"
    "    \"struct\": {\"kind\": \"Type\", \"id\": 10, \"ann\": {},"
    "                \"name\": \"Box\", \"args\": []},"
    "    \"params\": [], \"methods\": ["
    "      {\"kind\": \"FnDecl\", \"id\": 7, \"ann\": {},"
    "       \"name\": \"double\", \"params\": [],"
    "       \"return_type\": null, \"body\": null}"
    "    ]}"
    " ]}}";

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CwModule tests:\n\n");

    printf(" - valid module\n");
    CwModule_t* m = load(k_valid);
    T("load valid != NULL", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        printf("\n%d passed, %d failed\n", pass, fail);
        return 1;
    }
    T("format ok", strcmp(cwmodule_format(m), "cwind-typed-ast") == 0);
    T("version ok", cwmodule_version(m) == 1);
    T("symbol count 2", cwmodule_symbol_count(m) == 2);
    T("binding count 1", cwmodule_binding_count(m) == 1);
    T("node count 9", cwmodule_node_count(m) == 9);

    const CwSymbol_t* s = cwmodule_find_symbol(m, "Point");
    T("find_symbol(Point)", s != NULL && strcmp(s->kind, "struct") == 0
      && s->ref == 2);
    T("find_symbol(main)", cwmodule_find_symbol(m, "main") != NULL);
    T("find_symbol(missing) NULL", cwmodule_find_symbol(m, "nope") == NULL);
    T("symbol(0) name", cwmodule_symbol(m, 0) != NULL
      && strcmp(cwmodule_symbol(m, 0)->name, "Point") == 0);
    T("symbol(99) NULL", cwmodule_symbol(m, 99) == NULL);

    const CwBinding_t* b = cwmodule_binding(m, 0);
    T("binding fields", b != NULL && b->id == 1 && b->decl_id == 4
      && b->fn_id == 7 && b->owner != NULL
      && strcmp(b->owner, "Point") == 0 && b->trait == NULL);

    const CwNode_t* n = cwmodule_node(m, 7);
    T("node(7) is FnDecl", n != NULL && strcmp(n->kind, "FnDecl") == 0);
    T("node(1) is Program",
      cwmodule_node(m, 1) != NULL
      && strcmp(cwmodule_node(m, 1)->kind, "Program") == 0);
    T("node(999) NULL", cwmodule_node(m, 999) == NULL);
    T("node_at(0).id == 1", cwmodule_node_at(m, 0) != NULL
      && cwmodule_node_at(m, 0)->id == 1);
    T("node pool sorted", cwmodule_node_at(m, 7) != NULL
      && cwmodule_node_at(m, 7)->id == 8);
    T("ast_root != NULL", cwmodule_ast_root(m) != NULL);
    cwmodule_free(m);

    printf("\n - invalid inputs\n");
    m = load("{\"format\": \"other\", \"version\": 1,"
             " \"symbols\": [], \"bindings\": [], \"ast\": {}}");
    T("bad format rejected", m == NULL);
    T("error message set", cwmodule_error()[0] != '\0');

    m = load("{\"format\": \"cwind-typed-ast\","
             " \"version\": 2, \"symbols\": [],"
             " \"bindings\": [], \"ast\": {}}");
    T("bad version rejected", m == NULL);

    m = load("{\"format\": \"cwind-typed-ast\","
             " \"version\": 1, \"symbols\": [],"
             " \"bindings\": [], \"ast\": []}");
    T("ast not object rejected", m == NULL);

    m = load("{\"format\": \"cwind-typed-ast\","
             " \"version\": 1,"
             " \"symbols\": [{\"name\": \"x\","
             " \"kind\": \"fn\", \"ref\": 99}],"
             " \"bindings\": [],"
             " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {}}}");
    T("symbol ref missing rejected", m == NULL);

    m = load("{\"format\": \"cwind-typed-ast\","
             " \"version\": 1, \"symbols\": [],"
             " \"bindings\": [{\"id\": 1, \"decl_id\": 2,"
             " \"owner\": null, \"trait\": null, \"fn_id\": 3},"
             " {\"id\": 1, \"decl_id\": 2,"
             " \"owner\": null, \"trait\": null, \"fn_id\": 3}],"
             " \"ast\": {\"kind\": \"Program\", \"id\": 1,"
             " \"ann\": {}, \"items\": ["
             "   {\"kind\": \"X\", \"id\": 2, \"ann\": {}},"
             "   {\"kind\": \"Y\", \"id\": 3, \"ann\": {}}"
             " ]}}");
    T("duplicate binding id rejected", m == NULL);

    m = load("{\"format\": \"cwind-typed-ast\","
             " \"version\": 1, \"symbols\": [],"
             " \"bindings\": [{\"id\": 1, \"decl_id\": 2,"
             " \"owner\": null, \"trait\": null, \"fn_id\": 99}],"
             " \"ast\": {\"kind\": \"Program\", \"id\": 1,"
             " \"ann\": {}, \"items\": ["
             "   {\"kind\": \"X\", \"id\": 2, \"ann\": {}}"
             " ]}}");
    T("binding fn_id missing rejected", m == NULL);

    m = load("{\"format\": \"cwind-typed-ast\","
             " \"version\": 1, \"symbols\": [],"
             " \"bindings\": [],"
             " \"ast\": {\"kind\": \"Program\", \"id\": 1,"
             " \"ann\": {}, \"items\": ["
             "   {\"kind\": \"A\", \"id\": 1, \"ann\": {}},"
             "   {\"kind\": \"B\", \"id\": 1, \"ann\": {}}"
             " ]}}");
    T("duplicate node id rejected", m == NULL);

    m = load("{not json");
    T("syntax error rejected", m == NULL);
    m = cwmodule_load_file("build/definitely_missing_file.json");
    T("missing file rejected", m == NULL);
    T("missing file error", cwmodule_error()[0] != '\0');

    printf("\n - impl / extra binding positions\n");
    m = load(k_impl_extra);
    T("impl/extra sample loads", m != NULL);
    if (m) {
        T("sample symbols 2", cwmodule_symbol_count(m) == 2);
        T("sample bindings 2", cwmodule_binding_count(m) == 2);
        T("sample nodes 10", cwmodule_node_count(m) == 10);
        const CwBinding_t* b0 = cwmodule_binding(m, 0);
        const CwBinding_t* b1 = cwmodule_binding(m, 1);
        T("sample impl binding",
          b0 && b0->id == 1 && b0->decl_id == 4 && b0->owner
          && strcmp(b0->owner, "Box") == 0 && b0->trait
          && strcmp(b0->trait, "Show") == 0 && b0->fn_id == 6);
        T("sample extra binding",
          b1 && b1->id == 2 && b1->decl_id == 5 && b1->owner
          && strcmp(b1->owner, "Box") == 0 && b1->trait == NULL
          && b1->fn_id == 7);
        cwmodule_free(m);
    }

    printf("\n - real fixture (frontend output)\n");
    char fix[1024];
    fixture_path(fix, sizeof(fix), "bindings_sample.json");
    m = cwmodule_load_file(fix);
    T("fixture loads", m != NULL);
    if (m) {
        T("fixture symbols 3", cwmodule_symbol_count(m) == 3);
        const CwSymbol_t* s = cwmodule_find_symbol(m, "Show");
        T("fixture Show -> TraitDecl",
          s && strcmp(s->kind, "trait") == 0 && s->ref == 2);
        s = cwmodule_find_symbol(m, "Box");
        T("fixture Box -> StructDecl",
          s && strcmp(s->kind, "struct") == 0 && s->ref == 6);
        s = cwmodule_find_symbol(m, "main");
        T("fixture main -> FnDecl",
          s && strcmp(s->kind, "fn") == 0 && s->ref == 29);
        T("fixture bindings 2", cwmodule_binding_count(m) == 2);
        const CwBinding_t* b0 = cwmodule_binding(m, 0);
        const CwBinding_t* b1 = cwmodule_binding(m, 1);
        T("fixture impl binding",
          b0 && b0->id == 1 && b0->decl_id == 9
          && strcmp(b0->owner, "Box") == 0
          && strcmp(b0->trait, "Show") == 0 && b0->fn_id == 12);
        T("fixture extra binding",
          b1 && b1->id == 2 && b1->decl_id == 18
          && strcmp(b1->owner, "Box") == 0 && b1->trait == NULL
          && b1->fn_id == 20);
        T("fixture nodes 33", cwmodule_node_count(m) == 33);
        const CwNode_t* d0 = cwmodule_node(m, 9);
        const CwNode_t* d1 = cwmodule_node(m, 18);
        T("fixture decl kinds",
          d0 && strcmp(d0->kind, "ImplDecl") == 0
          && d1 && strcmp(d1->kind, "ExtraDecl") == 0);
        cwmodule_free(m);
    }

    printf("\n - deep validation rejections\n");
    m = load("{\"format\": \"cwind-typed-ast\", \"version\": 1,"
             " \"symbols\": [{\"name\": \"x\", \"kind\": \"fn\","
             " \"ref\": 1}], \"bindings\": [],"
             " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {}}}");
    T("symbol kind mismatch rejected", m == NULL);

    m = load("{\"format\": \"cwind-typed-ast\", \"version\": 1,"
             " \"symbols\": [],"
             " \"bindings\": [{\"id\": 1, \"decl_id\": 2,"
             " \"owner\": \"Box\", \"trait\": null, \"fn_id\": 3}],"
             " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {},"
             " \"items\": ["
             "   {\"kind\": \"FnDecl\", \"id\": 2, \"ann\": {},"
             "    \"name\": \"f\", \"params\": [],"
             "    \"return_type\": null, \"body\": null},"
             "   {\"kind\": \"FnDecl\", \"id\": 3, \"ann\": {},"
             "    \"name\": \"g\", \"params\": [],"
             "    \"return_type\": null, \"body\": null}"
             " ]}}");
    T("binding decl not Impl/Extra rejected", m == NULL);

    m = load("{\"format\": \"cwind-typed-ast\", \"version\": 1,"
             " \"symbols\": [],"
             " \"bindings\": [{\"id\": 1, \"decl_id\": 2,"
             " \"owner\": \"Box\", \"trait\": null, \"fn_id\": 3}],"
             " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {},"
             " \"items\": ["
             "   {\"kind\": \"ExtraDecl\", \"id\": 2, \"ann\": {},"
             "    \"struct\": {\"kind\": \"Type\", \"id\": 4, \"ann\": {},"
             "                \"name\": \"Box\", \"args\": []},"
             "    \"params\": [], \"methods\": []},"
             "   {\"kind\": \"StructDecl\", \"id\": 3, \"ann\": {},"
             "    \"name\": \"Box\", \"fields\": []}"
             " ]}}");
    T("binding fn not FnDecl rejected", m == NULL);

    m = load("{\"format\": \"cwind-typed-ast\", \"version\": 1,"
             " \"symbols\": [],"
             " \"bindings\": [{\"id\": 1, \"decl_id\": 2,"
             " \"owner\": \"Other\", \"trait\": null, \"fn_id\": 3}],"
             " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {},"
             " \"items\": ["
             "   {\"kind\": \"ExtraDecl\", \"id\": 2, \"ann\": {},"
             "    \"struct\": {\"kind\": \"Type\", \"id\": 4, \"ann\": {},"
             "                \"name\": \"Box\", \"args\": []},"
             "    \"params\": [], \"methods\": []},"
             "   {\"kind\": \"FnDecl\", \"id\": 3, \"ann\": {},"
             "    \"name\": \"f\", \"params\": [],"
             "    \"return_type\": null, \"body\": null}"
             " ]}}");
    T("binding owner mismatch rejected", m == NULL);

    m = load("{\"format\": \"cwind-typed-ast\", \"version\": 1,"
             " \"symbols\": [],"
             " \"bindings\": [{\"id\": 1, \"decl_id\": 2,"
             " \"owner\": \"Box\", \"trait\": \"OtherTrait\","
             " \"fn_id\": 3}],"
             " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {},"
             " \"items\": ["
             "   {\"kind\": \"ImplDecl\", \"id\": 2, \"ann\": {},"
             "    \"trait\": {\"kind\": \"Type\", \"id\": 4, \"ann\": {},"
             "               \"name\": \"Show\", \"args\": []},"
             "    \"struct\": {\"kind\": \"Type\", \"id\": 5, \"ann\": {},"
             "                \"name\": \"Box\", \"args\": []},"
             "    \"params\": [], \"methods\": []},"
             "   {\"kind\": \"FnDecl\", \"id\": 3, \"ann\": {},"
             "    \"name\": \"f\", \"params\": [],"
             "    \"return_type\": null, \"body\": null}"
             " ]}}");
    T("binding trait mismatch rejected", m == NULL);

    m = load("{\"format\": \"cwind-typed-ast\", \"version\": 1,"
             " \"symbols\": [],"
             " \"bindings\": [{\"id\": 1, \"decl_id\": 2,"
             " \"owner\": \"Box\", \"trait\": \"Show\", \"fn_id\": 3}],"
             " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {},"
             " \"items\": ["
             "   {\"kind\": \"ExtraDecl\", \"id\": 2, \"ann\": {},"
             "    \"struct\": {\"kind\": \"Type\", \"id\": 4, \"ann\": {},"
             "                \"name\": \"Box\", \"args\": []},"
             "    \"params\": [], \"methods\": []},"
             "   {\"kind\": \"FnDecl\", \"id\": 3, \"ann\": {},"
             "    \"name\": \"f\", \"params\": [],"
             "    \"return_type\": null, \"body\": null}"
             " ]}}");
    T("extra binding with trait rejected", m == NULL);

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
