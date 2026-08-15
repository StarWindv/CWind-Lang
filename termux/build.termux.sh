#!/usr/bin/env bash
# CWind - Termux (proot Ubuntu) 构建脚本
#
# 用法:
#   ./build.termux.sh            # 只编译 cwindc (Release)
#   ./build.termux.sh --tests    # 同时构建并运行 ctest (含 LLVM 后端流水线测试)
#
# 要点:
#   - 根目录 CMakeLists.txt 保持不变, 使用独立的 termux/CMakeLists.txt
#   - LLVM 固定使用仓库内置 ./.LLVM18, 不搜索/链接系统 LLVM
#   - C 编译器默认选 Ubuntu gcc (/usr/bin/gcc, glibc), 与 .LLVM18 静态库匹配;
#     可用环境变量 CC 覆盖
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$SCRIPT_DIR"
LLVM_DIR="$ROOT_DIR/.LLVM18"
TERMUX_CMAKE_DIR="$ROOT_DIR/termux"
BUILD_DIR="${BUILD_DIR:-$ROOT_DIR/build.termux}"

BUILD_TESTS=OFF
if [[ "${1:-}" == "--tests" ]]; then
    BUILD_TESTS=ON
fi

echo "==> CWind Termux build"
echo "    source : $ROOT_DIR"
echo "    cmake  : $TERMUX_CMAKE_DIR/CMakeLists.txt"
echo "    llvm   : $LLVM_DIR (内置, 不使用系统 LLVM)"
echo "    build  : $BUILD_DIR"

if [[ ! -f "$LLVM_DIR/include/llvm-c/Core.h" || ! -x "$LLVM_DIR/bin/llvm-config" ]]; then
    echo "错误: 未找到内置 LLVM18 (期望 $LLVM_DIR/include/llvm-c/Core.h 与 bin/llvm-config)" >&2
    exit 1
fi

# 选择 C 编译器: 默认 Ubuntu gcc (glibc aarch64), 可用 CC 环境变量覆盖
CC="${CC:-}"
if [[ -z "$CC" ]]; then
    if [[ -x /usr/bin/gcc ]]; then
        CC=/usr/bin/gcc
    elif command -v gcc >/dev/null 2>&1; then
        CC="$(command -v gcc)"
    else
        echo "错误: 未找到 gcc。在 proot Ubuntu 中先安装: apt-get install gcc libc6-dev binutils" >&2
        exit 1
    fi
fi
echo "    cc     : $CC"

# cmake/ctest: 优先 Ubuntu 原生版本, 避免 Termux cmake 把宿主误判为 Android
CMAKE_BIN="${CMAKE_BIN:-}"
if [[ -z "$CMAKE_BIN" ]]; then
    if [[ -x /usr/bin/cmake ]]; then
        CMAKE_BIN=/usr/bin/cmake
    elif command -v cmake >/dev/null 2>&1; then
        CMAKE_BIN="$(command -v cmake)"
    else
        echo "错误: 未找到 cmake (proot Ubuntu: apt-get install cmake)" >&2
        exit 1
    fi
fi
CTEST_BIN="${CTEST_BIN:-}"
if [[ -z "$CTEST_BIN" ]]; then
    if [[ -x /usr/bin/ctest ]]; then
        CTEST_BIN=/usr/bin/ctest
    elif command -v ctest >/dev/null 2>&1; then
        CTEST_BIN="$(command -v ctest)"
    fi
fi
echo "    cmake  : $CMAKE_BIN"

# LLVM 静态库所需系统库 (zlib/tinfo/xml2 dev 包)
if [[ ! -e /lib/aarch64-linux-gnu/libz.so && ! -e /usr/lib/aarch64-linux-gnu/libz.so ]] \
   || [[ ! -e /lib/aarch64-linux-gnu/libtinfo.so && ! -e /usr/lib/aarch64-linux-gnu/libtinfo.so ]]; then
    echo "提示: 如链接报 -lz/-ltinfo/-lxml2 找不到, 请安装: apt-get install zlib1g-dev libncurses-dev libxml2-dev" >&2
fi

echo "==> cmake configure"
"$CMAKE_BIN" -S "$TERMUX_CMAKE_DIR" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER="$CC" \
    -DCWIND_BUILD_TESTS="$BUILD_TESTS"

echo "==> cmake build"
JOBS="$(nproc 2>/dev/null || echo 2)"
"$CMAKE_BIN" --build "$BUILD_DIR" -j "$JOBS"

CWINDC="$BUILD_DIR/cwindc"
echo "==> 产物: $CWINDC"

if [[ "$BUILD_TESTS" == "ON" ]]; then
    echo "==> ctest"
    "$CTEST_BIN" --test-dir "$BUILD_DIR" --output-on-failure
else
    echo "==> 冒烟: cwindc --emit-exe (hello fixture)"
    "$CWINDC" --emit-exe "$BUILD_DIR/smoke_hello" \
        "$ROOT_DIR/test-c/test-rtl/fixtures/codegen_hello.json"
    set +e
    OUT="$("$BUILD_DIR/smoke_hello" 2>&1)"
    SMOKE_RC=$?
    set -e
    echo "    输出: $OUT (rc=$SMOKE_RC)"
    [[ "$OUT" == "7" ]] || { echo "错误: 冒烟输出应为 7, 实际 '$OUT'" >&2; exit 1; }
    echo "==> 冒烟通过"
fi

echo "==> 完成"
