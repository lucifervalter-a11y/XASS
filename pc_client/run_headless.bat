@echo off
setlocal
cd /d "%~dp0"

call run_agent.bat --prepare-only
if errorlevel 1 exit /b 1
if not exist ".venv\Scripts\python.exe" exit /b 1

call ".venv\Scripts\activate"
python client_agent.py %*
