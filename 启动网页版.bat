@echo off
setlocal
cd /d "%~dp0"
title Karaoke Forge - Local web app
set "PYTHONUTF8=1"
set "KARAOKE_FORGE_ROOT=%CD%"
set "KARAOKE_FORGE_FFMPEG_DIR=%CD%\.runtime\ffmpeg\bin"

if not exist "%KARAOKE_FORGE_FFMPEG_DIR%\ffmpeg.exe" goto :repair_ffmpeg
if not exist "%KARAOKE_FORGE_FFMPEG_DIR%\ffprobe.exe" goto :repair_ffmpeg
"%KARAOKE_FORGE_FFMPEG_DIR%\ffmpeg.exe" -hide_banner -version >nul 2>nul
if errorlevel 1 goto :repair_ffmpeg
"%KARAOKE_FORGE_FFMPEG_DIR%\ffprobe.exe" -hide_banner -version >nul 2>nul
if errorlevel 1 goto :repair_ffmpeg
goto :ffmpeg_ready

:repair_ffmpeg
echo Private FFmpeg is missing. Installing it now...
where powershell.exe >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Windows PowerShell was not found, so FFmpeg cannot be repaired.
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "scripts\bootstrap_ffmpeg_windows.ps1"
if errorlevel 1 (
  echo.
  echo [ERROR] Could not install the private FFmpeg runtime.
  echo Check your internet connection, then run this launcher again.
  pause
  exit /b 1
)

:ffmpeg_ready
set "PATH=%KARAOKE_FORGE_FFMPEG_DIR%;%PATH%"

if not exist ".venv\Scripts\python.exe" (
  echo First-time setup has not completed.
  echo Run the setup batch file in this folder first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -c "import inspect, websocket; from faster_whisper.utils import download_model; raise SystemExit(0 if 'revision' in inspect.signature(download_model).parameters else 1)" >nul 2>&1
if errorlevel 1 (
  echo Required app components are missing or outdated.
  echo Repairing them now in the existing private environment...
  ".venv\Scripts\python.exe" -m pip install --upgrade -e ".[web,align,netease,pronunciation]"
  if errorlevel 1 (
    echo.
    echo [ERROR] Could not install the one-click NetEase login components.
    echo Check your internet connection, then run this launcher again.
    pause
    exit /b 1
  )
)

echo Starting Karaoke Forge...
echo The browser will open automatically. Keep this window open.
echo Press Ctrl+C here when you want to stop the app.
".venv\Scripts\python.exe" -m karaoke_forge web

echo.
echo The web app has stopped.
pause
