@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up Python virtual environment for first-time launch...
    python -m venv ".venv"
    if errorlevel 1 (
        echo Failed to create virtual environment. Make sure Python 3.10+ is installed.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
) else (
    ".venv\Scripts\python.exe" -c "import numpy, soundfile, lz4, UnityPy, PIL, texture2ddecoder" >nul 2>&1
    if errorlevel 1 (
        echo Installing updated dependencies for v1.2.0...
        ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    )
)

start "" ".venv\Scripts\pythonw.exe" "%~dp0app.py"
