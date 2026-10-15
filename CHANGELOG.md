# Changelog

All notable changes to pr2resolve.

## [v1.6.0] — 2026-10-15

### Added
- Source-driven Resolve 21 timeline rate, geometry, duration and project handle registration.
- Parameter-generated static native Motion and forward constant-speed video maps, replacing captured sample profiles and identity maps on retimed clips.
- Portable conversion regressions in CI and a documented native-format evidence/verification boundary.

### Fixed
- Windows release uploads follow the build script's unique output directory instead of the obsolete fixed path.
- Sequence inventory includes every video and audio track, including audio-only timelines.
- Fractional Premiere playback speeds retain their precision instead of rounding to whole percentages.
- Premiere Motion uses stable ParameterIDs across UI languages. XML keeps animated points as a linear approximation, with original curves retained in timeline-state comments across save/load.
- Nested media bins use source paths, sequence UIDs and parent links instead of ambiguous display names.
- Native file references resolve regardless of definition order; cloned codec records receive distinct identities, and source metadata caches retain frame rate, geometry and source start frame.
- Reports are generated independently of XML output and remain available when native export is rejected. Batch XML outputs are retained when requested; diagnose-only batch mode writes no conversion files.

### Validation limits
- Native DRP remains experimental: this release was tested without launching or controlling Resolve. Offline tests do not prove import, rendering, Fairlight audio routing or timeline-switch stability.
- Native transform sizing assumes fit scaling. Animated Motion, unsupported effects, reverse/variable speed and audio retiming are not silently flattened into static native output.

## [v1.5.3] — 2026-09-17

### Fixed
- Native DRP gives every timeline a distinct native media-pool name, retains the directly owned Premiere media-bin hierarchy, and deduplicates shared media entries.
- Native multi-timeline merge rewrites private container references when cloning Resolve timeline records, preventing cross-timeline container reuse.
- Native tracks now retain their Premiere names.

### Changed
- Removed the unused `pr2/conversion.json` payload from native DRP output. Lumetri grades remain detected and are not represented as a native conversion.

## [v1.5.2] — 2026-09-17

### Fixed
- Native multi-timeline export now handles Resolve private `ListMgt::` XML as opaque data and retains every sequence container.

## [v1.5.1] — 2026-09-17

### Fixed
- Native DRP now packages selected Premiere timelines into one Resolve project, retains verified clip-label enums, and honors the output name suffix.
- API DRP imports source clips from the generated FCP7 XML, normalizes file URIs, and removes every intermediate XML after export.

## [v1.5.0] — 2026-09-16

### Added
- Native DRP writes Premiere clip-label colors through the verified nested `FieldsBlob` protobuf field 6; flag bits remain independent.

### Changed
- Removed native cube LUT references and generated assets after Resolve reported missing LUT files. All Lumetri source records remain in `pr2/conversion.json`; native grade application is not claimed.
- API DRT/DRP is documented as an explicitly selected compatibility fallback; native DRP remains the primary data-writing path.
- API clip-color fallback now uses the same 16-color Premiere-to-Resolve mapping as the verified native enum path.

### Fixed
- Replaced stale workstation tests that called removed ffprobe helpers with pure `.prproj` source-timecode and missing-metadata coverage.

## [v1.4.0] — 2026-09-16

### Added
- Premiere timeline state extraction retains sequence In/Out, sequence and track markers, track metadata, clip label presets, and raw unknown state.
- API DRP visibly applies sequence In/Out and markers, track name/mute/lock, and approximate clip label colors.
- Native DRP preserves empty-track topology and the complete `timeline_state` manifest in `pr2/conversion.json`; opaque Resolve UI bytes remain unmapped.

## [v1.3.0] — 2026-09-15

- Lumetri extraction retains every encountered ParameterID, raw encoding, bounds, animation state, and binary digest by clip identity.
- Native DRP compiles static Basic, Creative, RGB/Hue curve, wheel, HDR, and HSL point operations into bundled per-clip LUTs; it stores the complete source records in `pr2/conversion.json`.
- Sequence-scoped Premiere working/output/media profiles feed verified Resolve 21 Rec.709, ACEScct, and Rec.2100 HLG project settings. API DRP also assigns known media input profiles by path.
- Spatial operations and animation remain losslessly recorded because a 3D LUT cannot represent them.

## [v1.2.0] — 2026-09-15

### Added
- Lumetri grades are identified per clip by video track and timeline frame range; static Exposure, Contrast, and Saturation can be applied as an approximate CDL in DRT/API DRP export.
- Native DRP reports extracted Lumetri records but does not apply CDL.

### Changed
- Removed `converter.bat` and `converter.sh`, together with their release and CI dependencies; source users run `pr2resolve.py` or `tui.py` directly.

## [v1.1.0] — 2026-09-15

### Added
- Pure-data native DRP export from `.prproj` or FCP7 XML without media scanning or a running Resolve instance.
- Batch native export writes one independent DRP per selected sequence using the Resolve 21 template.
- Native DRP maps verified static Basic Motion rotation/scale profiles and rejects unsupported animated transforms explicitly.
- Native DRP preserves constant forward retimes through source range mapping, while rejecting reverse and variable retimes, transitions, nested sequences, generators, and unsupported video effects before writing a misleading package.
- Removed the legacy media file scanning and ffprobe chain; source metadata now comes from project data only.

## [v1.0.4] — 2026-07-02

### Added
- **GitHub Actions release workflow** — manual semver tag triggers three-platform build, checksums, and GitHub Release publishing
- **Public CI smoke tests** — synthetic ffprobe metadata tests run without private fixtures or media files
- **Community files** — contributing guide, security policy, pull request template, and issue forms for conversion problems, bugs, and feature requests

### Fixed
- Source media fps and duration reliability no longer depends on source timecode being resolved
- PRPROJ baseline timecode is preserved while ffprobe and local fallbacks merge additional metadata
- Missing ffprobe now emits a one-time degradation warning instead of silently lowering conversion reliability
- TUI launcher no longer leaks workstation-specific Windows paths into release builds
- TUI keypress and screen clearing now avoid Windows-only assumptions on macOS and Linux
- Windows GitHub Actions release builds no longer fail on Nuitka Dependency Walker prompts in non-interactive CI

### Changed
- ffprobe source metadata probing now uses a single JSON query for timecode, fps, frame count, and duration
- README quick start now presents Windows, macOS, and Linux release packages before source mode
- Build and release scripts now use ASCII-only logs and release titles for cross-platform terminal output
- Local release publishing now requires explicit release notes instead of generating minimal placeholder notes
- Release build jobs now use Python 3.13 for Nuitka 4.1.3 packaging stability

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

[unreleased]: https://github.com/Ersiter/pr2resolve/compare/v1.5.0...HEAD
[v1.5.0]: https://github.com/Ersiter/pr2resolve/compare/v1.4.0...v1.5.0
[v1.4.0]: https://github.com/Ersiter/pr2resolve/compare/v1.3.0...v1.4.0
[v1.3.0]: https://github.com/Ersiter/pr2resolve/compare/v1.2.0...v1.3.0
[v1.2.0]: https://github.com/Ersiter/pr2resolve/compare/v1.1.0...v1.2.0
[v1.1.0]: https://github.com/Ersiter/pr2resolve/compare/v1.0.4...v1.1.0
[v1.0.4]: https://github.com/Ersiter/pr2resolve/compare/v1.0.3...v1.0.4
[v1.0.3]: https://github.com/Ersiter/pr2resolve/compare/v1.0.1...v1.0.3
[v1.0.1]: https://github.com/Ersiter/pr2resolve/compare/v1.0.0...v1.0.1
[v1.0.0]: https://github.com/Ersiter/pr2resolve/releases/tag/v1.0.0
