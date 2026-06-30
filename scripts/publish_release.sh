#!/usr/bin/env bash
# pr2resolve — Release publisher
# Aggregates dist/ artifacts from all 3 platforms and creates a GitHub Release.
#
# Prerequisites:
#   1. Run build_windows.ps1, build_macos.sh, build_linux.sh on each platform
#   2. Copy all dist/pr2resolve-v*-*.{zip,tar.gz} into dist/ on one machine
#   3. Run this script from repo root
#
# Usage: bash scripts/publish_release.sh [--draft]
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(python3 -c "from pr2_constants import VERSION; print(VERSION)")
TAG="v${VERSION}"

# ── Check gh CLI ──────────────────────────────────────────────────────
command -v gh >/dev/null 2>&1 || { echo "FATAL: gh CLI not installed (https://cli.github.com)"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "FATAL: gh not authenticated. Run: gh auth login"; exit 1; }

# ── Collect artifacts from per-platform subdirectories ─────────────────
shopt -s nullglob
FILES=(dist/*/pr2resolve-v"${VERSION}"-*.zip dist/*/pr2resolve-v"${VERSION}"-*.tar.gz)
shopt -u nullglob

if [ ${#FILES[@]} -eq 0 ]; then
    echo "FATAL: No release artifacts found under dist/*/"
    echo "Expected: dist/windows-x86_64/pr2resolve-v${VERSION}-windows-x86_64.zip"
    echo "          dist/macos-*/pr2resolve-v${VERSION}-macos-*.tar.gz"
    echo "          dist/linux-x86_64/pr2resolve-v${VERSION}-linux-x86_64.tar.gz"
    exit 1
fi

echo "Artifacts found:"
for f in "${FILES[@]}"; do
    echo "  $f ($(du -h "$f" | cut -f1))"
done

# ── Generate combined SHA256SUMS ──────────────────────────────────────
SUMS="dist/SHA256SUMS.txt"
echo "Generating ${SUMS}..."
> "${SUMS}"
for f in "${FILES[@]}"; do
    if [[ "$OSTYPE" == "darwin"* ]]; then
        shasum -a 256 "$f" >> "${SUMS}"
    else
        sha256sum "$f" >> "${SUMS}"
    fi
done
FILES+=("${SUMS}")

# ── Release notes ─────────────────────────────────────────────────────
NOTES_FILE="RELEASE_NOTES_v${VERSION}.md"
if [ ! -f "$NOTES_FILE" ]; then
    echo "WARNING: $NOTES_FILE not found. Generating minimal notes..."
    echo "## pr2resolve v${VERSION}" > "$NOTES_FILE"
    echo "" >> "$NOTES_FILE"
    echo "See [CHANGELOG.md](CHANGELOG.md) for details." >> "$NOTES_FILE"
fi

# ── Create release ────────────────────────────────────────────────────
FLAGS="--title \"pr2resolve v${VERSION} — Premiere Pro to DaVinci Resolve 转换器\""
FLAGS="$FLAGS --notes-file \"$NOTES_FILE\""
if [[ "${1:-}" == "--draft" ]]; then
    FLAGS="$FLAGS --draft"
fi

echo "Creating GitHub Release ${TAG}..."
echo ""

# shellcheck disable=SC2086
gh release create "$TAG" "${FILES[@]}" $FLAGS

echo ""
echo "══ Published ══"
echo "  https://github.com/Ersiter/pr2resolve/releases/tag/${TAG}"
