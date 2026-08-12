/**
 * 独立测试: LLVM 声明层 (句柄类型 + 符号表声明)
 * 编译 (需要 LLVM 18):
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -I E:/LLVM/LLVM18/include
 *       -o test_cwllvm.exe test_cwllvm.c
 *       ../../compiler/cwllvm.c
 *       ../../compiler/cwmodule.c
 *       ../../compiler/cwtype.c
 *       ../../compiler/cwlayout.c
 *       ../../compiler/cwsymbol.c
 *       -L E:/LLVM/LLVM18/lib -lLLVM-C
 */

#include "../../compiler/cwllvm.h"
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
    printf("CwLlvm tests:\n\n");

    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);

    CwLlvm_t ll;
    T("init", cwllvm_init(&ll, "test_mod", &types, &layouts, &syms));

    printf("\n - handle type\n");
    LLVMTypeRef h = cwllvm_handle_type(&ll);
    T("handle is struct", h && LLVMGetTypeKind(h) == LLVMStructTypeKind);
    T("handle 4 elements",
      h && LLVMCountStructElementTypes(h) == 4);
    T("handle elements i64",
      h && LLVMGetTypeKind(LLVMStructGetTypeAtIndex(h, 0)) == LLVMIntegerTypeKind
      && LLVMGetIntTypeWidth(LLVMStructGetTypeAtIndex(h, 0)) == 64);
    T("handle not packed", h && !LLVMIsPackedStruct(h));

    printf("\n - declare symbols (fixture)\n");
    char fix[1024];
    fixture_path(fix, sizeof(fix), "bindings_sample.json");
    CwModule_t* fm = cwmodule_load_file(fix);
    T("fixture loads", fm != NULL);
    T("build symbols", fm && cwsym_build_from_module(&syms, fm));
    T("declare symbols", cwllvm_declare_symbols(&ll));

    LLVMValueRef fmain = LLVMGetNamedFunction(ll.module, "cwind.fn.main");
    T("main declared", fmain != NULL);
    LLVMTypeRef fmain_type = fmain ? LLVMGlobalGetValueType(fmain) : NULL;
    T("main type is function",
      fmain_type && LLVMGetTypeKind(fmain_type) == LLVMFunctionTypeKind);
    T("main has 0 params",
      fmain_type && LLVMCountParamTypes(fmain_type) == 0);
    T("main returns handle",
      fmain_type && LLVMGetReturnType(fmain_type) == h);

    LLVMValueRef fstr = LLVMGetNamedFunction(ll.module,
                                             "cwind.method.Box.str");
    T("Box.str declared", fstr != NULL);
    LLVMTypeRef fstr_type = fstr ? LLVMGlobalGetValueType(fstr) : NULL;
    T("Box.str has self param",
      fstr_type && LLVMCountParamTypes(fstr_type) == 1);
    LLVMTypeRef p0 = NULL;
    if (fstr_type) LLVMGetParamTypes(fstr_type, &p0);
    T("self param is handle", fstr_type && p0 == h);
    T("Box.double declared",
      LLVMGetNamedFunction(ll.module, "cwind.method.Box.double") != NULL);

    /* 重复声明幂等 */
    LLVMValueRef fmain2 = LLVMGetNamedFunction(ll.module, "cwind.fn.main");
    cwllvm_declare_symbols(&ll);
    T("redeclare idempotent",
      LLVMGetNamedFunction(ll.module, "cwind.fn.main") == fmain2);

    printf("\n - generic instance declaration\n");
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
    T("build generic symbols", gm && cwsym_build_from_module(&syms, gm));
    CwTypeId int_id = cwtype_intern(&types, "Int", NULL, 0);
    CwTypeId args_int[1] = { int_id };
    const CwNode_t* id_decl = gm ? cwmodule_node(gm, 2) : NULL;
    T("register instance",
      cwsym_add(&syms, "cwind.fn.id.Int", "id", CW_SYM_INSTANCE,
                NULL, NULL, args_int, 1, id_decl) != NULL);
    T("declare with instance", cwllvm_declare_symbols(&ll));
    T("instance declared",
      LLVMGetNamedFunction(ll.module, "cwind.fn.id.Int") != NULL);
    T("template NOT declared",
      LLVMGetNamedFunction(ll.module, "cwind.fn.id") == NULL);

    printf("\n - module dump\n");
    char* dump = cwllvm_dump(&ll);
    T("dump contains main",
      dump && strstr(dump, "cwind.fn.main") != NULL);
    T("dump contains handle type",
      dump && strstr(dump, "cw.handle") != NULL);
    if (dump) LLVMDisposeMessage(dump);

    printf("\n - cleanup\n");
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(fm);
    cwmodule_free(gm);
    T("cleanup ok", 1);

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
