@echo off
setlocal
cd /d "%~dp0"

wscript.exe "%~dp0run_tray.vbs"
endlocal
