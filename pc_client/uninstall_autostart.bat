@echo off
setlocal

set "VBS_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ServerredusPCAgent.vbs"
if exist "%VBS_PATH%" (
  del /f /q "%VBS_PATH%"
  echo [pc-client] autostart removed.
) else (
  echo [pc-client] autostart file not found.
)

