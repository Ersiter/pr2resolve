@echo off
setlocal enabledelayedexpansion

:: ============================================================
:: _color.bat - VT100 Color Test & Reference
:: ============================================================
:: Based on: https://learn.microsoft.com/zh-cn/windows/console/console-virtual-terminal-sequences
::
:: Windows 10+ cmd.exe supports VT100 escape sequences.
:: ESC = 0x1B (ASCII 27), written as literal byte in batch.
:: This file generates the ESC char and defines color variables.
::
:: Usage: call _color.bat (sets color variables for use in other scripts)
:: ============================================================

:: Generate ESC character (0x1B)
for /f "delims=" %%a in ('echo prompt $E ^| cmd') do set "ESC=%%a"

:: -- Basic colors --
set "C_RED=%ESC%[0;31m"
set "C_GREEN=%ESC%[0;32m"
set "C_YELLOW=%ESC%[0;33m"
set "C_BLUE=%ESC%[0;34m"
set "C_MAGENTA=%ESC%[0;35m"
set "C_CYAN=%ESC%[0;36m"
set "C_WHITE=%ESC%[0;37m"

:: -- Bright / dim --
set "C_DIM=%ESC%[0;90m"
set "C_BRIGHT_RED=%ESC%[0;91m"
set "C_BRIGHT_GREEN=%ESC%[0;92m"

:: -- Styles --
set "C_BOLD=%ESC%[1m"
set "C_RESET=%ESC%[0m"

:: -- Premiere Pro purple (RGB 140,69,255) --
:: Matches converter.sh PR_PURPLE
set "C_PR_PURPLE=%ESC%[38;2;140;69;255m"

:: -- Test display --
if "%~1"=="--test" (
    echo.
    echo  ============================================================
    echo   VT100 Color Test - _color.bat
    echo  ============================================================
    echo.
    echo  Basic:
    echo    %C_RED%RED%C_RESET%  %C_GREEN%GREEN%C_RESET%  %C_YELLOW%YELLOW%C_RESET%  %C_BLUE%BLUE%C_RESET%  %C_MAGENTA%MAGENTA%C_RESET%  %C_CYAN%CYAN%C_RESET%
    echo.
    echo  Bright:
    echo    %C_BRIGHT_RED%BRIGHT_RED%C_RESET%  %C_BRIGHT_GREEN%BRIGHT_GREEN%C_RESET%  %C_DIM%DIM%C_RESET%
    echo.
    echo  Styles:
    echo    %C_BOLD%BOLD%C_RESET%  normal
    echo.
    echo  PR Purple:
    echo    %C_PR_PURPLE%Premiere Pro Purple (140,69,255)%C_RESET%
    echo.
    echo  Mixed:
    echo    %C_PR_PURPLE%%C_BOLD%prxml2fcp7xml v1.0.0%C_RESET%
    echo    %C_GREEN%[ON]%C_RESET%  %C_RED%[OFF]%C_RESET%  %C_YELLOW%NOT SET%C_RESET%
    echo    %C_DIM%------------------------------------------------------------%C_RESET%
    echo.
    echo  ============================================================
    echo.

    :: Enable VT100 if not already enabled
    echo  If colors above show as raw escape codes, run in Windows Terminal
    echo  or enable VT100: reg add HKCU\Console /v VirtualTerminalLevel /t REG_DWORD /d 1 /f
    echo.
)

endlocal
