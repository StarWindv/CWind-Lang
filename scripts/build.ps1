$ParentDir = Split-Path -Parent $PSScriptRoot

Set-Location $ParentDir
mkdir build
Set-Location build
cmake -G "Ninja" -DCMAKE_C_COMPILER=clang .. -DCMAKE_C_STANDARD=11
ninja
