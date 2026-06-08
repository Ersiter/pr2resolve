<div align="center">

# pr2resolve

Premiere Pro to DaVinci Resolve timeline converter. Outputs FCP7 XML and DRT.

[**Chinese**](README.md)

</div>

---

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [What It Is](#what-it-is)
- [Why You Need It](#why-you-need-it)
- [CLI Reference](#cli-reference)
- [How It Works](#how-it-works)
- [Fix Rules](#fix-rules)
- [Known Limitations](#known-limitations)
- [References](#references)
- [License](#license)

---

## Installation

```bash
git clone <repo-url>
cd pr2resolve
```

Python 3.8+ is the only dependency. No pip install needed.

---

## Quick Start

### Windows

Double-click `converter.bat`.

```
[1] Select input file (.xml or .prproj)
[2] Set output directory
[3] Configure options (XML / DRT / Report)
[4] Start conversion
```

### macOS / Linux

```bash
chmod +x converter.sh
./converter.sh
```

### CLI

```bash
# Fix a PR-exported XML
python pr2resolve.py "input.xml"

# Parse .prproj directly (recommended)
python pr2resolve.py "project.prproj" -o ./output

# Pick a specific sequence
python pr2resolve.py "project.prproj" --sequence "Sequence 01"

# DRT output (DaVinci Studio must be running)
python pr2resolve.py "input.xml" --drt

# Generate a fix report
python pr2resolve.py "input.xml" --report

# Diagnose only, don't fix
python pr2resolve.py "input.xml" --diagnose-only
```

Import the XML into DaVinci:

```
File → Import Timeline → Import AAF, EDL, XML... → pick the .xml file
```

---

## What It Is

pr2resolve reads Premiere Pro timeline data and outputs files DaVinci Resolve can use.

Two input formats:
- PR-exported FCP7 XML (.xml)
- PR native project files (.prproj) — **use this one**, it has more data

Two output formats:
- FCP7 XML — zero dependencies, works with any DaVinci version
- DRT — needs DaVinci Studio; preserves Lumetri grades, speed curves, and other data XML can't hold

---

## Why You Need It

PR's FCP7 XML export to DaVinci is unreliable. Not your fault — the export is broken.

**Every clip shows Scale=100%.** You fit clips to the frame in PR. The XML says 100%. In DaVinci they render 2x or 3x bigger than expected. You calculate fix values by hand for each one.

**Lumetri grades disappear.** Your color work becomes a base64 blob in the XML. DaVinci doesn't know what to do with it. It skips the block. Hours of grading, gone.

**Paths are wrong. All media shows offline.** PR on Windows writes `file://localhost/C%3a/Users/...`. DaVinci rejects it. You relink every single file.

pr2resolve reads the input, fixes all of this, and writes clean XML.

**Why .prproj instead of XML export?** PR's built-in XML export is second-hand — PR generates a stripped-down copy before you even get it. The .prproj file is what PR saves natively (gzip-compressed XML), with Lumetri params, speed curves, and keyframes intact. Feed it .prproj directly.

**When to use DRT?** You spent a lot of time grading in PR and don't want to redo it in DaVinci. DRT goes through DaVinci's Scripting API and writes Lumetri params into Color Corrector nodes. DaVinci Studio needs to be running.

---

## CLI Reference

| Option | Type | Description |
|--------|------|-------------|
| `input` | Path | Input file (.xml or .prproj) |
| `-o`, `--output` | Path | Output directory (default: same as input) |
| `--report` | flag | Generate fix report (.md) |
| `--drt` | flag | Generate DRT (requires DaVinci Studio) |
| `--sequence` | str | Sequence name in .prproj (default: auto) |
| `--diagnose-only` | flag | Diagnose only, no fixes |
| `--version` | flag | Show version |

---

## How It Works

```
Input (.xml or .prproj)
    │
    ├─ XML → ElementTree structured parse
    ├─ .prproj → gzip decompress → ObjectID graph traversal
    │
    ▼
Scan 21 issues → Auto-fix by severity → Validate 23 checks
    │
    ▼
Output:
    ├─ output.xml   ← fixed FCP7 XML (always)
    ├─ output.md    ← fix report (--report)
    └─ output.drt   ← DaVinci native timeline (--drt, needs DaVinci running)
```

---

## Fix Rules

| Level | Rule | Description |
|-------|------|-------------|
| C0 | version | `xmeml version="4"` → `"5"` |
| C1-C2 | format | Fill missing video/audio `<format>` |
| C3-C4 | rate | Fill missing `<ntsc>` / `<timebase>` |
| C5 | pathurl | `file://localhost/...` → `file:///...` |
| C6 | media order | Move video before audio |
| M0 | Lumetri | XML: remove; DRT: map to Color nodes |
| M1-M2 | clipid/track | Fill `<masterclipid>` / `<sourcetrack>` |
| M4 | link | Generate `<link>` for same-source clips |
| M5 | file details | Fill missing samplecharacteristics |
| M6 | element order | Sort clipitem children per FCP7 spec |
| M7 | Scale | Source res / timeline res = fit scale |
| N1-N7 | details | timecode / float precision / rate consistency / etc. |

All rules apply automatically. They're not optional — skip them and the import breaks or clips render wrong. The only decisions you make: what to feed in, where to write output, and which output formats.

---

## Known Limitations

1. **PR text titles** — Generatoritems often show blank in DaVinci. FCP7 XML limitation.
2. **Nested sequences** — Frequently flattened or import fails.
3. **Moved media** — XML stores absolute paths. Relink in DaVinci after moving files.
4. **Import settings** — Uncheck "Use sizing information" to avoid double scaling.
5. **Free DaVinci** — Scripting API is Studio-only. DRT won't work. XML is fine.
6. **Lumetri isn't perfect** — XML path: removed. DRT path: basic params (Exposure, Contrast, Highlights, Shadows, Temperature, etc.) map to Color nodes. Vignette and Sharpen are approximate.

---

## References

- [PRPROJ-READER](https://github.com/sergeiventurinov/PRPROJ-READER) — .prproj reverse engineering
- [prproj_downgrade](https://github.com/snorkem/prproj_downgrade) — .prproj version downgrade tool
- [ppro-scripting](https://ppro-scripting.docsforadobe.dev) — Adobe object model docs
- [DaVinci Resolve Scripting API](https://resolvedevdoc.readthedocs.io/) — DaVinci API reference

---

## License

MIT — see [LICENSE](./LICENSE).
