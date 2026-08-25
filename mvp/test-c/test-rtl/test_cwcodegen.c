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

/* Map 垂直切片: 字面量 / 下标读写 / contains, 校验生成的 rt 调用 */
static const char* k_map_prog =
    "{\"format\": \"cwind-typed-ast\", \"version\": 1,"
    " \"symbols\": [{\"name\": \"main\", \"kind\": \"fn\", \"ref\": 2}],"
    " \"bindings\": [],"
    " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {}, \"items\": ["
    "   {\"kind\": \"FnDecl\", \"id\": 2, \"ann\": {}, \"name\": \"main\","
    "    \"params\": [],"
    "    \"return_type\": {\"kind\": \"Type\", \"id\": 3, \"ann\": {},"
    "                     \"name\": \"Int\", \"args\": []},"
    "    \"body\": {\"kind\": \"Block\", \"id\": 4, \"ann\": {}, \"stmts\": ["
    "      {\"kind\": \"LetStmt\", \"id\": 5,"
    "       \"ann\": {\"type\": {\"name\": \"Map\", \"args\": ["
    "               {\"name\": \"String\"}, {\"name\": \"Int\"}]}},"
    "       \"name\": \"m\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 6, \"ann\": {},"
    "                 \"name\": \"Map\", \"args\": ["
    "                   {\"kind\": \"Type\", \"id\": 7, \"ann\": {},"
    "                    \"name\": \"String\", \"args\": []},"
    "                   {\"kind\": \"Type\", \"id\": 8, \"ann\": {},"
    "                    \"name\": \"Int\", \"args\": []}]},"
    "       \"value\": {\"kind\": \"MapLit\", \"id\": 9,"
    "        \"ann\": {\"type\": {\"name\": \"Map\", \"args\": ["
    "                {\"name\": \"String\"}, {\"name\": \"Int\"}]}},"
    "        \"entries\": ["
    "          {\"kind\": \"MapEntry\", \"id\": 10,"
    "           \"ann\": {\"key_type\": {\"name\": \"String\"},"
    "                    \"value_type\": {\"name\": \"Int\"}},"
    "           \"key\": {\"kind\": \"StrLit\", \"id\": 11,"
    "                    \"ann\": {\"type\": {\"name\": \"String\"}},"
    "                    \"value\": \"a\", \"raw\": \"\\\"a\\\"\"},"
    "           \"value\": {\"kind\": \"IntLit\", \"id\": 12,"
    "                      \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                      \"value\": 1, \"raw\": \"1\"}}"
    "        ]}},"
    "      {\"kind\": \"ExprStmt\", \"id\": 13, \"ann\": {},"
    "       \"expr\": {\"kind\": \"Assign\", \"id\": 14, \"ann\": {},"
    "        \"op\": \"=\","
    "        \"target\": {\"kind\": \"Index\", \"id\": 15,"
    "         \"ann\": {\"type\": {\"name\": \"Int\"},"
    "                  \"container_type\": {\"name\": \"Map\"},"
    "                  \"index_type\": {\"name\": \"String\"}},"
    "         \"obj\": {\"kind\": \"Name\", \"id\": 16,"
    "                  \"ann\": {\"binding\": {\"kind\": \"var\", \"ref\": 5},"
    "                          \"type\": {\"name\": \"Map\"}},"
    "                  \"parts\": [\"m\"]},"
    "         \"index\": {\"kind\": \"StrLit\", \"id\": 17,"
    "                    \"ann\": {\"type\": {\"name\": \"String\"}},"
    "                    \"value\": \"b\", \"raw\": \"\\\"b\\\"\"}},"
    "        \"value\": {\"kind\": \"IntLit\", \"id\": 18,"
    "                   \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                   \"value\": 2, \"raw\": \"2\"}}},"
    "      {\"kind\": \"LetStmt\", \"id\": 19,"
    "       \"ann\": {\"type\": {\"name\": \"Int\"}}, \"name\": \"x\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 20, \"ann\": {},"
    "                 \"name\": \"Int\", \"args\": []},"
    "       \"value\": {\"kind\": \"Index\", \"id\": 21,"
    "        \"ann\": {\"type\": {\"name\": \"Int\"},"
    "                \"container_type\": {\"name\": \"Map\"},"
    "                \"index_type\": {\"name\": \"String\"}},"
    "        \"obj\": {\"kind\": \"Name\", \"id\": 22,"
    "                  \"ann\": {\"binding\": {\"kind\": \"var\", \"ref\": 5},"
    "                          \"type\": {\"name\": \"Map\"}},"
    "                  \"parts\": [\"m\"]},"
    "        \"index\": {\"kind\": \"StrLit\", \"id\": 23,"
    "                   \"ann\": {\"type\": {\"name\": \"String\"}},"
    "                   \"value\": \"a\", \"raw\": \"\\\"a\\\"\"}}},"
    "      {\"kind\": \"ExprStmt\", \"id\": 24, \"ann\": {},"
    "       \"expr\": {\"kind\": \"Call\", \"id\": 25,"
    "        \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                            \"callee_ref\": \"print\"},"
    "                \"type\": {\"name\": \"None\"}},"
    "        \"callee\": {\"kind\": \"Name\", \"id\": 26,"
    "                    \"ann\": {\"binding\": {\"kind\": \"builtin\","
    "                                            \"ref\": \"print\"},"
    "                            \"type\": {\"name\": \"Fn\"}},"
    "                    \"parts\": [\"builtins\", \"print\"]},"
    "        \"args\": [{\"kind\": \"Arg\", \"id\": 27, \"ann\": {},"
    "          \"value\": {\"kind\": \"Call\", \"id\": 28,"
    "           \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                               \"callee_ref\": \"contains\"},"
    "                   \"type\": {\"name\": \"Bool\"}},"
    "           \"callee\": {\"kind\": \"Attribute\", \"id\": 29,"
    "                       \"ann\": {\"member\": {\"kind\": \"builtin\","
    "                                               \"ref\": \"contains\"},"
    "                               \"type\": {\"name\": \"Bool\"}},"
    "                       \"obj\": {\"kind\": \"Name\", \"id\": 30,"
    "                                \"ann\": {\"binding\": {\"kind\": \"var\","
    "                                                        \"ref\": 5},"
    "                                        \"type\": {\"name\": \"Map\"}},"
    "                                \"parts\": [\"m\"]},"
    "                       \"name\": \"contains\"},"
    "           \"args\": [{\"kind\": \"Arg\", \"id\": 31, \"ann\": {},"
    "             \"value\": {\"kind\": \"StrLit\", \"id\": 32,"
    "                        \"ann\": {\"type\": {\"name\": \"String\"}},"
    "                        \"value\": \"b\", \"raw\": \"\\\"b\\\"\"}}]}}]}},"
    "      {\"kind\": \"ReturnStmt\", \"id\": 33, \"ann\": {},"
    "       \"value\": {\"kind\": \"Name\", \"id\": 34,"
    "                  \"ann\": {\"binding\": {\"kind\": \"var\", \"ref\": 19},"
    "                          \"type\": {\"name\": \"Int\"}},"
    "                  \"parts\": [\"x\"]}}"
    "    ]}}"
    " ]}}";

/* String 拼接: BinOp + 与复合赋值 += 各生成一次 cw_builtin_concat */
static const char* k_str_prog =
    "{\"format\": \"cwind-typed-ast\", \"version\": 1,"
    " \"symbols\": [{\"name\": \"main\", \"kind\": \"fn\", \"ref\": 2}],"
    " \"bindings\": [],"
    " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {}, \"items\": ["
    "   {\"kind\": \"FnDecl\", \"id\": 2, \"ann\": {}, \"name\": \"main\","
    "    \"params\": [],"
    "    \"return_type\": {\"kind\": \"Type\", \"id\": 3, \"ann\": {},"
    "                     \"name\": \"Int\", \"args\": []},"
    "    \"body\": {\"kind\": \"Block\", \"id\": 4, \"ann\": {}, \"stmts\": ["
    "      {\"kind\": \"LetStmt\", \"id\": 5,"
    "       \"ann\": {\"type\": {\"name\": \"String\"}}, \"name\": \"s\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 6, \"ann\": {},"
    "                 \"name\": \"String\", \"args\": []},"
    "       \"value\": {\"kind\": \"BinOp\", \"id\": 7,"
    "        \"ann\": {\"type\": {\"name\": \"String\"},"
    "                \"left_type\": {\"name\": \"String\"},"
    "                \"right_type\": {\"name\": \"String\"}},"
    "        \"left\": {\"kind\": \"StrLit\", \"id\": 8,"
    "                  \"ann\": {\"type\": {\"name\": \"String\"}},"
    "                  \"value\": \"a\", \"raw\": \"\\\"a\\\"\"},"
    "        \"op\": \"+\","
    "        \"right\": {\"kind\": \"StrLit\", \"id\": 9,"
    "                   \"ann\": {\"type\": {\"name\": \"String\"}},"
    "                   \"value\": \"b\", \"raw\": \"\\\"b\\\"\"}}},"
    "      {\"kind\": \"LetStmt\", \"id\": 10,"
    "       \"ann\": {\"type\": {\"name\": \"String\"}}, \"name\": \"t\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 11, \"ann\": {},"
    "                 \"name\": \"String\", \"args\": []},"
    "       \"value\": {\"kind\": \"StrLit\", \"id\": 12,"
    "                  \"ann\": {\"type\": {\"name\": \"String\"}},"
    "                  \"value\": \"x\", \"raw\": \"\\\"x\\\"\"}},"
    "      {\"kind\": \"ExprStmt\", \"id\": 13, \"ann\": {},"
    "       \"expr\": {\"kind\": \"Assign\", \"id\": 14, \"ann\": {},"
    "        \"op\": \"+=\","
    "        \"target\": {\"kind\": \"Name\", \"id\": 15,"
    "                    \"ann\": {\"binding\": {\"kind\": \"var\","
    "                                            \"ref\": 10},"
    "                            \"type\": {\"name\": \"String\"}},"
    "                    \"parts\": [\"t\"]},"
    "        \"value\": {\"kind\": \"StrLit\", \"id\": 16,"
    "                   \"ann\": {\"type\": {\"name\": \"String\"}},"
    "                   \"value\": \"y\", \"raw\": \"\\\"y\\\"\"}}},"
    "      {\"kind\": \"ReturnStmt\", \"id\": 17, \"ann\": {},"
    "       \"value\": {\"kind\": \"IntLit\", \"id\": 18,"
    "                  \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                  \"value\": 1, \"raw\": \"1\"}}"
    "    ]}}"
    " ]}}";

/* 模块级 builtin: type_of / readline / exit */
static const char* k_builtin_prog =
    "{\"format\": \"cwind-typed-ast\", \"version\": 1,"
    " \"symbols\": [{\"name\": \"main\", \"kind\": \"fn\", \"ref\": 2}],"
    " \"bindings\": [],"
    " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {}, \"items\": ["
    "   {\"kind\": \"FnDecl\", \"id\": 2, \"ann\": {}, \"name\": \"main\","
    "    \"params\": [],"
    "    \"return_type\": {\"kind\": \"Type\", \"id\": 3, \"ann\": {},"
    "                     \"name\": \"Int\", \"args\": []},"
    "    \"body\": {\"kind\": \"Block\", \"id\": 4, \"ann\": {}, \"stmts\": ["
    "      {\"kind\": \"ExprStmt\", \"id\": 5, \"ann\": {},"
    "       \"expr\": {\"kind\": \"Call\", \"id\": 6,"
    "        \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                            \"callee_ref\": \"print\"},"
    "                \"type\": {\"name\": \"None\"}},"
    "        \"callee\": {\"kind\": \"Name\", \"id\": 7,"
    "                    \"ann\": {\"binding\": {\"kind\": \"builtin\","
    "                                            \"ref\": \"print\"},"
    "                            \"type\": {\"name\": \"Fn\"}},"
    "                    \"parts\": [\"builtins\", \"print\"]},"
    "        \"args\": [{\"kind\": \"Arg\", \"id\": 8, \"ann\": {},"
    "          \"value\": {\"kind\": \"Call\", \"id\": 9,"
    "           \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                               \"callee_ref\": \"type_of\"},"
    "                   \"type\": {\"name\": \"Any\"}},"
    "           \"callee\": {\"kind\": \"Name\", \"id\": 10,"
    "                       \"ann\": {\"binding\": {\"kind\": \"builtin\","
    "                                               \"ref\": \"type_of\"},"
    "                               \"type\": {\"name\": \"Fn\"}},"
    "                       \"parts\": [\"builtins\", \"type_of\"]},"
    "           \"args\": [{\"kind\": \"Arg\", \"id\": 11, \"ann\": {},"
    "             \"value\": {\"kind\": \"IntLit\", \"id\": 12,"
    "                        \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                        \"value\": 1, \"raw\": \"1\"}}]}}]}},"
    "      {\"kind\": \"LetStmt\", \"id\": 13,"
    "       \"ann\": {\"type\": {\"name\": \"String\"}},"
    "       \"name\": \"line\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 14, \"ann\": {},"
    "                 \"name\": \"String\", \"args\": []},"
    "       \"value\": {\"kind\": \"Call\", \"id\": 15,"
    "        \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                            \"callee_ref\": \"readline\"},"
    "                \"type\": {\"name\": \"String\"}},"
    "        \"callee\": {\"kind\": \"Name\", \"id\": 16,"
    "                    \"ann\": {\"binding\": {\"kind\": \"builtin\","
    "                                            \"ref\": \"readline\"},"
    "                            \"type\": {\"name\": \"Fn\"}},"
    "                    \"parts\": [\"builtins\", \"readline\"]},"
    "        \"args\": []}},"
    "      {\"kind\": \"ExprStmt\", \"id\": 17, \"ann\": {},"
    "       \"expr\": {\"kind\": \"Call\", \"id\": 18,"
    "        \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                            \"callee_ref\": \"print\"},"
    "                \"type\": {\"name\": \"None\"}},"
    "        \"callee\": {\"kind\": \"Name\", \"id\": 19,"
    "                    \"ann\": {\"binding\": {\"kind\": \"builtin\","
    "                                            \"ref\": \"print\"},"
    "                            \"type\": {\"name\": \"Fn\"}},"
    "                    \"parts\": [\"builtins\", \"print\"]},"
    "        \"args\": [{\"kind\": \"Arg\", \"id\": 20, \"ann\": {},"
    "          \"value\": {\"kind\": \"Name\", \"id\": 21,"
    "                     \"ann\": {\"binding\": {\"kind\": \"var\","
    "                                             \"ref\": 13},"
    "                             \"type\": {\"name\": \"String\"}},"
    "                     \"parts\": [\"line\"]}}]}},"
    "      {\"kind\": \"ExprStmt\", \"id\": 22, \"ann\": {},"
    "       \"expr\": {\"kind\": \"Call\", \"id\": 23,"
    "        \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                            \"callee_ref\": \"exit\"},"
    "                \"type\": {\"name\": \"None\"}},"
    "        \"callee\": {\"kind\": \"Name\", \"id\": 24,"
    "                    \"ann\": {\"binding\": {\"kind\": \"builtin\","
    "                                            \"ref\": \"exit\"},"
    "                            \"type\": {\"name\": \"Fn\"}},"
    "                    \"parts\": [\"builtins\", \"exit\"]},"
    "        \"args\": [{\"kind\": \"Arg\", \"id\": 25, \"ann\": {},"
    "          \"value\": {\"kind\": \"IntLit\", \"id\": 26,"
    "                     \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                     \"value\": 3, \"raw\": \"3\"}}]}},"
    "      {\"kind\": \"ReturnStmt\", \"id\": 27, \"ann\": {},"
    "       \"value\": {\"kind\": \"IntLit\", \"id\": 28,"
    "                  \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                  \"value\": 1, \"raw\": \"1\"}}"
    "    ]}}"
    " ]}}";

/* Vector 剩余方法: extend_with / insert_at / index_of / remove_at */
static const char* k_vecm_prog =
    "{\"format\": \"cwind-typed-ast\", \"version\": 1,"
    " \"symbols\": [{\"name\": \"main\", \"kind\": \"fn\", \"ref\": 2}],"
    " \"bindings\": [],"
    " \"ast\": {\"kind\": \"Program\", \"id\": 1, \"ann\": {}, \"items\": ["
    "   {\"kind\": \"FnDecl\", \"id\": 2, \"ann\": {}, \"name\": \"main\","
    "    \"params\": [],"
    "    \"return_type\": {\"kind\": \"Type\", \"id\": 3, \"ann\": {},"
    "                     \"name\": \"Int\", \"args\": []},"
    "    \"body\": {\"kind\": \"Block\", \"id\": 4, \"ann\": {}, \"stmts\": ["
    "      {\"kind\": \"LetStmt\", \"id\": 5,"
    "       \"ann\": {\"type\": {\"name\": \"Vector\", \"args\": ["
    "               {\"name\": \"Int\"}]}},"
    "       \"name\": \"a\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 6, \"ann\": {},"
    "                 \"name\": \"Vector\", \"args\": ["
    "                   {\"kind\": \"Type\", \"id\": 7, \"ann\": {},"
    "                    \"name\": \"Int\", \"args\": []}]},"
    "       \"value\": {\"kind\": \"VectorLit\", \"id\": 8,"
    "        \"ann\": {\"type\": {\"name\": \"Vector\", \"args\": ["
    "                {\"name\": \"Int\"}]}},"
    "        \"elems\": ["
    "          {\"kind\": \"IntLit\", \"id\": 9,"
    "           \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "           \"value\": 1, \"raw\": \"1\"},"
    "          {\"kind\": \"IntLit\", \"id\": 10,"
    "           \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "           \"value\": 2, \"raw\": \"2\"}]}},"
    "      {\"kind\": \"LetStmt\", \"id\": 11,"
    "       \"ann\": {\"type\": {\"name\": \"Vector\", \"args\": ["
    "               {\"name\": \"Int\"}]}},"
    "       \"name\": \"b\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 12, \"ann\": {},"
    "                 \"name\": \"Vector\", \"args\": ["
    "                   {\"kind\": \"Type\", \"id\": 13, \"ann\": {},"
    "                    \"name\": \"Int\", \"args\": []}]},"
    "       \"value\": {\"kind\": \"VectorLit\", \"id\": 14,"
    "        \"ann\": {\"type\": {\"name\": \"Vector\", \"args\": ["
    "                {\"name\": \"Int\"}]}},"
    "        \"elems\": ["
    "          {\"kind\": \"IntLit\", \"id\": 15,"
    "           \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "           \"value\": 3, \"raw\": \"3\"}]}},"
    "      {\"kind\": \"ExprStmt\", \"id\": 16, \"ann\": {},"
    "       \"expr\": {\"kind\": \"Call\", \"id\": 17,"
    "        \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                            \"callee_ref\": \"extend_with\"},"
    "                \"type\": {\"name\": \"None\"}},"
    "        \"callee\": {\"kind\": \"Attribute\", \"id\": 18,"
    "                    \"ann\": {\"member\": {\"kind\": \"builtin\","
    "                                            \"ref\": \"extend_with\"}},"
    "                    \"obj\": {\"kind\": \"Name\", \"id\": 19,"
    "                              \"ann\": {\"binding\": {\"kind\": \"var\","
    "                                                      \"ref\": 5},"
    "                                      \"type\": {\"name\": \"Vector\"}},"
    "                              \"parts\": [\"a\"]},"
    "                    \"name\": \"extend_with\"},"
    "        \"args\": [{\"kind\": \"Arg\", \"id\": 20, \"ann\": {},"
    "          \"value\": {\"kind\": \"Name\", \"id\": 21,"
    "                     \"ann\": {\"binding\": {\"kind\": \"var\","
    "                                             \"ref\": 11},"
    "                             \"type\": {\"name\": \"Vector\"}},"
    "                     \"parts\": [\"b\"]}}]}},"
    "      {\"kind\": \"ExprStmt\", \"id\": 22, \"ann\": {},"
    "       \"expr\": {\"kind\": \"Call\", \"id\": 23,"
    "        \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                            \"callee_ref\": \"insert_at\"},"
    "                \"type\": {\"name\": \"None\"}},"
    "        \"callee\": {\"kind\": \"Attribute\", \"id\": 24,"
    "                    \"ann\": {\"member\": {\"kind\": \"builtin\","
    "                                            \"ref\": \"insert_at\"}},"
    "                    \"obj\": {\"kind\": \"Name\", \"id\": 25,"
    "                              \"ann\": {\"binding\": {\"kind\": \"var\","
    "                                                      \"ref\": 5},"
    "                                      \"type\": {\"name\": \"Vector\"}},"
    "                              \"parts\": [\"a\"]},"
    "                    \"name\": \"insert_at\"},"
    "        \"args\": ["
    "          {\"kind\": \"Arg\", \"id\": 26, \"ann\": {},"
    "           \"value\": {\"kind\": \"IntLit\", \"id\": 27,"
    "                      \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                      \"value\": 1, \"raw\": \"1\"}},"
    "          {\"kind\": \"Arg\", \"id\": 28, \"ann\": {},"
    "           \"value\": {\"kind\": \"IntLit\", \"id\": 29,"
    "                      \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                      \"value\": 9, \"raw\": \"9\"}}]}},"
    "      {\"kind\": \"LetStmt\", \"id\": 30,"
    "       \"ann\": {\"type\": {\"name\": \"UInt\"}}, \"name\": \"i\","
    "       \"type\": {\"kind\": \"Type\", \"id\": 31, \"ann\": {},"
    "                 \"name\": \"UInt\", \"args\": []},"
    "       \"value\": {\"kind\": \"Call\", \"id\": 32,"
    "        \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                            \"callee_ref\": \"index_of\"},"
    "                \"type\": {\"name\": \"UInt\"}},"
    "        \"callee\": {\"kind\": \"Attribute\", \"id\": 33,"
    "                    \"ann\": {\"member\": {\"kind\": \"builtin\","
    "                                            \"ref\": \"index_of\"}},"
    "                    \"obj\": {\"kind\": \"Name\", \"id\": 34,"
    "                              \"ann\": {\"binding\": {\"kind\": \"var\","
    "                                                      \"ref\": 5},"
    "                                      \"type\": {\"name\": \"Vector\"}},"
    "                              \"parts\": [\"a\"]},"
    "                    \"name\": \"index_of\"},"
    "        \"args\": [{\"kind\": \"Arg\", \"id\": 35, \"ann\": {},"
    "          \"value\": {\"kind\": \"IntLit\", \"id\": 36,"
    "                     \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                     \"value\": 9, \"raw\": \"9\"}}]}},"
    "      {\"kind\": \"ExprStmt\", \"id\": 37, \"ann\": {},"
    "       \"expr\": {\"kind\": \"Call\", \"id\": 38,"
    "        \"ann\": {\"call\": {\"callee_kind\": \"builtin\","
    "                            \"callee_ref\": \"remove_at\"},"
    "                \"type\": {\"name\": \"None\"}},"
    "        \"callee\": {\"kind\": \"Attribute\", \"id\": 39,"
    "                    \"ann\": {\"member\": {\"kind\": \"builtin\","
    "                                            \"ref\": \"remove_at\"}},"
    "                    \"obj\": {\"kind\": \"Name\", \"id\": 40,"
    "                              \"ann\": {\"binding\": {\"kind\": \"var\","
    "                                                      \"ref\": 5},"
    "                                      \"type\": {\"name\": \"Vector\"}},"
    "                              \"parts\": [\"a\"]},"
    "                    \"name\": \"remove_at\"},"
    "        \"args\": [{\"kind\": \"Arg\", \"id\": 41, \"ann\": {},"
    "          \"value\": {\"kind\": \"IntLit\", \"id\": 42,"
    "                     \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                     \"value\": 0, \"raw\": \"0\"}}]}},"
    "      {\"kind\": \"ReturnStmt\", \"id\": 43, \"ann\": {},"
    "       \"value\": {\"kind\": \"IntLit\", \"id\": 44,"
    "                  \"ann\": {\"type\": {\"name\": \"Int\"}},"
    "                  \"value\": 1, \"raw\": \"1\"}}"
    "    ]}}"
    " ]}}";

static int count_substr(const char* haystack, const char* needle) {
    int n = 0;
    const size_t nl = strlen(needle);
    while ((haystack = strstr(haystack, needle)) != NULL) {
        n++;
        haystack += nl;
    }
    return n;
}

static void test_map_codegen(void) {
    CwModule_t* m = cwmodule_load_string(k_map_prog, strlen(k_map_prog));
    T("map: module loads", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        return;
    }
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);
    T("map: build symbols", cwsym_build_from_module(&syms, m));

    CwLlvm_t ll;
    T("map: llvm init", cwllvm_init(&ll, "map", &types, &layouts, &syms));
    T("map: declare symbols", cwllvm_declare_symbols(&ll));

    CwCodegen_t cg;
    T("map: codegen init", cwcodegen_init(&cg, &ll, m));
    T("map: emit", cwcodegen_emit(&cg));
    if (cg.failed) {
        printf("  codegen error: %s\n", cwcodegen_error(&cg));
    }
    char* ir = cwllvm_dump(&ll);
    T("map: dump ok", ir != NULL);
    if (ir) {
        T("IR: map init call", strstr(ir, "call i1 @cwmap_init(") != NULL);
        T("IR: map put call", strstr(ir, "call i1 @cwmap_put(") != NULL);
        T("IR: map get call", strstr(ir, "call i1 @cwmap_get(") != NULL);
        T("IR: map contains call",
          strstr(ir, "call i1 @cw_builtin_contains(") != NULL);
        LLVMDisposeMessage(ir);
    }

    cwcodegen_destroy(&cg);
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
}

static void test_str_codegen(void) {
    CwModule_t* m = cwmodule_load_string(k_str_prog, strlen(k_str_prog));
    T("str: module loads", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        return;
    }
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);
    T("str: build symbols", cwsym_build_from_module(&syms, m));

    CwLlvm_t ll;
    T("str: llvm init", cwllvm_init(&ll, "str", &types, &layouts, &syms));
    T("str: declare symbols", cwllvm_declare_symbols(&ll));

    CwCodegen_t cg;
    T("str: codegen init", cwcodegen_init(&cg, &ll, m));
    T("str: emit", cwcodegen_emit(&cg));
    if (cg.failed) {
        printf("  codegen error: %s\n", cwcodegen_error(&cg));
    }
    char* ir = cwllvm_dump(&ll);
    T("str: dump ok", ir != NULL);
    if (ir) {
        T("IR: concat called for + and +=",
          count_substr(ir, "call i1 @cw_builtin_concat(") == 2);
        LLVMDisposeMessage(ir);
    }

    cwcodegen_destroy(&cg);
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
}

static void test_builtin_codegen(void) {
    CwModule_t* m = cwmodule_load_string(k_builtin_prog,
                                         strlen(k_builtin_prog));
    T("builtin: module loads", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        return;
    }
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);
    T("builtin: build symbols", cwsym_build_from_module(&syms, m));

    CwLlvm_t ll;
    T("builtin: llvm init", cwllvm_init(&ll, "builtin", &types, &layouts,
                                        &syms));
    T("builtin: declare symbols", cwllvm_declare_symbols(&ll));

    CwCodegen_t cg;
    T("builtin: codegen init", cwcodegen_init(&cg, &ll, m));
    T("builtin: emit", cwcodegen_emit(&cg));
    if (cg.failed) {
        printf("  codegen error: %s\n", cwcodegen_error(&cg));
    }
    char* ir = cwllvm_dump(&ll);
    T("builtin: dump ok", ir != NULL);
    if (ir) {
        T("IR: type_of call",
          strstr(ir, "call i1 @cw_builtin_type_of_owned(") != NULL);
        T("IR: readline call",
          strstr(ir, "call i1 @cw_builtin_readline(") != NULL);
        T("IR: exit call",
          strstr(ir, "call void @cw_builtin_exit(") != NULL);
        LLVMDisposeMessage(ir);
    }

    cwcodegen_destroy(&cg);
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
}

static void test_vecm_codegen(void) {
    CwModule_t* m = cwmodule_load_string(k_vecm_prog, strlen(k_vecm_prog));
    T("vecm: module loads", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        return;
    }
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);
    T("vecm: build symbols", cwsym_build_from_module(&syms, m));

    CwLlvm_t ll;
    T("vecm: llvm init", cwllvm_init(&ll, "vecm", &types, &layouts, &syms));
    T("vecm: declare symbols", cwllvm_declare_symbols(&ll));

    CwCodegen_t cg;
    T("vecm: codegen init", cwcodegen_init(&cg, &ll, m));
    T("vecm: emit", cwcodegen_emit(&cg));
    if (cg.failed) {
        printf("  codegen error: %s\n", cwcodegen_error(&cg));
    }
    char* ir = cwllvm_dump(&ll);
    T("vecm: dump ok", ir != NULL);
    if (ir) {
        T("IR: extend_with call",
          strstr(ir, "call i1 @cwvec_extend_with(") != NULL);
        T("IR: insert_at call",
          strstr(ir, "call i1 @cwvec_insert_at(") != NULL);
        T("IR: index_of call",
          strstr(ir, "call i1 @cwvec_index_of(") != NULL);
        T("IR: remove_at call",
          strstr(ir, "call i1 @cwvec_remove_at(") != NULL);
        LLVMDisposeMessage(ir);
    }

    cwcodegen_destroy(&cg);
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
}

/* 用户 struct: 构造/字段读写/实例方法/静态方法/结构体参数与返回 (读 fixture) */
static void test_struct_codegen(void) {
#ifndef CWIND_FIXTURE_DIR
    printf("  [SKIP] struct: 无 CWIND_FIXTURE_DIR\n");
    return;
#endif
    char path[1024];
    snprintf(path, sizeof(path), CWIND_FIXTURE_DIR "/codegen_structs.json");
    CwModule_t* m = cwmodule_load_file(path);
    T("struct: module loads", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        return;
    }
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);
    T("struct: build symbols", cwsym_build_from_module(&syms, m));

    CwLlvm_t ll;
    T("struct: llvm init", cwllvm_init(&ll, "struct", &types, &layouts,
                                       &syms));
    T("struct: declare symbols", cwllvm_declare_symbols(&ll));

    CwCodegen_t cg;
    T("struct: codegen init", cwcodegen_init(&cg, &ll, m));
    T("struct: emit", cwcodegen_emit(&cg));
    if (cg.failed) {
        printf("  codegen error: %s\n", cwcodegen_error(&cg));
    }
    char* ir = cwllvm_dump(&ll);
    T("struct: dump ok", ir != NULL);
    if (ir) {
        T("IR: instance method defined",
          strstr(ir, "define %cw.handle @cwind.method.Point.sum(") != NULL);
        T("IR: static method defined",
          strstr(ir, "define %cw.handle @cwind.method.Point.new(") != NULL);
        T("IR: struct param fn defined",
          strstr(ir, "define %cw.handle @cwind.fn.double(") != NULL);
        T("IR: struct return global",
          strstr(ir, "@fnret.cwind.method.Point.new = global [76 x i8]")
              != NULL);
        T("IR: instance method call",
          strstr(ir, "call %cw.handle @cwind.method.Point.sum(") != NULL);
        T("IR: static method call",
          strstr(ir, "call %cw.handle @cwind.method.Point.new(") != NULL);
        T("IR: struct param call",
          strstr(ir, "call %cw.handle @cwind.fn.double(") != NULL);
        T("IR: deep copy memcpy",
          strstr(ir, "call void @llvm.memcpy.p0.p0.i64(") != NULL);
        T("IR: field payload rebase", strstr(ir, "%f.pay") != NULL);
        LLVMDisposeMessage(ir);
    }

    cwcodegen_destroy(&cg);
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
}

/* 泛型函数实例化: 同一模板按调用点 type_args 生成单态实例 (读 fixture) */
static void test_generic_codegen(void) {
#ifndef CWIND_FIXTURE_DIR
    printf("  [SKIP] generic: 无 CWIND_FIXTURE_DIR\n");
    return;
#endif
    char path[1024];
    snprintf(path, sizeof(path), CWIND_FIXTURE_DIR "/codegen_generics.json");
    CwModule_t* m = cwmodule_load_file(path);
    T("generic: module loads", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        return;
    }
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);
    T("generic: build symbols", cwsym_build_from_module(&syms, m));

    CwLlvm_t ll;
    T("generic: llvm init", cwllvm_init(&ll, "generic", &types, &layouts,
                                        &syms));
    T("generic: declare symbols", cwllvm_declare_symbols(&ll));

    CwCodegen_t cg;
    T("generic: codegen init", cwcodegen_init(&cg, &ll, m));
    T("generic: emit", cwcodegen_emit(&cg));
    if (cg.failed) {
        printf("  codegen error: %s\n", cwcodegen_error(&cg));
    }
    char* ir = cwllvm_dump(&ll);
    T("generic: dump ok", ir != NULL);
    if (ir) {
        T("IR: Int instance",
          strstr(ir, "define %cw.handle @cwind.fn.id.Int(") != NULL);
        T("IR: String instance",
          strstr(ir, "define %cw.handle @cwind.fn.id.String(") != NULL);
        T("IR: Vector instance",
          strstr(ir, "define %cw.handle @cwind.fn.id.Vector.Int(") != NULL);
        T("IR: first instance",
          strstr(ir, "define %cw.handle @cwind.fn.first.Int(") != NULL);
        T("IR: instance call sites",
          count_substr(ir, "call %cw.handle @cwind.fn.id.Int(") >= 3);
        T("IR: template body not emitted",
          strstr(ir, "define %cw.handle @cwind.fn.id(") == NULL);
        LLVMDisposeMessage(ir);
    }

    cwcodegen_destroy(&cg);
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
}

/* 泛型 struct 方法实例化: owner params + 方法自身 params (读 fixture) */
static void test_genmethod_codegen(void) {
#ifndef CWIND_FIXTURE_DIR
    printf("  [SKIP] genmethod: 无 CWIND_FIXTURE_DIR\n");
    return;
#endif
    char path[1024];
    snprintf(path, sizeof(path),
             CWIND_FIXTURE_DIR "/codegen_genmethods.json");
    CwModule_t* m = cwmodule_load_file(path);
    T("genmethod: module loads", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        return;
    }
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);
    T("genmethod: build symbols", cwsym_build_from_module(&syms, m));

    CwLlvm_t ll;
    T("genmethod: llvm init", cwllvm_init(&ll, "genmethod", &types, &layouts,
                                          &syms));
    T("genmethod: declare symbols", cwllvm_declare_symbols(&ll));

    CwCodegen_t cg;
    T("genmethod: codegen init", cwcodegen_init(&cg, &ll, m));
    T("genmethod: emit", cwcodegen_emit(&cg));
    if (cg.failed) {
        printf("  codegen error: %s\n", cwcodegen_error(&cg));
    }
    char* ir = cwllvm_dump(&ll);
    T("genmethod: dump ok", ir != NULL);
    if (ir) {
        T("IR: Int instance get_x",
          strstr(ir, "define %cw.handle @cwind.method.Point.Int.get_x(")
              != NULL);
        T("IR: Int instance make",
          strstr(ir, "define %cw.handle @cwind.method.Point.Int.make(")
              != NULL);
        T("IR: String instance get_x",
          strstr(ir, "define %cw.handle @cwind.method.Point.String.get_x(")
              != NULL);
        T("IR: owner+method params pick",
          strstr(ir, "define %cw.handle @cwind.method.Point.Int.Int.pick(")
              != NULL);
        T("IR: generic method calls",
          count_substr(ir, "call %cw.handle @cwind.method.Point.Int.get_x(")
              >= 1);
        T("IR: no template body emitted",
          strstr(ir, "define %cw.handle @cwind.method.Point.get_x(") == NULL);
        LLVMDisposeMessage(ir);
    }

    cwcodegen_destroy(&cg);
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
}

/* 静态构造 Vector::new/Map::new/Set::new + Set 方法/遍历 (读 fixture) */
static void test_newset_codegen(void) {
#ifndef CWIND_FIXTURE_DIR
    printf("  [SKIP] newset: 无 CWIND_FIXTURE_DIR\n");
    return;
#endif
    char path[1024];
    snprintf(path, sizeof(path), CWIND_FIXTURE_DIR "/codegen_newset.json");
    CwModule_t* m = cwmodule_load_file(path);
    T("newset: module loads", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        return;
    }
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);
    T("newset: build symbols", cwsym_build_from_module(&syms, m));

    CwLlvm_t ll;
    T("newset: llvm init", cwllvm_init(&ll, "newset", &types, &layouts,
                                       &syms));
    T("newset: declare symbols", cwllvm_declare_symbols(&ll));

    CwCodegen_t cg;
    T("newset: codegen init", cwcodegen_init(&cg, &ll, m));
    T("newset: emit", cwcodegen_emit(&cg));
    if (cg.failed) {
        printf("  codegen error: %s\n", cwcodegen_error(&cg));
    }
    char* ir = cwllvm_dump(&ll);
    T("newset: dump ok", ir != NULL);
    if (ir) {
        T("IR: Vector::new init",
          strstr(ir, "call i1 @cwvec_init(") != NULL);
        T("IR: Map::new init",
          strstr(ir, "call i1 @cwmap_init(") != NULL);
        T("IR: Set::new init",
          strstr(ir, "call i1 @cwset_init(") != NULL);
        T("IR: Set clear",
          strstr(ir, "call void @cwset_clear(") != NULL);
        T("IR: Set iterate begin",
          strstr(ir, "call void @cwset_iter_begin(") != NULL);
        T("IR: Set iterate valid",
          strstr(ir, "call i1 @cwset_iter_valid(") != NULL);
        T("IR: Set iterate item",
          strstr(ir, "call i1 @cwset_iter_item(") != NULL);
        T("IR: Set iterate next",
          strstr(ir, "call void @cwset_iter_next(") != NULL);
        LLVMDisposeMessage(ir);
    }

    cwcodegen_destroy(&cg);
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
}

/* 新数值宽度 + 混合数值运算/比较 (读 fixture) */
static void test_numeric_codegen(void) {
#ifndef CWIND_FIXTURE_DIR
    printf("  [SKIP] numeric: 无 CWIND_FIXTURE_DIR\n");
    return;
#endif
    char path[1024];
    snprintf(path, sizeof(path), CWIND_FIXTURE_DIR "/codegen_numeric.json");
    CwModule_t* m = cwmodule_load_file(path);
    T("numeric: module loads", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        return;
    }
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);
    T("numeric: build symbols", cwsym_build_from_module(&syms, m));

    CwLlvm_t ll;
    T("numeric: llvm init", cwllvm_init(&ll, "numeric", &types, &layouts,
                                        &syms));
    T("numeric: declare symbols", cwllvm_declare_symbols(&ll));

    CwCodegen_t cg;
    T("numeric: codegen init", cwcodegen_init(&cg, &ll, m));
    T("numeric: emit", cwcodegen_emit(&cg));
    if (cg.failed) {
        printf("  codegen error: %s\n", cwcodegen_error(&cg));
    }
    char* ir = cwllvm_dump(&ll);
    T("numeric: dump ok", ir != NULL);
    if (ir) {
        T("IR: fib defined",
          strstr(ir, "define %cw.handle @cwind.fn.fib(") != NULL);
        T("IR: int->float promote",
          strstr(ir, "sitofp") != NULL);
        T("IR: float widen",
          strstr(ir, "fpext") != NULL);
        T("IR: float compare",
          strstr(ir, "fcmp") != NULL);
        T("IR: wide literal trunc",
          strstr(ir, "trunc i64") != NULL);
        T("IR: i64 arithmetic",
          strstr(ir, "add i64") != NULL);
        LLVMDisposeMessage(ir);
    }

    cwcodegen_destroy(&cg);
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
}

/* todo-58: Int16/UInt16 端到端 (读 fixture) */
static void test_todo58_codegen(void) {
#ifndef CWIND_FIXTURE_DIR
    printf("  [SKIP] todo58: 无 CWIND_FIXTURE_DIR\n");
    return;
#endif
    char path[1024];
    snprintf(path, sizeof(path), CWIND_FIXTURE_DIR "/codegen_todo58.json");
    CwModule_t* m = cwmodule_load_file(path);
    T("todo58: module loads", m != NULL);
    if (!m) {
        printf("  error: %s\n", cwmodule_error());
        return;
    }
    CwTypeTable_t types;
    CwLayoutCache_t layouts;
    CwSymTable_t syms;
    cwtype_table_init(&types);
    cwlayout_cache_init(&layouts, &types);
    cwsym_table_init(&syms);
    T("todo58: build symbols", cwsym_build_from_module(&syms, m));

    CwLlvm_t ll;
    T("todo58: llvm init", cwllvm_init(&ll, "todo58", &types, &layouts,
                                        &syms));
    T("todo58: declare symbols", cwllvm_declare_symbols(&ll));

    CwCodegen_t cg;
    T("todo58: codegen init", cwcodegen_init(&cg, &ll, m));
    T("todo58: emit", cwcodegen_emit(&cg));
    if (cg.failed) {
        printf("  codegen error: %s\n", cwcodegen_error(&cg));
    }
    char* ir = cwllvm_dump(&ll);
    T("todo58: dump ok", ir != NULL);
    if (ir) {
        T("IR: i16 arithmetic",
          strstr(ir, "add i16") != NULL);
        T("IR: i32 promoted arithmetic",
          strstr(ir, "add i32") != NULL);
        T("IR: sext Int16 -> Int32",
          strstr(ir, "sext i16") != NULL);
        T("IR: zext UInt16 -> Int32",
          strstr(ir, "zext i16") != NULL);
        LLVMDisposeMessage(ir);
    }

    cwcodegen_destroy(&cg);
    cwllvm_destroy(&ll);
    cwsym_table_destroy(&syms);
    cwlayout_cache_destroy(&layouts);
    cwtype_table_destroy(&types);
    cwmodule_free(m);
}

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
          strstr(ir, "define i32 @main(i32") != NULL);
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

    test_map_codegen();
    test_str_codegen();
    test_builtin_codegen();
    test_vecm_codegen();
    test_struct_codegen();
    test_generic_codegen();
    test_genmethod_codegen();
    test_newset_codegen();
    test_numeric_codegen();
    test_todo58_codegen();

    printf("\n%d passed, %d failed\n", pass, fail);
    return fail ? 1 : 0;
}
