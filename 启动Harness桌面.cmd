@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\desktop\Launch-Desktop.ps1"
set "LEARNING_DESKTOP_EXIT_CODE=%ERRORLEVEL%"
if not "%LEARNING_DESKTOP_EXIT_CODE%"=="0" pause
exit /b %LEARNING_DESKTOP_EXIT_CODE%
