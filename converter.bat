@echo off
setlocal enabledelayedexpansion

:: ============================================================
:: prxml2fcp7xml - Windows TUI Launcher
:: ============================================================

set "VERSION=1.0.0"
set "SCRIPT=%~dp0prxml_to_fcp7xml.py"
set "PYTHONIOENCODING=utf-8"

:: State
set "INPUT_FILE="
set "OUTPUT_DIR="
set "SEQ_NAME="
set "OPT_DRT=[OFF]"
set "OPT_REPORT=[ON]"

:: Find Python
set "PYTHON_CMD="
where python >nul 2>&1 && set "PYTHON_CMD=python"
if not defined PYTHON_CMD where py >nul 2>&1 && set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    echo ERROR: Python not found.
    pause
    exit /b 1
)

:: Check script
if not exist "%SCRIPT%" (
    echo ERROR: prxml_to_fcp7xml.py not found in %~dp0
    pause
    exit /b 1
)

:MENU
cls
echo ============================================================
echo   prxml2fcp7xml v%VERSION%  -  PR XML to FCP7 XML Fixer
echo ============================================================
echo.
if defined INPUT_FILE (
    echo   [INPUT]  !INPUT_FILE!
) else (
    echo   [INPUT]  NOT SET - Please select first
)
if defined OUTPUT_DIR (
    echo   [OUTPUT] !OUTPUT_DIR!
) else (
    echo   [OUTPUT] (same as input^)
)
if defined SEQ_NAME (
    echo   [SEQ]    !SEQ_NAME!
) else (
    echo   [SEQ]    (auto^)
)
echo.
echo   XML:     [ON]    FCP7 XML output (always on)
echo   DRT:     !OPT_DRT!  DaVinci DRT output (needs Resolve Studio)
echo   Report:  !OPT_REPORT!   Fix report (.md)
echo.
echo ------------------------------------------------------------
echo.
echo   [1] Select input file (.xml / .prproj)
echo   [2] Set output directory
echo   [3] Output options
echo   [4] START
echo   [0] Quit
echo.
echo ------------------------------------------------------------
echo.

choice /c 12340 /n /m "  Select [1-4, 0]: "
if errorlevel 5 goto DONE
if errorlevel 4 goto RUN
if errorlevel 3 goto OPTIONS
if errorlevel 2 goto OUT
if errorlevel 1 goto INP

:INP
echo.
echo  Enter path to input file (.xml or .prproj):
echo  (or drag and drop the file onto this window)
echo.
set "INPUT_FILE="
set /p "INPUT_FILE=  > "
if not defined INPUT_FILE goto MENU
call :stripquotes INPUT_FILE
if not exist "!INPUT_FILE!" (
    echo  File not found: !INPUT_FILE!
    pause
    set "INPUT_FILE="
)
goto MENU

:OUT
echo.
set "OUTPUT_DIR="
set /p "OUTPUT_DIR=  Path (Enter=same as input): "
if not defined OUTPUT_DIR goto MENU
call :stripquotes OUTPUT_DIR
goto MENU

:OPTIONS
cls
echo ============================================================
echo   Output Options
echo ============================================================
echo.
echo   [1] FCP7 XML       [ON]    (always on, primary output)
echo   [2] DRT            !OPT_DRT!  (optional, needs DaVinci Resolve Studio)
echo   [3] Fix report     !OPT_REPORT!
echo   [0] Back
echo.
choice /c 1230 /n /m "  Select [1-3, 0]: "
if errorlevel 4 goto MENU
if errorlevel 3 (
    if "!OPT_REPORT!"=="[ON]" (set "OPT_REPORT=[OFF]") else (set "OPT_REPORT=[ON]")
    goto OPTIONS
)
if "!OPT_DRT!"=="[ON]" (set "OPT_DRT=[OFF]") else (set "OPT_DRT=[ON]")
goto OPTIONS

:RUN
if not defined INPUT_FILE (
    echo.
    echo  No input file selected.
    pause
    goto MENU
)
echo.
echo  Running...
echo.
set "CMD=!PYTHON_CMD! "!SCRIPT!" "!INPUT_FILE!""
if defined OUTPUT_DIR set "CMD=!CMD! -o "!OUTPUT_DIR!""
if defined SEQ_NAME set "CMD=!CMD! --sequence "!SEQ_NAME!""
if "!OPT_REPORT!"=="[ON]" set "CMD=!CMD! --report"
if "!OPT_DRT!"=="[ON]" set "CMD=!CMD! --drt"
!CMD!
echo.
pause
goto MENU

:DONE
endlocal
exit /b 0

:stripquotes
set "%1=!%1:"=!"
goto :eof
