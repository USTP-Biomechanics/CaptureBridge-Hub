@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First-time setup required. Launching Setup_CaptureBridge_Hub.bat...
    call "%~dp0Setup_CaptureBridge_Hub.bat"
    if errorlevel 1 exit /b 1
)

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "src\tcp_arduino_sync.py"
    exit /b 0
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "src\tcp_arduino_sync.py"
    set "exit_code=%errorlevel%"
    if not "%exit_code%"=="0" pause
    exit /b %exit_code%
)

python "src\tcp_arduino_sync.py"
set "exit_code=%errorlevel%"
if not "%exit_code%"=="0" pause
exit /b %exit_code%
