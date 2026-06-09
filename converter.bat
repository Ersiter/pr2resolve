@echo off
setlocal enabledelayedexpansion

:: ============================================================
:: pr2resolve - Windows TUI Launcher
:: ============================================================

set "VERSION=1.0.0"
set "SCRIPT=%~dp0pr2resolve.py"
set "PYTHONIOENCODING=utf-8"

:: State
set "INPUT_FILE="
set "OUTPUT_DIR="
set "SEQ_NAME="
set "OPT_DRT=[OFF]"
set "OPT_REPORT=[ON]"
set "OPT_XML=[ON]"
set "OPT_ALL_SEQ=[OFF]"
set "OPT_SUFFIX=[ON]"

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
    echo ERROR: pr2resolve.py not found in %~dp0
    pause
    exit /b 1
)

:MENU
cls
echo.
echo  ============================================================
echo   pr2resolve v%VERSION%  -  PR XML to FCP7 XML Fixer
echo  ============================================================
echo.
echo  ------------------------------------------------------------
if defined INPUT_FILE (
    echo  [INPUT]  !INPUT_FILE!
) else (
    echo  [INPUT]  NOT SET - Please select first
)
if defined OUTPUT_DIR (
    echo  [OUTPUT] !OUTPUT_DIR!
) else (
    echo  [OUTPUT] (same as input^)
)
if defined SEQ_NAME (
    echo  [SEQ]    !SEQ_NAME!
) else (
    echo  [SEQ]    (auto^)
)
echo.
echo  XML:     !OPT_XML!   FCP7 XML output
echo  DRT:     !OPT_DRT!  DaVinci DRT output (needs Resolve Studio^)
echo  All Seq: !OPT_ALL_SEQ!  Export all sequences (.prproj only^)
echo  Suffix:  !OPT_SUFFIX!   _pr2resolve name tag
echo  Report:  !OPT_REPORT!   Fix report (.md^)
echo.
echo  ------------------------------------------------------------
echo.
echo  [1] Select input file (.xml / .prproj^)
echo  [2] Set output directory
echo  [3] Output options
echo  [4] START
echo  [0] Quit
echo.
echo  ------------------------------------------------------------
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
cls
echo.
echo  ============================================================
echo   Set Output Directory
echo  ============================================================
echo.
if defined OUTPUT_DIR (
    echo  Current: !OUTPUT_DIR!
) else (
    echo  Current: (same as input^)
)
echo.
echo  [1] Keep current
echo  [2] Script folder \output
echo  [3] Same as input file folder
echo  [4] Custom path
echo.
choice /c 1234 /n /m "  > "
if errorlevel 4 goto _OUT_CUSTOM
if errorlevel 3 goto _OUT_INPUT
if errorlevel 2 goto _OUT_SCRIPT
goto MENU
:_OUT_SCRIPT
set "OUTPUT_DIR=%~dp0output"
if not exist "!OUTPUT_DIR!" mkdir "!OUTPUT_DIR!"
echo  Set to: !OUTPUT_DIR!
timeout /t 1 >nul
goto MENU
:_OUT_INPUT
if not defined INPUT_FILE (
    echo  Please select input file first.
    timeout /t 1 >nul
    goto MENU
)
for %%F in ("!INPUT_FILE!") do set "OUTPUT_DIR=%%~dpF"
rem Remove trailing backslash
if "!OUTPUT_DIR:~-1!"=="\" set "OUTPUT_DIR=!OUTPUT_DIR:~0,-1!"
echo  Set to: !OUTPUT_DIR!
timeout /t 1 >nul
goto MENU
:_OUT_CUSTOM
echo.
set "OUTPUT_DIR="
set /p "OUTPUT_DIR=  Path: "
if not defined OUTPUT_DIR goto MENU
call :stripquotes OUTPUT_DIR
if not exist "!OUTPUT_DIR!" mkdir "!OUTPUT_DIR!"
echo  Set to: !OUTPUT_DIR!
timeout /t 1 >nul
goto MENU

:OPTIONS
cls
echo.
echo  ============================================================
echo   Output Options
echo  ============================================================
echo.
echo  [1] FCP7 XML       !OPT_XML!
echo  [2] DRT            !OPT_DRT!  (needs DaVinci Resolve Studio^)
echo  [3] All sequences  !OPT_ALL_SEQ!  (.prproj batch^)
echo  [4] Fix report     !OPT_REPORT!
echo  [5] Name suffix    !OPT_SUFFIX!  _pr2resolve tag
echo  [0] Back
echo.
choice /c 123450 /n /m "  Select [1-5, 0]: "
if errorlevel 6 goto MENU
if errorlevel 5 goto _TOG_SUFFIX
if errorlevel 4 goto _TOG_REPORT
if errorlevel 3 goto _TOG_ALLSEQ
if errorlevel 2 goto _TOG_DRT
if "!OPT_XML!"=="[ON]" (set "OPT_XML=[OFF]") else (set "OPT_XML=[ON]")
goto OPTIONS
:_TOG_ALLSEQ
if "!OPT_ALL_SEQ!"=="[ON]" (set "OPT_ALL_SEQ=[OFF]") else (set "OPT_ALL_SEQ=[ON]")
goto OPTIONS
:_TOG_REPORT
if "!OPT_REPORT!"=="[ON]" (set "OPT_REPORT=[OFF]") else (set "OPT_REPORT=[ON]")
goto OPTIONS
:_TOG_SUFFIX
if "!OPT_SUFFIX!"=="[ON]" (set "OPT_SUFFIX=[OFF]") else (set "OPT_SUFFIX=[ON]")
goto OPTIONS
:_TOG_DRT
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
if "!OPT_ALL_SEQ!"=="[ON]" set "CMD=!CMD! --all-sequences"
if "!OPT_SUFFIX!"=="[OFF]" set "CMD=!CMD! --no-suffix"
if "!OPT_XML!"=="[OFF]" set "CMD=!CMD! --no-xml"
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
