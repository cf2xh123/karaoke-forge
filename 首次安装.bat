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
echo It also installs a private FFmpeg runtime inside the project.
echo It does not require Conda, system Python, administrator access,
echo or changes to the system PATH.
echo.

where powershell.exe >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Windows PowerShell was not found.
  goto :failed
)

set "VENV_READY=0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
  if not errorlevel 1 set "VENV_READY=1"
)

if "%VENV_READY%"=="0" (
  echo [1/6] Preparing private Python 3.12 runtime...
  powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "scripts\bootstrap_windows.ps1"
  if errorlevel 1 goto :failed

  echo [2/6] Creating the isolated project environment...
  ".runtime\python\tools\python.exe" -m venv --clear ".venv"
  if errorlevel 1 goto :failed
) else (
  echo [1/6] Private project environment already exists.
  echo [2/6] Reusing the existing project environment.
)

echo [3/6] Installing the web app and lyric alignment components...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -e ".[web,align,netease,pronunciation]"
if errorlevel 1 goto :failed

echo [4/6] Installing private FFmpeg 8.1.2...
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "scripts\bootstrap_ffmpeg_windows.ps1"
if errorlevel 1 goto :failed

echo [5/6] Verifying private FFmpeg...
if not exist ".runtime\ffmpeg\bin\ffmpeg.exe" goto :failed
if not exist ".runtime\ffmpeg\bin\ffprobe.exe" goto :failed
set "PATH=%CD%\.runtime\ffmpeg\bin;%PATH%"
".runtime\ffmpeg\bin\ffmpeg.exe" -hide_banner -version >nul 2>nul
if errorlevel 1 goto :failed
echo Private FFmpeg is ready.

echo [6/6] Configuring model downloads...
echo Press Enter to try the verified domestic source, official source, and local proxy.
".venv\Scripts\python.exe" -m karaoke_forge model-download
if errorlevel 1 (
  echo.
  echo [WARNING] Model download access is not configured yet.
  echo Setup can still finish. Run the model download setup batch file later.
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
echo Existing files are kept, and an interrupted FFmpeg update is rolled back.
pause
exit /b 1
