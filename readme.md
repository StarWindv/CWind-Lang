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

# II. TODO

CWind 以 Rust 的语法为基础模板, 并进行了部分修改

 - [x] 增加`Int32`/`UInt32`/`Int64`/`UInt64`/`Float64`
 - [x] 完整的结构体, 泛型, 以及相关的[.]调用成员属性、方法
 - [x] `which` 钩子
 - [x] 对 `which` 钩子进行限制
 - [ ] 重做 `which` 后置钩子
 - [ ] 分化 `which` 钩子为`after`/`before`两种类型
 - [x] 完善 Rust-Like 的 `if-let-guard`, `match-guard` 等多种模式匹配的完整语法
 - [x] Rust 风格的带值 Enum
 - [x] Trait 关联类型
 - [x] 支持不同类型的数字之间的比较
 - [ ] GC: 不自研胖 GC (mempage/WAL), 直接按 Go 风格实现 (非移动三色标记-清扫 +
       写屏障 + 栈根, 先串行后并发), 以值类型为主, 抛弃胖 handle/record 模型
 - [ ] 更完整的泛型体操写法 (Rust-Like)
 - [ ] 包管理与导入
 - [ ] 完善包管理器
 - [ ] 更多内置方法和 trait
 - [ ] 自举
 - [ ] (极晚期) Rust-Like 的宏系统
