#!/usr/bin/env bash
# ============================================================
# prxml2fcp7xml - macOS/Linux TUI Launcher
# ============================================================

VERSION="1.0.0"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="${SCRIPT_DIR}/prxml_to_fcp7xml.py"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# State
INPUT_FILE=""
OUTPUT_DIR=""
SEQ_NAME=""
OPT_DRT="[OFF]"
OPT_REPORT="[ON]"

# Find Python
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON_CMD="$cmd"
        break
    fi
done
if [ -z "$PYTHON_CMD" ]; then
    echo -e "${RED}ERROR: Python not found. Please install Python 3.8+${NC}"
    exit 1
fi

# Check script
if [ ! -f "$SCRIPT" ]; then
    echo -e "${RED}ERROR: prxml_to_fcp7xml.py not found in ${SCRIPT_DIR}${NC}"
    exit 1
fi

print_header() {
    clear
    echo "============================================================"
    echo "  prxml2fcp7xml v${VERSION}  —  PR XML to FCP7 XML Fixer"
    echo "============================================================"
    echo ""
    if [ -n "$INPUT_FILE" ]; then
        echo -e "  [INPUT]  ${CYAN}${INPUT_FILE}${NC}"
    else
        echo -e "  [INPUT]  ${YELLOW}NOT SET - Please select first${NC}"
    fi
    if [ -n "$OUTPUT_DIR" ]; then
        echo "  [OUTPUT] ${OUTPUT_DIR}"
    else
        echo "  [OUTPUT] (same as input)"
    fi
    if [ -n "$SEQ_NAME" ]; then
        echo "  [SEQ]    ${SEQ_NAME}"
    else
        echo "  [SEQ]    (auto)"
    fi
    echo ""
    echo -e "  XML:     ${GREEN}[ON]${NC}    FCP7 XML output (always on)"
    echo -e "  DRT:     ${OPT_DRT}  DaVinci DRT output (needs Resolve Studio)"
    echo -e "  Report:  ${OPT_REPORT}   Fix report (.md)"
    echo ""
    echo "------------------------------------------------------------"
    echo ""
    echo "  [1] Select input file (.xml / .prproj)"
    echo "  [2] Set output directory"
    echo "  [3] Output options"
    echo "  [4] START"
    echo "  [0] Quit"
    echo ""
    echo "------------------------------------------------------------"
}

select_input() {
    echo ""
    echo "  Enter path to input file (.xml or .prproj):"
    read -r -p "  > " input
    # Strip quotes and trailing \r (Git Bash on Windows)
    input="${input%\"}"
    input="${input#\"}"
    input="${input%%$'\r'}"
    if [ -z "$input" ]; then
        echo "  No file specified."
        sleep 1
        return
    fi
    if [ ! -f "$input" ]; then
        echo "  File not found: $input"
        sleep 1
        return
    fi
    INPUT_FILE="$input"
}

set_output() {
    echo ""
    echo "  Enter output directory (or press Enter for same as input):"
    read -r -p "  > " dir
    dir="${dir%\"}"
    dir="${dir#\"}"
    dir="${dir%%$'\r'}"
    if [ -z "$dir" ]; then
        OUTPUT_DIR=""
        echo "  Output will be in the same directory as input."
    else
        mkdir -p "$dir" 2>/dev/null
        OUTPUT_DIR="$dir"
    fi
    sleep 1
}

options_menu() {
    while true; do
        clear
        echo "============================================================"
        echo "  Output Options"
        echo "============================================================"
        echo ""
        echo -e "  [1] FCP7 XML       ${GREEN}[ON]${NC}    (always on, primary output)"
        echo -e "  [2] DRT            ${OPT_DRT}   (optional, needs DaVinci Resolve Studio)"
        echo -e "  [3] Fix report     ${OPT_REPORT}"
        echo "  [0] Back"
        echo ""
        read -r -p "  Select [1-3, 0]: " choice
        choice="${choice%%$'\r'}"
        case "$choice" in
            1) ;; # Always on
            2) if [ "$OPT_DRT" = "[ON]" ]; then OPT_DRT="[OFF]"; else OPT_DRT="[ON]"; fi ;;
            3) if [ "$OPT_REPORT" = "[ON]" ]; then OPT_REPORT="[OFF]"; else OPT_REPORT="[ON]"; fi ;;
            0) return ;;
        esac
    done
}

run_pipeline() {
    if [ -z "$INPUT_FILE" ]; then
        echo ""
        echo -e "  ${RED}ERROR: No input file selected.${NC}"
        sleep 1
        return
    fi

    echo ""
    echo "  Running..."
    echo ""

    # Build command
    cmd=("$PYTHON_CMD" "$SCRIPT" "$INPUT_FILE")

    if [ -n "$OUTPUT_DIR" ]; then
        cmd+=(-o "$OUTPUT_DIR")
    fi
    if [ -n "$SEQ_NAME" ]; then
        cmd+=(--sequence "$SEQ_NAME")
    fi
    if [ "$OPT_REPORT" = "[ON]" ]; then
        cmd+=(--report)
    fi
    if [ "$OPT_DRT" = "[ON]" ]; then
        cmd+=(--drt)
    fi

    PYTHONIOENCODING=utf-8 "${cmd[@]}"

    echo ""
    echo "  Press Enter to return to menu..."
    read -r
}

# Main loop
while true; do
    print_header
    read -r -p "  Select [1-4, 0]: " choice
    choice="${choice%%$'\r'}"
    case "$choice" in
        1) select_input ;;
        2) set_output ;;
        3) options_menu ;;
        4) run_pipeline ;;
        0) echo ""; echo "  Goodbye."; exit 0 ;;
    esac
done
