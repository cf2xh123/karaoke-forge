@echo off
setlocal
cd /d "%~dp0"
title Karaoke Forge - First-time setup
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

echo.
echo  ==========================================
echo       Karaoke Forge first-time setup
echo  ==========================================
echo.
echo This setup uses a private Python runtime inside the project.
echo It does not require Conda or a system-wide Python installation.
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Preparing private Python 3.12 runtime...
  where powershell.exe >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Windows PowerShell was not found.
    goto :failed
  )
  powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "scripts\bootstrap_windows.ps1"
  if errorlevel 1 goto :failed

  echo [2/4] Creating the isolated project environment...
  ".runtime\python\tools\python.exe" -m venv ".venv"
  if errorlevel 1 goto :failed
) else (
  echo [1/4] Private project environment already exists.
  echo [2/4] Reusing the existing project environment.
)

echo [3/4] Installing the web app and lyric alignment components...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -e ".[web,align,netease,pronunciation]"
if errorlevel 1 goto :failed

echo [4/4] Checking FFmpeg...
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo [WARNING] FFmpeg was not found on PATH.
  echo The web app can open, but video generation will not work yet.
  echo Download FFmpeg from https://ffmpeg.org/download.html and add it to PATH.
) else (
  echo FFmpeg is ready.
)

echo.
echo Setup completed successfully.
echo You can now double-click the web launcher batch file.
echo Demucs vocal separation remains an optional separate install.
pause
exit /b 0

:failed
echo.
echo [ERROR] Setup did not complete.
echo Check the messages above and your network connection, then run this file again.
pause
exit /b 1
