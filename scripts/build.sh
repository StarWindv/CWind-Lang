#!/bin/sh

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PARENT_DIR=$(dirname "$SCRIPT_DIR")

# shellcheck disable=SC2164
cd "$PARENT_DIR"
mkdir -p build && cd build || exit
cmake .. -DCMAKE_C_STANDARD=11 -DCMAKE_C_COMPILER=clang
# shellcheck disable=SC2046
make -j$(nproc)
cd - || exit
