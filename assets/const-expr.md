# CWind const-expr 限制面

const-fn / const-type / const 常量的当前能力边界 (能做什么、不能做什么、
能返回什么)。以 `mvp/frontend/src/cwind_frontend/{sa/const_check.py, comptime/}`
的现行实现与 `mvp/frontend/tests/cases/const/` 的实测用例为准。

## 1. 声明面

| 形式                      | 可声明位置                                                                                                          | 约束                                                  |
|---------------------------|---------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------|
| `const fn f(...) -> T {}` | 任意 fn 声明处: 顶层、inline `mod`、`extra` 块固有方法、std 库、`extern "CWind"` 内建面 (`const fn String::length`) | 返回类型必须是 const type (见 §2)                     |
| `const type X;`           | **仅 std** 的 `extern "CWind"` 面                                                                                   | 用户文件里写 → 报错 (`const_type_nonstd_rejected`)    |
| `const NAME: T = expr;`   | 顶层常量、关联常量 (`S::CONST`)                                                                                     | `T` 必须是 const type; 初始化式必须编译期可得 (见 §3) |

- `const fn` 的调用**只在 const 初始化式里**于编译期求值; 普通函数体里对
  `const fn` 的调用就是一次普通运行期调用。
- 求值结果烧回 AST 后与普通字面量/构造式无异 (fold、借用检查、后端照常)。

## 2. 什么类型可以是 const type (const 值类型 / const fn 返回类型)

| 类别                                            | 例                                                                                                 | 支持状态                                                                       |
|-------------------------------------------------|----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------|
| std `extern "CWind"` 里标了 `const type` 的内建 | `String` `Tuple` `None` `Int8..Int64` `UInt8..UInt64` `Int` `UInt` `Float` `Float64` `Byte` `Bool` | ✓                                                                             |
| 结构体 (任意用户 struct)                        | `struct P {...}`                                                                                   | ✓ 结构性放行                                                                  |
| 枚举 (含带载荷)                                 | `enum Option<T>`、用户枚举                                                                         | ✓ 结构性放行                                                                  |
| 定长数组                                        | `[i32; 3]`                                                                                         | ✓ 元素类型本身须 const type                                                   |
| 引用 / 裸指针 / fn 签名                         | `&T` `*const T` `fn(i32) -> i32`                                                                   | ✓ 类型合法; 但初始化式禁止借用 (§3), 实际少用                                 |
| `extern "CWind"` 里**没标** `const type` 的类型 | 未标记的内建                                                                                       | ✗ `type 'X' is not a const type`                                              |
| 容器                                            | `Vector<T>` `Map<K,V>` `Set<T>`                                                                    | ✗ 无标记 (堆分配, 禁入 const 值/返回位); `container_vector` / `container_map` |
| 拼错的名字 / 泛型参数裸名                       | `Typo` `T`                                                                                         | 本检查放行, 由常规类型诊断处理                                                 |

- `const fn` 的**局部变量**不受此表限制: 堆容器可以作局部
  (`const_fn_heap_local_ok`: `let s: String = ...`), 只有**返回位**禁止。
- 判定来自声明表 (`analyzer.const_types`), 不按名字特判。

## 3. const 初始化式 — 能算什么、不能算什么

| 表达式                                   | 支持状态 | 备注                                                                                                                                                          |
|------------------------------------------|----------|---------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 字面量 (整/浮/字符串/布尔)               | ✓       |                                                                                                                                                               |
| 四则、位运算、比较                       | ✓       | C 语义; `inline_scalar` / `inline_folded`                                                                                                                     |
| cast (`x as T`)                          | ✓       | C 截断/转换语义                                                                                                                                               |
| 读取其他 const (含关联常量)              | ✓       | 前向链 pass 2 + 内联后折; `forward_ref` `inline_assoc`                                                                                                        |
| `const fn` 调用                          | ✓       | 编译期求值烧录 (§6); `const_fn_decl_ok`                                                                                                                       |
| 枚举变体构造 (无/带载荷)                 | ✓       | `unit_variant_ok` `variant_payload_ok`                                                                                                                        |
| struct 字面量 (**位置式** `P { 1, 2 }`)  | ✓       | `inline_composite`; 不支持 `P { x: 1 }` 冒号写法                                                                                                              |
| 定长数组字面量 `[a, b, c]`               | ✓       |                                                                                                                                                               |
| 字符串拼接                               | ✓       | `string_concat_ok`                                                                                                                                            |
| 方法调用 (含 std 内建方法、`extra` 方法) | ✓       | `method_call_initializer` (`"abc".contains("b")`); 接收者可以是结构体字面量                                                                                   |
| `None`、fn 指针值                        | ✓       |                                                                                                                                                               |
| `/` 与 `%`                               | ⚠       | **不参与常量折叠** — 保留在产物里运行期计算 (Python 与后端截断语义分歧); `inline_div_mod`。作 **const fn 实参**时必须**整除尽**, 否则报 `must divide exactly` |
| 非 const fn 的调用                       | ✗       | `Calls in a const initializer must target a const fn`                                                                                                         |
| 借用 `&` / 解引用 `*` / 闭包             | ✗       | `borrow_initializer` `deref_initializer` `closure_initializer`                                                                                                |
| 赋值、`static` 字段读、extern static 读  | ✗       | `static_field_initializer` `extern_static_initializer`                                                                                                        |
| 循环依赖的 const 链                      | ✗       | `cyclic_initializer`                                                                                                                                          |
| 自引用/递归 const                        | ✗       | `_check_const_cycles` 整环检测                                                                                                                                |

## 4. const fn 体内 — 能做什么、不能做什么

| 状态 | 项目                         | 说明                                                                                                                   |
|------|------------------------------|------------------------------------------------------------------------------------------------------------------------|
| ✓   | 完整语句面                   | 控制流、循环、match、局部变量、泛型实例化 — 按普通 fn 全量 SA 检查                                                     |
| ✓   | 递归                         | 求值时编译成原生代码执行 (例: `fib(42)` 实测), 深度受原生栈限制                                                        |
| ✓   | 调用其他函数                 | std 与用户函数按依赖闭包拉入求值单元; 内建方法 (`s.length()`) 走 extern 面                                             |
| ✓   | 局部堆容器                   | `String`/`Vector` 等仅限局部, 不得出返回位 (§2)                                                                        |
| ✗   | 返回非 const type            | `const fn bad() -> Vector<Int>` → `return type ... is not a const type` (`const_fn_return_rejected`)                   |
| ✗   | 依赖另一个**待求值**的 const | 求值单元编译期间再次触发求值 → 递归守卫 `CWIND_CONSTFN_BUILDING` 报错 (外层程序不受影响: 内联按序先解掉被依赖的 const) |
| ⚠   | 副作用                       | 求值是**真实原生执行** — body 里的 print/extern 调用会在编译期发生; 目前未设禁                                         |

## 5. 边界跨界表 — 实参能传什么、返回能返回什么

求值单元经 `#[export]` 包装函数 + ctypes 过 C 边界

白名单 = SA 的 `_c_abi_violation`
(与 extern 同一套), 外加 const-fn 专属 precheck。

### 实参 (含方法接收者)

| 类型                                    | 支持状态 | 说明                                                                         |
|-----------------------------------------|----------|------------------------------------------------------------------------------|
| 数值/布尔标量                           | ✓       | `Int`/`UInt` 为 16 位; 其余定宽 (Int8..64/UInt8..64/Float/Float64/Byte/Bool) |
| `String`                                | ✓       | `char*` 双向 (UTF-8, 不含 NUL)                                               |
| 非泛型纯内联 struct 按值                | ✓       | 字段=定长标量/定长数组/内联嵌套; 全标量 ≤16B 或含数组/嵌套时无大小限制       |
| 定长数组 `[T; N]`                       | ✓       | 形参位按 C 退化为元素指针; 元素须标量或非泛型结构体                          |
| 单态化枚举 (含载荷)                     | ✓       | `Option<Int>` 等按 `{int32 tag; payload}`; `Option<String>` 可               |
| 裸指针 `*const/*mut T` (非泛型)         | ✓       | 不透明句柄直传; 泛型被指类型 ✗                                              |
| 引用 `&T` / `&mut T`                    | ✓       | 指针降级 (常量初始化式里造不出借用, 主要经方法接收者出现)                    |
| `void` (无值)                           | ✗       | `void cannot be a const-fn argument`                                         |
| `fn(...)` 函数指针                      | ✗       | precheck 显式拒: `function-pointer arguments cannot cross`                   |
| `Option` (参数位)                       | ✗       | 仅返回位的 nullable 约定可用                                                 |
| `Vector` / `Map` / `Set` / 任何泛型实例 | ✗       | 无稳定 C 布局 (`generic instances ... are not mappable`)                     |

### 返回值 (能返回什么)

| 类型                                   | 支持状态   | 说明                                                                                                                        |
|----------------------------------------|------------|-----------------------------------------------------------------------------------------------------------------------------|
| 数值/布尔标量                          | ✓         | 同上宽度; 烧录为字面量                                                                                                      |
| `String`                               | ✓         | 烧录为字符串字面量                                                                                                          |
| `None` (void)                          | ✓         | 烧录为 `None`                                                                                                               |
| 非泛型纯内联 struct                    | ✓         | 烧录为 struct 构造式 (`const_fn_struct_ok`, 单字母类型名如 `P` 亦可)                                                        |
| 单态化枚举 (无/带载荷)                 | ✓ 同 Rust | 烧录为变体构造; 载荷类型须自身可跨界; `const_fn_option_ok`                                                                  |
| `Option<String>` / `Option<指针/引用>` | ✓         | nullable 指针约定 (NULL=`None`)                                                                                             |
| 定长数组 `[T; N]`                      | ✓         | 以合成结构体 `__cw_const_arr` 携带跨界, 解码回数组字面量; 元素须标量/非泛型结构体; `const_fn_array_ok` (前端→后端→运行实测) |
| `Vector` / `Map` / `Set`               | ✗         | 堆容器, 非 const type 且无 C 布局                                                                                           |
| 泛型 struct 实例 (`Foo<Int>`)          | ✗         | 后端聚合分类未做单态替换 (todo-131/139); 枚举是唯一做了单态替换的聚合                                                       |
| 裸指针 / 引用返回                      | ✗         | 即便 FFI 放行, 烧录阶段拒: `pointer results cannot be burned into a const initializer`                                      |
| 未标记的 extern 内建                   | ✗         | 见 §2                                                                                                                       |

## 6. 求值机制 (何时、如何、缓存)

| 事项     | 现状                                                                                                                                              |
|----------|---------------------------------------------------------------------------------------------------------------------------------------------------|
| 时机     | inline pass: const 读取已克隆成纯字面量实参后 → 现场求值; 结果烧回 AST, 随后 fold 收尾                                                            |
| 单元内容 | 被调 const fn + 传递依赖闭包 + `#[export] fn cw_const_eval`; facility 依赖按消费**精确 `use`** (非通配); `const fn`/`const type` 标记在单元内剥离 |
| 执行     | 单元编译为 no-std share DLL, ctypes 调用 — 非 VM、真原生                                                                                          |
| 构建缓存 | `%TEMP%\cwind-constfn\<key>\unit.dll`, key = 单元源码哈希 (`UNIT_VERSION` 内嵌)                                                                   |
| 结果缓存 | 同目录 `results.json`, key = (单元 key, 实参); 命中连 DLL 都不加载; `CWIND_CONSTFN_NO_RESULT_CACHE=1` 旁路                                        |
| 嵌套守卫 | 子编译带 `CWIND_CONSTFN_BUILDING`, 单元内再触发求值 → 报错而非递归                                                                                |
| 失败诊断 | 子编译完整多行诊断逐行嵌入调用点 span (与过程宏同排版); 求值失败的调用点保留 → 编译失败                                                           |
| 工作区   | 项目 `<project>/target/constfn/`, 无锚单文件 `%TEMP%\cwind-constfn\work\`                                                                         |

## 7. 相关用例索引

`mvp/frontend/tests/cases/const/` — 每行表格的实测出处:
`const_fn_{decl,call,struct,array,option,heap_local}_ok`、
`const_fn_return_rejected`、`const_type_nonstd_rejected`、
`container_{vector,map,nested}`、`inline_scalar` `inline_folded`
`inline_fold_nested` `inline_composite` `inline_div_mod` `inline_module_const`、
`unit_variant_ok` `variant_payload_ok` `string_concat_ok`
`method_call_initializer` `borrow/deref/closure/static_field/extern_static/assign_*`、
`cyclic_initializer` `forward_ref` `inline_assoc`。
