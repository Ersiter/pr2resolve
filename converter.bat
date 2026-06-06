@echo off
setlocal enabledelayedexpansion

:: ============================================================
:: prxml2fcp7xml - Windows TUI Launcher
:: ============================================================

set "VERSION=1.0.0"
set "SCRIPT=%~dp0prxml_to_fcp7xml.py"

:: State
set "INPUT_FILE="
set "OUTPUT_DIR="
set "SEQ_NAME="
set "OPT_XML=[ON]"
set "OPT_DRT=[OFF]"
set "OPT_REPORT=[ON]"

:: Find Python
set "PYTHON_CMD="
set "PYTHONIOENCODING=utf-8"
where python >nul 2>&1 && set "PYTHON_CMD=python"
if not defined PYTHON_CMD where py >nul 2>&1 && set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    echo ERROR: Python not found. Please install Python 3.8+ and add to PATH.
    pause
    exit /b 1
)

:: Check script exists
if not exist "%SCRIPT%" (
    echo ERROR: prxml_to_fcp7xml.py not found in %~dp0
    pause
    exit /b 1
)

:MAIN_MENU
cls
echo ============================================================
echo   prxml2fcp7xml v%VERSION%  -  PR XML to FCP7 XML Fixer
echo ============================================================
echo.
if defined INPUT_FILE (
    echo   [INPUT]  %INPUT_FILE%
) else (
    echo   [INPUT]  NOT SET - Please select first
)
if defined OUTPUT_DIR (
    echo   [OUTPUT] %OUTPUT_DIR%
) else (
    echo   [OUTPUT] (same as input)
)
if defined SEQ_NAME (
    echo   [SEQ]    %SEQ_NAME%
) else (
    echo   [SEQ]    (auto)
)
echo.
echo   XML:     %OPT_XML%   FCP7 XML output (always on)
echo   DRT:     %OPT_DRT%  DaVinci DRT output (needs Resolve Studio)
echo   Report:  %OPT_REPORT%   Fix report (.md)
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
if errorlevel 5 goto QUIT
if errorlevel 4 goto START
if errorlevel 3 goto OPTIONS_MENU
if errorlevel 2 goto SET_OUTPUT
if errorlevel 1 goto SELECT_INPUT

:SELECT_INPUT
echo.
echo  Enter path to input file (.xml or .prproj):
echo  (or drag and drop the file onto this window)
echo.
set "INPUT_FILE="
set /p "INPUT_FILE=  > "
if not defined INPUT_FILE goto _INPUT_EMPTY
set "INPUT_FILE=%INPUT_FILE:"=%"
if not defined INPUT_FILE goto _INPUT_EMPTY
if exist "%INPUT_FILE%" goto MAIN_MENU
echo  File not found: %INPUT_FILE%
timeout /t 2 >nul
goto MAIN_MENU
:_INPUT_EMPTY
echo  No file specified.
timeout /t 2 >nul
goto MAIN_MENU

:SET_OUTPUT
echo.
echo  Enter output directory (or press Enter for same as input):
set "OUTPUT_DIR="
set /p "OUTPUT_DIR=  > "
if not defined OUTPUT_DIR goto MAIN_MENU
set "OUTPUT_DIR=%OUTPUT_DIR:"=%"
if not defined OUTPUT_DIR goto MAIN_MENU
if exist "%OUTPUT_DIR%" goto MAIN_MENU
mkdir "%OUTPUT_DIR%" 2>nul
if exist "%OUTPUT_DIR%" goto MAIN_MENU
echo  Cannot create directory: %OUTPUT_DIR%
set "OUTPUT_DIR="
timeout /t 2 >nul
goto MAIN_MENU

:OPTIONS_MENU
cls
echo ============================================================
echo   Output Options
echo ============================================================
echo.
echo   [1] FCP7 XML       %OPT_XML%    (always on, primary output)
echo   [2] DRT            %OPT_DRT%   (optional, needs DaVinci Resolve Studio)
echo   [3] Fix report     %OPT_REPORT%
echo   [0] Back
echo.
choice /c 1230 /n /m "  Select [1-3, 0]: "
if errorlevel 4 goto MAIN_MENU
if errorlevel 3 (
    if "%OPT_REPORT%"=="[ON]" (set "OPT_REPORT=[OFF]") else (set "OPT_REPORT=[ON]")
    goto OPTIONS_MENU
)
if errorlevel 2 (
    if "%OPT_DRT%"=="[ON]" (set "OPT_DRT=[OFF]") else (set "OPT_DRT=[ON]")
    goto OPTIONS_MENU
)
if errorlevel 1 goto OPTIONS_MENU

:START
if not defined INPUT_FILE (
    echo.
    echo  ERROR: No input file selected.
    timeout /t 2 >nul
    goto MAIN_MENU
)

echo.
echo  Running...
echo.

:: Build command
set "CMD=%PYTHON_CMD% "%SCRIPT%" "%INPUT_FILE%""

if defined OUTPUT_DIR (
    set "CMD=!CMD! -o "%OUTPUT_DIR%""
)
if defined SEQ_NAME (
    set "CMD=!CMD! --sequence "%SEQ_NAME%""
)
if "%OPT_REPORT%"=="[ON]" (
    set "CMD=!CMD! --report"
)
if "%OPT_DRT%"=="[ON]" (
    set "CMD=!CMD! --drt"
)

:: Run
%CMD%
echo.
echo  Press any key to return to menu...
pause >nul
goto MAIN_MENU

:QUIT
echo.
echo  Goodbye.
endlocal
exit /b 0
