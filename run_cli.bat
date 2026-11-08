@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
set "PYTHON=%~dp0python\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0.venv312\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
if "%~1"=="" (
  echo Avito Radar CLI
  echo.
  echo Команды:
  echo   run_cli.bat list
  echo   run_cli.bat run --once
  echo   run_cli.bat run --once --no-telegram
  echo   run_cli.bat export exports\manual.xlsx
  echo   run_cli.bat add "https://www.avito.ru/..."
  echo.
  "%PYTHON%" cli.py list
  echo.
  pause
  exit /b 0
)
"%PYTHON%" cli.py %*
exit /b %ERRORLEVEL%
