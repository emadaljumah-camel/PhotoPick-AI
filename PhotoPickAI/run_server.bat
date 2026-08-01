@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo PhotoPickAI virtual environment was not found at .venv\Scripts\python.exe
    echo Create it first, then install requirements:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo   .venv\Scripts\python.exe -m pip install -r server\requirements.txt
    exit /b 1
)

".venv\Scripts\python.exe" -m uvicorn server.main:app --host 0.0.0.0 --port 8000

endlocal
