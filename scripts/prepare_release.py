#!/usr/bin/env python3
"""Prepare release metadata before creating a semver tag.

Usage:
    python scripts/prepare_release.py 1.0.4
    python scripts/prepare_release.py 1.0.4 --tagline "release summary"

If future version markers are added, update VERSION_TARGETS in one place.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_TAGLINE = "metadata reliability, release automation, and TUI portability"


@dataclass(frozen=True)
class VersionTarget:
    path: str
    pattern: str
    replacement: str
    count: int = 1


VERSION_TARGETS = (
    VersionTarget(
        "pr2_constants.py",
        r'VERSION = "[^"]+"',
        'VERSION = "{version}"',
    ),
    VersionTarget(
        "converter.bat",
        r'set "VERSION=[^"]+"',
        'set "VERSION={version}"',
    ),
    VersionTarget(
        "converter.sh",
        r'VERSION="[^"]+"',
        'VERSION="{version}"',
    ),
    VersionTarget(
        "README.md",
        r"\*v[0-9]+\.[0-9]+\.[0-9]+ [^\n]+\*",
        "*v{version} — {tagline}*",
    ),
    VersionTarget(
        "README_EN.md",
        r"\*v[0-9]+\.[0-9]+\.[0-9]+ [^\n]+\*",
        "*v{version} — {tagline}*",
    ),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _validate_version(version: str) -> None:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit(f"FATAL: version must match MAJOR.MINOR.PATCH, got: {version}")


def _replace_once(root: Path, target: VersionTarget, version: str, tagline: str) -> bool:
    path = root / target.path
    text = path.read_text(encoding="utf-8")
    replacement = target.replacement.format(version=version, tagline=tagline)
    updated, changes = re.subn(target.pattern, replacement, text, count=target.count)
    if changes != target.count:
        raise SystemExit(
            f"FATAL: expected {target.count} replacement(s) in {target.path}, got {changes}"
        )
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8", newline="")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Release version without leading v, for example 1.0.4")
    parser.add_argument(
        "--tagline",
        default=DEFAULT_TAGLINE,
        help="README status tagline after the displayed version",
    )
    args = parser.parse_args()

    _validate_version(args.version)
    root = _repo_root()

    changed = []
    for target in VERSION_TARGETS:
        if _replace_once(root, target, args.version, args.tagline):
            changed.append(target.path)

    if changed:
        print("Updated:")
        for path in changed:
            print(f"  {path}")
    else:
        print("No version marker changes needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
