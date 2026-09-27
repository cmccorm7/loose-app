@echo off
setlocal
title YM Desk

rem Double-clicking does not set the working directory, so move to the folder
rem that holds this file before doing anything else.
cd /d "%~dp0"

rem The py launcher is the dependable way to find Python on Windows. Plain
rem "python" is often the Microsoft Store stub, which is not a Python at all
rem and exits without running anything, so it is only the fallback.
set "PYTHON="
py -3 --version >nul 2>&1 && set "PYTHON=py -3"
if not defined PYTHON (
    python --version >nul 2>&1 && set "PYTHON=python"
)

if not defined PYTHON (
    echo.
    echo ==================================================================
    echo   YM Desk needs Python, and could not find it on this computer.
    echo ==================================================================
    echo.
    echo   Install it from:
    echo       https://www.python.org/downloads/
    echo.
    echo   On the installer's first screen, tick
    echo       "Add python.exe to PATH"
    echo   then run this file again.
    echo.
    pause
    exit /b 1
)

%PYTHON% "%~dp0launch.py" %*
set "RESULT=%ERRORLEVEL%"

if not "%RESULT%"=="0" (
    echo.
    echo YM Desk stopped ^(code %RESULT%^). The reason should be above.
    echo.
    pause
)
exit /b %RESULT%
