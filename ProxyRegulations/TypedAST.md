# CWind Typed-AST

本文描述编译器前端最终输出的一种形态: 在现有 `--parse --json` 的 AST 上为每个节点
附加 `id` 与 `ann`(信息层), 并在顶层提供 `symbols` 与 `bindings` 两个索引表。

状态: 本文已被前端实现, `cwindf --typed-ast file.wind` 的输出即本规范描述的内容。
格式、字段名仍是实现细节, 可以随需求调整, 但调整时本文与实现应同步修改。

## 0. 相对初稿的修正

初稿 (`TypedAST.md` 的早期版本) 存在几处歧义与不一致, 本文已修正:

1. **节点形状统一**。初稿说"原有字段与 `--parse --json` 完全一致, 只多 `id` 与
   `ann`", 但引用规则又要求 `binding.ref`、`member.member_ref` 等新字段。现在所有
   引用信息一律放进 `ann`, 节点序列化形状固定为
   `kind / id / line / column / 原有字段 / ann`; `--parse --json` 保持字节级不变。
2. **`bindings` id 是独立编号空间**。初稿的 AST id 从 1 递增, 而 bindings 示例又出现
   `id: 101`, 两者关系未定义。现在明确规定: `bindings[*].id` 从 1 独立编号, 与 AST
   id 互不冲突; 引用方通过 `kind` / `callee_kind` 判断 id 属于哪个命名空间。
3. **部分未知的类型不整体丢弃**。初稿规定"类型确定不了时 `type` 为 `null`"; 这会把
   `Vector<T>` 这类"外层已知、叶子未知"的类型整体抹掉。现在: 整体未知时
   `ann.type = null` 且出现 `opaque: true`; 只有叶子依赖泛型时保留外层结构, 叶子用
   `{"name": "T", "opaque": true}` 表示。
4. **`ForStmt` 的迭代变量类型改名为 `var_type`**。避免与 AST 节点自身的 `type` 字段
   (for 循环的类型标注) 混淆。
5. **补齐节点覆盖**。初稿只列了少量表达式/语句, 未覆盖 `Type`、`Param`、`Field`、
   `FnDecl`、`ConstDecl`、`TypeDecl`、`MapEntry` 等实际节点; 本文补齐, 并明确"未列出
   的节点 ann 为空"。
6. **明确伪类型**。函数引用返回 `{"name": "Fn"}`, 值存在但具体类型未知的情况返回
   `{"name": "Any"}`; 二者不是可声明的用户类型。
7. **id 顺序精确化**。"按源码顺序"在 `ForStmt` 等节点上不可判定(其 `type` 字段存于
   body 之后), 现定义为按 `--parse --json` 的字段序列化顺序先父后子分配, 保证确定性
   并与 JSON 布局一致。

## 1. 整体结构

```json
{
  "format": "cwind-typed-ast",
  "version": 1,
  "symbols": [
    { "name": "Point", "kind": "struct", "ref": 2 }
  ],
  "bindings": [
    { "id": 1, "decl_id": 8, "owner": "Point", "trait": null, "fn_id": 12 }
  ],
  "ast": {}
}
```

- `symbols`: 顶层符号。`kind` 取值 `const` / `type` / `struct` / `enum` / `trait` /
  `fn` / `group`; `ref` 指向该符号声明节点的 AST id。`impl`、`extra`、`group` 应用
  不是符号, 不出现在此表中。
- `bindings`: `impl` / `extra` 提供的方法绑定。`id` 是独立于 AST id 的编号(从 1
  递增); 表行按源码声明顺序排列(不按 `owner` 分组), 因此 `id` 与 `decl_id` 单调
  递增; `decl_id` 指向 `ImplDecl` / `ExtraDecl` 节点 id; `owner` 是被扩展的 struct
  名; `trait` 为所属 trait 名, `extra` 的方法为 `null`; `fn_id` 指向方法声明
  `FnDecl` 节点 id。
- `ast`: 带 `id` / `ann` 的完整 AST。

## 2. 节点

每个 AST 节点统一序列化为:

```json
{
  "kind": "Call",
  "id": 23,
  "line": 6,
  "column": 12,
  "...原有字段": {},
  "ann": {}
}
```

- `id`: 同一文档内唯一, 从 1 递增, 按 `--parse --json` 的字段顺序先父后子分配。
- 除 `id` / `ann` 外的字段与 `--parse --json` 的输出完全一致。
- `ann`: 信息层; 没有任何信息时为空对象 `{}`。

## 3. 类型表示

所有类型字段统一为结构化对象:

```json
{ "name": "Vector", "args": [ { "name": "Int" } ] }
```

- 无泛型实参时不出现 `args`。
- 类型整体无法确定(如检查失败、错误恢复)时, `ann.type` 为 `null`, 并在同一 `ann`
  中出现 `"opaque": true`。
- 只有部分叶子依赖泛型参数时, 保留外层结构, 叶子标记 opaque:
  `{ "name": "Vector", "args": [ { "name": "T", "opaque": true } ] }`。
- 内置泛型类型(`Vector` / `Map` / `Set`)的实参未知时, 以 `{"name": "Any"}` 占位,
  不会输出无实参的裸泛型名(`{"name": "Vector"}` 只可能表示实参未知的
  `Vector<Any>`)。
- 类型别名一律展开为底层类型(`Email` 展开为 `{"name": "String"}`)。
- 伪类型: `Fn` 表示对函数/方法的引用; `Any` 表示"值存在但具体类型未知"(如
  `builtins::type_of` 的返回值, 或元素类型无法推断的字面量占位)。二者不可在源码中
  声明。

## 4. ann 字段

### 通用

| 字段     | 说明                                              |
|----------|---------------------------------------------------|
| `type`   | 表达式或声明的解析后类型(结构化类型对象或 `null`) |
| `opaque` | 仅当 `type` 为 `null` 时出现, 值为 `true`         |

其余类型字段(`left_type`、`init_type` 等)只在已知时出现; 未知时省略该键。

### 表达式

| 节点                                         | ann 字段                                          |
|----------------------------------------------|---------------------------------------------------|
| `Name`                                       | `type`、`binding`                                 |
| `Attribute`                                  | `type`、`member`                                  |
| `Call`                                       | `type`、`call`                                    |
| `Index`                                      | `type`、`container_type`、`index_type`            |
| `Slice`                                      | `type`、`container_type`                          |
| `BinOp`                                      | `type`、`left_type`、`right_type`                 |
| `UnaryOp`                                    | `type`、`operand_type`                            |
| `Assign`                                     | `type`、`target_type`、`value_type`               |
| `IntLit` / `FloatLit` / `BoolLit` / `StrLit` | `type`                                            |
| `VectorLit`                                  | `type`、`element_type`                            |
| `MapLit`                                     | `type`                                            |
| `MapEntry`                                   | `key_type`、`value_type`                          |
| `StructConstruct`                            | `type`、`field_types`(按字段声明顺序, 泛型替换后) |

### 语句与声明

| 节点         | ann 字段                                                                               |
|--------------|----------------------------------------------------------------------------------------|
| `LetStmt`    | `type`、`init_type`                                                                    |
| `ForStmt`    | `var_type`(迭代变量类型)、`iterable_type`                                              |
| `ReturnStmt` | `type`、`expected_return`                                                              |
| `ConstDecl`  | `type`、`folded_value`(仅常量折叠成功时)                                               |
| `TypeDecl`   | `type`(展开后的底层类型)                                                               |
| `Field`      | `type`(泛型替换后; 依赖泛型时叶子 opaque)                                              |
| `Param`      | `type`(泛型替换后; 依赖泛型时叶子 opaque)                                              |
| `FnDecl`     | `type`(返回类型; 所属类型已知时 `Self` 已替换, trait 声明等无 owner 的场合保留 `Self`) |
| `Type`       | `type`(展开后的类型)                                                                   |

`Param` 的 `self` 同样遵循该规则: 方法体内指向所属类型, 无 owner 的声明中为
`{"name": "Self"}`。

未列出的节点(`Program`、`Block`、`TypeParam`、`Variant`、`StructDecl`、
`EnumDecl`、`TraitDecl`、`ImplDecl`、`ExtraDecl`、`Distribution`、`GroupDecl`、
`GroupApply`、`BreakStmt`、`ContinueStmt`、`ElifBranch`、`IfStmt`、`WhileStmt`、
`ExprStmt`、`Arg`、`ErrorStmt`)的 `ann` 留空; 其子节点已携带相应信息。

## 5. 引用规则

所有引用都带 `kind` 字段, 消费方按 `kind` 判断 `ref` 的命名空间:

| `kind`                                       | `ref` 含义                 |
|----------------------------------------------|----------------------------|
| `var` / `fn` / `const` / `field` / `variant` | AST 节点 id                |
| `method`                                     | `bindings` 表 id           |
| `builtin`                                    | 内置对象/函数/方法名字符串 |

各引用字段:

- `Name.ann.binding`: `{ "kind": ..., "ref": ... }`。单段名解析为局部变量(指向
  `LetStmt` / `Param` / `ForStmt` 节点)、函数(`FnDecl` 节点)、常量(`ConstDecl`
  节点)或内置对象(名字符串); 双段路径可解析为静态字段(`Field` 节点)、方法
  (`bindings` id)、枚举变体(`Variant` 节点)或 `builtins::` 函数(名字符串)。
  校验块(`where { ... }`)中的 `self` 没有对应声明节点, 只注释 `type`, 不产生
  `binding`; 字段校验中引用字段名时指向对应 `Field` 节点。
- `Attribute.ann.member`: `{ "kind": ..., "ref": ... }`。字段指向 `Field` 节点 id;
  方法指向 `bindings` 表 id; 内置方法用名字符串。
- `Call.ann.call`: `{ "callee_kind": ..., "callee_ref": ..., "type_args"?: ... }`。
  `callee_kind` 为 `fn` / `method` / `builtin`; `callee_ref` 分别指向 `FnDecl` 节点
  id、`bindings` 表 id 或内置名字符串。`type_args` 仅在调用点推导出泛型实参时出现:
  `{ 泛型参数名: 类型对象 }`。

## 8. 已知限制与边界行为

- `call.type_args` 只覆盖用户函数/方法的调用。内置方法的泛型参数名不是语言可见的
  名称, 数据文件也未完整定义(如 `Map` 有两个参数但只有 `generic = "value"`), 因此
  内置方法调用不输出 `type_args`; 接收者类型本身已携带全部泛型实参。
- `Map` 下标取键返回 `Map<K, V>` 的 `V`(即 `SameAsGeneric:2`), 与 `Map::get` 的
  返回规则一致。
- 泛型上下文中无法解析的字段访问(如 `Point<T>` 的 `p.x`): `ann.member` 仍指向
  `Field` 节点, 但 `ann.type` 为 `null` 且 `opaque` 为 `true`, 不会静默丢弃成员信息。
- 无函数体的声明(`trait` 方法等): `Param` / `FnDecl` 仍会注释, `Self` 保留原样;
  有函数体时会按所属类型重新解析并覆盖(如 `extra<T> Point<T>` 中 `self` /
  `-> Self` 为 `Point<T>` 且 `T` 标记 opaque)。
- 常量折叠覆盖整型与浮点表达式: 整型目标做宽度与整数性校验, `Float` 目标做 f32
  范围校验, 且整数值必须是 f32 可精确表示的(如 `16777216 + 1` 会被拒绝, 因为 f32
  无法表示 16777217; `0.1` 这类非整数字面量不做精确表示校验)。

## 6. 输出方式

`cwindf --typed-ast file.wind` 输出上述 JSON(总是 JSON, `--json` 不改变输出)。
失败时行为与 `--sa` 一致: 词法/语法/语义任一阶段产生错误都停止后续阶段, 所有诊断
渲染到 stderr, 退出码为 1。

## 7. 示例

```cwind
struct Point<T> { x: T, y: T }
extra<T> Point<T> { fn new(x: T, y: T) -> Self { return Point { x, y }; } }
fn main(p: Point<Int>) -> Int {
    let q: Point<Int> = Point::new(p.x, 1);
    return q.y;
}
```

`cwindf --typed-ast` 输出(节选):

```json
{
  "format": "cwind-typed-ast",
  "version": 1,
  "symbols": [
    { "name": "Point", "kind": "struct", "ref": 2 },
    { "name": "main", "kind": "fn", "ref": 24 }
  ],
  "bindings": [
    { "id": 1, "decl_id": 8, "owner": "Point", "trait": null, "fn_id": 12 }
  ],
  "ast": {
    "kind": "Program",
    "line": 1,
    "column": 1,
    "id": 1,
    "ann": {},
    "items": [
      {
        "kind": "StructDecl",
        "line": 1,
        "column": 1,
        "id": 2,
        "ann": {},
        "name": "Point",
        "params": [
          {
            "kind": "TypeParam",
            "line": 1,
            "column": 14,
            "id": 3,
            "ann": {},
            "name": "T",
            "bound": null
          }
        ],
        "fields": [
          {
            "kind": "Field",
            "line": 1,
            "column": 19,
            "id": 4,
            "ann": {
              "type": { "name": "T", "opaque": true }
            },
            "name": "x",
            "type": {
              "kind": "Type",
              "line": 1,
              "column": 22,
              "id": 5,
              "ann": { "type": { "name": "T", "opaque": true } },
              "name": "T",
              "args": []
            },
            "pub": false,
            "static": false,
            "validation": null,
            "initializer": null
          }
        ]
      },
      {
        "kind": "Call",
        "line": 4,
        "column": 25,
        "id": 33,
        "ann": {
          "call": {
            "callee_kind": "method",
            "callee_ref": 1,
            "type_args": { "T": { "name": "Int" } }
          },
          "type": { "name": "Point" }
        },
        "callee": {
          "kind": "Name",
          "line": 4,
          "column": 25,
          "id": 34,
          "ann": {
            "binding": { "kind": "method", "ref": 1 }
          },
          "parts": [ "Point", "new" ]
        },
        "args": [
          {
            "kind": "Arg",
            "line": 4,
            "column": 36,
            "id": 35,
            "ann": {},
            "value": {
              "kind": "Attribute",
              "line": 4,
              "column": 36,
              "id": 36,
              "ann": {
                "member": { "kind": "field", "ref": 4 },
                "type": { "name": "Int" }
              },
              "obj": {
                "kind": "Name",
                "line": 4,
                "column": 36,
                "id": 37,
                "ann": {
                  "binding": { "kind": "var", "ref": 25 },
                  "type": {
                    "name": "Point",
                    "args": [ { "name": "Int" } ]
                  }
                },
                "parts": [ "p" ]
              },
              "name": "x"
            },
            "unpack": false
          }
        ]
      }
    ]
  }
}
```
