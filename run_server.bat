@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON_EXE="
call :find_python
if defined PYTHON_EXE goto python_ready

echo [xass-server] Python 3.11+ not found. Trying automatic installation...
where winget >nul 2>nul
if errorlevel 1 goto python_install_failed
winget install --id Python.Python.3.12 --exact --source winget --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto python_install_failed
call :find_python
if not defined PYTHON_EXE goto python_install_failed

:python_ready
"%PYTHON_EXE%" bootstrap_server_dependencies.py
if errorlevel 1 goto dependency_install_failed
if /I "%~1"=="--prepare-only" exit /b 0

set "XASS_PORT=%PORT%"
if not defined XASS_PORT set "XASS_PORT=8000"
echo [xass-server] Starting backend on 0.0.0.0:%XASS_PORT%...
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port %XASS_PORT% %*
exit /b %errorlevel%

:find_python
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if defined PYTHON_EXE call :validate_python
if defined PYTHON_EXE exit /b 0
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
if defined PYTHON_EXE call :validate_python
if defined PYTHON_EXE exit /b 0
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
if defined PYTHON_EXE call :validate_python
if defined PYTHON_EXE exit /b 0
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined PYTHON_EXE if exist "%%~fD\python.exe" set "PYTHON_EXE=%%~fD\python.exe"
if defined PYTHON_EXE call :validate_python
if defined PYTHON_EXE exit /b 0
for /d %%D in ("%ProgramFiles%\Python3*") do if not defined PYTHON_EXE if exist "%%~fD\python.exe" set "PYTHON_EXE=%%~fD\python.exe"
if defined PYTHON_EXE call :validate_python
exit /b 0

:validate_python
"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 set "PYTHON_EXE="
exit /b 0

:python_install_failed
echo [xass-server] Automatic Python installation failed.
echo [xass-server] Install Python 3.11+ or App Installer/winget and retry.
if /I not "%~1"=="--prepare-only" pause
exit /b 1

:dependency_install_failed
echo [xass-server] Dependency installation failed. Check internet access and retry.
if /I not "%~1"=="--prepare-only" pause
exit /b 1
