@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title XVVIIX Launcher

if not exist "game_launcher.py" goto :missing_source
if exist ".venv\Scripts\python.exe" goto :run_venv

where py.exe >nul 2>&1
if not errorlevel 1 goto :run_py

where python.exe >nul 2>&1
if not errorlevel 1 goto :run_python

echo ERROR: Python was not found.
echo Install Python 3.11 or newer from https://www.python.org/downloads/windows/
echo During setup, enable "Add python.exe to PATH" and "tcl/tk and IDLE".
set "XVVIIX_EXIT=1"
goto :finished

:run_venv
echo Starting XVVIIX with the project's Windows virtual environment...
".venv\Scripts\python.exe" "game_launcher.py"
set "XVVIIX_EXIT=%errorlevel%"
goto :finished

:run_py
echo No Windows .venv found. Starting with the system Python launcher...
py -3 "game_launcher.py"
set "XVVIIX_EXIT=%errorlevel%"
goto :finished

:run_python
echo No Windows .venv found. Starting with Python from PATH...
python "game_launcher.py"
set "XVVIIX_EXIT=%errorlevel%"
goto :finished

:missing_source
echo ERROR: game_launcher.py was not found beside START_XVVIIX.bat.
echo Extract the complete project before starting the launcher.
set "XVVIIX_EXIT=1"

:finished
if not "%XVVIIX_EXIT%"=="0" (
    echo.
    echo XVVIIX Launcher exited with code %XVVIIX_EXIT%.
    echo Review xvviix_launcher.log in this folder or in %%LOCALAPPDATA%%\XVVIIXLauncher.
    echo To prepare a Windows environment, run these commands in this folder:
    echo   py -3 -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
)
exit /b %XVVIIX_EXIT%
