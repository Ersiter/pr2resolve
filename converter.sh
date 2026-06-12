#!/usr/bin/env bash
# ============================================================
# pr2resolve - macOS/Linux TUI Launcher
# ============================================================

VERSION="0.9.4"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="${SCRIPT_DIR}/pr2resolve.py"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
DIM='\033[0;90m'
BOLD='\033[1m'
# Premiere Pro icon purple (RGB 140,69,255)
PR_PURPLE='\033[38;2;140;69;255m'
NC='\033[0m'

# State
INPUT_FILE=""
OUTPUT_DIR=""
SEQ_NAME=""
OPT_DRT="[OFF]"
OPT_REPORT="[OFF]"
OPT_XML="[ON]"
OPT_ALL_SEQ="[OFF]"
OPT_MODE="[AUTO]"
OPT_SUFFIX="[OFF]"
OPT_DRP="[OFF]"

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
    echo -e "${RED}ERROR: pr2resolve.py not found in ${SCRIPT_DIR}${NC}"
    exit 1
fi

print_header() {
    clear
    echo ""
    echo -e "  ${PR_PURPLE}============================================================${NC}"
    echo -e "  ${BOLD}${PR_PURPLE}  pr2resolve v${VERSION}  -  PR XML to FCP7 XML Fixer${NC}"
    echo -e "  ${PR_PURPLE}============================================================${NC}"
    echo ""
    echo -e "  ${DIM}------------------------------------------------------------${NC}"
    if [ -n "$INPUT_FILE" ]; then
        echo -e "  [INPUT]  ${GREEN}${INPUT_FILE}${NC}"
    else
        echo -e "  [INPUT]  ${YELLOW}NOT SET${NC} - Please select first"
    fi
    if [ -n "$OUTPUT_DIR" ]; then
        echo -e "  [OUTPUT] ${GREEN}${OUTPUT_DIR}${NC}"
    else
        echo -e "  [OUTPUT] (same as input)"
    fi
    if [ -n "$SEQ_NAME" ]; then
        echo -e "  [SEQ]    ${GREEN}${SEQ_NAME}${NC}"
    else
        echo -e "  [SEQ]    (auto)"
    fi
    echo ""
    # Conditional color for toggle states
    xml_clr=$([ "$OPT_XML" = "[ON]" ] && echo "$GREEN" || echo "")
    drt_clr=$([ "$OPT_DRT" = "[ON]" ] && echo "$GREEN" || echo "")
    rpt_clr=$([ "$OPT_REPORT" = "[ON]" ] && echo "$GREEN" || echo "")
    all_seq_clr=$([ "$OPT_ALL_SEQ" = "[ON]" ] && echo "$GREEN" || echo "")
    suffix_clr=$([ "$OPT_SUFFIX" = "[ON]" ] && echo "$GREEN" || echo "")
    drp_clr=$([ "$OPT_DRP" = "[ON]" ] && echo "$GREEN" || ([ "$OPT_DRP" = "[BG]" ] && echo "$YELLOW" || echo ""))
    mode_clr=$([ "$OPT_MODE" != "[AUTO]" ] && echo "$YELLOW" || echo "")
    if [ "$OPT_DRP" = "[ON]" ]; then
        drp_label="DaVinci DRP interactive (needs Resolve GUI)"
    elif [ "$OPT_DRP" = "[BG]" ]; then
        drp_label="DaVinci DRP background export"
    else
        drp_label="DaVinci DRP project export (needs Resolve GUI)"
    fi
    echo -e "  XML:     ${xml_clr}${OPT_XML}${NC}   FCP7 XML output"
    echo -e "  DRT:     ${drt_clr}${OPT_DRT}${NC}  DaVinci DRT output (needs Resolve Studio)"
    echo -e "  DRP:     ${drp_clr}${OPT_DRP}${NC}  ${drp_label}"
    echo -e "  Mode:    ${mode_clr}${OPT_MODE}${NC} Sequence: AUTO/ALL/MAN"
    echo -e "  Suffix:  ${suffix_clr}${OPT_SUFFIX}${NC}   _pr2resolve name tag"
    echo -e "  Report:  ${rpt_clr}${OPT_REPORT}${NC}   Fix report (.md)"
    echo ""
    echo -e "  ${DIM}------------------------------------------------------------${NC}"
    echo ""
    echo -e "  ${BOLD}[1]${NC} Select input file (.xml / .prproj)"
    echo -e "  ${BOLD}[2]${NC} Set output directory"
    echo -e "  ${BOLD}[3]${NC} Output options"
    echo -e "  ${BOLD}[4] START${NC}"
    echo -e "  ${BOLD}[0]${NC} Quit"
    echo ""
    echo -e "  ${DIM}------------------------------------------------------------${NC}"
}

set_output() {
    clear
    echo ""
    echo -e "  ${PR_PURPLE}============================================================${NC}"
    echo -e "  ${BOLD}  Set Output Directory${NC}"
    echo -e "  ${PR_PURPLE}============================================================${NC}"
    echo ""
    if [ -n "$OUTPUT_DIR" ]; then
        echo -e "  Current: ${GREEN}${OUTPUT_DIR}${NC}"
    else
        echo "  Current: (same as input)"
    fi
    echo ""
    echo "  [1] Keep current"
    echo "  [2] Script folder /output"
    echo "  [3] Same as input file folder"
    echo "  [4] Custom path"
    echo ""
    read -n 1 -r -p "  > " opt
    echo ""

    case "$opt" in
        1) return ;;
        2)
            OUTPUT_DIR="$SCRIPT_DIR/output"
            mkdir -p "$OUTPUT_DIR"
            echo -e "  ${GREEN}Set to: $OUTPUT_DIR${NC}"
            sleep 1 ;;
        3)
            if [ -n "$INPUT_FILE" ]; then
                OUTPUT_DIR="$(dirname "$INPUT_FILE")"
                echo -e "  ${GREEN}Set to: $OUTPUT_DIR${NC}"
            else
                echo -e "  ${YELLOW}Please select input file first.${NC}"
            fi
            sleep 1 ;;
        4)
            read -r -p "  Path: " custom
            custom="${custom//\"/}"
            custom="${custom%%$'\r'}"
            if [ -n "$custom" ]; then
                mkdir -p "$custom" 2>/dev/null
                OUTPUT_DIR="$custom"
                echo -e "  ${GREEN}Set to: $OUTPUT_DIR${NC}"
            fi
            sleep 1 ;;
    esac
}

select_input() {
    echo ""
    echo "  Enter path to input file (.xml or .prproj):"
    read -r -p "  > " input
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

options_menu() {
    while true; do
        clear
        echo ""
        echo -e "  ${PR_PURPLE}============================================================${NC}"
        echo -e "  ${BOLD}  Output Options${NC}"
        echo -e "  ${PR_PURPLE}============================================================${NC}"
        echo ""
        xml_clr=$([ "$OPT_XML" = "[ON]" ] && echo "$GREEN" || echo "")
        drt_clr=$([ "$OPT_DRT" = "[ON]" ] && echo "$GREEN" || echo "")
        rpt_clr=$([ "$OPT_REPORT" = "[ON]" ] && echo "$GREEN" || echo "")
        all_seq_clr=$([ "$OPT_ALL_SEQ" = "[ON]" ] && echo "$GREEN" || echo "")
        suffix_clr=$([ "$OPT_SUFFIX" = "[ON]" ] && echo "$GREEN" || echo "")
        drp_clr=$([ "$OPT_DRP" = "[ON]" ] && echo "$GREEN" || ([ "$OPT_DRP" = "[BG]" ] && echo "$YELLOW" || echo ""))
        mode_clr=$([ "$OPT_MODE" != "[AUTO]" ] && echo "$YELLOW" || echo "")
        echo -e "  ${BOLD}[1]${NC} FCP7 XML       ${xml_clr}${OPT_XML}${NC}"
        echo -e "  ${BOLD}[2]${NC} DRT            ${drt_clr}${OPT_DRT}${NC}  (needs DaVinci Resolve Studio)"
        echo -e "  ${BOLD}[3]${NC} Export Mode    ${mode_clr}${OPT_MODE}${NC}  (AUTO/ALL/MAN)"
        echo -e "  ${BOLD}[4]${NC} Fix report     ${rpt_clr}${OPT_REPORT}${NC}"
        echo -e "  ${BOLD}[5]${NC} Name suffix    ${suffix_clr}${OPT_SUFFIX}${NC}  _pr2resolve tag"
        echo -e "  ${BOLD}[6]${NC} DRP project    ${drp_clr}${OPT_DRP}${NC}  OFF/BG(no GUI)/ON(needs GUI)"
        echo -e "  ${BOLD}[0]${NC} Back"
        echo ""
        read -n 1 -r -p "  Select [1-6, 0]: " choice
        echo ""
        choice="${choice%%$'\r'}"
        case "$choice" in
            1) if [ "$OPT_XML" = "[ON]" ]; then OPT_XML="[OFF]"; else OPT_XML="[ON]"; fi ;;
            2) if [ "$OPT_DRT" = "[ON]" ]; then OPT_DRT="[OFF]"; else OPT_DRT="[ON]"; fi ;;
            3) case "$OPT_MODE" in
                   "[AUTO]") OPT_MODE="[ALL]" ;;
                   "[ALL]") OPT_MODE="[MAN]" ;;
                   "[MAN]") OPT_MODE="[AUTO]" ;;
               esac ;;
            4) if [ "$OPT_REPORT" = "[ON]" ]; then OPT_REPORT="[OFF]"; else OPT_REPORT="[ON]"; fi ;;
            5) if [ "$OPT_SUFFIX" = "[ON]" ]; then OPT_SUFFIX="[OFF]"; else OPT_SUFFIX="[ON]"; fi ;;
            6) case "$OPT_DRP" in
                   "[OFF]") OPT_DRP="[BG]" ;;
                   "[BG]") OPT_DRP="[ON]" ;;
                   "[ON]") OPT_DRP="[OFF]" ;;
               esac ;;
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
    if [ "$OPT_MODE" = "[ALL]" ]; then
        cmd+=(--all-sequences)
    fi
    if [ "$OPT_SUFFIX" = "[OFF]" ]; then
        cmd+=(--no-suffix)
    fi
    if [ "$OPT_XML" = "[OFF]" ]; then
        cmd+=(--no-xml)
    fi
    if [ "$OPT_DRP" = "[BG]" ]; then
        cmd+=(--drp)
    elif [ "$OPT_DRP" = "[ON]" ]; then
        cmd+=(--drp-gui)
    fi

    PYTHONIOENCODING=utf-8 "${cmd[@]}"

    echo ""
    echo "  Press Enter to return to menu..."
    read -r
}

# Main loop
while true; do
    print_header
    read -n 1 -r -p "  Select [1-4, 0]: " choice
    echo ""
    choice="${choice%%$'\r'}"
    case "$choice" in
        1) select_input ;;
        2) set_output ;;
        3) options_menu ;;
        4) run_pipeline ;;
        0) echo ""; echo "  Goodbye."; exit 0 ;;
    esac
done
