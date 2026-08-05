@echo off
setlocal

set "PATH=C:\Program Files\Git\cmd;%PATH%"

echo [INFO] Pulling updates from origin/main...
git pull --rebase --autostash origin main
if errorlevel 1 (
  echo [ERROR] git pull failed.
  exit /b 1
)

echo [INFO] Checking Python and backend requirements...
call run_server.bat --prepare-only
if errorlevel 1 (
  echo [ERROR] Dependency preparation failed.
  exit /b 1
)

echo [OK] Update complete.
endlocal
