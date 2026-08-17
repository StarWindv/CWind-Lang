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
| LLVM18 | ./.LLVM18 |

### 1.1.2 Linux

| 工具   | 安装路径  |
 |--------|-----------|
| CMake  | 系统 Path |
| Make   | 系统 Path |
| LLVM18 | ./.LLVM18 |

### 1.1.3 Termux

| 工具   | 安装路径            |
|--------|---------------------|
| CMake  | Proot Path          |
| Proot  | 直接进入 proot 环境 |
| Make   | Proot Path          |
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

- [ ] 修复泛型方法传入的 self 所绑定的类型错误的问题
- [ ] 修复`for`便利容器时类型校验错误的 Bug ( 例如`for kv in map.entry()`会被`cwindc`拒绝)
- [ ] 修复 `impl<T: BoundTrait<Another>> for Type<T>` 中不检查是否存在 `BoundTrait` 的 Bug
- [ ] 修复顶层变量声明失败的问题 ( 如 `const Data: Map<A, B> = { xxxx: xxxx }` 在 `cwindc` 中会被标识为 `undeclared variable` )

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
 - [ ] `pub` 与非 `pub` 函数的可见性控制
 - [ ] 允许为结构体字段设置 `pub`
 - [ ] Tuple 字面量
 - [ ] 数值`as`
 - [ ] 尾返回无需 `return`
 - [ ] 部分 `let` 声明时可无需类型自动推断
 - [ ] 重做 `which` 后置钩子 (从`return`之前插入分支改为在调用处插入 hook)
 - [ ] 分化 `which` 钩子为`after`/`before`两种类型
 - [ ] 可检查返回值的特殊`return`钩子
 - [ ] 对 `group` 和结构体字段精化的进一步测试
 - [x] [Regex 引擎](https://github.com/cwind-project/cwind-regex) (已实现 `i/g/m/s/u/y/x`, `v`尚未实现)
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
