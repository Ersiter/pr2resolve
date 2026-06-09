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
    OUTPUT_SUFFIX,
    FCP7_VERSION, FCP7_DOCTYPE, CRITICAL, MAJOR, MINOR,
    FCP7_CLIPIITEM_ORDER, ScaleIssue, Issue,
    _build_file_index, _get_sequence_format, _get_sequence_resolution,
    load_xml, load_prproj,
)

# ── Engine (consolidated: diagnostics + fix + validate + output + prproj + drt) ─
from pr2_engine import (
    _scan, _apply_fixes, _validate,
    _write_fixed_xml, _generate_report, _make_output_name,
    _PrprojIndex, _prproj_parse_sequence, _prproj_list_sequences,
    _prproj_extract_all_lumetri,
    _check_resolve_running, _ensure_resolve_running,
    _drt_sandbox_export, _drt_supplement_lumetri,
    _drt_batch_export, _drp_export, _recycle,
    _shutdown_resolve,
)

# ── Recycle utility (also lives in pr2_engine, re-export for convenience) ──


# ═══════════════════════════════════════════════════════════════════════════════
# Interactive Mode — cross-platform key input
# ═══════════════════════════════════════════════════════════════════════════════

def _get_key() -> str:
    """Read a single keypress cross-platform.

    Returns:
        'UP', 'DOWN', 'SPACE', 'ENTER', 'A', 'a', 'ESC', or the raw character
    """
    if sys.platform == "win32":
        import msvcrt
        ch = msvcrt.getch()
        if ch == b"\xe0":
            ch2 = msvcrt.getch()
            if ch2 == b"H":
                return "UP"
            elif ch2 == b"P":
                return "DOWN"
            elif ch2 == b"K":
                return "LEFT"
            elif ch2 == b"M":
                return "RIGHT"
            return f"\\xe0{ch2[0]}"
        if ch == b"\r":
            return "ENTER"
        if ch == b" ":
            return "SPACE"
        if ch == b"\x1b":
            return "ESC"
        try:
            return ch.decode("utf-8", errors="replace")
        except Exception:
            return "?"
    else:
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                # Check for escape sequence
                extra = sys.stdin.read(2)
                if extra == "[A":
                    return "UP"
                elif extra == "[B":
                    return "DOWN"
                elif extra == "[C":
                    return "RIGHT"
                elif extra == "[D":
                    return "LEFT"
                else:
                    return "ESC"
            if ch == "\r" or ch == "\n":
                return "ENTER"
            if ch == " ":
                return "SPACE"
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _interactive_select(sequences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Interactive multi-sequence selection with arrow-key TUI.

    Keys:
      UP/DOWN  — move cursor
      SPACE    — toggle checkbox
      A        — select all / deselect all
      ENTER    — confirm selection

    Args:
        sequences: List of sequence dicts (name, width, height, clip_count, uid)

    Returns:
        Subset of sequences that the user selected
    """
    if not sequences:
        return []

    n = len(sequences)
    cursor = 0
    selected: set[int] = set()

    def _render() -> None:
        """Clear screen and redraw the selection UI."""
        # Move cursor to top and clear
        sys.stdout.write("\x1b[H\x1b[J")
        print("=" * 56)
        print("  Select sequences to export (SPACE=toggle, A=all, ENTER=done)")
        print("=" * 56)
        print()
        for i, s in enumerate(sequences):
            mark = "[X]" if i in selected else "[ ]"
            cur = " >" if i == cursor else "  "
            name = s["name"][:40]
            info = f"{s['width']}x{s['height']}  {s['clip_count']} clips"
            print(f"{cur} {mark} {i+1:2d}. {name:<42s} {info}")
        print()
        sel_count = len(selected)
        print(f"  Selected: {sel_count}/{n}  |  ENTER to confirm  |  ESC to cancel")
        sys.stdout.flush()

    while True:
        _render()
        key = _get_key()

        if key == "UP":
            cursor = (cursor - 1) % n
        elif key == "DOWN":
            cursor = (cursor + 1) % n
        elif key == "SPACE":
            if cursor in selected:
                selected.discard(cursor)
            else:
                selected.add(cursor)
        elif key in ("A", "a"):
            if len(selected) == n:
                selected.clear()
            else:
                selected = set(range(n))
        elif key == "ENTER":
            if selected:
                break
            # If nothing selected, select the current item
            selected = {cursor}
            break
        elif key == "ESC":
            selected.clear()
            break

    # Move cursor below the UI
    print()
    if not selected:
        print("  Cancelled. No sequences selected.")
        return []

    result = [sequences[i] for i in sorted(selected)]
    print(f"  Exporting {len(result)} sequence(s):")
    for s in result:
        print(f"    - {s['name'][:50]}")
    print()
    return result


def _choose_export_mode(sequences: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Choose export mode for multiple sequences.

    When .prproj has >1 non-empty sequences, offers three modes:
      [1] Auto (recommended) — use max(clip_count) heuristic
      [2] All sequences    — batch export all
      [3] Manual selection — interactive checkbox UI

    Args:
        sequences: Non-empty sequence dicts

    Returns:
        (selected_sequences, mode_name) where mode_name is 'auto', 'all', or 'manual'
    """
    n = len(sequences)
    if n <= 1:
        return [sequences[0]], "auto"

    # Check if stdin is a TTY (interactive terminal)
    if not sys.stdin.isatty():
        # Non-interactive mode: auto-select
        best = max(sequences, key=lambda s: s["clip_count"])
        print(f"  Non-interactive mode. Auto-selected: {best['name']}")
        return [best], "auto"

    print()
    print(f"  Found {n} non-empty sequences in project.")
    print(f"  How would you like to export?")
    print()
    print(f"    [1] Auto (recommended)    — smart pick: \"{max(sequences, key=lambda s: s['clip_count'])['name'][:40]}\"")
    print(f"    [2] All sequences         — export all {n}")
    print(f"    [3] Manual selection      — pick which ones")
    print()

    while True:
        try:
            choice = input("  Select [1-3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "1"

        if choice == "1":
            best = max(sequences, key=lambda s: s["clip_count"])
            print(f"  Auto mode: \"{best['name'][:50]}\"")
            return [best], "auto"
        elif choice == "2":
            print(f"  Batch mode: all {n} sequences")
            return sequences, "all"
        elif choice == "3":
            result = _interactive_select(sequences)
            if result:
                return result, "manual"
            # User cancelled — fall back to auto
            best = max(sequences, key=lambda s: s["clip_count"])
            print(f"  Falling back to auto: \"{best['name'][:50]}\"")
            return [best], "auto"


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
    no_suffix: bool = False,
    no_xml: bool = False,
) -> int:
    """Run the full fix pipeline on an input file."""
    _print_banner()

    if output_dir is None:
        output_dir = input_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = input_path.stem
    # output_path / report_path deferred — computed from sequence name after parsing
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
                    selected_seqs = [matching[0]]
                    export_mode = "named"
                else:
                    print(f"  Error: Sequence '{sequence_name}' not found")
                    print(f"  Available: {[s['name'] for s in sequences]}")
                    return 1
            elif len(non_empty) == 0:
                print("  Error: No non-empty sequences found in .prproj")
                return 1
            elif len(non_empty) == 1:
                selected_seqs = [non_empty[0]]
                export_mode = "auto"
            elif all_sequences:
                selected_seqs = non_empty
                export_mode = "all"
            else:
                # Interactive mode selection
                selected_seqs, export_mode = _choose_export_mode(non_empty)

            selected = selected_seqs[0]  # primary sequence for display/single-mode
            seq_name = selected["name"]

            # ─ Batch mode (all or manual with >1 selection) ────────
            if len(selected_seqs) > 1:
                print(f"  Batch mode ({export_mode}): exporting {len(selected_seqs)} sequences")
                xml_paths: list[Path] = []
                seq_names: list[str] = []
                for s in selected_seqs:
                    fcp = _prproj_parse_sequence(prproj_root, s["uid"], input_path)
                    tmp_xml = output_dir / _make_output_name(s["name"], add_suffix=not no_suffix)
                    # Scan + fix + write (single write after fix pass)
                    issues = _scan(fcp)
                    _apply_fixes(fcp, issues)
                    _write_fixed_xml(fcp, tmp_xml)
                    xml_paths.append(tmp_xml)
                    seq_names.append(s["name"])
                    print(f"    {s['name'][:30]}: {s['clip_count']} clips, {len(issues)} issues")

                    # Per-sequence report
                    if report:
                        rpt_path = output_dir / f"{Path(_make_output_name(s['name'], add_suffix=not no_suffix)).stem}_fix_report.md"
                        _generate_report(issues, _validate(fcp), len([i for i in issues if i.fixed]),
                                         input_path, tmp_xml, rpt_path, fcp)

                if drt:
                    resolve = _ensure_resolve_running(timeout=60, nogui=not gui)
                    if resolve:
                        results = _drt_batch_export(resolve, xml_paths, output_dir, seq_names)
                        ok = sum(1 for r in results if r[0])
                        print(f"  Batch DRT: {ok}/{len(results)} exported")
                    else:
                        print("  DRT skipped: DaVinci not available")

                # DRP export (batch mode)
                if drp_path:
                    print(f"  DRP export: {drp_path}")
                    if nogui:
                        print("  (DRP requires GUI — auto-enabling)")
                        nogui = False
                    resolve = _ensure_resolve_running(timeout=60, nogui=False)
                    if resolve:
                        _drp_export(resolve, xml_paths, drp_path, stem, seq_names)
                    else:
                        print("  DRP skipped: DaVinci not available")

                # Cleanup temp XMLs only when DRT consumed them
                if drt:
                    for tmp in xml_paths:
                        tmp.unlink(missing_ok=True)
                _shutdown_resolve()
                return 0
            # ─ End batch mode ──────────────────────────────────

            print("  Converting .prproj to FCP7 XML...")
            root = _prproj_parse_sequence(prproj_root, selected["uid"], input_path)
            print("  Conversion complete.")
            print()

            lumetri_data = _prproj_extract_all_lumetri(prproj_root, selected["uid"])
            if lumetri_data:
                print(f"  Lumetri params: {sum(len(v) for v in lumetri_data.values())} across {len(lumetri_data)} clips")
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

    # Build output filename from sequence name
    output_name = _make_output_name(seq_name, add_suffix=not no_suffix)
    output_path = output_dir / output_name
    report_path = output_dir / f"{Path(output_name).stem}_fix_report.md"

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

    # Write output — skip when --no-xml (DRT still needs the file for import)
    xml_written = False
    if not no_xml or drt:
        print(f"  Writing: {output_path}")
        _write_fixed_xml(root, output_path)
        xml_written = output_path.exists()

    # Report
    if report and not no_xml:
        _generate_report(scan_issues, validation_issues, fix_count, input_path, output_path, report_path, root)
        print(f"  Report: {report_path}")

    # DRP output (single-sequence mode)
    if drp_path:
        print()
        print(f"  DRP export: {drp_path}")
        if nogui:
            print("  (DRP requires GUI — auto-enabling)")
            nogui = False
        resolve_drp = _ensure_resolve_running(timeout=60, nogui=False)
        if resolve_drp:
            _drp_export(resolve_drp, [output_path], drp_path, stem, [seq_name])
        else:
            print("  DRP skipped: DaVinci not available")

    # DRT output
    if drt:
        print()
        drt_path = output_dir / f"{Path(output_name).stem}.drt"
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
    else:
        print(f"  Done. {fix_count} fixes (no file written).")

    # Clean up headless Resolve if we launched it
    if drt or drp_path:
        _shutdown_resolve()

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
    parser.add_argument("--no-suffix", action="store_true", dest="no_suffix",
                        help=f"Omit '{OUTPUT_SUFFIX}' suffix from output filename")
    parser.add_argument("--no-xml", action="store_true", dest="no_xml",
                        help="Skip FCP7 XML output (diagnose only, or use with --drt)")
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
        no_suffix=args.no_suffix,
        no_xml=args.no_xml,
    )


if __name__ == "__main__":
    sys.exit(main())
