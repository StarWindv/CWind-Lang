/**
 * 独立测试: 符号表 + 名称修饰 + 泛型实例 key
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwsymbol.exe test_cwsymbol.c
 *       ../../compiler/cwmodule.c
 *       ../../compiler/cwtype.c
 *       ../../compiler/cwsymbol.c
 */

#include "../../compiler/cwmodule.h"
#include "../../compiler/cwtype.h"
#include "../../compiler/cwsymbol.h"

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

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CwSymbol tests:\n\n");

    printf(" - name mangling\n");
    char m[256];
    T("fn mangle", cw_mangle_fn(m, sizeof(m), "main")
      && strcmp(m, "cwind.fn.main") == 0);
    T("method mangle", cw_mangle_method(m, sizeof(m), "Box", "str")
      && strcmp(m, "cwind.method.Box.str") == 0);

    CwTypeTable_t types;
    cwtype_table_init(&types);
    CwTypeId int_id = cwtype_intern(&types, "Int", NULL, 0);
    CwTypeId str_id = cwtype_intern(&types, "String", NULL, 0);
    T("instance mangle 1 arg",
      cw_mangle_instance(m, sizeof(m), "cwind.fn.id", &types, &int_id, 1)
      && strcmp(m, "cwind.fn.id.Int") == 0);
    CwTypeId two[2] = { int_id, str_id };
    T("instance mangle 2 args",
      cw_mangle_instance(m, sizeof(m), "cwind.fn.id", &types, two, 2)
      && strcmp(m, "cwind.fn.id.Int.String") == 0);
    CwTypeId vec_args[1] = { int_id };
    CwTypeId vec_id = cwtype_intern(&types, "Vector", vec_args, 1);
    T("instance mangle nested",
      cw_mangle_instance(m, sizeof(m), "cwind.fn.id", &types, &vec_id, 1)
      && strcmp(m, "cwind.fn.id.Vector.Int") == 0);
    CwTypeId map_args[2] = { str_id, int_id };
    CwTypeId map_id = cwtype_intern(&types, "Map", map_args, 2);
    T("instance mangle map",
      cw_mangle_instance(m, sizeof(m), "cwind.fn.id", &types, &map_id, 1)
      && strcmp(m, "cwind.fn.id.Map.String.Int") == 0);
    T("mangle rejects NULL", !cw_mangle_fn(m, sizeof(m), NULL));

    printf("\n - symbol table\n");
    CwSymTable_t syms;
    cwsym_table_init(&syms);
    const CwSymEntry_t* e1 = cwsym_add(&syms, "cwind.fn.main", "main",
                                       CW_SYM_FN, NULL, NULL, NULL, 0, NULL);
    const CwSymEntry_t* e2 = cwsym_add(&syms, "cwind.fn.main", "main",
                                       CW_SYM_FN, NULL, NULL, NULL, 0, NULL);
    T("add dedupe", e1 != NULL && e1 == e2);
    T("find by mangled",
      cwsym_find_mangled(&syms, "cwind.fn.main") == e1);
    T("find missing NULL", cwsym_find_mangled(&syms, "nope") == NULL);
    T("find by owner/name",
      cwsym_find(&syms, NULL, "main") == e1);
    T("find owner mismatch",
      cwsym_find(&syms, "Box", "main") == NULL);

    CwTypeId args_int[1] = { int_id };
    const CwSymEntry_t* gi = cwsym_add(&syms, "cwind.fn.id.Int", "id",
                                       CW_SYM_INSTANCE, NULL, NULL,
                                       args_int, 1, NULL);
    CwTypeId args_str[1] = { str_id };
    const CwSymEntry_t* gs = cwsym_add(&syms, "cwind.fn.id.String", "id",
                                       CW_SYM_INSTANCE, NULL, NULL,
                                       args_str, 1, NULL);
    T("generic instances distinct", gi != NULL && gs != NULL && gi != gs);
    T("instance args stored",
      gi && gi->inst_count == 1 && gi->inst_args[0] == int_id);

    printf("\n - build from module (fixture)\n");
    char fix[1024];
    fixture_path(fix, sizeof(fix), "bindings_sample.json");
    CwModule_t* fm = cwmodule_load_file(fix);
    T("fixture loads", fm != NULL);
    T("build from module", fm && cwsym_build_from_module(&syms, fm));
    const CwSymEntry_t* fmain = cwsym_find_mangled(&syms, "cwind.fn.main");
    T("fixture fn main", fmain && fmain->kind == CW_SYM_FN
      && strcmp(fmain->name, "main") == 0);
    const CwSymEntry_t* fstr = cwsym_find(&syms, "Box", "str");
    T("fixture method Box.str", fstr && fstr->kind == CW_SYM_METHOD
      && fstr->owner && strcmp(fstr->owner, "Box") == 0
      && fstr->trait && strcmp(fstr->trait, "Show") == 0);
    const CwSymEntry_t* fdbl = cwsym_find(&syms, "Box", "double");
    T("fixture method Box.double (extra)",
      fdbl && fdbl->kind == CW_SYM_METHOD && fdbl->trait == NULL);
    T("rebuild idempotent",
      cwsym_build_from_module(&syms, fm)
      && cwsym_find_mangled(&syms, "cwind.fn.main") == fmain);

    printf("\n - generic template registration\n");
    CwModule_t* gm = load(
        "{\"format\": \"cwind-typed-ast\", \"version\": 1,"
        " \"symbols\": [{\"name\": \"id\", \"kind\": \"fn\", \"ref\": 2}],"
        " \"bindings\": [],"
        " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {},"
        " \"items\": [{\"kind\": \"FnDecl\", \"id\": 2, \"ann\": {},"
        " \"name\": \"id\","
        " \"type_params\": [{\"kind\": \"TypeParam\", \"id\": 3,"
        " \"ann\": {}, \"name\": \"T\", \"bound\": null}],"
        " \"params\": [], \"return_type\": null, \"body\": null}]}}");
    T("generic module loads", gm != NULL);
    T("build generic module", gm && cwsym_build_from_module(&syms, gm));
    const CwSymEntry_t* tpl = cwsym_find_mangled(&syms, "cwind.fn.id");
    T("template registered", tpl && tpl->kind == CW_SYM_TEMPLATE
      && tpl->inst_count == 0);

    printf("\n - cleanup\n");
    cwsym_table_destroy(&syms);
    cwtype_table_destroy(&types);
    cwmodule_free(fm);
    cwmodule_free(gm);
    T("cleanup ok", 1);

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
