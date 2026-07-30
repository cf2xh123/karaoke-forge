@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Karaoke Forge - 首次安装

echo.
echo  ==========================================
echo       Karaoke Forge 首次安装
echo  ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 Python。
  echo 请先安装 Python 3.10 或更高版本，并勾选 Add Python to PATH。
  echo https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 正在创建独立运行环境...
  python -m venv .venv
  if errorlevel 1 goto :failed
) else (
  echo [1/3] 已找到运行环境。
)

echo [2/3] 正在安装网页和歌词对齐组件，首次安装需要一些时间...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -e ".[web,align,netease,pronunciation]"
if errorlevel 1 goto :failed

echo [3/3] 正在检查 FFmpeg...
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo.
  echo [提醒] 没有找到 FFmpeg。网页可以打开，但视频暂时无法生成。
  echo 请从 https://ffmpeg.org/download.html 安装，并把 ffmpeg 加入 PATH。
) else (
  echo FFmpeg 已就绪。
)

echo.
echo 安装完成！以后直接双击“启动网页版.bat”即可。
pause
exit /b 0

:failed
echo.
echo [错误] 安装没有完成。请检查上方信息或 README.md。
pause
exit /b 1
