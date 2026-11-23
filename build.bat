@echo off
setlocal
cd /d "%~dp0"
title FPV DVR Stitcher - build

echo ============================================================
echo  FPV DVR Stitcher - one-time build of the Windows program
echo ============================================================
echo.

rem ---------------------------------------------------------------- Python
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY goto :nopython
echo Using Python:
%PY% --version
echo.

rem ------------------------------------------------- private Python folder
if not exist ".venv\Scripts\python.exe" (
    echo Creating build environment...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)
set "VPY=.venv\Scripts\python.exe"
echo Installing build tools (numpy, pillow, onnxruntime, pyinstaller)...
"%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

rem ------------------------------------------------------------- build exe
echo Building the program (takes a minute)...
"%VPY%" -m PyInstaller --noconfirm --clean --log-level WARN --onefile --windowed ^
    --collect-binaries onnxruntime --hidden-import PIL._tkinter_finder ^
    --name "FPV DVR Stitcher" fpv_dvr_stitcher.py
if errorlevel 1 goto :fail
copy /y README.txt dist\ >nul

rem ------------------------------------------------ FFmpeg next to the exe
if exist "dist\ffmpeg.exe" if exist "dist\ffprobe.exe" goto :ffok
for /f "delims=" %%F in ('where ffmpeg 2^>nul') do if not exist "dist\ffmpeg.exe" copy /y "%%F" dist\ >nul
for /f "delims=" %%F in ('where ffprobe 2^>nul') do if not exist "dist\ffprobe.exe" copy /y "%%F" dist\ >nul
if exist "dist\ffmpeg.exe" if exist "dist\ffprobe.exe" goto :ffok

echo Downloading FFmpeg (about 100-200 MB, one time only)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';" ^
  "Invoke-WebRequest 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip' -OutFile 'ffmpeg_dl.zip';" ^
  "Expand-Archive -Force 'ffmpeg_dl.zip' 'ffmpeg_dl';" ^
  "$b = Get-ChildItem -Recurse 'ffmpeg_dl' -Filter 'ffmpeg.exe' | Select-Object -First 1;" ^
  "Copy-Item $b.FullName 'dist\';" ^
  "Copy-Item (Join-Path $b.DirectoryName 'ffprobe.exe') 'dist\';" ^
  "Remove-Item -Recurse -Force 'ffmpeg_dl','ffmpeg_dl.zip'"
if not exist "dist\ffprobe.exe" (
    echo.
    echo  Could not download FFmpeg automatically.
    echo  Get "ffmpeg-release-essentials.zip" from https://www.gyan.dev/ffmpeg/builds/
    echo  and copy ffmpeg.exe and ffprobe.exe from its "bin" folder into the "dist" folder.
)

:ffok
echo.
echo ============================================================
echo  Done!  Your program is in the "dist" folder:
echo     dist\FPV DVR Stitcher.exe   (+ ffmpeg.exe, ffprobe.exe)
echo  You can move that whole folder anywhere, e.g. to your Desktop.
echo ============================================================
start "" explorer "dist"
pause
exit /b 0

:nopython
echo Python 3 was not found on this PC.
echo.
echo Install it (free) from https://www.python.org/downloads/
echo   - tick "Add python.exe to PATH" on the first installer screen
echo or open a Command Prompt and run:   winget install Python.Python.3.12
echo.
echo Then run build.bat again.
pause
exit /b 1

:fail
echo.
echo Something went wrong - see the messages above.
pause
exit /b 1
