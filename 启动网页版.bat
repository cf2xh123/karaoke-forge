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

".venv\Scripts\python.exe" -c "import websocket" >nul 2>&1
if errorlevel 1 (
  echo One-click NetEase login components are not installed yet.
  echo Installing them now in the existing private environment...
  ".venv\Scripts\python.exe" -m pip install -e ".[web,netease]"
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
