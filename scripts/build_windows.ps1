# pr2resolve - Windows build script
# Usage: powershell -File scripts/build_windows.ps1 [-PythonExecutable <path>] [-OutputFolder <path>]
# Prerequisites: Python 3.14, requirements-build.txt, optional upx (PATH), MSVC Build Tools 2022
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
$pythonVersion = & $PYTHON_BIN -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne "3.14") {
    Write-Host "FATAL: Windows package must use Python 3.14; selected '$PYTHON_BIN' reports '$($pythonVersion.Trim())'." -ForegroundColor Red
    exit 1
}
Write-Host "  Python: $PYTHON_BIN (3.14)"

# fusionscript is a CPython extension. When Resolve is installed, import its
# public module under the exact interpreter that Nuitka will embed; version
# reporting alone cannot detect an ABI mismatch.
$apiProbe = @'
import os
import string
import sys
from pathlib import Path

program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
module_candidates = [Path(os.environ[p]) for p in ("RESOLVE_SCRIPT_API",) if os.environ.get(p)]
module_candidates.append(program_data / r"Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules")

install_candidates = [Path(os.environ["RESOLVE_INSTALL_DIR"])] if os.environ.get("RESOLVE_INSTALL_DIR") else []
install_candidates.extend(Path(os.environ[p]) / r"Blackmagic Design\DaVinci Resolve"
                          for p in ("PROGRAMFILES", "PROGRAMFILES(X86)")
                          if os.environ.get(p))
system_drive = os.environ.get("SystemDrive", "C:")
install_candidates.extend([
    Path(system_drive) / "Program Files" / r"Blackmagic Design\DaVinci Resolve",
    Path(system_drive) / "ProgramData" / r"Blackmagic Design\DaVinci Resolve",
])
install_candidates.extend(Path(f"{letter}:\\Program Files") / r"Blackmagic Design\DaVinci Resolve"
                          for letter in string.ascii_uppercase)

install = next((path for path in install_candidates if (path / "fusionscript.dll").is_file()), None)
modules = next((path for path in module_candidates if (path / "DaVinciResolveScript.py").is_file()), None)
if install is None or modules is None:
    print("Resolve scripting module or fusionscript.dll not found; ABI probe skipped.")
    raise SystemExit(2)

os.environ["RESOLVE_SCRIPT_LIB"] = str(install / "fusionscript.dll")
sys.path.insert(0, str(modules))
import DaVinciResolveScript as dvr
if not callable(getattr(dvr, "scriptapp", None)):
    raise RuntimeError("DaVinciResolveScript imported without callable scriptapp")
print(f"Resolve API import OK: {sys.executable}")
'@
$apiProbePath = Join-Path $env:TEMP "pr2resolve-api-probe-$([guid]::NewGuid().ToString('N')).py"
try {
    [System.IO.File]::WriteAllText($apiProbePath, $apiProbe, [System.Text.UTF8Encoding]::new($false))
    $apiProbeInfo = New-Object System.Diagnostics.ProcessStartInfo
    $apiProbeInfo.FileName = $PYTHON_BIN
    $apiProbeInfo.Arguments = '"' + $apiProbePath + '"'
    $apiProbeInfo.UseShellExecute = $false
    $apiProbeInfo.CreateNoWindow = $true
    $apiProbeInfo.RedirectStandardOutput = $true
    $apiProbeInfo.RedirectStandardError = $true
    $apiProbeProcess = New-Object System.Diagnostics.Process
    $apiProbeProcess.StartInfo = $apiProbeInfo
    [void]$apiProbeProcess.Start()
    $apiStdoutTask = $apiProbeProcess.StandardOutput.ReadToEndAsync()
    $apiStderrTask = $apiProbeProcess.StandardError.ReadToEndAsync()
    if (-not $apiProbeProcess.WaitForExit(15000)) {
        $apiProbeProcess.Kill()
        $apiProbeProcess.WaitForExit()
        throw "Resolve API import probe timed out after 15 seconds."
    }
    $apiStdout = $apiStdoutTask.Result.Trim()
    $apiStderr = $apiStderrTask.Result.Trim()
    if ($apiProbeProcess.ExitCode -eq 2) {
        Write-Host "  WARNING: $apiStdout" -ForegroundColor Yellow
    } elseif ($apiProbeProcess.ExitCode -ne 0) {
        throw "Resolve API import failed (exit $($apiProbeProcess.ExitCode)). $apiStdout $apiStderr"
    } else {
        Write-Host "  $apiStdout"
    }
} catch {
    Write-Host "FATAL: Selected Python cannot load the installed Resolve scripting API. $_" -ForegroundColor Red
    exit 1
} finally {
    if (Test-Path -LiteralPath $apiProbePath) {
        Remove-Item -LiteralPath $apiProbePath -Force
    }
}

# Read version from source
$VERSION = (& $PYTHON_BIN -c "from pr2_constants import VERSION; print(VERSION)").Trim()
Write-Host "pr2resolve v$VERSION - Windows x86_64 build" -ForegroundColor Cyan

# Pre-flight checks
$missing = @()

# python + Python build dependencies
$nuitkaInfo = New-Object System.Diagnostics.ProcessStartInfo
$nuitkaInfo.FileName = $PYTHON_BIN
$nuitkaInfo.Arguments = '-m nuitka --version'
$nuitkaInfo.UseShellExecute = $false
$nuitkaInfo.CreateNoWindow = $true
$nuitkaInfo.RedirectStandardOutput = $true
$nuitkaInfo.RedirectStandardError = $true
$nuitkaProcess = New-Object System.Diagnostics.Process
$nuitkaProcess.StartInfo = $nuitkaInfo
$nuitkaExit = 1
try {
    [void]$nuitkaProcess.Start()
    $nuitkaStdout = $nuitkaProcess.StandardOutput.ReadToEnd()
    $nuitkaStderr = $nuitkaProcess.StandardError.ReadToEnd()
    $nuitkaProcess.WaitForExit()
    $nuitkaExit = $nuitkaProcess.ExitCode
} catch {
    $nuitkaStderr = $_.Exception.Message
}
if ($nuitkaExit -ne 0) {
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
    "--include-data-file=resources/resolve21_native_template.drp=resources/resolve21_native_template.drp",
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
$lockDeadline = (Get-Date).AddSeconds(10)
$lockReleased = $false
do {
    try {
        $probe = [System.IO.File]::Open($EXE_PATH, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::None)
        $probe.Dispose()
        $lockReleased = $true
    } catch [System.IO.IOException] {
        Start-Sleep -Milliseconds 200
    } catch {
        Write-Host "FATAL: Unable to probe executable lock: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
} while (-not $lockReleased -and (Get-Date) -lt $lockDeadline)
if (-not $lockReleased) {
    Write-Host "FATAL: executable remained locked for 10 seconds: $EXE_PATH" -ForegroundColor Red
    exit 1
}
try {
    Compress-Archive -Path $EXE_PATH -DestinationPath $ARCHIVE_PATH
} catch {
    if (Test-Path -LiteralPath $ARCHIVE_PATH) {
        Remove-Item -LiteralPath $ARCHIVE_PATH -Force
    }
    Write-Host "FATAL: packaging failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

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
