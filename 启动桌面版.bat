@echo off
setlocal
cd /d "%~dp0"
title Karaoke Forge - Native desktop
set "PYTHONUTF8=1"
set "KARAOKE_FORGE_ROOT=%CD%"
set "KARAOKE_FORGE_FFMPEG_DIR=%CD%\.runtime\ffmpeg\bin"

if not exist ".venv\Scripts\python.exe" (
  echo First-time setup has not completed. Run the setup batch file first.
  pause
  exit /b 1
)

if not exist "%KARAOKE_FORGE_FFMPEG_DIR%\ffmpeg.exe" goto :repair_ffmpeg
if not exist "%KARAOKE_FORGE_FFMPEG_DIR%\ffprobe.exe" goto :repair_ffmpeg
"%KARAOKE_FORGE_FFMPEG_DIR%\ffmpeg.exe" -hide_banner -version >nul 2>nul
if errorlevel 1 goto :repair_ffmpeg
"%KARAOKE_FORGE_FFMPEG_DIR%\ffprobe.exe" -hide_banner -version >nul 2>nul
if errorlevel 1 goto :repair_ffmpeg
goto :ffmpeg_ready

:repair_ffmpeg
echo Private FFmpeg or ffprobe is missing or unusable. Repairing it now...
where powershell.exe >nul 2>nul
if errorlevel 1 (
  echo Windows PowerShell was not found, so the private runtime cannot be repaired.
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "scripts\bootstrap_ffmpeg_windows.ps1"
if errorlevel 1 (
  echo Could not prepare the private FFmpeg runtime.
  pause
  exit /b 1
)

:ffmpeg_ready
set "PATH=%KARAOKE_FORGE_FFMPEG_DIR%;%PATH%"

".venv\Scripts\python.exe" -c "import karaoke_forge.desktop; from PySide6.QtWidgets import QApplication; from PySide6.QtMultimedia import QMediaPlayer; import inspect, websocket, yt_dlp, pykakasi, alkana; from faster_whisper.utils import download_model; raise SystemExit(0 if 'revision' in inspect.signature(download_model).parameters else 1)" >nul 2>&1
if errorlevel 1 (
  echo Installing native desktop components into the private environment...
  ".venv\Scripts\python.exe" -m pip install --upgrade -e ".[desktop,align,netease,pronunciation]"
  if errorlevel 1 (
    echo Could not install desktop components. Check the network and try again.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -c "from karaoke_forge.desktop.app import MainWindow" >nul 2>&1
if errorlevel 1 (
  echo Desktop dependencies could not load. Diagnostic information:
  ".venv\Scripts\python.exe" -c "from karaoke_forge.desktop.app import MainWindow"
  pause
  exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" -m karaoke_forge desktop %*
exit /b 0
