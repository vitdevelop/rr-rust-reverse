#!/usr/bin/env bash
# Reproducible CMake configure for the LLDB / lldb-dap build used by this effort.
#
#   llvm-project ref : llvmorg-22.1.8  (src/llvm-project, matches system lldb 22.1.8)
#   generator        : Ninja
#   build type       : Release + assertions   (per rr-reverse-debugging-plan.md Phase 1)
#   projects         : clang (expression evaluator), lldb (+ lldb-dap, lldb-server)
#   targets          : X86 only  (rr is x86-64 Linux only)
#   python bindings  : ON, via pip-installed SWIG 4.5 at ~/.local/bin/swig
#
# Usage:  scripts/configure-llvm.sh
# Then:   ninja -C build lldb lldb-dap lldb-server
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/src/llvm-project/llvm"
BUILD="$ROOT/build"
INSTALL="$ROOT/install"
SWIG_BIN="${SWIG_BIN:-$HOME/.local/bin/swig}"

cmake -S "$SRC" -B "$BUILD" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_ENABLE_PROJECTS="clang;lldb" \
  -DLLVM_TARGETS_TO_BUILD="X86" \
  -DCMAKE_C_COMPILER=clang \
  -DCMAKE_CXX_COMPILER=clang++ \
  -DLLVM_USE_LINKER=gold \
  -DLLVM_OPTIMIZED_TABLEGEN=ON \
  -DLLVM_PARALLEL_LINK_JOBS=4 \
  -DLLVM_INCLUDE_BENCHMARKS=OFF \
  -DLLVM_INCLUDE_EXAMPLES=OFF \
  -DLLDB_ENABLE_PYTHON=ON \
  -DLLDB_ENABLE_LIBEDIT=ON \
  -DLLDB_ENABLE_CURSES=ON \
  -DPython3_EXECUTABLE="$(command -v python3)" \
  -DSWIG_EXECUTABLE="$SWIG_BIN" \
  -DLLDB_INCLUDE_TESTS=ON \
  -DLLVM_INCLUDE_TESTS=ON \
  -DCMAKE_INSTALL_PREFIX="$INSTALL"
