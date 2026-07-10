#!/usr/bin/env bash
#
# Build the legacy C++/pybind11 backend for head-to-head benchmarking.
#
# The C++ implementation was removed from the working tree when the package was
# reimplemented in pure Python/Polars; it still lives on the `main` branch under
# native/. This script builds it *without* switching your current branch (via a
# detached git worktree) and installs the resulting extension into
# meds_etl_cpp/ so run_bench.py / stress.py can load it as the `cpp` backend.
#
# Requirements:
#   * bazel (the native/ build is Bazel-based)
#   * a C++17 toolchain + cmake/ninja (Arrow is built from source, BUNDLED)
#   * network access on first build (Bazel/Arrow fetch dependencies)
#
# Usage:
#   benchmarks/build_cpp.sh            # build from origin/main (or local main)
#   CPP_REF=main benchmarks/build_cpp.sh
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CPP_REF="${CPP_REF:-main}"
TARGET_DIR="${REPO_ROOT}/meds_etl_cpp"

if ! command -v bazel >/dev/null 2>&1; then
  echo "error: bazel not found on PATH; install bazel to build the C++ backend." >&2
  exit 1
fi

echo "[build_cpp] using git ref: ${CPP_REF}"
WORKTREE="$(mktemp -d "${TMPDIR:-/tmp}/meds_etl_cpp_build.XXXXXX")"
cleanup() {
  git -C "${REPO_ROOT}" worktree remove --force "${WORKTREE}" >/dev/null 2>&1 || true
  rm -rf "${WORKTREE}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[build_cpp] checking out native/ tree into a detached worktree ..."
git -C "${REPO_ROOT}" worktree add --detach "${WORKTREE}" "${CPP_REF}" >/dev/null

NATIVE_DIR="${WORKTREE}/native"
if [[ ! -d "${NATIVE_DIR}" ]]; then
  echo "error: ${CPP_REF} has no native/ directory; is that the right ref?" >&2
  exit 1
fi

COMPILE_MODE="${COMPILE_MODE:-opt}"

# The main branch's .bazelrc hardcodes -mavx (x86 only). On Apple Silicon / any
# non-x86 host that flag is rejected by clang, so build with an AVX-free rc
# (mirroring setup.py's DISABLE_CPU_ARCH path, which falls back to backupbazelrc).
BAZEL_STARTUP_ARGS=()
ARCH="$(uname -m)"
if [[ "${ARCH}" != "x86_64" ]]; then
  RC_FILE="${NATIVE_DIR}/armsafe.bazelrc"
  cat > "${RC_FILE}" <<'EOF'
build --cxxopt=-std=c++17
build --cxxopt=-Wall
common --enable_workspace
EOF
  BAZEL_STARTUP_ARGS=(--noworkspace_rc "--bazelrc=${RC_FILE}")
  echo "[build_cpp] non-x86 arch (${ARCH}) detected; building without -mavx"
fi

BAZEL_BUILD_ARGS=()
# On recent macOS, Apple's libtool reports "cctools_ld-<n>", but the pinned
# Arrow's static-merge check expects the older "cctools-<n>" string and would
# otherwise reject it as "incompatible GNU libtool". Shim just the version query
# and route it into Bazel's (host) action environment PATH.
if [[ "$(uname -s)" == "Darwin" ]]; then
  SHIM_DIR="${WORKTREE}/_libtool_shim"
  mkdir -p "${SHIM_DIR}"
  cat > "${SHIM_DIR}/libtool" <<'EOF'
#!/bin/bash
for a in "$@"; do
  if [[ "$a" == "-V" || "$a" == "--version" ]]; then
    echo "Apple Inc. version cctools-1267"
    exit 0
  fi
done
exec /usr/bin/libtool "$@"
EOF
  chmod +x "${SHIM_DIR}/libtool"
  SHIM_PATH="${SHIM_DIR}:${PATH}"
  BAZEL_BUILD_ARGS+=("--action_env=PATH=${SHIM_PATH}" "--host_action_env=PATH=${SHIM_PATH}")
  echo "[build_cpp] installed libtool version shim for Arrow static-merge check"
fi

echo "[build_cpp] bazel build -c ${COMPILE_MODE} //:meds_etl_cpp (this can take a while) ..."
( cd "${NATIVE_DIR}" && bazel "${BAZEL_STARTUP_ARGS[@]}" build -c "${COMPILE_MODE}" \
    "${BAZEL_BUILD_ARGS[@]}" //:meds_etl_cpp )

SO_SRC="${NATIVE_DIR}/bazel-bin/meds_etl_cpp.so"
if [[ ! -f "${SO_SRC}" ]]; then
  # Some Bazel/pybind versions emit an ABI-tagged name; grab whatever matched.
  SO_SRC="$(find "${NATIVE_DIR}/bazel-bin" -maxdepth 1 -name 'meds_etl_cpp*.so' | head -n1 || true)"
fi
if [[ -z "${SO_SRC}" || ! -f "${SO_SRC}" ]]; then
  echo "error: build succeeded but no meds_etl_cpp*.so found in bazel-bin." >&2
  exit 1
fi

cp -f "${SO_SRC}" "${TARGET_DIR}/meds_etl_cpp.so"
chmod u+rw "${TARGET_DIR}/meds_etl_cpp.so"
echo "[build_cpp] installed -> ${TARGET_DIR}/meds_etl_cpp.so"
echo "[build_cpp] done. Run head-to-head with:"
echo "  python benchmarks/stress.py --scaling --backends polars cpp"
