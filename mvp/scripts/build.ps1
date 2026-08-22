$ParentDir = Split-Path -Parent $PSScriptRoot
$GrandpaDir = Split-Path -Parent $ParentDir

Set-Location $ParentDir
$ClangPath = Join-Path $ParentDir "../.LLVM18/bin/clang.exe"
Write-Host $ClangPath
$Compiler = if (Test-Path $ClangPath) { $ClangPath } else { "clang" }
if (Test-Path build/) {
    Remove-Item build/ -Force -Recurse
}
New-Item -Path build -ItemType Directory -Force | Out-Null
Set-Location build
cmake -G "Ninja" -DCMAKE_C_COMPILER="$Compiler" .. -DCMAKE_C_STANDARD=11 -DCMAKE_C_FLAGS="-O3"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
ninja
Set-Location $ParentDir
if (Test-Path $GrandpaDir/build/) {
    Remove-Item $GrandpaDir/build/ -Force -Recurse
}
Move-Item build $GrandpaDir
Set-Location $GrandpaDir
