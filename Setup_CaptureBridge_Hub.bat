@echo off
setlocal
cd /d "%~dp0"

if not exist "requirements.txt" (
    echo requirements.txt not found.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if not errorlevel 1 (
        echo Creating virtual environment with py -3...
        py -3 -m venv .venv
    ) else (
        where python >nul 2>nul
        if not errorlevel 1 (
            echo Creating virtual environment with python...
            python -m venv .venv
        ) else (
            echo Python 3 was not found.
            echo Install Python 3 and then run this setup again.
            pause
            exit /b 1
        )
    )
)

echo Installing Python dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

echo.
echo Setup complete.
echo You can now start the app by double-clicking Run_CaptureBridge_Hub.bat
pause
exit /b 0
