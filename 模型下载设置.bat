@echo off
setlocal
cd /d "%~dp0"
title Karaoke Forge - Model download setup
set "PYTHONUTF8=1"

if not exist ".venv\Scripts\python.exe" (
  echo First-time setup has not completed.
  echo Run the setup batch file in this folder first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m karaoke_forge model-download
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" (
  echo Model download setup did not find a working connection yet.
  echo No third-party mirror was enabled automatically.
)
pause
exit /b %RESULT%
