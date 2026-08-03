@echo off
setlocal
cd /d "%~dp0"
title Karaoke Forge - Local web app
set "PYTHONUTF8=1"

if not exist ".venv\Scripts\python.exe" (
  echo First-time setup has not completed.
  echo Run the setup batch file in this folder first.
  pause
  exit /b 1
)

echo Starting Karaoke Forge...
echo The browser will open automatically. Keep this window open.
echo Press Ctrl+C here when you want to stop the app.
".venv\Scripts\python.exe" -m karaoke_forge web

echo.
echo The web app has stopped.
pause
