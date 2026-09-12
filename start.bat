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

rem Konfigurierten Port aus config.json lesen (Fallback: 7880)
set "PORT=7880"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "try { (Get-Content -Raw 'config.json' -ErrorAction SilentlyContinue | ConvertFrom-Json).server_port } catch { }"`) do set "PORT=%%P"
if "%PORT%"=="" set "PORT=7880"

echo [INFO] Starte lokalen Server auf Port %PORT%...
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:%PORT%"
python server.py

pause
