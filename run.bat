@echo off
rem Runs FPV DVR Stitcher straight from the Python source (no .exe build needed).
rem Needs Python 3 and FFmpeg (ffmpeg.exe + ffprobe.exe next to this file or on the PATH).
setlocal
cd /d "%~dp0"
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    echo Python 3 was not found. Install it from https://www.python.org/downloads/
    echo or run:  winget install Python.Python.3.12
    pause
    exit /b 1
)
if not exist ".venv\Scripts\pythonw.exe" (
    echo First run - setting up, please wait...
    %PY% -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
)
start "" ".venv\Scripts\pythonw.exe" fpv_dvr_stitcher.py %*
