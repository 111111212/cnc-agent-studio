@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo Starting CNC virtual PLC gateway and control panel...
start "CNC Virtual PLC" cmd /k "python panel\plc_sim.py"
timeout /t 2 >nul
set PANEL_ALLOW_WRITE=1
set PANEL_HOST=127.0.0.1
set PANEL_PORT=8787
set PANEL_AUTO_CONNECT=1
start "CNC Panel" cmd /k "python panel\server.py"
echo.
echo Panel:   http://127.0.0.1:8787
echo Gateway: 127.0.0.1:15020
echo Close the two new windows to stop both services.
