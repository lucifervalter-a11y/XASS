@echo off
setlocal
cd /d "%~dp0"

set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS_PATH=%STARTUP_DIR%\ServerredusPCAgent.vbs"
set "RUN_BAT=%~dp0run_agent.bat"

> "%VBS_PATH%" echo Set WshShell = CreateObject("WScript.Shell")
>> "%VBS_PATH%" echo WshShell.Run chr(34) ^& "%RUN_BAT%" ^& chr(34), 0
>> "%VBS_PATH%" echo Set WshShell = Nothing

echo [pc-client] autostart installed: %VBS_PATH%
echo [pc-client] restart Windows to verify startup.

