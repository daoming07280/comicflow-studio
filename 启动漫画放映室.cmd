@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 尚未安装运行环境，请先运行 scripts\install.ps1。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "scripts\launch.py"
