@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Karaoke Forge - 安装 Demucs 人声分离

echo.
echo  ==========================================
echo       安装 Demucs 人声分离（可选）
echo  ==========================================
echo.
echo  仅在复杂伴奏让歌词识别不准时需要。
echo  CPU 版约 120 MB，实测一首 218 秒歌曲约 57 秒完成。
echo  NVIDIA 版另需下载约 1.9 GB，速度更快但不是必须。
echo  首次实际分离还会联网下载所选模型，之后会使用本机缓存。
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 请先双击“首次安装.bat”。
  pause
  exit /b 1
)

echo.
choice /C CN /N /M "请选择：[C] CPU 版（推荐）  [N] NVIDIA 显卡版："
if errorlevel 2 (
  set "DEMUCS_DEVICE=cuda"
) else (
  set "DEMUCS_DEVICE=cpu"
)

".venv\Scripts\python.exe" "scripts\install_demucs.py" --device %DEMUCS_DEVICE%
if errorlevel 1 goto :failed

echo.
echo 安装与检查完成。请重新启动网页版。
pause
exit /b 0

:failed
echo.
echo [错误] 安装没有完成。已安装的 CPU 版本如仍可用，不会被删除。
pause
exit /b 1
