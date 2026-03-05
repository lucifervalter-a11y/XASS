@echo off
setlocal
cd /d "%~dp0"

set "PY_CMD="
where py >nul 2>nul
if %errorlevel%==0 set "PY_CMD=py -3"
if not defined PY_CMD (
  where python >nul 2>nul
  if %errorlevel%==0 set "PY_CMD=python"
)
if not defined PY_CMD (
  echo [pc-client] Python not found. Install Python 3.11+ and retry.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [pc-client] creating virtual environment...
  %PY_CMD% -m venv .venv
  if errorlevel 1 (
    echo [pc-client] failed to create virtualenv.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate"
echo [pc-client] installing/updating dependencies...
python -m pip install --upgrade pip
if errorlevel 1 (
  echo [pc-client] pip upgrade failed.
  pause
  exit /b 1
)
pip install -r requirements.txt
if errorlevel 1 (
  echo [pc-client] dependency installation failed.
  pause
  exit /b 1
)

python client_agent.py %*
