# pr2resolve - Windows build script
# Usage: powershell -File scripts/build_windows.ps1 [-PythonExecutable <path>] [-OutputFolder <path>]
# Prerequisites: Python, requirements-build.txt, optional upx (PATH), MSVC Build Tools 2022
# Output: dist/windows-x86_64-{run-id}/pr2resolve-v{VERSION}-windows-x86_64.zip

param(
    [string]$PythonExecutable = "",
    [string]$OutputFolder = "dist"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

# Select a working interpreter before invoking Python. A configured path takes
# precedence and must work; fallback discovery only considers PATH commands.
$pythonCandidates = @()
$explicitPython = $PythonExecutable
if (-not $explicitPython -and $env:PR2_PYTHON) { $explicitPython = $env:PR2_PYTHON }
if ($explicitPython) {
    $pythonCandidates = @($explicitPython)
} else {
    foreach ($commandName in @("python.exe", "py.exe")) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($command) { $pythonCandidates += $command.Source }
    }
}
$PYTHON_BIN = $null
foreach ($candidate in ($pythonCandidates | Select-Object -Unique)) {
    try {
        $probe = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $probe -match "Python 3\.") {
            $PYTHON_BIN = $candidate
            break
        }
    } catch {
        # Try the next configured or discovered interpreter.
    }
}
if (-not $PYTHON_BIN) {
    if ($explicitPython) {
        Write-Host "FATAL: Configured Python interpreter is unavailable or invalid: $explicitPython" -ForegroundColor Red
    } else {
        Write-Host "FATAL: No working Python interpreter found. Set -PythonExecutable or PR2_PYTHON." -ForegroundColor Red
    }
    exit 1
}
Write-Host "  Python: $PYTHON_BIN"

# Read version from source
$VERSION = (& $PYTHON_BIN -c "from pr2_constants import VERSION; print(VERSION)").Trim()
Write-Host "pr2resolve v$VERSION - Windows x86_64 build" -ForegroundColor Cyan

# Pre-flight checks
$missing = @()

# python + Python build dependencies
$nuitkaProbe = & $PYTHON_BIN -m nuitka --version 2>$null
if ($LASTEXITCODE -ne 0) {
    $missing += "nuitka (use $PYTHON_BIN -m pip install -r requirements-build.txt)"
}

# UPX is optional; an uncompressed executable remains a valid deliverable.
$upxCommand = Get-Command upx -ErrorAction SilentlyContinue
$UPX_BIN = if ($upxCommand) { $upxCommand.Source } else { $null }
Write-Host "  UPX: $(if ($UPX_BIN) { $UPX_BIN } else { 'not found (optional)' })"

if ($missing.Count -gt 0) {
    Write-Host "FATAL: Missing: $($missing -join ', ')" -ForegroundColor Red
    exit 1
}

$PLATFORM = "windows-x86_64"
$ARCHIVE_NAME = "pr2resolve-v$VERSION-$PLATFORM"
$outputRoot = if ([System.IO.Path]::IsPathRooted($OutputFolder)) {
    [System.IO.Path]::GetFullPath($OutputFolder)
} else {
    [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $OutputFolder))
}
$runId = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$DIST_DIR = Join-Path $outputRoot "$PLATFORM-$runId-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$ARCHIVE_PATH = "$DIST_DIR\$ARCHIVE_NAME.zip"
# Build in a fresh directory so pre-existing distributions remain untouched.
New-Item -ItemType Directory -Force -Path $DIST_DIR | Out-Null

# Nuitka onefile compile
$EXE_PATH = "$DIST_DIR\pr2resolve.exe"
Write-Host "[1/5] Nuitka onefile compile..." -ForegroundColor Yellow
$nuitkaArgs = @(
    "--mode=onefile",
    "--output-dir=$DIST_DIR",
    "--output-filename=pr2resolve.exe",
    "--msvc=latest",
    "--lto=yes",
    "--onefile-no-compression",
    "--include-windows-runtime-dlls=no",
    "--assume-yes-for-downloads",
    "--python-flag=no_docstrings",
    "--python-flag=no_asserts",
    "--noinclude-setuptools-mode=nofollow",
    "--noinclude-pytest-mode=nofollow",
    "--noinclude-unittest-mode=nofollow",
    "--noinclude-pydoc-mode=nofollow",
    "--noinclude-IPython-mode=nofollow",
    "--noinclude-dask-mode=nofollow",
    "--noinclude-numba-mode=nofollow",
    "--noinclude-default-mode=warning",
    "--remove-output",
    "tui.py"
)
& $PYTHON_BIN -m nuitka @nuitkaArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "FATAL: Nuitka build failed" -ForegroundColor Red
    exit $LASTEXITCODE
}

if (-not (Test-Path $EXE_PATH)) {
    Write-Host "FATAL: $EXE_PATH not created" -ForegroundColor Red
    exit 1
}

# Smoke test
Write-Host "[2/5] Smoke testing executable..." -ForegroundColor Yellow
& $EXE_PATH --version
if ($LASTEXITCODE -ne 0) {
    Write-Host "FATAL: $EXE_PATH --version failed" -ForegroundColor Red
    exit $LASTEXITCODE
}

# UPX compression
Write-Host "[3/5] UPX compression..." -ForegroundColor Yellow
if ($UPX_BIN) {
    & $UPX_BIN --best --lzma $EXE_PATH
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARNING: UPX failed, continuing with uncompressed binary" -ForegroundColor Yellow
    }
} else {
    Write-Host "  UPX unavailable; continuing with uncompressed binary" -ForegroundColor Yellow
}

# Package
Write-Host "[4/5] Packaging $ARCHIVE_NAME.zip..." -ForegroundColor Yellow
Compress-Archive -Path $EXE_PATH -DestinationPath $ARCHIVE_PATH

# Clean up bare binary (only the zip is distributed)
Write-Host "[5/5] Cleaning up..." -ForegroundColor Yellow
Remove-Item -LiteralPath $EXE_PATH

# SHA256
$hash = (Get-FileHash -Algorithm SHA256 $ARCHIVE_PATH).Hash.ToLower()
$SUM_FILE = "$DIST_DIR\SHA256SUMS.txt"
"$hash  $ARCHIVE_NAME.zip" | Out-File -Append -Encoding ascii $SUM_FILE

# Report
$size = [math]::Round((Get-Item $ARCHIVE_PATH).Length / 1MB, 2)
Write-Host ""
Write-Host "== Build complete ==" -ForegroundColor Green
Write-Host "  $ARCHIVE_PATH  ($size MB)"
Write-Host "  SHA256: $hash"
Write-Host ""
Write-Host "  $hash  $ARCHIVE_NAME.zip" -ForegroundColor Gray
