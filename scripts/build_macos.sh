#!/usr/bin/env bash
# pr2resolve - macOS build script
# Usage: bash scripts/build_macos.sh
# Prerequisites: python 3.14, requirements-build.txt, upx (brew), Xcode CLT (clang)
# Output: dist/macos-{arch}/pr2resolve-v{VERSION}-macos-{arch}.tar.gz
# Cleanup exception: this script may directly remove only its own dist/<platform> artifacts.
set -euo pipefail
cd "$(dirname "$0")/.."

# Detect arch
ARCH=$(uname -m)
PLATFORM="macos-${ARCH}"
echo "pr2resolve - ${PLATFORM} build"

# Read version
VERSION=$(python3 -c "from pr2_constants import VERSION; print(VERSION)")
ARCHIVE_NAME="pr2resolve-v${VERSION}-${PLATFORM}"
DIST_DIR="dist/${PLATFORM}"
ARCHIVE_PATH="${DIST_DIR}/${ARCHIVE_NAME}.tar.gz"

guard_dist_cleanup() {
    case "${DIST_DIR}" in
        dist/*) ;;
        *) echo "FATAL: unsafe DIST_DIR: ${DIST_DIR}"; exit 1 ;;
    esac
    if [[ "${DIST_DIR}" == *".."* || "${DIST_DIR}" == "dist/" || "${DIST_DIR}" == "dist" ]]; then
        echo "FATAL: unsafe DIST_DIR: ${DIST_DIR}"
        exit 1
    fi
}

# Pre-flight checks
missing=()
command -v python3 >/dev/null 2>&1 || missing+=("python3")
command -v upx     >/dev/null 2>&1 || missing+=("upx (brew install upx)")
python3 -m nuitka --version >/dev/null 2>&1 || missing+=("nuitka (python3 -m pip install -r requirements-build.txt)")

if [ ${#missing[@]} -gt 0 ]; then
    echo "FATAL: Missing dependencies: ${missing[*]}"
    echo ""
    echo "Install guide:"
    echo "  brew install python@3.14 upx"
    echo "  python3 -m pip install -r requirements-build.txt"
    echo "  xcode-select --install"
    exit 1
fi

# Clean
guard_dist_cleanup
rm -rf "${DIST_DIR}"
mkdir -p "${DIST_DIR}"

# Nuitka compile
BINARY="${DIST_DIR}/pr2resolve"
echo "[1/4] Nuitka onefile compile..."
python3 -m nuitka \
  --mode=onefile \
  --output-dir="${DIST_DIR}" \
  --output-filename=pr2resolve \
  --clang \
  --lto=yes \
  --onefile-no-compression \
  --python-flag=no_docstrings \
  --python-flag=no_asserts \
  --noinclude-setuptools-mode=nofollow \
  --noinclude-pytest-mode=nofollow \
  --noinclude-unittest-mode=nofollow \
  --noinclude-pydoc-mode=nofollow \
  --noinclude-IPython-mode=nofollow \
  --noinclude-dask-mode=nofollow \
  --noinclude-numba-mode=nofollow \
  --noinclude-default-mode=warning \
  --remove-output \
  tui.py

# Locate the binary
# Nuitka onefile may produce app bundle on macOS
if [ ! -f "${BINARY}" ]; then
    APP="${DIST_DIR}/pr2resolve.app/Contents/MacOS/pr2resolve"
    if [ -f "${APP}" ]; then cp "${APP}" "${BINARY}"; fi
fi

if [ ! -f "${BINARY}" ]; then
    echo "FATAL: Compiled binary not found at ${BINARY}"
    exit 1
fi

# UPX compression
echo "[2/4] UPX compression..."
upx --best --lzma "${BINARY}" || echo "WARNING: UPX failed, continuing"

# Package
echo "[3/4] Packaging ${ARCHIVE_NAME}.tar.gz..."
tar -czf "${ARCHIVE_PATH}" -C "${DIST_DIR}" pr2resolve

# Clean up bare binary
echo "[4/4] Cleaning up..."
if [ "${BINARY}" != "${DIST_DIR}/pr2resolve" ]; then
    echo "FATAL: unsafe binary cleanup path: ${BINARY}"
    exit 1
fi
rm -f "${BINARY}"

# SHA256
HASH=$(shasum -a 256 "${ARCHIVE_PATH}" | awk '{print $1}')
echo "${HASH}  ${ARCHIVE_NAME}.tar.gz" > "${DIST_DIR}/SHA256SUMS.txt"

# Report
SIZE=$(du -h "${ARCHIVE_PATH}" | cut -f1)
echo ""
echo "== Build complete =="
echo "  ${ARCHIVE_PATH}  (${SIZE})"
echo "  SHA256: ${HASH}"
echo ""
echo "  ${HASH}  ${ARCHIVE_NAME}.tar.gz"
