此项目仍在原型阶段, 此 readme 仅用于指示如何在 Windows / Linux / Termux 上构建 CWind

---

# I. BUILD

## 1.1 PREPARE

表格中的 LLVM18 需要自行下载符合架构的预编译项目并移动到目标位置

### 1.1.1 Windows

| 工具   | 安装路径  |
 |--------|-----------|
| Ninja  | 系统 Path |
| CMake  | 系统 Path |
| gcc    | 系统 Path |
| LLVM18 | ./.LLVM18 |

### 1.1.2 Linux

如果你使用的是`WSL`, 那么你应该下载为`Windows`构建的 LLVM, 构建脚本可以继续使用`build.sh`

| 工具   | 安装路径  |
|--------|-----------|
| CMake  | 系统 Path |
| Make   | 系统 Path |
| gcc    | 系统 Path |
| LLVM18 | ./.LLVM18 |

### 1.1.3 Termux

| 工具   | 安装路径            |
|--------|---------------------|
| CMake  | Proot Path          |
| Proot  | 直接进入 proot 环境 |
| Make   | Proot Path          |
| gcc    | 系统 Path           |
| LLVM18 | ./.LLVM18           |

---

## 2.2 COMMAND

### 2.2.1 Use Scripts

#### 2.2.1.1 Windows: 

```shell
./scripts/build.ps1
```

#### 2.2.1.2 Linux: 

```shell
chmod +x ./scripts/build.sh
./scripts/build.sh
```

#### 2.2.1.3 Termux: 

```shell
chmod +x ./termux/build.termux.sh
mv ./termux/build.termux.sh .
./termux/build.termux.sh
```

---

# II. BUG

打勾的即为已修复, 部分用例可在`bugs`中找到

 - [x] 1. 修复泛型方法传入的 self 所绑定的类型错误的问题
 - [x] 2. 修复`for`遍历容器时类型校验错误的 Bug ( 例如`for kv in map.entry()`会被`cwindc`拒绝)
 - [x] 3. 修复 `impl<T: BoundTrait<Another>> for Type<T>` 中不检查是否存在 `BoundTrait` 的 Bug
 - [x] 4. 修复顶层变量声明失败的问题 ( 如 `const Data: Map<A, B> = { xxxx: xxxx }` 在 `cwindc` 中会被标识为 `undeclared variable` )
 - [x] 5. 类型检查不穿透容器
 - [x] 6. 不支持 `!>` 和 `!<` 的问题 (Lexer 映射到 `LE` 和 `GE` 即可, 不需要额外 Token)
 - [ ] 7. 编译期未验证 group 精化类型
 - [ ] 8. 实现`trait`时, 需要返回`Self`的函数未能将`Self`绑定到自身名称上
   (例如在结构体`MyStruct`的方法`fn method(...) -> Self`中, 未能将`Self`绑定到`MyStruct`)
 - [ ] 9. 实现无泛型参数的内置`trait`和部分其它内置`trait`时, 完全不检查是否真的实现了某函数方法
 - [ ] 10. 如果手动同时实现了`From<A> for B`和`Into<B>`中的`into`方法, 没有检查是否出现了重复的`into`
 - [ ] 11. 未检查实现内置`trait`对应方法时的返回值与参数
 - [ ] 12. 编译器前端未验证某对象在`builtins::print`时是否具有`Display::to_string`方法
 - [ ] 13. 编译器前端未在对象被打印时生成对应的`Display::to_string`方法调用
 - [ ] 14. 编译器后端未使用对象的`Display::to_string`方法
 - [ ] 15. 函数传参时未移动所有权 (赦免 self, 其在现阶段需要传递引用) (我们不应该搞值拷贝那一套)
 - [ ] 16. 编译器前端未验证`group@struct -> { field }`中的`field`是否符合`group`所接收的类型
 - [ ] 17. 编译器后端未验证`String.format()`方法所接收的参数数量是否匹配花括号数量
 - [ ] 18. `Parser`未能正确验证空`for-in`循环后花括号的开闭
 - [ ] 19. `From` 应该是`Into`的关联`trait`, 而不应该要求手动实现`into`(在`impl From<A> for B`时, 未自动实现`A.into`)

---

# III. TODO

CWind 以 Rust 的语法为基础母板, 进行了些许修改与添加

 - [x] 增加`Int32`/`UInt32`/`Int64`/`UInt64`/`Float64`
 - [x] 完整的结构体, 泛型, 以及相关的[.]调用成员属性、方法
 - [x] `which` 钩子
 - [x] 对 `which` 钩子进行限制
 - [x] Map / Vector 字面量
 - [x] never `!` 类型
 - [x] 引用类型的`self`
 - [x] 全自动的编译期已知值的精化类型验证与已知常数折叠(可跨函数)
 - [ ] 建议型的显式函数编译期展开标记, 思路来自[`Alum`](https://github.com/wayuto/Alum)的`func(pure)`
 - [ ] 编译期虚拟机(以更方便地做 SMT 验证、控制和编译期计算)
 - [ ] `pub` 与非 `pub` 函数的可见性控制
 - [ ] 根据声明类型来区分`Map`和`Set`
 - [ ] 允许为结构体字段设置 `pub`
 - [x] 简化的精化类型
 - [ ] 完整的精化类型 (需要包管理机制+std)
 - [x] Tuple 字面量
 - [ ] 数值`as`
 - [ ] 尾返回无需 `return`
 - [ ] 需要增加`move`关键字
 - [ ] 需要增加`&`表示传递引用而非移动所有权
 - [ ] 部分 `let` 声明时可无需类型自动推断
 - [ ] 重做 `which` 后置钩子 (从`return`之前插入分支改为在调用处插入 hook)
 - [ ] 分化 `which` 钩子为`after`/`before`两种类型
 - [ ] 可检查返回值的特殊`return`钩子
 - [ ] 对 `group` 和结构体字段精化的进一步测试
 - [x] [Regex 引擎](https://github.com/cwind-project/cwind-regex) (已实现 `i/g/m/s/u/y/x`, `v`尚未实现)
 - [ ] 将[Regex 引擎](https://github.com/cwind-project/cwind-regex)绑定到`String.matches`方法上
 - [x] `if-chains`
 - [x] `match-guard`
 - [x] 带值 Enum
 - [x] Trait 关联类型
 - [x] 支持不同类型的数字之间的比较
 - [ ] 结构体 newType
 - [ ] 完整 GC
 - [ ] 更完整的泛型体操写法 (Rust-Like)
 - [ ] 包管理与导入
 - [ ] std
 - [ ] 完善包管理器
 - [ ] 更多内置方法和 trait
 - [ ] 前端自举
 - [ ] (极晚期) Rust-Like 的宏系统
