#!/bin/sh

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PARENT_DIR=$(dirname "$SCRIPT_DIR")
GRANDPA_DIR=$(dirname "$PARENT_DIR")

echo $GRANDPA_DIR

# shellcheck disable=SC2164
cd "$PARENT_DIR"
mkdir -p build && cd build || exit
cmake .. -DCMAKE_C_STANDARD=11 -DCMAKE_C_COMPILER=gcc
# shellcheck disable=SC2046
make -j$(nproc)
cd "$PARENT_DIR"
mv --force build "$GRANDPA_DIR"
cd "$GRANDPA_DIR"
