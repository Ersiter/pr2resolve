# pr2resolve — Windows build script
# Usage: powershell -File scripts/build_windows.ps1
# Prerequisites: python 3.14, requirements-build.txt, upx (PATH), MSVC Build Tools 2022
# Output: dist/pr2resolve-v{VERSION}-windows-x86_64.zip
# Cleanup exception: this script may directly remove only its own dist\<platform> artifacts.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

# ── Read version from source ──────────────────────────────────────────
$VERSION = (python -c "from pr2_constants import VERSION; print(VERSION)").Trim()
Write-Host "pr2resolve v$VERSION — Windows x86_64 build" -ForegroundColor Cyan

# ── Pre-flight checks ─────────────────────────────────────────────────
$missing = @()

# python + Python build dependencies
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    $missing += "python"
} else {
    python -m nuitka --version *> $null
    if ($LASTEXITCODE -ne 0) {
        $missing += "nuitka (python -m pip install -r requirements-build.txt)"
    }
}

# UPX — try PATH, then known location
$UPX_BIN = "upx"
if ((Get-Command upx -ErrorAction SilentlyContinue) -eq $null) {
    $known = "$env:LOCALAPPDATA\upx\upx.exe"
    if (Test-Path $known) { $UPX_BIN = $known } else { $missing += "upx" }
}
Write-Host "  UPX: $UPX_BIN"

if ($missing.Count -gt 0) {
    Write-Host "FATAL: Missing: $($missing -join ', ')" -ForegroundColor Red
    exit 1
}

$PLATFORM = "windows-x86_64"
$ARCHIVE_NAME = "pr2resolve-v$VERSION-$PLATFORM"
$DIST_DIR = "dist\$PLATFORM"
$ARCHIVE_PATH = "$DIST_DIR\$ARCHIVE_NAME.zip"

function Assert-BuildArtifactPath {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Path,
        [Parameter(Mandatory=$true)]
        [string]$ExpectedRelative
    )
    $repoRoot = [System.IO.Path]::GetFullPath((Get-Location).Path)
    $resolved = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Path))
    $expected = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $ExpectedRelative))
    if ($resolved -ne $expected -and -not $resolved.StartsWith($expected + [System.IO.Path]::DirectorySeparatorChar)) {
        Write-Host "FATAL: unsafe build artifact path: $Path" -ForegroundColor Red
        exit 1
    }
}

# ── Clean previous build ──────────────────────────────────────────────
Assert-BuildArtifactPath -Path $DIST_DIR -ExpectedRelative "dist\$PLATFORM"
if (Test-Path $DIST_DIR) { Remove-Item -Recurse -Force $DIST_DIR }
New-Item -ItemType Directory -Force -Path $DIST_DIR | Out-Null

# ── Nuitka onefile compile ────────────────────────────────────────────
$EXE_PATH = "$DIST_DIR\pr2resolve.exe"
Write-Host "[1/4] Nuitka onefile compile..." -ForegroundColor Yellow
python -m nuitka `
  --mode=onefile `
  --output-dir=$DIST_DIR `
  --output-filename=pr2resolve.exe `
  --msvc=latest `
  --lto=yes `
  --onefile-no-compression `
  --include-windows-runtime-dlls=no `
  --python-flag=no_docstrings `
  --python-flag=no_asserts `
  --noinclude-setuptools-mode=nofollow `
  --noinclude-pytest-mode=nofollow `
  --noinclude-unittest-mode=nofollow `
  --noinclude-pydoc-mode=nofollow `
  --noinclude-IPython-mode=nofollow `
  --noinclude-dask-mode=nofollow `
  --noinclude-numba-mode=nofollow `
  --noinclude-default-mode=warning `
  --remove-output `
  tui.py

if (-not (Test-Path $EXE_PATH)) {
    Write-Host "FATAL: $EXE_PATH not created" -ForegroundColor Red
    exit 1
}

# ── UPX compression ───────────────────────────────────────────────────
Write-Host "[2/4] UPX compression..." -ForegroundColor Yellow
& $UPX_BIN --best --lzma $EXE_PATH
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: UPX failed, continuing with uncompressed binary" -ForegroundColor Yellow
}

# ── Package ───────────────────────────────────────────────────────────
Write-Host "[3/4] Packaging $ARCHIVE_NAME.zip..." -ForegroundColor Yellow
Compress-Archive -Path $EXE_PATH -DestinationPath $ARCHIVE_PATH

# ── Clean up bare binary (only the zip is distributed) ────────────────
Write-Host "[4/4] Cleaning up..." -ForegroundColor Yellow
Assert-BuildArtifactPath -Path $EXE_PATH -ExpectedRelative "dist\$PLATFORM\pr2resolve.exe"
Remove-Item $EXE_PATH

# ── SHA256 ────────────────────────────────────────────────────────────
$hash = (Get-FileHash -Algorithm SHA256 $ARCHIVE_PATH).Hash.ToLower()
$SUM_FILE = "$DIST_DIR\SHA256SUMS.txt"
"$hash  $ARCHIVE_NAME.zip" | Out-File -Append -Encoding ascii $SUM_FILE

# ── Report ────────────────────────────────────────────────────────────
$size = [math]::Round((Get-Item $ARCHIVE_PATH).Length / 1MB, 2)
Write-Host ""
Write-Host "══ Build complete ══" -ForegroundColor Green
Write-Host "  $ARCHIVE_PATH  ($size MB)"
Write-Host "  SHA256: $hash"
Write-Host ""
Write-Host "  $hash  $ARCHIVE_NAME.zip" -ForegroundColor Gray
