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
$ParentDir = Split-Path -Parent $PSScriptRoot

Set-Location $ParentDir
$ClangPath = Join-Path $ParentDir ".LLVM18/bin/clang.exe"
$Compiler = if (Test-Path $ClangPath) { $ClangPath } else { "clang" }
New-Item -Path build -ItemType Directory -Force | Out-Null
Set-Location build
cmake -G "Ninja" -DCMAKE_C_COMPILER="$Compiler" .. -DCMAKE_C_STANDARD=11
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
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
