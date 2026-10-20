@echo off
chcp 65001 >nul
if not exist "%~dp0backend\.venv\Scripts\python.exe" (
  echo Backend Python environment missing: backend\.venv
  pause
  exit /b 1
)
"%~dp0backend\.venv\Scripts\python.exe" "%~dp0start-local.py"
if errorlevel 1 pause
