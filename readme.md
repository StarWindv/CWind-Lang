此项目尚未完工, 此 readme 仅用于指示如何在 Windows 上构建 CWind

# BUILD

## PREPARE

| 工具   | 安装路径  |
 |--------|-----------|
| Ninja  | 系统 Path |
| CMake  | 系统 Path |
| LLVM18 | ./.LLVM18 |

## COMMAND

### Use Scripts

```shell
./scripts/build.ps1
```

## TODO

CWind 就是要朝着`Rust的亲戚`这个方向上走一走, 没有什么好避讳的

 - [x] 增加`Int32`/`UInt32`/`Int64`/`UInt64`/`Float64`
 - [x] 完整的结构体, 泛型, 以及相关的[.]调用成员属性、方法
 - [x] `which` 钩子
 - [x] 对 `which` 钩子进行限制
 - [x] 完善 Rust-Like 的 `if-let-guard`, `match-guard` 等多种模式匹配的完整语法
 - [x] Rust 风格的带值 Enum
 - [ ] GC: 不自研胖 GC (mempage/WAL), 直接按 Go 风格实现 (非移动三色标记-清扫 +
       写屏障 + 栈根, 先串行后并发), 以值类型为主, 抛弃胖 handle/record 模型
 - [x] 支持不同类型的数字之间的比较
 - [ ] 更完整的泛型体操写法 (Rust-Like)
 - [ ] 包管理与导入
 - [ ] 完善包管理器
 - [ ] 更多内置方法和 trait
 - [ ] 自举
 - [ ] (极晚期) Rust-Like 的宏系统
