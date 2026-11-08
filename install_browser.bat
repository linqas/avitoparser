@echo off
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.playwright-browsers"
set "TMP=%~dp0.tmp"
set "TEMP=%~dp0.tmp"
if not exist ".tmp" mkdir ".tmp"
if not exist ".playwright-browsers" mkdir ".playwright-browsers"
if exist "python\python.exe" (
    "python\python.exe" -m playwright install chromium
) else if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" -m playwright install chromium
) else (
    ".venv\Scripts\python.exe" -m playwright install chromium
)
if errorlevel 1 echo Chromium installation failed. Check internet and try again.
pause
