# Changelog

All notable changes to pr2resolve.

## [v1.0.3] — 2026-06-30

### Added
- **DJI tmcd timecode parser** — pure-Python MP4 atom parser reads `tmcd` track directly from drone footage
- **SourceTCCache** — caches resolved source timecodes per media file, eliminating redundant ffprobe calls
- **Smart media detection** — `<file>` elements stripped only when ALL media is offline
- **Multi-sequence .prproj** — batch export all non-empty sequences from a single Premiere project
- **DRP color science settings** — DaVinci project inherits color science from default config
- **TUI launcher** (`tui.py`) — interactive menu matching `converter.sh` UX, compiles to standalone .exe

### Fixed
- Timeline gap reconstruction for clips with non-zero offset
- Clip duration overflow from `PlaybackSpeed` / speed-change inheritance
- Drop-frame timecode NTSC correction (29.97 DF mode)
- Scale auto-fit for mismatched resolutions (1728×3072 → 1080×1920)
- Zero-file timecode sentinel values in XML output
- NTSC rate inconsistency detection across timeline segments

### Changed
- Pathurls resolve in-place during import (no global path expansion after the fact)
- DRP export always includes ALL non-empty sequences (independent of mode selection)

## [v1.0.1] — 2026-06-13

### Added
- **DRP project export** — background (headless) and interactive (GUI) modes
- **Architecture refactor** — ClipData/FileData/FilterSpec/LinkMember/TrackData/TransitionData dataclasses
- **TUI 3-state DRP toggle** — OFF / BG / ON cycling in converter.bat/.sh
- **DaVinci DC format alignment** — output matches Resolve's own FCP7 XML export format

### Fixed
- Percent-encoded characters (CJK filenames, spaces) in `file:///` pathurls
- Empty DRP guard — failed imports now abort instead of producing silent empty files
- Headless Resolve orphan process (`try/finally` cleanup)
- Chinese project name ASCII sanitizer removed (DaVinci supports them natively)

## [v1.0.0] — 2026-06-12

First production release. Architecture refactor complete — domain model separated from XML rendering. Dual-entry: FCP7 XML and `.prproj`.

[unreleased]: https://github.com/Ersiter/pr2resolve/compare/v1.0.3...HEAD
[v1.0.3]: https://github.com/Ersiter/pr2resolve/compare/v1.0.1...v1.0.3
[v1.0.1]: https://github.com/Ersiter/pr2resolve/compare/v1.0.0...v1.0.1
[v1.0.0]: https://github.com/Ersiter/pr2resolve/releases/tag/v1.0.0
