@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo                 PeakCut (Local Mode)
echo ========================================================
echo.

:: 0. Stop any previous instance running on port 8765
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)

:: 1. Check Python
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python is not found on your system PATH.
    echo Please install Python 3.10+ and make sure it is added to PATH.
    pause
    exit /b 1
)

:: 2. Check Virtual Environment (.venv)
if not exist ".venv\Scripts\python.exe" (
    echo [*] Setting up virtual environment in .venv...
    python -m venv .venv
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [*] Installing requirements...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to install requirements.
        pause
        exit /b 1
    )
) else (
    ".venv\Scripts\python.exe" -c "import faster_whisper, psutil, fastapi, sqlalchemy, httpx, uvicorn" >nul 2>&1
    if %ERRORLEVEL% neq 0 (
        echo [*] New dependencies detected. Installing/updating packages in .venv...
        ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    )
)

:: 3. Check FFmpeg
where ffmpeg >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [WARNING] ffmpeg was not detected on your PATH!
    echo PeakCut uses ffmpeg to cut, convert, and render video clips.
    echo If clips fail to render, install ffmpeg and add it to PATH.
    echo.
)

:: 4. Check Speech Model
if not exist "data\models\sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8\tokens.txt" (
    echo [*] Speech-to-text model not found.
    echo [*] Downloading local speech model (Parakeet, ~480 MB)...
    echo     This runs 100%% offline on your device once downloaded.
    ".venv\Scripts\python.exe" -m studio.fetch_model
    if %ERRORLEVEL% neq 0 (
        echo [WARNING] Speech model download failed or was cancelled.
        echo You can download it later by running: .venv\Scripts\python.exe -m studio.fetch_model
    )
)

:: 5. Open browser in background
start "" http://127.0.0.1:8765

:: 6. Launch Server
echo.
echo [*] Starting PeakCut local server at http://127.0.0.1:8765 ...
echo [*] Press Ctrl+C in this terminal window to stop the server.
echo.
".venv\Scripts\python.exe" -m studio.server

pause
