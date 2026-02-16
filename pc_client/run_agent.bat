@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [pc-client] creating virtual environment...
  python -m venv .venv
)

call ".venv\Scripts\activate"
if not exist ".deps_installed" (
  echo [pc-client] installing dependencies...
  python -m pip install --upgrade pip
  pip install -r requirements.txt
  type nul > .deps_installed
)

python client_agent.py %*
