<div align="center">

> **Current Status: Native DRP experimental**
>
> *v1.6.0 — source-driven timeline settings, Motion and time maps*

# pr2resolve — Premiere Pro to DaVinci Resolve Converter

Premiere Pro to DaVinci Resolve timeline converter. Outputs FCP7 XML, DRT, and DRP.

[**中文 README**](README.md)

Contest materials: [project brief](docs/contest/submission-brief.md) · [3-minute demo](docs/contest/demo-script-3min.md) · [reproducibility checklist](docs/contest/reproducibility-checklist.md)

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

On Windows, macOS, and Linux, run the CLI or TUI directly:

```bash
python pr2resolve.py "project.prproj" -o ./output
python tui.py
```

---

## Prerequisites

- **Release package users**: Python is not required.
- **Source users**: Python 3.10+ is required. On Windows, check `Add Python to PATH` during installation.
- **Pure-data `.prproj` conversion**: media paths, frame rates, timecode, duration, and resolution are read from the `.prproj` object graph, so source media may be offline or unavailable on the current machine.
- **Native DRP without media scanning**: automatic selection, `--sequence`, or `--all-sequences` writes the selected timelines into one native DRP without starting Resolve or scanning media. The template targets Resolve 21.
- **Source-driven settings**: timeline rate, resolution, extent and registration follow source data. Media bins retain parent links and assign items by media path or sequence UID rather than display name.
- **Sequence inventory**: audio/video, caption and data tracks contribute to non-empty selection, so caption-only sequences reach selection and diagnosis. Unsupported target content retains its source records and report entries.
- **Motion and time maps**: stable ParameterIDs identify localized Motion controls. XML keeps keyframes with linear interpolation and retains the original Premiere curves in a comment. Native static transforms, forward constant-speed and monotone graphdict video maps are generated from parameters. Curves use a piecewise-linear approximation, retain the full graph and edit boundaries, and report last-frame alignment residuals.
- **API retime exports**: DRT/DRP packages receive trim and time-map corrections from the same encoder, matched through timeline references and track positions. Ambiguous matches or unsupported curves report failure and preserve the source file. Package corrections do not change the live API timeline.
- **Validation boundary**: this version passes offline structure and source-project regressions; Resolve import and audiovisual checks remain outstanding. Native zoom compensation assumes the template uses fit sizing; other input-scaling modes have not been verified. Animated Motion, reverse/holds, Bezier retiming, audio retiming, non-default anchors, transitions, nested sequences and generators fail explicitly. Native/API curve rendering parity is unverified.
- **Build maintainers**: official release binaries are built with Python 3.14, Nuitka, UPX, and a platform compiler toolchain. Python build packages are listed in `requirements-build.txt`.

Verify source-mode setup:

```bash
python --version
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

# Pure-data native DRP (no Resolve required)
python pr2resolve.py "project.prproj" --native-drp

# Generate a fix report
python pr2resolve.py "input.xml" --report

# Native project and report without XML output
python pr2resolve.py "project.prproj" --native-drp --all-sequences --no-xml --report

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
- Native DRP — primary delivery path; writes timelines, media bins, track names, verified clip-label color enums, and color configuration without starting Resolve
- API DRT/DRP — explicit compatibility fallback, selected by the user for unsupported native schemas or failed import validation; requires DaVinci Studio

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

- **How are timeline state and grades migrated?**

    The `.prproj` reader retains Lumetri effect order, shaders and keyframes by timeline clip identity. Native DRP and API share one DRX graph, with a serial node per Lumetri effect. Static exposure, contrast and saturation are approximate; other controls and animation remain source records. Node data has offline cross-checks; actual color and Resolve loading remain unverified. No cube LUT is embedded.

| State | Native DRP | API DRT / DRP |
|---|---|---|
| Clip-label colors | Maps 16 presets; custom RGB is unmapped | Same preset mapping, matched by track and clip position |
| Independent track colors, flags | Not implemented | Not implemented; label colors are distinct from flags |
| Timeline markers | Writes position, name, note and duration; uses Blue for now | Adds missing markers; warns on conflicts or rejected writes |
| Sequence In/Out | Not written; explicit state produces warnings | Sets pairs, clears explicitly unset marks; warns on one-sided state |
| Track name, mute, lock | Writes custom names and lock/disable bits; offline-validated | Uses the same source state with official API methods and reports failures |
| Work area, track/source-clip markers | Retains source work-area and extracted track-marker records; not applied | Not applied |
| Captions | Decodes FormattedTextData text runs, font lists and source ticks; retains raw payloads; full styling/target tracks are incomplete | Same source records; an API path for arbitrary caption cues is not established |
| Lumetri layers | One serial node per source effect using the shared DRX Body; three static controls are approximate, other controls/animation unmapped | Applies the same DRX; matching or application failures prevent successful export reporting |

Source marker color indices remain in the XML state comment; a supplied index produces an unmapped-color warning. These writes have offline test coverage; visible results still need verification in an idle Resolve environment. See [format evidence and verification limits](NATIVE_FORMAT.md).

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
| `--native-drp` | flag/path | Pure-data native DRP (one project containing selected timelines; use --all-sequences for all non-empty timelines) |
| `--all-sequences` | flag | Export all non-empty sequences from .prproj |
| `--sequence` | str | Sequence name in .prproj |
| `--no-suffix` | flag | Omit `_pr2resolve` suffix from output filename |
| `--no-xml` | flag | Skip FCP7 XML output (use with --drt, --drp, or --native-drp) |
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
| M0 | Lumetri | XML: remove unsupported plugin blocks; native/API: shared serial DRX graph with three approximate static controls; other controls and keyframes remain source records |
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
6. **Lumetri spatial and temporal operations** — The DRX graph approximates exposure, contrast and saturation. Denoise, sharpen, blur, vignette, curves and keyframes remain unmapped. Multiple Lumetri effects retain their order; cross-application color equivalence is not established.
7. **Color-space vocabularies differ** — Native DRP maps Rec.709, ACEScct and Rec.2100 HLG configurations through its templates; HDR projects and input color spaces still need live verification. API DRP requests each timeline's settings by source sequence UID and known input spaces by media path; actual acceptance and native/API color parity remain unverified.
8. **Native projects still need import verification** — offline registration, media-reference, marker, grade and time-map checks do not establish rendered output, audio playback, Fairlight routing or timeline-switch stability. Visible marker and source start-timecode application remains unverified; native In/Out and flags are not applied. Track mute/lock is written and needs live verification. `--report` works without XML output and generated reports remain available after an export is rejected.

See [conversion implementation notes](NATIVE_FORMAT.md) for field evidence, industry references and the remaining acceptance checks.

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
