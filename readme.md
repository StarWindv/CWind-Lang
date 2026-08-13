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

### Manually

```powershell
mkdir build
cd build
cmake -G "Ninja" -DCMAKE_C_COMPILER=clang .. -DCMAKE_C_STANDARD=11
ninja
```

## TODO

 - [ ] 增加`Int32`/`UInt32`/`Int64`/`UInt64`/`Float64`
 - [ ] 完成 GC
 - [ ] 支持不同类型的数字之间的比较
 - [ ] 更完整的泛型体操写法
 - [ ] 包管理与导入
 - [ ] 完善包管理器
 - [ ] 更多内置方法和 trait
 - [ ] FFI
 - [ ] 多线程
 - [ ] 协程与异步 ( 无栈协程 )
