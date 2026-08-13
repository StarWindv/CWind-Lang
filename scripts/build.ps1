$ParentDir = Split-Path -Parent $PSScriptRoot

Set-Location $ParentDir
$ClangPath = Join-Path $ParentDir ".LLVM18/bin/clang.exe"
$Compiler = if (Test-Path $ClangPath) { $ClangPath } else { "clang" }
New-Item -Path build -ItemType Directory -Force | Out-Null
Set-Location build
cmake -G "Ninja" -DCMAKE_C_COMPILER="$Compiler" .. -DCMAKE_C_STANDARD=11
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
ninja
Set-Location $ParentDir
