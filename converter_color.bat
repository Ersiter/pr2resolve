@echo off
setlocal enabledelayedexpansion

:: ============================================================
:: converter_color.bat - VT100 Color Experiment
:: ============================================================
:: Separate from converter.bat for testing color support.
:: When colors are confirmed working, merge into converter.bat.
::
:: Requires: Windows 10+ with VT100 support enabled.
:: Enable: reg add HKCU\Console /v VirtualTerminalLevel /t REG_DWORD /d 1 /f
:: Or use Windows Terminal (enabled by default).
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

:: Generate ESC character (0x1B)
for /f "delims=" %%a in ('echo prompt $E ^| cmd') do set "ESC=%%a"

:: Color definitions
set "C_RED=%ESC%[0;31m"
set "C_GREEN=%ESC%[0;32m"
set "C_YELLOW=%ESC%[0;33m"
set "C_DIM=%ESC%[0;90m"
set "C_BOLD=%ESC%[1m"
:: Premiere Pro purple (RGB 140,69,255)
set "C_PR=%ESC%[38;2;140;69;255m"
set "NC=%ESC%[0m"

:: Verify VT100 works - if not, colors will show as garbage
echo  %C_PR%VT100 color test%C_RESET%
echo  If you see escape codes above instead of purple text,
echo  your terminal does not support VT100.
echo.
echo  Fix: Use Windows Terminal, or enable VT100:
echo    reg add HKCU\Console /v VirtualTerminalLevel /t REG_DWORD /d 1 /f
echo.
pause

:: Find Python
set "PYTHON_CMD="
where python >nul 2>&1 && set "PYTHON_CMD=python"
if not defined PYTHON_CMD where py >nul 2>&1 && set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    echo %C_RED%ERROR: Python not found.%NC%
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo %C_RED%ERROR: prxml_to_fcp7xml.py not found in %~dp0%NC%
    pause
    exit /b 1
)

:MENU
cls
echo.
echo  %C_PR%============================================================%NC%
echo  %C_BOLD%%C_PR%  prxml2fcp7xml v%VERSION%  -  PR XML to FCP7 XML Fixer%NC%
echo  %C_PR%============================================================%NC%
echo.
echo  %C_DIM%------------------------------------------------------------%NC%
if defined INPUT_FILE (
    echo  [INPUT]  %C_GREEN%!INPUT_FILE!%NC%
) else (
    echo  [INPUT]  %C_YELLOW%NOT SET%NC% - Please select first
)
if defined OUTPUT_DIR (
    echo  [OUTPUT] %C_GREEN%!OUTPUT_DIR!%NC%
) else (
    echo  [OUTPUT] (same as input^)
)
if defined SEQ_NAME (
    echo  [SEQ]    %C_GREEN%!SEQ_NAME!%NC%
) else (
    echo  [SEQ]    (auto^)
)
echo.
if "!OPT_XML!"=="[ON]" (set "_XC=%C_GREEN%") else (set "_XC=%NC%")
if "!OPT_DRT!"=="[ON]" (set "_DC=%C_GREEN%") else (set "_DC=%NC%")
if "!OPT_REPORT!"=="[ON]" (set "_RC=%C_GREEN%") else (set "_RC=%NC%")
echo  XML:     %_XC%!OPT_XML!%NC%   FCP7 XML output
echo  DRT:     %_DC%!OPT_DRT!%NC%  DaVinci DRT output (needs Resolve Studio^)
echo  Report:  %_RC%!OPT_REPORT!%NC%   Fix report (.md^)
echo.
echo  %C_DIM%------------------------------------------------------------%NC%
echo.
echo  [1] Select input file (.xml / .prproj^)
echo  [2] Set output directory
echo  [3] Output options
echo  [4] START
echo  [0] Quit
echo.
echo  %C_DIM%------------------------------------------------------------%NC%
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
    echo  %C_RED%File not found: !INPUT_FILE!%NC%
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
echo  %C_PR%============================================================%NC%
echo  %C_BOLD%  Output Options%NC%
echo  %C_PR%============================================================%NC%
echo.
if "!OPT_XML!"=="[ON]" (set "_XC=%C_GREEN%") else (set "_XC=%NC%")
if "!OPT_DRT!"=="[ON]" (set "_DC=%C_GREEN%") else (set "_DC=%NC%")
if "!OPT_REPORT!"=="[ON]" (set "_RC=%C_GREEN%") else (set "_RC=%NC%")
echo  [1] FCP7 XML       %_XC%!OPT_XML!%NC%
echo  [2] DRT            %_DC%!OPT_DRT!%NC%  (needs DaVinci Resolve Studio^)
echo  [3] Fix report     %_RC%!OPT_REPORT!%NC%
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
    echo  %C_RED%No input file selected.%NC%
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
