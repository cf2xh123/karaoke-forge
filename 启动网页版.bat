@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Karaoke Forge - 本地网页

if not exist ".venv\Scripts\python.exe" (
  echo 尚未完成首次安装。
  echo 请先双击“首次安装.bat”。
  pause
  exit /b 1
)

echo 正在启动 Karaoke Forge...
echo 浏览器会自动打开。请不要关闭这个窗口，使用结束后按 Ctrl+C。
".venv\Scripts\python.exe" -m karaoke_forge web

echo.
echo 网页已经停止。
pause
