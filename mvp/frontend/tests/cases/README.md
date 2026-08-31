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

### bug46 — 函数调用允许 `&mut arg`
`_parse_type` 的 `&` 分支不消费 `MUT`（`x: &mut T` 死于 "expected type name"），`_parse_unary`
同样（`take(&mut a)` 死于 "unexpected token 'mut'"）。两层一起修：`Type.mut` 携带类型位可变性
（字符串模型渲染 `&mut T`），`UnaryOp.mutable` 携带借用表达式位。语义随 Rust：可变借用位置必须
收到 `&mut`（`&T` 实参报 "must be &mut Int"），共享位置可收 `&mut`（可变收窄）；`&mut expr`
的操作数必须是 `mut` 绑定（"cannot borrow immutable … as mutable"）；`&mut T` 绑定可写穿
（同 `&mut self`）。原始复现 `bugs/bug46.wind` 里剩余的报错（`*mut c_void` 的 `as` 转换、
`as_mut_ptr` 内建、null 指针字面量）分属 todo-75/95/140，不在本 bug 范围。
端到端：`pipeline_bug46`（CTest，&mut 形参写穿回调用者）。

### bug47 — 容器字面量主动绑定注解的元素类型
`let v: Vector<f64> = [1.0 as f64, …]` 曾报 "cannot initialize Vector<f64> with
Vector<Float64>"：字面量元素类型停留在自身推断（Float / 别名未展开名），与注解比较时
`f32`/`f64` 不在数值表里而失败；就算对上了，`element_type` 注解也不是声明宽度，后端装箱按
错误宽度读写（Map 值位曾读出垃圾）。现在 Vector/Map 字面量在注解给出元素类型时主动绑定它
（Rust `let v: Vec<f64> = vec![1.0, 2.0];`）：结果类型、`element_type`/`key_type`/`value_type`
注解一律取注解侧，元素逐个校验（数值间沿用既有隐式转换）。注意 `map_value_type_mismatch`
的报错点随之从 let 聚合位前移到逐元素位。
端到端：`pipeline_bug47`。

### bug48 — 泛型容器内的类型别名在分析前展开（bug-43 回归）
bug-43 修了 impl 目标的别名展开，但 `_expand_type` 只看基名：`Vector<f32>` 里的 `f32`
原样留在类型串里，与展开后的字面量类型比较必败。现在展开递归进泛型实参（`Vector<f32>` →
`Vector<Float>`、`Vector<Vec<u8>>` → `Vector<Vector<UInt8>>`）与原始指针被指类型。
`type X = … where` 精化别名在实参位**保留原名**（`_refinement` 按名查谓词，摊平即丢约束；
`refined_alias_in_container` 钉住），只有兼容性比较走 deep 展开把 `Vector<Test1>` 与
`Vector<Int8>` 视为同一类型。

### bug49 — `extra` 泛型参数传播到块内函数
`extra Cell<T> { … }` 的 `<T>` 被解析进目标类型的 args，`ExtraDecl.params` 为空，SA 的
`defined`、方法绑定 `owner_params` 与后端实例化读的 owner `params` 全部拿不到参数名。
`_parse_extra` 现把类型名后的实参列表归一化成前导泛型参数（`extra Cell<T>` ≡
`extra<T> Cell<T>`，实参同时保留在类型上）；裸名构造 `Cell { v }` 在泛型 owner 内绑定到带参
owner 类型、在用点绑定到期望类型，否则实例布局查不到。端到端：`pipeline_bug49`。

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

### todo144 — typed-AST 类型对象补定义位置与别名溯源
`fqn_def` 项目树经通用发现跑管线结果（clean）；`test_todo144.py` 额外断言 typed JSON 的
**结构**：类型对象分存 `def`（定义位置规范模块路径，`libs/wrap.wind` → `std::wrap`，
`pub use` 重导出多路径全部归一）/`name`/`alias`（被展开掉的原始拼写，如 `Vec`/`WI`）。
白名单（基础数值/基础容器/编译器内建）与类型形参无 `def`；入口本地类型不写 `def`；
`Map<String, Opt<Int>>` 的实参位递归补 `def`。规范文本见 `TypedAST.md` §3.1。

### bug52 — trait 实现一致性比较前展开别名（todo-153 补用例）
trait 声明与 impl 用不同拼写引用同一类型（`u32` vs `UInt32`、用户 `typedef MyInt =
Int32` 的任一侧）必须对齐：参数、返回类型、泛型实参位（`Vector<MyInt>`）、引用被指
类型（`&MyInt`）全部展开后比较。方法级泛型形参按 alpha 等价比较（trait 的 `U` 对
impl 的 `W`），且比较期间并入 active_generics，不得被同名类型别名展开。负向控制：
展开后仍失配的照常拒绝（参数/返回两条错误文案锁定在 sidecar 里）。`prelude_alias_repro`
项目树复刻 `bugs/bug52.wind` 原始形态。单文件用例用用户 typedef（in-memory 无 prelude）。

### bug53 — 兄弟作用域同名绑定（后端作用域栈回归）
while/for/if 链各分支体都是独立作用域：兄弟块的 `let s` 互不冲突；体内 `let` 可遮蔽
外层，出块后外层绑定恢复；兄弟 for-in 的同名迭代变量同样合法。SA 侧本就放行（bug 是
后端漏压作用域栈），单文件用例锁定 SA 语义，运行期行为由 `pipeline_bug53` 端到端
锁定。单文件用例只能用内建类型名（in-memory 模式无 prelude）。

### bug54 — 本地声明遮蔽 prelude 通配导入，std 内部引用不被劫持
本地 `panic(String)` 遮蔽 prelude 重导出的 `panic(&String)` 时：入口的 bare 调用解析到
本地声明；std 依赖闭包体内的 panic 调用（如 `option.wind` 的 `unwrap_failed`）仍解析到
std 自己的函数。真实根因是 std 闭包引用被入口扁平作用域劫持（详见
`.handover/record/handover.bug53-55.md`）。**此类用例必须项目树形态**（in-memory 单
文件模式没有 prelude）：每个 case 自带最小 libs 树（prelude 重导出 + panic.wind，
第三例另带 option.wind）。`shadow_plus_std_chain` 同时放本地 panic 与 std Option 链路。

### bug60 — 已知值表达式溢出（变量携带的编译期溢出）
字面量溢出早有 `_check_literal_range` 挡，但 `let a: UInt32 = 0xffffffff; a + 1` 这类
**变量携带的已知值**运算静默通过（`_fold_expr` 的 BinOp 路径只折叠纯字面量，不查范围）。
现在 `_fold_expr` 直接折叠 BinOp（变量经 `VarInfo.folded` 参与），`_check_expr_range`
把每个 BinOp 的折叠值与其**结果类型**（`_common_numeric`，同后端提升规则）比对，出界即拒；
覆盖 let 初始化、重赋值、实参、无目标调用位。豁免两条：① 双操作数都还是裸字面量默认
`Int`/`UInt` 的运算（宽度未定，归 `todo-22` 推断；后端裸字面量运算按 64 位）；② 运行期
才能知道的值（回绕语义由 `std::expansion` 的 `wrapping_*` trait 承担，前端不拦）。
`masked_wrapping_ok` 钉住「加宽+掩码」的合法 wrapping 手法，`boundary_exact_ok` 钉住
恰好在上限/下限的值。

### bug61 — 导入 trait == 导入其实现（bug-56 回归，impl 注册不依赖 re-export）
bug-56 修复后 prelude 靠手写 `pub use std::expansion::uXX;` 把 impl 拉进编译面
（赦免式 hack），ca9e412 删掉这些行后 `wrapping_*` 方法全灭。对齐 rustc 语义：
rustc_resolve 在 def-collection 时登记**全部** trait impl（与可见性/re-export 无关，
`late.rs` trait_impls），方法求解经 `trait_impls_of` 查询（`for_each_relevant_impl`）。
CWind 对应实现：`_impl_registry_for` 按模块根惰性构建 per-root impl registry
（trait 名 → (文件, owner)），entry 级 `_pull_trait_impls` 在 prelude/包/显式 use 合并后
**单次**把编译面中每个 trait 的 impl 块连依赖闭包拉进根程序（节点实例与普通导入共享，
`_scope_flat` 防重命名；`module_cache` 复用避免同文件双实例导致的 duplicate-impl）。
拉取只发生在 entry parse —— 放进 `_select_module_items` 会经子 parse 递归回 registry
构建并二次膨胀（调试实录见 `.handover`）。用例：`wrapping_via_prelude`（最小 libs，
无 expansion re-export）、`trait_only_import`（只 use trait，不 use impl 模块）、
`trait_impl_import_locked`（bug-56 原树锁定：显式 use impl 模块与 pull 共存不重复）。

---

## 既走通用发现、又留 bespoke 文件的区

- **bug36**（`test_bug36.py`）：管线结果由通用发现跑；bespoke 文件额外断言"模块内 parse error 的
  `source` 必须指向出错的模块文件（`stdlib.wind`）且行列正确"——这是管线结果 schema 表达不了的。
  原始 bug：导入模块里的解析错误被错报到入口文件文本上。
- **todo112**（`test_todo112.py`）：分组 `use` 的展平，除管线结果外还要断言入口文件实际展开出的
  `UseDecl` 精确列表，故不进通用发现，由本文件用 `expect.json` 的 `use_decls` 键校验。
- **todo144**（`test_todo144.py`）：同上模式——通用发现校验管线 clean，bespoke 文件断言
  typed JSON 类型对象的 `def`/`alias` 字段（见上文）。
