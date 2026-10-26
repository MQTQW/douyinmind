@echo off
chcp 65001 >nul
cd /d "%~dp0backend"
".venv\Scripts\python.exe" -m app.assistant
if errorlevel 1 pause
