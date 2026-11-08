@echo off
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.playwright-browsers"
set "TMP=%~dp0.tmp"
set "TEMP=%~dp0.tmp"
if not exist ".tmp" mkdir ".tmp"
if exist "python\python.exe" (
    "python\python.exe" main.py
) else if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" main.py
) else (
    ".venv\Scripts\python.exe" main.py
)
if errorlevel 1 pause
