/**
 * 独立测试: 函数体代码生成 (LLVM IR 文本断言)
 * 编译 (需要 LLVM 18):
 *   gcc -std=c11 -O2 -Wall -Wextra -pedantic
 *       -I E:/LLVM/LLVM18/include
 *       -o test_cwcodegen.exe test_cwcodegen.c
 *       ../../compiler/cwcodegen.c
 *       ../../compiler/cwllvm.c
 *       ../../compiler/cwmodule.c
 *       ../../compiler/cwtype.c
 *       ../../compiler/cwlayout.c
 *       ../../compiler/cwsymbol.c
 *       -L E:/LLVM/LLVM18/lib -lLLVM-C
 */

#include "../../compiler/cwcodegen.h"
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

/* 测试用的 TypedAST JSON 超过 C99 单字面量 4095 字节建议上限, 拆两段拼接 */
static const char* k_prog_a =
    "{\"format\": \"cwind-typed-ast\", \"version\": 1,"
    " \"symbols\": ["
    "   {\"name\": \"add\", \"kind\": \"fn\", \"ref\": 2},"
    "   {\"name\": \"main\", \"kind\": \"fn\", \"ref\": 13}"
    " ], \"bindings\": [],"
    " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {}, \"items\": ["
    "   {\"kind\": \"FnDecl\", \"id\": 2, \"ann\": {}, \"name\": \"add\","
    "    \"params\": ["
    "      {\"kind\": \"Param\", \"id\": 3,"
    "       \"ann\": {\"type\": {\"name\": \"Int\"}}, \"name\": \"a\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 4, \"ann\": {},"
    "                 \"name\": \"Int\", \"args\": []}},"
    "      {\"kind\": \"Param\", \"id\": 5,"
    "       \"ann\": {\"type\": {\"name\": \"Int\"}}, \"name\": \"b\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 6, \"ann\": {},"
    "                 \"name\": \"Int\", \"args\": []}}"
    "    ],"
    "    \"return_type\": {\"kind\": \"Type\", \"id\": 7, \"ann\": {},"
    "                     \"name\": \"Int\", \"args\": []},"
    "    \"body\": {\"kind\": \"Block\", \"id\": 8, \"ann\": {}, \"stmts\": ["
    "      {\"kind\": \"ReturnStmt\", \"id\": 9, \"ann\": {},"
    "       \"value\": {\"kind\": \"BinOp\", \"id\": 10,"
    "        \"ann\": {\"type\": {\"name\": \"Int\"}}, \"op\": \"+\","
    "        \"left\": {\"kind\": \"Name\", \"id\": 11,"
    "                  \"ann\": {\"binding\": {\"kind\": \"var\", \"ref\": 3},"
    "                          \"type\": {\"name\": \"Int\"}},"
    "                  \"parts\": [\"a\"]},"
    "        \"right\": {\"kind\": \"Name\", \"id\": 12,"
    "                   \"ann\": {\"binding\": {\"kind\": \"var\", \"ref\": 5},"
    "                           \"type\": {\"name\": \"Int\"}},"
    "                   \"parts\": [\"b\"]}}}"
    "    ]}},"
    "   {\"kind\": \"FnDecl\", \"id\": 13, \"ann\": {}, \"name\": \"main\","
    "    \"params\": [],"
    "    \"return_type\": {\"kind\": \"Type\", \"id\": 14, \"ann\": {},"
    "                     \"name\": \"Int\", \"args\": []},"
    "    \"body\": {\"kind\": \"Block\", \"id\": 15, \"ann\": {}, \"stmts\": ["
    "      {\"kind\": \"LetStmt\", \"id\": 16,"
    "       \"ann\": {\"type\": {\"name\": \"Int\"}}, \"name\": \"x\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 17, \"ann\": {},"
    "                 \"name\": \"Int\", \"args\": []},"
    "       \"value\": {\"kind\": \"IntLit\", \"id\": 18,"
    "                  \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                  \"value\": 3, \"raw\": \"3\"}},";
static const char* k_prog_b =
    "      {\"kind\": \"LetStmt\", \"id\": 33,"
    "       \"ann\": {\"type\": {\"name\": \"Bool\"}}, \"name\": \"ok\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 34, \"ann\": {},"
    "                 \"name\": \"Bool\", \"args\": []},"
    "       \"value\": {\"kind\": \"BinOp\", \"id\": 35,"
    "        \"ann\": {\"type\": {\"name\": \"Bool\"}}, \"op\": \"&&\","
    "        \"left\": {\"kind\": \"BinOp\", \"id\": 36,"
    "                  \"ann\": {\"type\": {\"name\": \"Bool\"}}, \"op\": \">\","
    "                  \"left\": {\"kind\": \"Name\", \"id\": 37,"
    "                            \"ann\": {\"binding\": {\"kind\": \"var\","
    "                                                    \"ref\": 16},"
    "                                    \"type\": {\"name\": \"Int\"}},"
    "                            \"parts\": [\"x\"]},"
    "                  \"right\": {\"kind\": \"IntLit\", \"id\": 38,"
    "                             \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                             \"value\": 2, \"raw\": \"2\"}},"
    "        \"right\": {\"kind\": \"BinOp\", \"id\": 39,"
    "                   \"ann\": {\"type\": {\"name\": \"Bool\"}}, \"op\": \"<\","
    "                   \"left\": {\"kind\": \"Name\", \"id\": 40,"
    "                             \"ann\": {\"binding\": {\"kind\": \"var\","
    "                                                     \"ref\": 16},"
    "                                     \"type\": {\"name\": \"Int\"}},"
    "                             \"parts\": [\"x\"]},"
    "                   \"right\": {\"kind\": \"IntLit\", \"id\": 41,"
    "                              \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                              \"value\": 10, \"raw\": \"10\"}}}"
    "      },"
    "      {\"kind\": \"ExprStmt\", \"id\": 19, \"ann\": {},"
    "       \"expr\": {\"kind\": \"Call\", \"id\": 20,"
    "        \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                            \"callee_ref\": \"print\"},"
    "                \"type\": {\"name\": \"None\"}},"
    "        \"callee\": {\"kind\": \"Name\", \"id\": 21,"
    "                    \"ann\": {\"binding\": {\"kind\": \"builtin\","
    "                                            \"ref\": \"print\"},"
    "                            \"type\": {\"name\": \"Fn\"}},"
    "                    \"parts\": [\"builtins\", \"print\"]},"
    "        \"args\": [{\"kind\": \"Arg\", \"id\": 22, \"ann\": {},"
    "          \"value\": {\"kind\": \"Call\", \"id\": 23,"
    "           \"ann\": {\"call\": {\"callee_kind\": \"fn\","
    "                               \"callee_ref\": 2},"
    "                   \"type\": {\"name\": \"Int\"}},"
    "           \"callee\": {\"kind\": \"Name\", \"id\": 24,"
    "                       \"ann\": {\"binding\": {\"kind\": \"fn\","
    "                                               \"ref\": 2},"
    "                               \"type\": {\"name\": \"Fn\"}},"
    "                       \"parts\": [\"add\"]},"
    "           \"args\": ["
    "             {\"kind\": \"Arg\", \"id\": 25, \"ann\": {},"
    "              \"value\": {\"kind\": \"Name\", \"id\": 26,"
    "               \"ann\": {\"binding\": {\"kind\": \"var\", \"ref\": 16},"
    "                       \"type\": {\"name\": \"Int\"}},"
    "               \"parts\": [\"x\"]}},"
    "             {\"kind\": \"Arg\", \"id\": 27, \"ann\": {},"
    "              \"value\": {\"kind\": \"IntLit\", \"id\": 28,"
    "               \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "               \"value\": 4, \"raw\": \"4\"}}"
    "           ]}}]}},"
    "      {\"kind\": \"Assign\", \"id\": 42, \"ann\": {}, \"op\": \"+=\","
    "       \"target\": {\"kind\": \"Name\", \"id\": 43,"
    "                   \"ann\": {\"binding\": {\"kind\": \"var\", \"ref\": 16},"
    "                           \"type\": {\"name\": \"Int\"}},"
    "                   \"parts\": [\"x\"]},"
    "       \"value\": {\"kind\": \"IntLit\", \"id\": 44,"
    "                  \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                  \"value\": 2, \"raw\": \"2\"}},"
    "      {\"kind\": \"ReturnStmt\", \"id\": 29, \"ann\": {},"
    "       \"value\": {\"kind\": \"BinOp\", \"id\": 30,"
    "        \"ann\": {\"type\": {\"name\": \"Int\"}}, \"op\": \"*\","
    "        \"left\": {\"kind\": \"Name\", \"id\": 31,"
    "                  \"ann\": {\"binding\": {\"kind\": \"var\", \"ref\": 16},"
    "                          \"type\": {\"name\": \"Int\"}},"
    "                  \"parts\": [\"x\"]},"
    "        \"right\": {\"kind\": \"IntLit\", \"id\": 32,"
    "                   \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                   \"value\": 2, \"raw\": \"2\"}}}"
    "    ]}}"
    " ]}}";

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("CwCodegen tests:\n\n");

    char prog_buf[8192];
    snprintf(prog_buf, sizeof(prog_buf), "%s%s", k_prog_a, k_prog_b);
    CwModule_t* m = cwmodule_load_string(prog_buf, strlen(prog_buf));
    T("module loads", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        printf("\n%d passed, %d failed\n", pass, fail);
        return 1;
    }

    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);
    T("build symbols", cwsym_build_from_module(&syms, m));

    CwLlvm_t ll;
    T("llvm init", cwllvm_init(&ll, "test", &types, &layouts, &syms));
    T("declare symbols", cwllvm_declare_symbols(&ll));

    CwCodegen_t cg;
    T("codegen init", cwcodegen_init(&cg, &ll, m));
    T("emit", cwcodegen_emit(&cg));
    if (cg.failed) {
        printf("  codegen error: %s\n", cwcodegen_error(&cg));
    }
    char* ir = cwllvm_dump(&ll);
    T("dump ok", ir != NULL);
    if (ir) {
        T("IR: record type", strstr(ir, "%cw.record") != NULL);
        T("IR: handle type", strstr(ir, "%cw.handle") != NULL);
        T("IR: add function",
          strstr(ir, "define %cw.handle @cwind.fn.add(") != NULL);
        T("IR: main function",
          strstr(ir, "define %cw.handle @cwind.fn.main(") != NULL);
        T("IR: wrapper",
          strstr(ir, "define i32 @main()") != NULL);
        T("IR: integer add", strstr(ir, "add i16") != NULL);
        T("IR: integer mul", strstr(ir, "mul i16") != NULL);
        T("IR: print call",
          strstr(ir, "call i1 @cw_builtin_print") != NULL);
        T("IR: ret handle", strstr(ir, "ret %cw.handle") != NULL);
        T("IR: short-circuit blocks",
          strstr(ir, "logical.rhs") != NULL
          && strstr(ir, "logical.short") != NULL);
        T("IR: compound assign", strstr(ir, "%acc") != NULL);
        LLVMDisposeMessage(ir);
    }

    cwcodegen_destroy(&cg);
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
