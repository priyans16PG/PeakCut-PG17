# PeakCut Local Startup Script for PowerShell
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "                PeakCut (Local Mode)                    " -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# 0. Close any previous instance on port 8765
$conn = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
}

# 1. Check Python
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Error "Python was not found on your system PATH. Please install Python 3.10+."
    exit 1
}

# 2. Check Virtual Environment
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[*] Creating virtual environment (.venv)..." -ForegroundColor Yellow
    python -m venv .venv
    Write-Host "[*] Installing dependencies..." -ForegroundColor Yellow
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

# 3. Check FFmpeg
$ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpegCmd) {
    Write-Host "[WARNING] FFmpeg not found on PATH. Video rendering requires ffmpeg." -ForegroundColor Yellow
}

# 4. Check Speech Model
$modelTokenFile = "data\models\sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8\tokens.txt"
if (-not (Test-Path $modelTokenFile)) {
    Write-Host "[*] Downloading speech model (~480 MB)..." -ForegroundColor Yellow
    & .\.venv\Scripts\python.exe -m studio.fetch_model
}

# 5. Open browser
Start-Process "http://127.0.0.1:8765"

# 6. Start Server
Write-Host ""
Write-Host "[*] Starting PeakCut server at http://127.0.0.1:8765 ..." -ForegroundColor Green
Write-Host "[*] Press Ctrl+C to stop the server." -ForegroundColor Gray
Write-Host ""
& .\.venv\Scripts\python.exe -m studio.server
