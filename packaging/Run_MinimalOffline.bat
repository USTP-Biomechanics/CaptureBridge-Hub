@echo off
setlocal
cd /d "%~dp0"

set "PYTHONNOUSERSITE=1"
set "PYTHONPATH="

if exist "%~dp0python\pythonw.exe" (
    start "" "%~dp0python\pythonw.exe" "%~dp0app\tcp_arduino_sync.py"
    exit /b 0
)

if exist "%~dp0python\python.exe" (
    "%~dp0python\python.exe" "%~dp0app\tcp_arduino_sync.py"
    set "exit_code=%errorlevel%"
    if not "%exit_code%"=="0" pause
    exit /b %exit_code%
)

echo Portable Python runtime not found.
echo Expected: %~dp0python\python.exe
pause
exit /b 1
