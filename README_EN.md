<div align="center">

> **Current Status: Stable**  
> *v1.0.3 — native PRPROJ timelines, source timecode, DRP, and TUI entry polish*

# .PRPROJ-.DRT Converter

Premiere Pro to DaVinci Resolve timeline converter. Outputs FCP7 XML, DRT, and DRP.

[**中文 README**](README.md)

<img src="fav.png" alt="pr2resolve TUI" width="600">

</div>

---

<!-- omit from toc -->
## Table of Contents

- [Quick Start](#quick-start)
  - [Windows](#windows)
  - [macOS](#macos)
  - [Linux](#linux)
  - [From Source](#from-source)
- [Prerequisites](#prerequisites)
  - [TUI](#tui)
  - [CLI](#cli)
- [What Can I Do](#what-can-i-do)
- [Why You Need Me](#why-you-need-me)
- [CLI Reference](#cli-reference)
- [How It Works](#how-it-works)
- [Fix Rules](#fix-rules)
- [Known Limitations](#known-limitations)
- [References](#references)
- [License](#license)

## Quick Start

Most users should download the matching package from the [latest Release](https://github.com/Ersiter/pr2resolve/releases/latest). Release packages include the executable and do not require Python.

### Windows

Download `pr2resolve-v*-windows-x86_64.zip`, extract it, then double-click `pr2resolve.exe` for the interactive TUI.

### macOS

Download `pr2resolve-v*-macos-*.tar.gz`, extract it, then run:

```bash
tar -xzf pr2resolve-v*-macos-*.tar.gz
chmod +x pr2resolve
./pr2resolve
```

If macOS blocks the unsigned binary, allow it in System Settings for this run or follow your local security policy.

### Linux

Download `pr2resolve-v*-linux-*.tar.gz`, extract it, then run:

```bash
tar -xzf pr2resolve-v*-linux-*.tar.gz
chmod +x pr2resolve
./pr2resolve
```

### From Source

Use source mode when you need to edit, debug, or avoid release packages. Source mode requires Python 3.10+.

Windows:

```bat
converter.bat
```

macOS / Linux:

```bash
chmod +x converter.sh
./converter.sh
```

You can also call the CLI directly:

```bash
python pr2resolve.py "project.prproj" -o ./output
```

---

## Prerequisites

- **Release package users**: Python is not required.
- **Source users**: Python 3.10+ is required. On Windows, check `Add Python to PATH` during installation.
- **Reliable `.prproj` conversion**: FFmpeg is recommended on all three platforms, and `ffprobe` should be available on `PATH`. `ffprobe` is not a Python package.
- **Build maintainers**: official release binaries are built with Python 3.14, Nuitka, UPX, and a platform compiler toolchain. Python build packages are listed in `requirements-build.txt`.

pr2resolve can still export without `ffprobe`, but `.prproj` source media timecode, frame rate, and duration detection may be incomplete. That can produce shifted clips, offline-looking media, or broken timelines.

Verify source-mode and `ffprobe` setup:

```bash
python --version
ffprobe -version
```

---

### TUI

```
Pick a number:
[1] Select input file (.xml or .prproj)
[2] Set output directory
[3] Configure options (XML / DRT / DRP / Mode / Suffix / Report)
[4] Start conversion
[0] Quit
```

### CLI

```bash
# Fix a PR-exported XML
python pr2resolve.py "input.xml"

# Parse .prproj directly (recommended)
python pr2resolve.py "project.prproj" -o ./output

# Pick a specific sequence
python pr2resolve.py "project.prproj" --sequence "Sequence 01"

# Export all non-empty sequences
python pr2resolve.py "project.prproj" --all-sequences

# DRT output (DaVinci Studio must be running)
python pr2resolve.py "input.xml" --drt

# DRP background export (all non-empty sequences, headless)
python pr2resolve.py "project.prproj" --drp

# DRP interactive export (all non-empty sequences, keeps project open)
python pr2resolve.py "project.prproj" --drp-gui

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

## What Can I Do

**pr2resolve reads Premiere Pro timeline data and outputs files DaVinci Resolve can use directly (or opens them in DaVinci on the spot).**

Two input formats:
- PR-exported FCP7 XML (.xml)
- PR native project files (.prproj) — **use this one**, it has more data

Three output formats:
- FCP7 XML — zero dependencies, works with any DaVinci version
- DRT — needs DaVinci Studio; exports timelines through the Resolve API and can supplement some Lumetri parameters
- DRP — project export with all non-empty sequences

---

## Why You Need Me

**Born from real PR-to-Resolve roundtrip pain and the flood of complaints online — PR's FCP7 XML export is notoriously bad. After digging in:**

- **Every clip shows Scale=100%.**

    You scaled clips to fit in PR. The XML writes Scale=100%. In DaVinci they render 2× or 3× bigger than the frame. You calculate fix values by hand for each one.

- **Lumetri grades are lost.**

    Your color work becomes a blob of base64 in the XML. DaVinci doesn't understand it, skips the block, and can even crash (it opens but hangs on timeline changes — likely an IO backlog from parsing errors).

- **Offline media from bad paths.**

    PR writes `file://localhost/C%3a/Users/...` on Windows. DaVinci doesn't recognize this format. You relink every single file.

**pr2resolve reads the input, fixes all of this, and writes clean FCP7 XML.**

- **Why .prproj instead of XML export?**

    PR's built-in XML export is second-hand — PR generates a stripped-down copy before you even get it. The .prproj file is what PR saves natively (gzip-compressed XML), with richer timeline, media, and effect context. Feed it .prproj directly, no need to export XML first.

- **When to use DRT?**

    You spent time grading in PR and don't want to redo it in DaVinci. DRT goes through DaVinci's Scripting API and writes Lumetri params directly into Color Corrector nodes. DaVinci Studio needs to be running.

---

## CLI Reference

| Option | Type | Description |
|--------|------|-------------|
| `input` | Path | Input file (.xml or .prproj) |
| `-o`, `--output` | Path | Output directory (default: same as input) |
| `--report` | flag | Generate fix report (.md) |
| `--drt` | flag | Generate DRT (requires DaVinci Studio) |
| `--drp` | flag/path | Background DRP export (all non-empty sequences) |
| `--drp-gui` | flag/path | Interactive DRP export (all non-empty sequences) |
| `--all-sequences` | flag | Export all non-empty sequences from .prproj |
| `--sequence` | str | Sequence name in .prproj |
| `--no-suffix` | flag | Omit `_pr2resolve` suffix from output filename |
| `--no-xml` | flag | Skip FCP7 XML output (use with --drt or --drp) |
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
Scan 21 known issues → Auto-fix by severity → Validate 23 checks
    │
    ▼
Output:
    ├─ output.xml   ← fixed FCP7 XML (default)
    ├─ output.md    ← fix report (--report)
    ├─ output.drt   ← DaVinci native timeline (--drt, needs DaVinci running)
    └─ output.drp   ← DaVinci project export (--drp / --drp-gui)
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
5. **Free DaVinci** — This project's DRT/DRP path is verified against DaVinci Resolve Studio; the free edition is not a supported target. XML is fine.
6. **Lumetri isn't perfect** — XML path: removed. DRT path: basic params (Exposure, Contrast, Highlights, Shadows, Temperature, etc.) map to Color nodes. Vignette and Sharpen are approximate.

---

## References

- [PRPROJ-READER](https://github.com/sergeiventurinov/PRPROJ-READER) — .prproj reverse engineering
- [prproj_downgrade](https://github.com/snorkem/prproj_downgrade) — .prproj version downgrade tool
- [ppro-scripting](https://ppro-scripting.docsforadobe.dev) — Adobe object model docs
- [DaVinci Resolve Scripting API](https://resolvedevdoc.readthedocs.io/) — DaVinci API reference
- [DaVinci Resolve MCP](https://github.com/samuelgursky/davinci-resolve-mcp) — DaVinci MCP open-source project

---

## License

[MIT LICENSE](./LICENSE)
