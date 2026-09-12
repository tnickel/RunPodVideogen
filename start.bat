@echo off
title RunPod VideoGen Studio
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================================
echo        🚀 RunPod VideoGen Studio - Startet...
echo ========================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [FEHLER] Python wurde nicht im PATH gefunden!
    pause
    exit /b 1
)

echo [INFO] Starte lokalen Server auf Port 7880...
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:7880"
python server.py

pause
