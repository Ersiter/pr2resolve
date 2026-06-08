<div align="center">

# pr2resolve

Convert Premiere Pro timelines to DaVinci Resolve-ready FCP7 XML. Optional DRT export if you have Resolve Studio running.

[**Chinese Version**](README.md)

</div>

---

## Have You Encountered This

PR's FCP7 XML export to DaVinci is a well-known mess — clips show up at the wrong scale, color grades are gone, media is offline. It's not you. It's PR's XML.

**Scale to Frame Size vanishes.** You fit clips to the frame in PR. The XML says Scale=100%. In DaVinci, every clip renders 2-3x bigger than it should. You do the math by hand for each one.

**Lumetri grading gets thrown out.** Your color work becomes a raw base64 blob in the XML. DaVinci has no Lumetri plugin — it skips the block entirely. All that grading? Gone. Start over in DaVinci.

**pathurl format makes DaVinci lose your media.** PR on Windows exports `file://localhost/C%3a/Users/...`. DaVinci doesn't recognize it. All media shows offline. You relink each file by hand.

This tool handles the dirty work.

---

## What It Does

- **Fixes scale** -- Compares source resolution against timeline resolution, computes the right fit scale
- **Strips Lumetri** -- XML path: removes the Lumetri blocks DaVinci ignores. DRT path: maps color params to DaVinci Color nodes
- **Fixes paths** -- Converts all pathurl values to `file:///`
- **Fills in gaps** -- Adds `<format>`, `<ntsc>`, `<sourcetrack>`, `<masterclipid>`, and whatever else DaVinci needs
- **Two ways in** -- Accepts PR-exported XML, but recommend giving it the `.prproj` file directly (more data)
- **Two ways out** -- FCP7 XML works anywhere, zero deps. DRT goes through the DaVinci API, keeps more data

---

## Why .prproj Beats XML Export

PR's FCP7 XML export is second-hand — PR makes a stripped-down copy that already lost data. `.prproj` is what PR saves natively (gzip-compressed XML), with Lumetri params, speed curves, transform keyframes intact.

This tool reads `.prproj` directly and pulls the full timeline. **Got a .prproj? Drop it here. Don't export from PR first.**

---

## What DRT Gets You

FCP7 XML is an interchange format — it can only say what the spec allows. DaVinci Color nodes, full keyframes, optical flow speed changes — none of that fits in XML.

DRT (DaVinci Resolve Timeline) is native to DaVinci. It does what XML can't: Lumetri params map straight to Color Corrector nodes. Scale/Fit stays precise. Speed algorithms pass through intact.

**Requirement: DaVinci Resolve Studio must be running.**
1. Open DaVinci Studio
2. Toggle DRT on in this tool
3. Tool imports the fixed XML via Scripting API
4. Lumetri Color data gets applied
5. You get a .drt

Without DaVinci, XML still comes out fine. DRT is the bonus round.

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
python pr2resolve.py "input.xml"

# .prproj -- output XML directly (recommended)
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

---

## How It Works

```
Input (.xml or .prproj)
    |
    +-- XML -> ElementTree structured parse
    +-- .prproj -> gzip decompress -> ObjectID graph walk
    |
    v
Scan 23 issues -> Fix by severity -> Validate 23 checks
    |
    v
Output:
    +-- .xml   -- fixed FCP7 XML (always)
    +-- .md    -- fix report (optional)
    +-- .drt   -- DaVinci native timeline (optional, needs DaVinci running)
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
| Dependencies | None, Python stdlib only |
| DRT | DaVinci Resolve Studio (free version lacks Scripting API) |

---

## Known Limitations

1. **Text titles** -- PR generatoritems often show up blank in DaVinci. FCP7 XML limitation, can't fix.
2. **Nested sequences** -- Often flattened or fail on import.
3. **Moved media** -- XML uses absolute paths. Move files? Relink in DaVinci.
4. **Double scaling** -- Uncheck "Use sizing information" when importing to avoid an extra scaling pass.
5. **Free DaVinci = no DRT** -- Scripting API is Studio-only. XML works either way.

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
pr2drt/
├── pr2resolve.py    # Core CLI tool (parse / diagnose / fix / validate / DRT)
├── converter.bat           # Windows TUI
├── converter.sh            # macOS/Linux TUI
├── tests/
│   └── test_validator.py   # 18 unit tests
├── README.md               # Chinese documentation
├── README_EN.md            # English documentation
└── LICENSE
```
