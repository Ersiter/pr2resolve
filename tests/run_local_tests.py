#!/usr/bin/env python3
"""Local-only test runner for pr2resolve.

This file lives under ignored tests/. It is for this workstation's fixtures
and must not be pushed to GitHub.
"""
from __future__ import annotations

import argparse
import inspect
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import test_validator as tv  # type: ignore[import-not-found]  # noqa: E402
except ModuleNotFoundError:
    from tests import _test_validator as tv  # noqa: E402


FAST_TESTS = {
    "test_tui_has_no_top_level_windows_imports",
    "test_tui_import_does_not_require_msvcrt",
    "test_prproj_source_metadata_preserves_timecode_and_rate",
    "test_prproj_missing_source_metadata_remains_unresolved",
    "test_native_clip_color_updates_verified_nested_enum",
    "test_native_capability_check_handles_empty_and_multiple_effect_lists",
}


def _all_tests() -> dict[str, Callable[[], None]]:
    return {
        name: fn
        for name, fn in inspect.getmembers(tv, inspect.isfunction)
        if name.startswith("test_")
    }


def _select_tests(mode: str, only: str | None) -> list[tuple[str, Callable[[], None]]]:
    tests = _all_tests()
    if only:
        if only not in tests:
            raise SystemExit(f"Unknown test: {only}")
        return [(only, tests[only])]
    if mode == "fast":
        names = sorted(name for name in FAST_TESTS if name in tests)
    else:
        names = sorted(tests)
    return [(name, tests[name]) for name in names]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("fast", "full"), default="fast")
    parser.add_argument("--only", default=None)
    args = parser.parse_args()

    selected = _select_tests(args.mode, args.only)
    passed = failed = 0
    start_all = time.perf_counter()
    for name, fn in selected:
        start = time.perf_counter()
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"OK   {name} ({time.perf_counter() - start:.2f}s)")
    elapsed = time.perf_counter() - start_all
    print(f"\n{passed} passed, {failed} failed, {elapsed:.2f}s, mode={args.mode}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
