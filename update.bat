@echo off
setlocal

set "PATH=C:\Program Files\Git\cmd;%PATH%"

echo [INFO] Pulling updates from origin/main...
git pull --rebase --autostash origin main
if errorlevel 1 (
  echo [ERROR] git pull failed.
  exit /b 1
)

if exist ".venv\Scripts\python.exe" (
  echo [INFO] Installing Python requirements...
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)

echo [OK] Update complete.
endlocal
