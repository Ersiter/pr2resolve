@echo off
setlocal enabledelayedexpansion

set "VERSION=1.0.0"
set "SCRIPT=%~dp0prxml_to_fcp7xml.py"
set "PYTHONIOENCODING=utf-8"

set "PYTHON_CMD="
where python >nul 2>&1 && set "PYTHON_CMD=python"
if not defined PYTHON_CMD where py >nul 2>&1 && set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    echo ERROR: Python not found.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo ERROR: prxml_to_fcp7xml.py not found in %~dp0
    pause
    exit /b 1
)

set "INPUT_FILE="
set "OUTPUT_DIR="

:MENU
cls
echo ============================================================
echo   prxml2fcp7xml v%VERSION%  -  PR XML to FCP7 XML Fixer
echo ============================================================
echo.
if defined INPUT_FILE (
    echo   [INPUT]  !INPUT_FILE!
) else (
    echo   [INPUT]  NOT SET
)
if defined OUTPUT_DIR (
    echo   [OUTPUT] !OUTPUT_DIR!
) else (
    echo   [OUTPUT] (same as input)
)
echo.
echo   [1] Select input file
echo   [2] Set output directory
echo   [3] START
echo   [0] Quit
echo.
choice /c 1230 /n /m "  Select: "
if errorlevel 4 goto DONE
if errorlevel 3 goto RUN
if errorlevel 2 goto OUT
if errorlevel 1 goto INP

:INP
echo.
set "INPUT_FILE="
set /p "INPUT_FILE=  Path: "
if not defined INPUT_FILE goto MENU
call :stripquotes INPUT_FILE
if not exist "!INPUT_FILE!" (
    echo   Not found: !INPUT_FILE!
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

:RUN
if not defined INPUT_FILE (
    echo   No input file.
    pause
    goto MENU
)
echo.
echo   Running...
echo.
set "CMD=!PYTHON_CMD! "!SCRIPT!" "!INPUT_FILE!""
if defined OUTPUT_DIR set "CMD=!CMD! -o "!OUTPUT_DIR!""
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
