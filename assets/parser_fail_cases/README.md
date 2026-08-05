# Parser 负例集 (词法通过 / Parser 必须报错)

这 9 个文件每个只包含一处结构错误, 且只使用词法合法的记号 (关键字 / 标识符 / 十进制字面量 / 运算符 / 字符串 / 注释).
用途: 回归测试 Parser 的报错位置与报错信息.

| 文件                              | 错误                           | 期望报错位置 | 期望报错信息 (建议)                         | 备注                             |
|-----------------------------------|--------------------------------|--------------|---------------------------------------------|----------------------------------|
| case01_missing_operand.wind       | `1 + ;` 缺少右操作数           | `;`          | unexpected token ';' in expression          | 不要把错误指到 `let`             |
| case02_mismatched_brackets.wind   | `(1 + 2]` 括号不匹配           | `]`          | expected ')' after parenthesized expression |                                  |
| case03_empty_param_list.wind      | `fn bad( -> Int` 缺参数名      | `->`         | expected parameter name                     |                                  |
| case04_empty_while_condition.wind | `while ()` 条件为空            | `)`          | unexpected token ')' in expression          | 需包在函数内, 否则先报顶层 while |
| case05_stray_else.wind            | `else` 脱离 if                 | `else`       | unexpected token 'else' in function body    | 需包在函数内                     |
| case06_missing_initializer.wind   | `const data: Map = ;` 缺初始化 | `;`          | expected expression after '='               |                                  |
| case07_missing_semicolon.wind     | `let c: Int = 3` 缺分号        | `3` 之后     | expected ';' after let declaration          | 不要把错误指到下一行 `let d`     |
| case08_forin_missing_var.wind     | `for in arr` 缺迭代变量        | `in`         | expected iteration variable before 'in'     | 不要说 expected 'in'             |
| case09_extra_brace.wind           | 顶层多余 `}`                   | `}`          | unexpected token '}' at top level           |                                  |

说明:
- 所有文件词法层必须通过 (只含十进制字面量、合法字符串与注释).
- Parser 对每个文件都必须报错且退出码非 0.
- "期望报错信息" 为建议措辞, 语义与位置正确即可, 不必逐字一致.
