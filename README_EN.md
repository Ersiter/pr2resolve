<div align="center">

# pr2resolve

Convert Premiere Pro timelines to DaVinci Resolve compatible formats. Dual output: FCP7 XML and DRT.

[**Chinese Version**](README.md)

</div>

---

## Features

- **Dual entry** — Accepts PR-exported FCP7 XML (.xml) and native PR projects (.prproj). Use .prproj for most complete data.
- **Dual output** — FCP7 XML with zero dependencies always available. DRT via DaVinci Scripting API preserves Lumetri grades, speed curves, and other data XML cannot carry.
- **Scale auto-fix** — Detects source vs timeline resolution mismatch, computes and writes correct fit scale.
- **Lumetri dual strategy** — XML path removes Lumetri blocks DaVinci ignores. DRT path maps color params to native Color Corrector nodes.
- **Path normalization** — Converts `file://localhost/...` to `file:///...`.
- **Element completion** — Auto-generates `<format>`, `<ntsc>`, `<sourcetrack>`, `<masterclipid>`, `<link>`, and other elements DaVinci requires.
- **Multi-track support** — Video tracks, audio tracks, clip trimming (in/out), speed changes (PlaybackSpeed).
- **Interactive TUI** — Windows (.bat) and macOS / Linux (.sh) menu-driven interface.

---

## Requirements

| Item | Requirement |
|------|-------------|
| Python | **3.8** or newer |
| Operating System | Windows 10+, macOS 10.15+, Linux |
| Dependencies | None (Python standard library only) |
| DRT Output | DaVinci Resolve Studio (free version lacks Scripting API) |

---

## Quick Start

### Windows

Double-click `converter.bat` to launch the TUI.

```
1. Double-click converter.bat
2. Press 1 to select an input file (.xml or .prproj)
3. Press 2 to set output directory (four modes available)
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
# PR XML — fixed output
python pr2resolve.py "input.xml"

# .prproj — parse directly (recommended)
python pr2resolve.py "project.prproj" -o ./output

# .prproj with specific sequence
python pr2resolve.py "project.prproj" --sequence "Sequence 01"

# DRT output (requires DaVinci Resolve Studio running)
python pr2resolve.py "input.xml" --drt

# Generate fix report
python pr2resolve.py "input.xml" --report

# Diagnose only, no fixes
python pr2resolve.py "input.xml" --diagnose-only
```

### Import into DaVinci

```
DaVinci Resolve -> File -> Import Timeline -> Import AAF, EDL, XML...
-> Select the generated .xml file

DRT: File -> Import Timeline -> Import DRT...
```

---

## How It Works

```
Input (.xml or .prproj)
    |
    +-- .xml -> ElementTree structured parse
    +-- .prproj -> gzip decompress -> ObjectID graph traversal
    |
    v
Diagnostics — scans 21 known issues (C0-C6, M0-M7, N1-N7)
    |
    v
Fix engine — auto-repairs by Critical -> Major -> Normal priority
    |
    v
Validator — 23 FCP7 specification compliance checks
    |
    v
Output:
    +-- output.xml  -- corrected FCP7 XML (always)
    +-- output.md   -- fix report (optional)
    +-- output.drt  -- DaVinci native timeline (optional, requires DaVinci running)
```

---

## Fix Rules

| Level | Rule | Description |
|-------|------|-------------|
| C0 | version | `xmeml version="4"` -> `"5"` |
| C1-C2 | format | Fill missing video/audio `<format>` |
| C3-C4 | rate | Fill missing `<ntsc>` / `<timebase>` |
| C5 | pathurl | `file://localhost/...` -> `file:///...` |
| C6 | media order | Move video before audio |
| M0 | Lumetri | XML path: remove. DRT path: map to Color nodes |
| M1-M2 | clipid/track | Fill `<masterclipid>` / `<sourcetrack>` |
| M4 | link | Auto-generate `<link>` for same-source clips |
| M5 | file details | Fill missing samplecharacteristics in `<file>` |
| M6 | element order | Sort clipitem children per FCP7 spec |
| M7 | Scale | Source res / timeline res = fit scale |
| N1-N7 | details | timecode / float precision / rate consistency / displayformat |

---

## Known Limitations

1. **Text titles** — PR generatoritems often display as blank in DaVinci (FCP7 XML format limitation)
2. **Nested sequences** — Often flattened or fail during FCP7 XML import
3. **Media paths** — XML references absolute paths. Move media? Relink in DaVinci.
4. **Double scaling** — Uncheck "Use sizing information" when importing to avoid extra scaling
5. **Free DaVinci** — Scripting API is Studio-only; DRT unavailable. XML unaffected.
6. **Lumetri mapping** — DRT path: basic params (Exposure, Contrast, Highlights, Shadows, Temperature, etc.) map to Color nodes. Compound effects (Vignette, Sharpen) are approximate.

---

## FAQ

### Q: "Python not found"

Install Python 3.8+ and ensure it is added to system PATH:
- Windows: https://www.python.org/downloads/ -> check "Add Python to PATH"
- macOS: `brew install python3`
- Linux: `sudo apt install python3`

### Q: Should I use .prproj or PR-exported XML

**Use .prproj.** It is PR's native save format with full Lumetri params, speed curves, and keyframes. PR's built-in FCP7 XML export is a simplified copy that has already lost data. If you have the .prproj file, feed it directly.

### Q: What does DRT get me

DRT does what XML cannot — Lumetri params written directly to DaVinci Color nodes. Projects with heavy color work in PR benefit most from .prproj + DRT. Requires DaVinci Resolve Studio running.

### Q: Media offline after import

XML references absolute paths. After moving media, use DaVinci's Relink feature.

### Q: Clips look wrong scale after import

The fixed XML should have corrected Scale values. If still wrong, set DaVinci Image Scaling to "Center crop with no resizing".

---

## References

- [PRPROJ-READER](https://github.com/sergeiventurinov/PRPROJ-READER) — .prproj format reverse engineering
- [prproj_downgrade](https://github.com/snorkem/prproj_downgrade) — .prproj version downgrade tool
- [ppro-scripting.docsforadobe.dev](https://ppro-scripting.docsforadobe.dev) — Adobe official object model docs
- [DaVinci Resolve Scripting API](https://resolvedevdoc.readthedocs.io/) — DaVinci Python API reference

---

## License

[MIT License](LICENSE)

---

## Project Structure

```
pr2resolve/
├── pr2resolve.py          # Core CLI tool
├── converter.bat           # Windows TUI
├── converter.sh            # macOS / Linux TUI
├── tests/
│   └── test_validator.py   # 18 unit tests
├── README.md               # Chinese documentation
├── README_EN.md            # English documentation
└── LICENSE
```
