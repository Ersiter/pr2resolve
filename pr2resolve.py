#!/usr/bin/env python3
"""pr2resolve - Premiere Pro to DaVinci Resolve timeline converter.

Dual-entry (FCP7 XML / .prproj) -> Unified Timeline Model -> FCP7 XML / DRT output.

Thin CLI entry point. Core logic lives in pr2_engine.py.
Constants and data models live in pr2_constants.py.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

# ── Constants + data models (single source of truth) ──────────────
from pr2_constants import (
    VERSION, DEFAULT_FPS, NTSC_RATES, PAL_RATES, FPS_TOLERANCE,
    FCP7_VERSION, FCP7_DOCTYPE, CRITICAL, MAJOR, MINOR,
    FCP7_CLIPIITEM_ORDER, ScaleIssue, Issue,
    _build_file_index, _get_sequence_format, _get_sequence_resolution,
    load_xml, load_prproj,
)

# ── Engine (consolidated: diagnostics + fix + validate + output + prproj + drt) ─
from pr2_engine import (
    _scan, _apply_fixes, _validate,
    _write_fixed_xml, _generate_report,
    _PrprojIndex, _prproj_parse_sequence, _prproj_list_sequences,
    _prproj_extract_all_lumetri,
    _check_resolve_running, _ensure_resolve_running,
    _drt_sandbox_export, _drt_supplement_lumetri,
    _recycle,
)

# ── Recycle utility (also lives in pr2_engine, re-export for convenience) ──


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def _print_banner() -> None:
    """Print the tool banner."""
    print("=" * 56)
    print(f"  pr2resolve v{VERSION}")
    print(f"  Premiere Pro to DaVinci Resolve Converter")
    print("=" * 56)
    print()


def _print_issues(issues: list[Issue]) -> None:
    """Print issues to stdout in a formatted table."""
    if not issues:
        print("  No issues found.")
        return
    for severity in [CRITICAL, MAJOR, MINOR]:
        sev_issues = [i for i in issues if i.severity == severity]
        if not sev_issues:
            continue
        icon = {"CRITICAL": "[C]", "MAJOR": "[M]", "MINOR": "[N]"}.get(severity, "[?]")
        print(f"  {icon} {severity} ({len(sev_issues)}):")
        for issue in sev_issues:
            status = "[FIXED]" if issue.fixed else ""
            print(f"    {status} [{issue.rule_id}] {issue.message}")
        print()


def _run_pipeline(
    input_path: Path,
    output_dir: Optional[Path] = None,
    report: bool = False,
    diagnose_only: bool = False,
    sequence_name: Optional[str] = None,
    drt: bool = False,
    drp_path: Optional[Path] = None,
    all_sequences: bool = False,
    nogui: bool = False,
    gui: bool = False,
) -> int:
    """Run the full fix pipeline on an input file."""
    _print_banner()

    if output_dir is None:
        output_dir = input_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = input_path.stem
    output_path = output_dir / f"{stem}_fixed{input_path.suffix}"
    report_path = output_dir / f"{stem}_fix_report.md"
    backup_path = input_path.with_suffix(input_path.suffix + ".bak")

    # Load
    print(f"  Loading: {input_path}")
    try:
        if input_path.suffix.lower() == ".prproj":
            prproj_root = load_prproj(input_path)
            print("  Format: .prproj (Premiere Pro project)")
            idx = _PrprojIndex.build(prproj_root)
            sequences = _prproj_list_sequences(prproj_root, idx)
            if not sequences:
                print("  Error: No sequences found in .prproj")
                return 1

            non_empty = [s for s in sequences if s["clip_count"] > 0]
            if sequence_name:
                matching = [s for s in sequences if s["name"] == sequence_name]
                if matching:
                    selected = matching[0]
                else:
                    print(f"  Error: Sequence '{sequence_name}' not found")
                    print(f"  Available: {[s['name'] for s in sequences]}")
                    return 1
            elif len(non_empty) == 1:
                selected = non_empty[0]
            elif len(sequences) == 1:
                selected = sequences[0]
            else:
                print(f"  Found {len(sequences)} sequences:")
                for i, s in enumerate(sequences, 1):
                    marker = " <--" if s["clip_count"] > 0 else ""
                    print(f"    [{i}] {s['name']}  {s['width']}x{s['height']}  {s['clip_count']} clips{marker}")
                selected = max(sequences, key=lambda s: s["clip_count"])
                print(f"  Auto-selected: [{sequences.index(selected)+1}] {selected['name']}")

            print(f"  Sequence: {selected['name']} ({selected['width']}x{selected['height']}, {selected['clip_count']} clips)")
            print()
            print("  Converting .prproj to FCP7 XML...")
            root = _prproj_parse_sequence(prproj_root, selected["uid"], input_path)
            print("  Conversion complete.")
            print()

            lumetri_data = _prproj_extract_all_lumetri(prproj_root, selected["uid"])
            if lumetri_data:
                print(f"  Lumetri params: {sum(len(v) for v in lumetri_data.values())} across {len(lumetri_data)} clips")

            output_path = output_dir / f"{stem}.xml"
        else:
            root = load_xml(input_path)
            lumetri_data = {}
            print("  Format: FCP7 XML")
    except Exception as e:
        print(f"  Error loading file: {e}")
        return 1

    seq = root.find("sequence")
    seq_name = seq.findtext("name", "(unnamed)") if seq is not None else "(no sequence)"
    print(f"  Sequence: {seq_name}")
    print()

    # Diagnose
    print("  Scanning for issues...")
    scan_issues = _scan(root)
    _print_issues(scan_issues)

    if diagnose_only:
        print(f"  Diagnose-only mode. {len(scan_issues)} issues found.")
        return 0

    # Backup original
    if not backup_path.exists():
        shutil.copy2(str(input_path), str(backup_path))
        print(f"  Backup: {backup_path.name}")

    # Fix
    print("  Applying fixes...")
    fix_count = _apply_fixes(root, scan_issues)
    print(f"  Applied {fix_count} fixes.")
    print()

    # Validate
    print("  Validating...")
    validation_issues = _validate(root)
    if validation_issues:
        print(f"  {len(validation_issues)} validation issues remain:")
        for vi in validation_issues:
            print(f"    [{vi.rule_id}] {vi.message}")
    else:
        print("  All 23 validation checks passed.")
    print()

    # Write output
    print(f"  Writing: {output_path}")
    _write_fixed_xml(root, output_path)

    # Report
    if report:
        _generate_report(scan_issues, validation_issues, fix_count, input_path, output_path, report_path, root)
        print(f"  Report: {report_path}")

    # DRT output
    xml_written = output_path.exists()
    if drt:
        print()
        drt_path = output_dir / f"{stem}.drt"
        print("  DRT output requested. Checking DaVinci Resolve...")

        def _try_drt(resolve_obj: Any) -> bool:
            print("  DRT uses a temporary sandbox project to avoid")
            print("  touching your current project. It will briefly")
            print("  switch projects and restore afterward.")
            seq_name_drt = seq.findtext("name", "Imported") if seq is not None else "Imported"
            if not _drt_sandbox_export(resolve_obj, output_path, drt_path, seq_name_drt):
                return False
            if lumetri_data:
                _drt_supplement_lumetri(resolve_obj, lumetri_data)
            _recycle(output_path)
            print(f"  DRT: {drt_path}")
            print(f"     (intermediate XML moved to recycle bin)")
            return True

        resolve = _check_resolve_running()
        if resolve is not None:
            print("  DaVinci Resolve detected.")
            if _try_drt(resolve):
                xml_written = False
        elif not xml_written:
            print("  [X] DaVinci Resolve not detected, and XML output failed.")
            print("     DRT generation is not possible.")
        else:
            print("  [!] DaVinci Resolve not detected.")
            print("     XML was generated successfully (can be used as-is).")
            print("     DRT requires DaVinci Resolve Studio running.")
            print()
            print("      [A]uto-launch DaVinci  - start Resolve automatically")
            print("      [R]etry               - check again for DaVinci")
            print("      [L]eave               - continue without DRT (keep XML)")
            print()
            choice = input("  > ").strip().lower()
            if choice == "a":
                print("  Launching DaVinci Resolve...")
                print("  DaVinci is starting. This may take 10-30 seconds.")
                print("  After it finishes loading, it will automatically create")
                print("  a new project if none is open.")
                print()
                resolve = _ensure_resolve_running(timeout=60)
                if resolve is not None:
                    if _try_drt(resolve):
                        xml_written = False
                else:
                    print("  DaVinci did not become available. XML kept.")
            elif choice == "r":
                for attempt in range(1, 4):
                    print(f"  Checking DaVinci... (attempt {attempt}/3)")
                    resolve = _check_resolve_running()
                    if resolve is not None:
                        if _try_drt(resolve):
                            xml_written = False
                        break
                    if attempt < 3:
                        print("  Still not detected. Press Enter to retry, or type 'l' to leave.")
                        if input("  > ").strip().lower() == "l":
                            break
                else:
                    print("  Continuing without DRT. XML kept.")
            else:
                print("  Continuing without DRT. XML kept.")

    print()
    if xml_written:
        print(f"  Done. {fix_count} fixes applied to {output_path.name}")
    elif drt:
        print(f"  Done. Output: {drt_path.name}")
    return 0


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="pr2resolve",
        description="Fix Premiere Pro FCP7 XML for DaVinci Resolve compatibility.",
    )
    parser.add_argument("input", type=Path, help="Input file (.xml or .prproj)")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output directory")
    parser.add_argument("--report", action="store_true", help="Generate fix report (.md)")
    parser.add_argument("--drt", action="store_true", help="Generate DRT via DaVinci Scripting API")
    parser.add_argument("--drp", type=Path, default=None,
                        help="Generate DRP project package to specified path")
    parser.add_argument("--all-sequences", action="store_true",
                        help="Export all non-empty sequences (.prproj)")
    parser.add_argument("--nogui", action="store_true", dest="nogui",
                        help="Use headless DaVinci mode (default for DRT)")
    parser.add_argument("--gui", action="store_true", dest="gui",
                        help="Use GUI DaVinci mode (required for DRP)")
    parser.add_argument("--sequence", type=str, default=None, help="Sequence name (.prproj)")
    parser.add_argument("--diagnose-only", action="store_true", help="Diagnose only, no fixes")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args()


# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    """Main entry point."""
    args = _parse_args()
    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}")
        return 1
    return _run_pipeline(
        input_path=args.input,
        output_dir=args.output,
        report=args.report,
        diagnose_only=args.diagnose_only,
        sequence_name=args.sequence,
        drt=args.drt,
        drp_path=args.drp,
        all_sequences=args.all_sequences,
        nogui=args.nogui,
        gui=args.gui,
    )


if __name__ == "__main__":
    sys.exit(main())
