@echo off
title RunPod VideoGen Studio
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================================
echo        RunPod VideoGen Studio - Startet...
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

rem ---------------------------------------------------------------------------
rem Alte Server-Instanz auf dem Port suchen und beenden.
rem Es werden nur eigene Python-Prozesse gekillt; fremde Prozesse auf dem Port
rem erzeugen eine Warnung, damit nichts anderes abgeschossen wird.
rem ---------------------------------------------------------------------------
echo [INFO] Pruefe, ob auf Port %PORT% bereits eine alte Instanz laeuft...
powershell -NoProfile -Command "$conns = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue; if ($conns) { $owners = $conns | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($opid in $owners) { $proc = Get-Process -Id $opid -ErrorAction SilentlyContinue; if ($proc -and $proc.ProcessName -match '^python') { Write-Host ('[INFO] Beende alte Server-Instanz: ' + $proc.ProcessName + ' (PID ' + $opid + ')'); Stop-Process -Id $opid -Force -ErrorAction SilentlyContinue } else { Write-Host ('[WARNUNG] Port %PORT% wird von fremdem Prozess belegt: ' + $proc.ProcessName + ' (PID ' + $opid + ') - wird NICHT beendet!') } }; Start-Sleep -Milliseconds 800 } else { Write-Host '[INFO] Keine alte Instanz auf Port %PORT% gefunden - Port ist frei.' }"

echo [INFO] Starte lokalen Server auf Port %PORT%...
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:%PORT%"
python server.py

pause
