#!/usr/bin/env python3
r"""pr2resolve TUI — interactive menu launcher (like converter.sh).

Compiled via Nuitka into a single .exe. All work files stay in this directory.
Does NOT modify E:\pr2resolve.
"""

from __future__ import annotations

import os
import sys
import msvcrt
from pathlib import Path

# Let Nuitka find pr2resolve source at compile time.
# At runtime the modules are bundled into the exe so the path insert is harmless.
_PR2RESOLVE_SRC = r"E:\pr2resolve"
if _PR2RESOLVE_SRC not in sys.path:
    sys.path.insert(0, _PR2RESOLVE_SRC)

from pr2resolve import _run_pipeline, VERSION  # type: ignore[import-not-found]
from pr2resolve import main as pr2resolve_main  # type: ignore[import-not-found]

# ── ANSI Colors ──────────────────────────────────────────────────────────────────
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
DIM = "\033[0;90m"
BOLD = "\033[1m"
# Premiere Pro icon purple (RGB 140,69,255)
PR_PURPLE = "\033[38;2;140;69;255m"
NC = "\033[0m"  # reset

# ── State ────────────────────────────────────────────────────────────────────────
INPUT_FILE: str = ""
OUTPUT_DIR: str = ""
OPT_XML = True
OPT_DRT = False
OPT_DRP = "OFF"   # "OFF" | "BG" | "ON"
OPT_MODE = "AUTO"  # "AUTO" | "ALL" | "MAN"
OPT_SUFFIX = True
OPT_REPORT = False


# ═══════════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════════

def _clr(on: bool) -> str:
    """Return GREEN if on, else empty (for ON/OFF display)."""
    return GREEN if on else ""


def _clr_mode(mode: str) -> str:
    """Return YELLOW for non-default modes, else empty."""
    return YELLOW if mode != "AUTO" else ""


def _clr_drp(drp: str) -> str:
    """GREEN for ON, YELLOW for BG, else empty."""
    if drp == "ON":
        return GREEN
    if drp == "BG":
        return YELLOW
    return ""


def _onoff(on: bool) -> str:
    return "ON" if on else "OFF"


def _getch() -> str:
    """Read a single keypress (Windows only)."""
    ch = msvcrt.getch()
    # Decode bytes; handle special keys that return 2-byte sequences
    try:
        return ch.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return "?"


def _pause(msg: str = "Press Enter to return to menu...") -> None:
    print()
    print(f"  {msg}")
    while True:
        ch = _getch()
        if ch in ("\r", "\n"):
            break


def _strip_input(raw: str) -> str:
    """Strip surrounding quotes and carriage returns from user input."""
    s = raw.strip()
    s = s.strip('"').strip("'")
    s = s.rstrip("\r\n")
    return s


# ═══════════════════════════════════════════════════════════════════════════════════
# Screens
# ═══════════════════════════════════════════════════════════════════════════════════

def _print_header() -> None:
    os.system("cls")
    print()
    print(f"  {PR_PURPLE}{'=' * 60}{NC}")
    print(f"  {BOLD}{PR_PURPLE}  pr2resolve v{VERSION}{NC}")
    print(f"  {BOLD}{PR_PURPLE}  Premiere Pro to DaVinci Resolve Converter{NC}")
    print(f"  {PR_PURPLE}{'=' * 60}{NC}")
    print()
    print(f"  {DIM}{'-' * 60}{NC}")
    if INPUT_FILE:
        print(f"  [INPUT]  {GREEN}{INPUT_FILE}{NC}")
    else:
        print(f"  [INPUT]  {YELLOW}NOT SET{NC} - Please select first")
    if OUTPUT_DIR:
        print(f"  [OUTPUT] {GREEN}{OUTPUT_DIR}{NC}")
    else:
        print(f"  [OUTPUT] (same as input)")
    print()

    # Toggle states
    drp_label = (
        f"DaVinci DRP interactive {YELLOW}(all sequences){NC}"
        if OPT_DRP == "ON"
        else f"DaVinci DRP background export {YELLOW}(all sequences){NC}"
        if OPT_DRP == "BG"
        else "DaVinci DRP project export"
    )
    print(f"  XML:     {_clr(OPT_XML)}[{_onoff(OPT_XML)}]{NC}   FCP7 XML output")
    print(f"  DRT:     {_clr(OPT_DRT)}[{_onoff(OPT_DRT)}]{NC}  DaVinci DRT output (needs Resolve Studio)")
    print(f"  DRP:     {_clr_drp(OPT_DRP)}[{OPT_DRP}]{NC}  {drp_label}")
    print(f"  Mode:    {_clr_mode(OPT_MODE)}[{OPT_MODE}]{NC} Sequence: AUTO/ALL/MAN")
    print(f"  Suffix:  {_clr(OPT_SUFFIX)}[{_onoff(OPT_SUFFIX)}]{NC}   _pr2resolve name tag")
    print(f"  Report:  {_clr(OPT_REPORT)}[{_onoff(OPT_REPORT)}]{NC}   Fix report (.md)")
    print()
    print(f"  {DIM}{'-' * 60}{NC}")
    print()
    print(f"  {BOLD}[1]{NC} Select input file (.xml / .prproj)")
    print(f"  {BOLD}[2]{NC} Set output directory")
    print(f"  {BOLD}[3]{NC} Output options")
    print(f"  {BOLD}[4] START{NC}")
    print(f"  {BOLD}[0]{NC} Quit")
    print()
    print(f"  {DIM}{'-' * 60}{NC}")


def select_input() -> None:
    global INPUT_FILE
    print()
    print("  Enter path to input file (.xml or .prproj):")
    raw = input("  > ")
    path = _strip_input(raw)
    if not path:
        print("  No file specified.")
        _pause()
        return
    if not Path(path).is_file():
        print(f"  File not found: {path}")
        _pause()
        return
    INPUT_FILE = path


def set_output() -> None:
    global OUTPUT_DIR
    os.system("cls")
    print()
    print(f"  {PR_PURPLE}{'=' * 60}{NC}")
    print(f"  {BOLD}  Set Output Directory{NC}")
    print(f"  {PR_PURPLE}{'=' * 60}{NC}")
    print()
    if OUTPUT_DIR:
        print(f"  Current: {GREEN}{OUTPUT_DIR}{NC}")
    else:
        print("  Current: (same as input)")
    print()
    print("  [1] Keep current")
    print("  [2] E:\\tmp\\pr2resolve_pack\\output")
    print("  [3] Same as input file folder")
    print("  [4] Custom path")
    print()

    ch = _getch()
    if ch == "1":
        return
    elif ch == "2":
        out = Path(r"E:\tmp\pr2resolve_pack\output")
        out.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR = str(out)
        print(f"\n  {GREEN}Set to: {OUTPUT_DIR}{NC}")
        _pause()
    elif ch == "3":
        if INPUT_FILE:
            OUTPUT_DIR = str(Path(INPUT_FILE).parent)
            print(f"\n  {GREEN}Set to: {OUTPUT_DIR}{NC}")
        else:
            print(f"\n  {YELLOW}Please select input file first.{NC}")
        _pause()
    elif ch == "4":
        print()
        custom = input("  Path: ")
        custom = _strip_input(custom)
        if custom:
            Path(custom).mkdir(parents=True, exist_ok=True)
            OUTPUT_DIR = custom
            print(f"\n  {GREEN}Set to: {OUTPUT_DIR}{NC}")
        _pause()


def options_menu() -> None:
    global OPT_XML, OPT_DRT, OPT_DRP, OPT_MODE, OPT_SUFFIX, OPT_REPORT
    while True:
        os.system("cls")
        print()
        print(f"  {PR_PURPLE}{'=' * 60}{NC}")
        print(f"  {BOLD}  Output Options{NC}")
        print(f"  {PR_PURPLE}{'=' * 60}{NC}")
        print()
        print(f"  {BOLD}[1]{NC} FCP7 XML       {_clr(OPT_XML)}[{_onoff(OPT_XML)}]{NC}")
        print(f"  {BOLD}[2]{NC} DRT            {_clr(OPT_DRT)}[{_onoff(OPT_DRT)}]{NC}  (needs Resolve Studio)")
        print(f"  {BOLD}[3]{NC} DRP project    {_clr_drp(OPT_DRP)}[{OPT_DRP}]{NC}  OFF/BG(no GUI)/ON {YELLOW}- all sequences{NC}")
        print(f"  {BOLD}[4]{NC} Export Mode    {_clr_mode(OPT_MODE)}[{OPT_MODE}]{NC}  (AUTO/ALL/MAN)")
        print(f"  {BOLD}[5]{NC} Name suffix    {_clr(OPT_SUFFIX)}[{_onoff(OPT_SUFFIX)}]{NC}  _pr2resolve tag")
        print(f"  {BOLD}[6]{NC} Fix report     {_clr(OPT_REPORT)}[{_onoff(OPT_REPORT)}]{NC}")
        print(f"  {BOLD}[0]{NC} Back")
        print()

        ch = _getch()
        if ch == "1":
            OPT_XML = not OPT_XML
        elif ch == "2":
            OPT_DRT = not OPT_DRT
        elif ch == "3":
            if OPT_DRP == "OFF":
                OPT_DRP = "BG"
            elif OPT_DRP == "BG":
                OPT_DRP = "ON"
            else:
                OPT_DRP = "OFF"
        elif ch == "4":
            if OPT_MODE == "AUTO":
                OPT_MODE = "ALL"
            elif OPT_MODE == "ALL":
                OPT_MODE = "MAN"
            else:
                OPT_MODE = "AUTO"
        elif ch == "5":
            OPT_SUFFIX = not OPT_SUFFIX
        elif ch == "6":
            OPT_REPORT = not OPT_REPORT
        elif ch == "0":
            return


def run_pipeline() -> None:
    if not INPUT_FILE:
        print()
        print(f"  {RED}ERROR: No input file selected.{NC}")
        _pause()
        return

    print()
    print("  Running...")
    print()

    input_path = Path(INPUT_FILE)
    output_dir = Path(OUTPUT_DIR) if OUTPUT_DIR else None

    # Map TUI state → _run_pipeline parameters (mirrors converter.sh argument logic)
    drp_path = None
    drp_gui = None
    if OPT_DRP == "BG":
        drp_path = True   # signal: auto-compute path
    elif OPT_DRP == "ON":
        drp_gui = True   # signal: auto-compute path

    _run_pipeline(
        input_path=input_path,
        output_dir=output_dir,
        report=OPT_REPORT,
        drt=OPT_DRT,
        drp_path=drp_path,      # type: ignore[arg-type]
        drp_gui=drp_gui,        # type: ignore[arg-type]
        all_sequences=(OPT_MODE == "ALL"),
        no_suffix=(not OPT_SUFFIX),
        no_xml=(not OPT_XML),
    )

    print()
    _pause()


# ═══════════════════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # If CLI arguments provided, pass through to pr2resolve directly
    if len(sys.argv) > 1:
        sys.exit(pr2resolve_main())

    # Otherwise, enter interactive TUI
    while True:
        _print_header()
        ch = _getch()
        if ch == "1":
            select_input()
        elif ch == "2":
            set_output()
        elif ch == "3":
            options_menu()
        elif ch == "4":
            run_pipeline()
        elif ch == "0":
            print()
            print("  Goodbye.")
            break


if __name__ == "__main__":
    main()
