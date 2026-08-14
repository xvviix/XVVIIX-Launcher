@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title XVVIIX Launcher

where py.exe >nul 2>&1
if not errorlevel 1 (
    py -3 "game_launcher.py"
    set "XVVIIX_EXIT=!errorlevel!"
    goto :finished
)

where python.exe >nul 2>&1
if not errorlevel 1 (
    python "game_launcher.py"
    set "XVVIIX_EXIT=!errorlevel!"
    goto :finished
)

echo ERROR: Python was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo During setup, enable "Add python.exe to PATH" and "tcl/tk and IDLE".
set "XVVIIX_EXIT=1"

:finished
if not "!XVVIIX_EXIT!"=="0" (
    echo.
    echo XVVIIX Launcher exited with code !XVVIIX_EXIT!.
    echo Review xvviix_launcher.log in this folder or in %%LOCALAPPDATA%%\XVVIIXLauncher.
    pause
)
exit /b !XVVIIX_EXIT!
