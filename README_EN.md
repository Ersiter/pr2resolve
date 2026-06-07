# prxml2fcp7xml

Convert Premiere Pro project files into DaVinci Resolve-compatible FCP7 XML format. Optional DRT output for full color grading data preservation.

[**Chinese Version**](README.md)

---

## Have You Encountered This

Premiere Pro's FCP7 XML export breaks in DaVinci Resolve: clips are scaled wrong, color grades vanish, and media goes offline. This is not user error -- PR's XML export is fundamentally flawed.

**Scale to Frame Size disappears.** You set clips to fit the frame in PR, but the XML exports Scale=100%. In DaVinci, clips render at their original size -- often 2-3x larger than expected. You have to manually calculate the corrected scale for every clip.

**Lumetri color grades are discarded.** Your carefully tuned color work becomes a massive base64-encoded blob in the XML. DaVinci has no Lumetri plugin -- it silently ignores the entire block. Your grading work is lost, and you must start over from scratch.

**pathurl format breaks media linking.** PR on Windows exports `file://localhost/C%3a/Users/...`. DaVinci doesn't recognize this format -- all media shows as offline, and you must relink everything manually.

This tool automates fixes for all of these problems.

---

## What It Does

- **Fixes Scale mismatches** -- Auto-detects source/timeline resolution differences and computes the correct fit scale value
- **Cleans Lumetri noise** -- FCP7 XML path: removes meaningless Lumetri blocks to reduce file size. DRT path: maps Lumetri parameters to DaVinci native Color nodes
- **Fixes path formatting** -- Converts all pathurl values to standard `file:///` format
- **Fills missing elements** -- Auto-generates missing `<format>`, `<ntsc>`, `<sourcetrack>`, `<masterclipid>`, and other elements DaVinci requires
- **Dual entry points** -- Accepts PR-exported FCP7 XML, but recommends `.prproj` native project files for more complete data
- **Dual output paths** -- FCP7 XML always available with zero dependencies; DRT via DaVinci Scripting API for maximum data fidelity

---

## Why Use .prproj Instead of PR XML Export

PR's built-in FCP7 XML export is "second-hand data" -- PR generates a simplified XML that has already lost information. The `.prproj` file is PR's native project format (gzip-compressed XML), containing the most complete set of Lumetri parameters, speed curves, transform keyframes, and more.

This tool parses `.prproj` directly, extracting the full timeline data from the source. **If you have the .prproj file, give it to this tool directly -- do not export from PR first.**

---

## The Value of DRT Output

FCP7 XML is an interchange format with a hard ceiling -- it can only express what FCP7 defines. DaVinci's Color node tree, full transform keyframes, optical flow speed algorithms, etc., simply cannot be expressed in XML.

DRT (DaVinci Resolve Timeline) is DaVinci's native format. It can do what XML cannot: Lumetri parameters map directly to DaVinci Color Corrector nodes, Scale/Fit strategies are precisely preserved, and speed algorithms transfer intact.

**DRT output requires DaVinci Resolve Studio running.** Workflow:
1. Open DaVinci Resolve Studio
2. Enable the DRT option in this tool
3. The tool auto-imports the fixed XML via Scripting API
4. Lumetri Color node data is automatically supplemented
5. The .drt file is exported

Without DaVinci running, DRT gracefully degrades -- XML is still generated normally.

---

## Quick Start

### Windows

Double-click `converter.bat` to launch the TUI.

```
1. Double-click converter.bat
2. Press 1 to select an input file (.xml or .prproj)
3. Press 2 to set output directory (or press Enter to use the same directory)
4. Press 3 to configure export options (XML / DRT / Report)
5. Press 4 to start conversion
```

### macOS / Linux

```bash
chmod +x converter.sh
./converter.sh
```

### CLI (All Platforms)

```bash
# PR XML -- fixed XML
python prxml_to_fcp7xml.py "input.xml"

# .prproj -- output XML directly (recommended)
python prxml_to_fcp7xml.py "project.prproj" -o ./output

# .prproj with specific sequence
python prxml_to_fcp7xml.py "project.prproj" --sequence "Sequence 01"

# DRT output (requires DaVinci Resolve Studio running)
python prxml_to_fcp7xml.py "input.xml" --drt

# Generate fix report
python prxml_to_fcp7xml.py "input.xml" --report

# Diagnose only, no fixes
python prxml_to_fcp7xml.py "input.xml" --diagnose-only
```

---

## How It Works

```
Input: PR FCP7 XML (.xml) or PR project (.prproj)
    |
    +-- Entry A: FCP7 XML parsing (xml.etree.ElementTree)
    |   -- structured semantic parsing, not line-by-line regex
    |
    +-- Entry B: .prproj parsing (gzip -> ObjectID graph traversal)
    |   -- Sequence -> TrackGroup -> TrackItem -> SubClip -> MasterClip
    |
    v
Unified Diagnostics Engine -- scans for 23 known issues, generates Issue[]
    |
    v
Fix Engine -- auto-repairs by C(ritical) -> M(ajor) -> N(ormal) priority
    |
    v
Validator -- 23 FCP7 specification compliance checks
    |
    v
Output:
    +-- output.xml   -- corrected FCP7 XML (always output)
    +-- output.md    -- fix report (optional)
    +-- output.drt   -- DaVinci native timeline (optional, requires DaVinci running)
```

---

## Fix Rules Reference

| Level | Rule | Description |
|-------|------|-------------|
| **C0** | version | `xmeml version="4"` -> `"5"` |
| **C1-C2** | format | Fill missing video/audio `<format>` |
| **C3-C4** | rate | Fill missing `<ntsc>` / `<timebase>` |
| **C5** | pathurl | `file://localhost/...` -> `file:///...` |
| **C6** | media order | Move video before audio |
| **M0** | Lumetri | XML path: remove. DRT path: map to Color nodes |
| **M1-M2** | clipid/track | Fill missing `<masterclipid>` / `<sourcetrack>` |
| **M7** | Scale | Source resolution / timeline resolution = correct fit scale |
| **N1-N7** | details | timecode / float precision / frame rate consistency |

---

## Requirements

| Item | Requirement |
|------|-------------|
| Python | 3.8 or newer |
| Operating System | Windows 10+, macOS 10.15+, Linux |
| External Dependencies | None (Python standard library only) |
| DRT Output | DaVinci Resolve Studio (free version has no Scripting API) |

---

## Known Limitations

1. **Text/Generator clips** -- PR generatoritems (titles, text) often display as empty in DaVinci. This is a limitation of the FCP7 XML format itself.
2. **Nested sequences** -- PR nested sequences are often flattened or fail during FCP7 XML import into DaVinci.
3. **Media paths** -- XML references absolute paths. If media is moved, use DaVinci's Relink feature.
4. **DaVinci double scaling** -- When importing, uncheck "Use sizing information" to prevent DaVinci from applying an additional scale.
5. **DRT requires DaVinci Studio** -- The free version has no Scripting API; DRT is unavailable. XML is unaffected.

---

## FAQ

### Q: "Python not found"

Install Python 3.8+ and ensure it is added to system PATH.
- Windows: https://www.python.org/downloads/ -> check "Add Python to PATH"
- macOS: `brew install python3`
- Linux: `sudo apt install python3`

### Q: Media offline after import into DaVinci

XML references absolute paths. If media has moved, use DaVinci's Relink feature to locate it.

### Q: Clips look wrong (wrong scale) after import

The fixed XML from this tool should have already corrected the Scale values. If it still looks wrong, check DaVinci import settings -- set Image Scaling to "Center crop with no resizing".

### Q: Should I use .prproj or PR-exported XML

**Use .prproj.** Unless your PR version doesn't support saving projects (it does -- .prproj is PR's save format), there's no reason to export XML first and then fix it. Give .prproj directly to this tool.

### Q: What is DRT and when should I use it

DRT does what XML cannot: Lumetri grading mapped to DaVinci Color nodes. If you did significant color work in PR, .prproj + DRT preserves the most data. But DRT requires DaVinci Resolve Studio running.

### Q: Can Lumetri color grades be perfectly restored

FCP7 XML path: No -- Lumetri is a PR-proprietary effect and is removed from XML. DRT path: Basic parameters (Exposure, Contrast, Highlights, Shadows, Temperature, etc.) can be mapped to DaVinci Color nodes. Creative LUTs can be extracted as .cube files. Compound effects like Vignette and Sharpen are approximate at best.

---

## License

[MIT License](LICENSE)

---

## Project Structure

```
pr2drt/
├── prxml_to_fcp7xml.py    # Core CLI tool (parse / diagnose / fix / validate / DRT)
├── converter.bat           # Windows TUI
├── converter.sh            # macOS/Linux TUI
├── tests/
│   └── test_validator.py   # 18 unit tests
├── README.md               # Chinese documentation
├── README_EN.md            # English documentation
└── LICENSE
```
