@echo off
setlocal
cd /d "%~dp0"
title Karaoke Forge - Install Demucs
set "PYTHONUTF8=1"

echo.
echo  ==========================================
echo       Install Demucs vocal separation
echo  ==========================================
echo.
echo Install this only when accompaniment reduces lyric recognition accuracy.
echo The CPU build is recommended and needs about 120 MB.
echo The NVIDIA build needs an additional download of about 1.9 GB.
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] The first-time setup has not completed.
  echo Run the first-time setup batch file before installing Demucs.
  pause
  exit /b 1
)

choice /C CN /N /M "Choose [C] CPU recommended or [N] NVIDIA GPU: "
if errorlevel 2 (
  set "DEMUCS_DEVICE=cuda"
) else (
  set "DEMUCS_DEVICE=cpu"
)

".venv\Scripts\python.exe" "scripts\install_demucs.py" --device %DEMUCS_DEVICE%
if errorlevel 1 goto :failed

echo.
echo Demucs installation and checks completed. Restart the web app.
pause
exit /b 0

:failed
echo.
echo [ERROR] Demucs installation did not complete.
echo An existing CPU build, if any, was not removed.
pause
exit /b 1
