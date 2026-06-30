首个独立可执行文件版本 — 双击即用，无需安装 Python 或任何依赖。

## v1.0.3 新特性

**单文件可执行程序** — Windows `.exe` / macOS / Linux 三平台独立二进制。Nuitka + UPX 极致压缩，6.5 MB 自包含，零依赖运行。

**DJI tmcd 时码原生解析** — 纯 Python MP4 atom 解析器直接从无人机素材的 `tmcd` track 读取时间码，无需 ffprobe。支持 30fps 整数帧率。

**SourceTCCache 时码缓存** — 解析过的源素材时间码按文件缓存，消除重复 ffprobe 调用。批量转换显著加速。

**智能离线检测** — 仅在**所有**媒体均不可访问时移除 `<file>` 元素，防止单文件离线导致整个 DRT 损坏。

**多序列批量导出** — `.prproj` 项目的全部非空序列可一次导出，每序列独立 FCP7 XML + 报告。

**DRP 色彩科学继承** — 导出的达芬奇工程自动应用默认色彩科学设置（ACES / DaVinci YRGB）。

## 修复

- **时间线空隙重建** — 带偏移量的 clip 自动生成空隙片段，精确对齐 timeline。
- **素材长度溢出** — `PlaybackSpeed` / 变速继承链导致的时长计算错误已修正。
- **Drop-frame 时码** — NTSC 29.97 DF 格式时码在 Premiere ↔ DaVinci 转换中正确保持。
- **缩放自动适配** — 1728×3072 素材在 1080×1920 时间线中自动计算 62.5% 缩放，DaVinci Scale to Frame 等价。
- **零值哨兵时码** — XML 输出不再出现 `00:00:00:00` 占位符。
- **NTSC 混合帧率检测** — 时间线内出现多种帧率时报告不一致警告。

## 兼容性

无 Breaking changes。输入为 Premiere Pro `.prproj` 或 FCP7 XML，输出为 DaVinci 兼容 FCP7 XML / DRT / DRP。Python 3.14+ 编译。

## 📦 下载

| 平台 | 文件 | SHA256 |
|------|------|--------|
| Windows x86_64 | `pr2resolve-v1.0.3-windows-x86_64.zip` | `23c54b76…bc18423` |
| macOS x86_64 | `pr2resolve-v1.0.3-macos-x86_64.tar.gz` | `f18f95d5…0870bf7` |
| Linux x86_64 | `pr2resolve-v1.0.3-linux-x86_64.tar.gz` | `0f0974d0…9572f7` |
| SHA256SUMS | `SHA256SUMS.txt` | — |

---

First standalone binary release — double-click to run, no Python or dependencies required.

## New in v1.0.3

**Standalone binary** — Single self-contained executable for Windows, macOS, and Linux. Nuitka + UPX compression yields a ~6.5 MB binary with zero runtime dependencies.

**DJI tmcd timecode parser** — Pure-Python MP4 atom parser reads `tmcd` track timecode directly from DJI drone footage. No ffprobe required for 30fps integer frame rates.

**SourceTCCache** — Resolved source timecodes cached per media file, eliminating redundant ffprobe invocations during batch conversion.

**Smart offline detection** — Strip `<file>` elements only when ALL media is unreachable, preventing single-file offline status from corrupting the entire DRT.

**Multi-sequence batch export** — Export all non-empty sequences from a `.prproj` project in one pass with per-sequence FCP7 XML + reports.

**DRP color science** — Exported DaVinci project inherits default color science configuration (ACES / DaVinci YRGB).

## Fixed

- **Timeline gap reconstruction** — Off-set clips now generate gap elements for precise timeline alignment.
- **Clip duration overflow** — `PlaybackSpeed` / speed-change inheritance now yields correct durations.
- **Drop-frame timecode** — NTSC 29.97 DF timecode preserved correctly across Premiere ↔ DaVinci conversion.
- **Scale auto-fit** — 1728×3072 footage in 1080×1920 timeline correctly calculates 62.5% scale (DaVinci Scale to Frame equivalent).
- **Sentinel zero timecodes** — XML output no longer emits `00:00:00:00` placeholder values.
- **Mixed-NTSC detection** — Warns when a timeline contains clips with inconsistent frame rates.

## Compatibility

No breaking changes. Input: Premiere Pro `.prproj` or FCP7 XML. Output: DaVinci-compatible FCP7 XML / DRT / DRP. Compiled with Python 3.14+.

## 📦 Downloads

| Platform | File | SHA256 |
|----------|------|--------|
| Windows x86_64 | `pr2resolve-v1.0.3-windows-x86_64.zip` | `23c54b76…bc18423` |
| macOS x86_64 | `pr2resolve-v1.0.3-macos-x86_64.tar.gz` | `f18f95d5…0870bf7` |
| Linux x86_64 | `pr2resolve-v1.0.3-linux-x86_64.tar.gz` | `0f0974d0…9572f7` |
| SHA256SUMS | `SHA256SUMS.txt` | — |
