@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv" (
  echo 第一次執行：安裝所需套件，約需幾分鐘...
  py -m venv .venv || (echo 找不到 Python，請先安裝 Python 3（安裝時勾選 Add python.exe to PATH） & pause & exit /b 1)
  .venv\Scripts\python -m pip install --upgrade pip playwright
  .venv\Scripts\python -m playwright install chromium
)
.venv\Scripts\python download_geo.py %*
pause
