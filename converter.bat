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
set "OPT_XML=[ON]"

:: Colors (VT100 - requires Windows 10+ Terminal)
for /f "delims=" %%a in ('echo prompt $E ^| cmd') do set "ESC=%%a"
set "C_RED=%ESC%[0;31m"
set "C_GREEN=%ESC%[0;32m"
set "C_YELLOW=%ESC%[0;33m"
set "C_DIM=%ESC%[0;90m"
set "C_BOLD=%ESC%[1m"
set "C_PR=%ESC%[38;2;140;69;255m"
set "C_RESET=%ESC%[0m"

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
echo.
echo  %C_PR%============================================================%C_RESET%
echo  %C_BOLD%%C_PR%  prxml2fcp7xml v%VERSION%  -  PR XML to FCP7 XML Fixer%C_RESET%
echo  %C_PR%============================================================%C_RESET%
echo.
echo  %C_DIM%------------------------------------------------------------%C_RESET%
if defined INPUT_FILE (
    echo  [INPUT]  %C_GREEN%!INPUT_FILE!%C_RESET%
) else (
    echo  [INPUT]  %C_YELLOW%NOT SET%C_RESET% - Please select first
)
if defined OUTPUT_DIR (
    echo  [OUTPUT] %C_GREEN%!OUTPUT_DIR!%C_RESET%
) else (
    echo  [OUTPUT] (same as input^)
)
if defined SEQ_NAME (
    echo  [SEQ]    %C_GREEN%!SEQ_NAME!%C_RESET%
) else (
    echo  [SEQ]    (auto^)
)
echo.
if "!OPT_XML!"=="[ON]" (set "_XC=%C_GREEN%") else (set "_XC=%C_RESET%")
if "!OPT_DRT!"=="[ON]" (set "_DC=%C_GREEN%") else (set "_DC=%C_RESET%")
if "!OPT_REPORT!"=="[ON]" (set "_RC=%C_GREEN%") else (set "_RC=%C_RESET%")
echo  XML:     %_XC%!OPT_XML!%C_RESET%   FCP7 XML output
echo  DRT:     %_DC%!OPT_DRT!%C_RESET%  DaVinci DRT output (needs Resolve Studio^)
echo  Report:  %_RC%!OPT_REPORT!%C_RESET%   Fix report (.md^)
echo.
echo  %C_DIM%------------------------------------------------------------%C_RESET%
echo.
echo  [1] Select input file (.xml / .prproj^)
echo  [2] Set output directory
echo  [3] Output options
echo  [4] START
echo  [0] Quit
echo.
echo  %C_DIM%------------------------------------------------------------%C_RESET%
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
echo.
echo  %C_PR%============================================================%C_RESET%
echo  %C_BOLD%  Output Options%C_RESET%
echo  %C_PR%============================================================%C_RESET%
echo.
if "!OPT_XML!"=="[ON]" (set "_XC=%C_GREEN%") else (set "_XC=%C_RESET%")
if "!OPT_DRT!"=="[ON]" (set "_DC=%C_GREEN%") else (set "_DC=%C_RESET%")
if "!OPT_REPORT!"=="[ON]" (set "_RC=%C_GREEN%") else (set "_RC=%C_RESET%")
echo  [1] FCP7 XML       %_XC%!OPT_XML!%C_RESET%
echo  [2] DRT            %_DC%!OPT_DRT!%C_RESET%  (needs DaVinci Resolve Studio^)
echo  [3] Fix report     %_RC%!OPT_REPORT!%C_RESET%
echo  [0] Back
echo.
choice /c 1230 /n /m "  Select [1-3, 0]: "
if errorlevel 4 goto MENU
if errorlevel 3 goto _TOG_REPORT
if errorlevel 2 goto _TOG_DRT
if "!OPT_XML!"=="[ON]" (set "OPT_XML=[OFF]") else (set "OPT_XML=[ON]")
goto OPTIONS
:_TOG_REPORT
if "!OPT_REPORT!"=="[ON]" (set "OPT_REPORT=[OFF]") else (set "OPT_REPORT=[ON]")
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
