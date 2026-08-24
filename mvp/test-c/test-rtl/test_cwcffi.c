/**
 * Copyright (C) 2026 CWind-Project
 * License: BSD-3.0
 *
 * 独立测试: CFFI (todo-48/49/62/63)
 *  - extern 块 #[link(...)] 属性收集 (cwmodule_link_*, 含 relative 锚点)
 *  - 信封顶层 source 字段 (cwmodule_source)
 *  - extern 函数符号: CW_SYM_EXTERN, 不做 CWind 名称修饰;
 *    带 #[link_name] 时 LLVM 符号 = 重命名后的 C 名 (todo-62)
 *  - extern 声明的节点访问 (params / return_type / body == null)
 *
 * 编译:
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -o test_cwcffi.exe test_cwcffi.c
 *       ../../compiler/cwmodule.c
 *       ../../compiler/cwtype.c
 *       ../../compiler/cwsymbol.c
 */

#include "../../compiler/cwmodule.h"
#include "../../compiler/cwsymbol.h"
#include "../../rt-src/include/stl/json/cwind_json.h"

#include <stdio.h>
#include <string.h>

static int pass = 0, fail = 0;

#define T(name, cond) do {                                             \
    if (cond) { printf("  [PASS] %s\n", name); pass++; }               \
    else      { printf("  [FAIL] %s\n", name); fail++; }               \
} while (0)

static const char* MODULE_JSON =
    "{"
    "\"format\": \"cwind-typed-ast\", \"version\": 1,"
    " \"source\": \"E:/proj/src/app.wind\","
    " \"symbols\": ["
    "  {\"name\": \"abs\", \"kind\": \"fn\", \"ref\": 3},"
    "  {\"name\": \"ffi_add\", \"kind\": \"fn\", \"ref\": 9},"
    "  {\"name\": \"main\", \"kind\": \"fn\", \"ref\": 16},"
    "  {\"name\": \"my_fn\", \"kind\": \"fn\", \"ref\": 21},"
    "  {\"name\": \"TAG\", \"kind\": \"static\", \"ref\": 24}"
    " ],"
    " \"bindings\": [],"
    " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {},"
    " \"items\": ["
    /* ExternBlock #1: 无 link 属性, libc abs */
    "  {\"kind\": \"ExternBlock\", \"id\": 2, \"ann\": {},"
    "   \"abi\": \"C\","
    "   \"fns\": [{\"kind\": \"FnDecl\", \"id\": 3, \"ann\": {},"
    "     \"name\": \"abs\", \"type_params\": [],"
    "     \"params\": [{\"kind\": \"Param\", \"id\": 4, \"ann\": {},"
    "       \"name\": \"input\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 5, \"ann\": {},"
    "         \"name\": \"Int32\", \"args\": []},"
    "       \"mutable\": false}],"
    "     \"return_type\": {\"kind\": \"Type\", \"id\": 6, \"ann\": {},"
    "       \"name\": \"Int32\", \"args\": []},"
    "     \"body\": null, \"pub\": false, \"static\": false,"
    "     \"which\": null, \"extern_abi\": \"C\"}],"
    "   \"pub\": false,"
    "   \"link_name\": null, \"link_kind\": null, \"link_path\": null},"
    /* ExternBlock #2: name + kind + path 全参数, path 锚定源文件目录 */
    "  {\"kind\": \"ExternBlock\", \"id\": 7, \"ann\": {},"
    "   \"abi\": \"C\","
    "   \"fns\": [{\"kind\": \"FnDecl\", \"id\": 9, \"ann\": {},"
    "     \"name\": \"ffi_add\", \"type_params\": [],"
    "     \"params\": ["
    "      {\"kind\": \"Param\", \"id\": 10, \"ann\": {},"
    "       \"name\": \"a\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 11, \"ann\": {},"
    "         \"name\": \"Int32\", \"args\": []}, \"mutable\": false},"
    "      {\"kind\": \"Param\", \"id\": 12, \"ann\": {},"
    "       \"name\": \"b\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 13, \"ann\": {},"
    "         \"name\": \"Int32\", \"args\": []}, \"mutable\": false}],"
    "     \"return_type\": {\"kind\": \"Type\", \"id\": 14, \"ann\": {},"
    "       \"name\": \"Int32\", \"args\": []},"
    "     \"body\": null, \"pub\": false, \"static\": false,"
    "     \"which\": null, \"extern_abi\": \"C\"}],"
    "   \"pub\": false,"
    "   \"link_name\": \"cwindmath\", \"link_kind\": \"static\","
    "   \"link_path\": \"./libcwindmath.a\","
    "   \"link_relative\": \"source\"},"
    /* 用户 main */
    "  {\"kind\": \"FnDecl\", \"id\": 16, \"ann\": {},"
    "   \"name\": \"main\", \"type_params\": [], \"params\": [],"
    "   \"return_type\": {\"kind\": \"Type\", \"id\": 17, \"ann\": {},"
    "     \"name\": \"Int\", \"args\": []},"
    "   \"body\": {\"kind\": \"Block\", \"id\": 18, \"ann\": {},"
    "     \"stmts\": []},"
    "   \"pub\": false, \"static\": false, \"which\": null,"
    "   \"extern_abi\": null},"
    /* ExternBlock #3 (todo-62): fn / static 均带 link_name 重命名 */
    "  {\"kind\": \"ExternBlock\", \"id\": 20, \"ann\": {},"
    "   \"abi\": \"C\","
    "   \"fns\": [{\"kind\": \"FnDecl\", \"id\": 21, \"ann\": {},"
    "     \"name\": \"my_fn\", \"type_params\": [], \"params\": [],"
    "     \"return_type\": {\"kind\": \"Type\", \"id\": 22, \"ann\": {},"
    "       \"name\": \"Int32\", \"args\": []},"
    "     \"body\": null, \"pub\": false, \"static\": false,"
    "     \"which\": null, \"extern_abi\": \"C\","
    "     \"link_name\": \"real_c_fn\"}],"
    "   \"statics\": [{\"kind\": \"ExternStatic\", \"id\": 24, \"ann\": {},"
    "     \"name\": \"TAG\","
    "     \"type\": {\"kind\": \"Type\", \"id\": 25, \"ann\": {},"
    "       \"name\": \"Int32\", \"args\": []},"
    "     \"mutable\": true, \"pub\": false,"
    "     \"link_name\": \"ffi_tagged\"}],"
    "   \"pub\": false,"
    "   \"link_name\": null, \"link_kind\": null, \"link_path\": null,"
    "   \"link_relative\": null},"
    /* ExternBlock #4: 与 #2 的 name/kind/path 相同但锚点不同,
     * 不得被去重合并 (todo-63) */
    "  {\"kind\": \"ExternBlock\", \"id\": 30, \"ann\": {},"
    "   \"abi\": \"C\", \"fns\": [], \"statics\": [],"
    "   \"pub\": false,"
    "   \"link_name\": \"cwindmath\", \"link_kind\": \"static\","
    "   \"link_path\": \"./libcwindmath.a\","
    "   \"link_relative\": null}"
    " ]}}"
    "";

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CwCFFI tests (todo-48/49/62/63):\n\n");

    printf(" - module load + link collection\n");
    CwModule_t* m = cwmodule_load_string(
        MODULE_JSON, strlen(MODULE_JSON));
    if (!m) {
        printf("  load error: %s\n", cwmodule_error());
    }
    T("module loads", m != NULL);
    /* #2 (source 锚点) 与 #4 (cwd 锚点) 的 name/kind/path 相同,
     * 但 relative 不同 -> 不去重合并 */
    T("two link entries (relative participates in dedup)",
      m && cwmodule_link_count(m) == 2);
    const CwLinkInfo_t* l = cwmodule_link(m, 0);
    T("link name", l && l->name && strcmp(l->name, "cwindmath") == 0);
    T("link kind", l && l->kind && strcmp(l->kind, "static") == 0);
    T("link path", l && l->path
      && strcmp(l->path, "./libcwindmath.a") == 0);
    T("link relative = source", l && l->relative
      && strcmp(l->relative, "source") == 0);
    const CwLinkInfo_t* l2 = cwmodule_link(m, 1);
    T("second entry same path", l2 && l2->path
      && strcmp(l2->path, "./libcwindmath.a") == 0);
    T("second entry relative defaults to cwd",
      l2 && l2->relative == NULL);
    T("out-of-range link is NULL", cwmodule_link(m, 2) == NULL);

    printf("\n - envelope source field (todo-63)\n");
    const char* src = cwmodule_source(m);
    T("source present", src && strcmp(src, "E:/proj/src/app.wind") == 0);

    printf("\n - extern declaration node access\n");
    const CwSymbol_t* abs_sym = cwmodule_find_symbol(m, "abs");
    T("abs symbol found",
      abs_sym && strcmp(abs_sym->kind, "fn") == 0);
    const CwNode_t* abs_decl = abs_sym
        ? cwmodule_node(m, abs_sym->ref) : NULL;
    T("abs decl kind FnDecl", abs_decl
      && strcmp(abs_decl->kind, "FnDecl") == 0);
    cw_value* abi = abs_decl
        ? cwmodule_node_field(abs_decl, "extern_abi") : NULL;
    T("extern_abi present",
      abi && cw_typeof(abi) == CW_STRING
      && strcmp(cw_string_cstr(abi), "C") == 0);
    T("abs has 1 param",
      abs_decl && cwmodule_fn_param_count(abs_decl) == 1);
    T("abs body is absent",
      abs_decl && cwmodule_fn_body(abs_decl) == NULL);
    cw_value* p0 = abs_decl ? cwmodule_fn_param(abs_decl, 0) : NULL;
    cw_value* p0name = p0 ? cw_object_get(p0, "name") : NULL;
    T("param named 'input'",
      p0name && cw_typeof(p0name) == CW_STRING
      && strcmp(cw_string_cstr(p0name), "input") == 0);
    cw_value* p0type = p0 ? cw_object_get(p0, "type") : NULL;
    T("param type Int32",
      p0type && cwmodule_type_is(p0type)
      && strcmp(cwmodule_type_name(p0type), "Int32") == 0);
    cw_value* rt = abs_decl ? cwmodule_fn_return_type(abs_decl) : NULL;
    T("return type Int32", rt && cwmodule_type_is(rt)
      && strcmp(cwmodule_type_name(rt), "Int32") == 0);

    printf("\n - extern symbols skip CWind mangling\n");
    CwSymTable_t syms;
    cwsym_table_init(&syms);
    T("build from module", cwsym_build_from_module(&syms, m));
    const CwSymEntry_t* se = cwsym_find(&syms, NULL, "abs");
    T("extern entry found", se != NULL);
    T("kind CW_SYM_EXTERN", se && se->kind == CW_SYM_EXTERN);
    T("symbol keeps raw C name", se && se->mangled
      && strcmp(se->mangled, "abs") == 0);
    const CwSymEntry_t* ffi = cwsym_find(&syms, NULL, "ffi_add");
    T("second extern entry raw too", ffi && ffi->kind == CW_SYM_EXTERN
      && ffi->mangled && strcmp(ffi->mangled, "ffi_add") == 0);
    const CwSymEntry_t* mn = cwsym_find_mangled(&syms, "cwind.fn.main");
    T("user fn still mangled", mn && mn->kind == CW_SYM_FN);

    printf("\n - link_name renames the linked symbol (todo-62)\n");
    const CwSymEntry_t* alias = cwsym_find(&syms, NULL, "my_fn");
    T("aliased extern found by CWind name", alias != NULL);
    T("aliased kind CW_SYM_EXTERN",
      alias && alias->kind == CW_SYM_EXTERN);
    /* LLVM 符号必须是重命名后的 C 名 */
    T("LLVM symbol is the link_name", alias && alias->mangled
      && strcmp(alias->mangled, "real_c_fn") == 0);
    T("CWind name preserved on entry",
      alias && alias->name && strcmp(alias->name, "my_fn") == 0);
    T("lookup by renamed symbol works",
      cwsym_find_mangled(&syms, "real_c_fn") != NULL);
    const CwSymbol_t* tag_sym = cwmodule_find_symbol(m, "TAG");
    T("extern static symbol registered",
      tag_sym && strcmp(tag_sym->kind, "static") == 0);
    const CwNode_t* tag_decl = tag_sym
        ? cwmodule_node(m, tag_sym->ref) : NULL;
    cw_value* tag_ln = tag_decl
        ? cwmodule_node_field(tag_decl, "link_name") : NULL;
    T("static carries its link_name",
      tag_ln && cw_typeof(tag_ln) == CW_STRING
      && strcmp(cw_string_cstr(tag_ln), "ffi_tagged") == 0);

    printf("\n - cleanup\n");
    cwsym_table_destroy(&syms);
    cwmodule_free(m);
    T("cleanup ok", 1);

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
