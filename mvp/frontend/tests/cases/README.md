# `cases/` — 数据驱动的前端测试用例

这里存放前端（词法 / 语法 / 语义）的**数据驱动**用例。约定：

- **单文件区**：`cases/<area>/<name>.wind` 是源码，可选同名 `<name>.json` 是预期
  （缺省即"应当干净通过"）。预期 schema 见 `../harness.py` 顶部（`kind / count /
  errors / forbid_errors / positions / warnings / warnings_exact / stage`）。
- **项目树区**：`cases/<area>/<case>/` 是一棵完整小工程（`libs/` 模块 + `main.wind`
  入口，或 `Breeze.toml` + `src/`），`<case>/expect.json` 是预期，可用 `entry` 键指定
  相对 case 根的入口路径。

**执行者**：`../test_cases.py` 自动发现上面两类并逐个跑。所以**加一个 case 只要往对应
区里丢数据文件，不写任何 Python**；加一个"新数据区"才需要在 `test_cases.py` 的两张清单
（`SINGLE_FILE_AREAS` / `PROJECT_TREE_AREAS`）里登记一行。

**不走通用发现**的区（各自有 bespoke 测试文件，断言 typed-AST / token 结构，`json` 是
另一种 schema）：`sa`(`test_sa.py`)、`parser`(`test_parser.py`)、`lexer`(`test_lexer.py`)、
`cffi`(`test_cffi.py`)、`cli`、`render_err`、`builtin_methods`、`cfg`(`test_todo86_93.py`)、
`hex`(`test_todo85.py`)、`common`(共享夹具)、`todo38/46/47/58/60`、`bug24/29/30/31/43`、
`todo103_106`。另有几个区既被通用发现跑（校验管线结果）、又留一个 bespoke 文件加结构断言，
见下文标注。

下面逐区记录**为什么存在**（原每区一个 driver 脚本里的 docstring，现集中于此）。

---

## 单文件区

### bug33 — 数值 `as` 转型目标是类型别名
`u32`（来自 `std::prelude` 或本地 `typedef`）会展开成数值内置类型，`0 as u32` 必须放行，
且 typed AST 要带上展开后的目标类型（`UInt32`），后端 `cg_expr_cast` 才能选对宽度。别名指向
非数值（String）时仍要报错。

### bug34 — `extra` 体内 `Self` 绑定到所属类型
`extra User` 里不带 `return` 的尾表达式（`let mut u: Self = Self { ... }; u`）曾报
"Return type mismatch: expected User, got Self"：声明类型仍是原始字符串 `Self`，而函数返回
类型已解析成所属类型。`Self` 形参与局部声明的类型都要绑定到所属类型。

### bug35 — 定长内联数组字段破坏 `extra` 解析（`[x; N]` 重复字面量）
复现把 `Self { [0 as UInt32; 624], ... }` 放进 extra 方法。三个缺陷叠加：①
`_brace_is_struct_construct` 把 `[0; 624]` 里的 `;` 当成语句分隔符，导致 `Self {` 根本不被
识别为结构体构造；② `[x; N]` 重复字面量不可解析（只有逗号形式）；③ SA + 后端无重复处理，
且数组类型内的元素别名（`u32`）不展开。

### bug37 — 同文件无名 `extern "C"` 块的函数可裸名调用
`_build_module_table` 从没把 `ExternBlock` 成员登记进其所在文件的裸名可见集
（`_declaration_name` 对无名块返回 `None`），于是同文件里 `atexit(clean)` 被误判
"belongs to another module and is not visible here"。附带：std prelude 导出面对每个模块文件
可见（Rust 语义），导入模块可用 prelude 别名（`u32`/`i32`）而不撞同一道门。

### bug38 — 泛型坍缩后的实参类型要检查
原始报告（"SA 没有检查泛型坍缩后的类型与传入值是否匹配"）其实是旧工具的假阳性：当前 SA 对
`Box<String>` 接收者上期待 `T` 的方法已报 `argument 1 of 'set' must be String, got Int`。
本用例钉住该检查，防止静默回归。

### bug39 — 具体接收者类型上的未知方法必须报错
`a.unwrap_of("")` 曾以 opaque 类型静默通过 SA。泛型 opaque 接收者（裸 `T`、trait bound 方法
调用）仍被容忍，因为方法可能在实例化后才存在。

### bug40 — `extern "C"` 块内允许 `pub fn` / `pub static` 成员
以前解析器要求可见性只能待在块级，成员上写 `pub` 会死于 "expected function name"。成员可见性
是块级 `pub` 与成员自身 `pub` 的或，并参与模块导出面（成员 pub 时 `use m::member;` 可解析）。

### todo17 — 数值 `as` 转型
`expr as T` 走后端既有标量强制语义：int→int 按目标宽度截断/符号扩展（二进制补码），float→int
向零截断，int↔float 数值转换。优先级遵循 Rust：一元比 `as` 紧，`as` 比算术和比较紧。常量上下文
按一致语义折叠转型。非数值操作数/目标以各自诊断拒绝。

### todo74 — `!` 取反运算符
两层契约：① 变量上的布尔逻辑非（以前只在字面量上验过）：let 初始化、重新赋值、while 条件、
match 守卫、字段/下标操作数、调用实参、`!!x` 链；② Rust 风格整数按位非（todo-74 扩展）：
`!int_expr` 保留操作数宽度，`!5: Int` 是 `-6`，`!0: UInt` 是全宽掩码。浮点/字符串/容器以一条
精确诊断拒绝。

### todo87 — extern 函数的变参 `...`
只有 extern 块可声明尾随 `...`；前面至少一个固定参数，调用实参须 ≥ 固定参数个数，多出的实参
无固定参数检查（C 变参语义）。后端把调用映射到 LLVM 变参 C 函数：窄整数提升到 i32，Bool→i32，
Float→double，Int64/UInt64 原样，String/裸指针按地址传。

### todo108 — 指向枚举的裸指针跨 FFI 边界
`*mut E` / `*const E` 按 C opaque 句柄语义（`MyEnum*` / `void*`）：指针值在形参位与返回位都按
地址透传，无内容转换或回写。同型指针保留既有 `==`/`!=` 地址比较。

### todo120 — FFI 返回 `*const S` / `*mut S` 结构体指针
extern 可返回结构体指针，SA 接受把它解引用成可用的 const CWind 对象（后端做 C 布局 → CWind
blob 转换）。这里只确认源码解析并通过 SA；完整 C 布局往返由 `pipeline_cffi_strptr_deref` CTest
夹具断言。`struct_ptr_return_ok.wind` 即原 `test_todo120.py` 里那段手写内联源转成的数据用例。

### todo122 — `extra` 块内的关联常量
`extra Point { const MAX: Int32 = 99; }` 声明作用域属于所属类型的常量，读作 `Point::MAX`
（方法内 `Self::MAX`）。赋值（普通或复合）像顶层 const 一样被拒绝，取值也按 const 一样做类型 /
范围检查与精化。

---

## 项目树区（`<case>/expect.json`，由 `test_cases.py` 跑）

### bug32 — std 内部通配导入 + 经重组 libc 包装模块的显式 item 导入
修的三个都在解析器导入系统：① 模块*内部* `use m::*;` 无法把其成员喂给依赖闭包（别名索引按
use 的末段键，通配时末段是 `*`）；② 无名 `pub extern "C"` 块被通配模块选择跳过，迁进独立 libc
包装模块的绑定从不进入编译/导出面（"Unknown function"）；③ `_merge_auto_prelude` 把显式导入的
展平当成入口本地声明，一旦某导入的依赖闭包恰好拉来同名就静默剥掉 prelude 的 `pub use` 重导出
（"belongs to another module"）。

### bug42 — 模块限定的泛型 trait 实现
`impl mod::Trait<T> for Type` 曾死于解析器（"expected 'for' in impl declaration"），因为限定的
类型名不能带泛型实参。SA 侧现把限定 trait 路径经 `use` 别名表归一到展平后的裸 trait 名（同 todo-81
对 `module::Enum::Variant` 的处理）。

### todo13 / bug-44 — 结构体字段 `pub` 可见性
解析器已接受字段上的 `pub`，SA 做跨模块访问门控（todo-90 `_check_field_visibility`）。这些树用例
钉住整个表面：`pub` 字段跨模块可读、可写、可位置构造；私有字段拒绝读、写、位置构造，以及从另一
模块的 `extra` 块访问，每处恰好一条 "is private" 诊断；同模块代码自由访问私有字段（Rust 模块语义），
`pub` 构造器桥接模块。

### todo119 — `crate`/`super`/`self` 导入头 + `pub(std)`
`use crate::a::b;` 锚定模块树根（默认裸路径的显式写法）；`use self::x;` 锚定导入文件自身模块路径；
`use super::x;` 锚定其父模块。关键字只能起头，`self`/`super` 要求文件位于某模块树（`libs/` 或 Breeze
源根）内。`pub(std)` 使条目仅在其自身模块根树内公开：位于不同根（或在所有根之外）的导入者视其为私有，
`pub use` 门面无法把它洗过该边界。（`test_todo119.py` 另留两个无源回归。）

### todo124 — 模块导入用 `as` 改名
`use a::b as c;` 在 `c` 下注册模块命名空间（`c::item` 限定调用与 `c::Type` 类型位都解析）；
`use m::item as c;` 让入口文件里的裸 `c` 引用指代 `item`（按文件、作用域感知：局部绑定 `c` 遮蔽别名）。
通配与分组导入不可改名，以专门解析错误失败。

### todo125 — 分组导入内的 `as` 改名
`use a::b::{c as d, e};` 展开为 `use a::b::c as d;` 加 `use a::b::e;`——每个元素自带可选改名，语义与
扁平的 todo-124 形式完全一致（item 导入按文件重写裸引用，模块元素在别名下的命名空间注册）。重复的
`(name, alias)` 折叠；同一 item 可用两个名字选入；闭花括号后的尾随 `as` 仍是错误。

### todo126 — `[pub] export crate <name> [as <alias>];`
CWind 版的 Rust 2015 `extern crate`：把整个顶层 crate（`libs/<name>` 模块树）在本文件里按一个名字绑定。
它是**受限**的模块导入——crate 名必须是单个标识符，不带 `::` 路径、item 分组或 `*`——复用 `use` 机制，
故 `export crate foo;` 恰如 `use foo;`，另带一个 `crate_export` 溯源标志供 SA/manifest 区分两种写法。
无 `pub` 时仅本文件可见。（`test_todo126.py` 另留 manifest 标志与 cfg 门控两个回归。）

---

## 既走通用发现、又留 bespoke 文件的区

- **bug36**（`test_bug36.py`）：管线结果由通用发现跑；bespoke 文件额外断言"模块内 parse error 的
  `source` 必须指向出错的模块文件（`stdlib.wind`）且行列正确"——这是管线结果 schema 表达不了的。
  原始 bug：导入模块里的解析错误被错报到入口文件文本上。
- **todo112**（`test_todo112.py`）：分组 `use` 的展平，除管线结果外还要断言入口文件实际展开出的
  `UseDecl` 精确列表，故不进通用发现，由本文件用 `expect.json` 的 `use_decls` 键校验。
