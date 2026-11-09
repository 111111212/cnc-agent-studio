@echo off
chcp 65001 >nul
cd /d "%~dp0.."
REM Production default: read-only telemetry. Change to 1 only after sign-off.
if "%PANEL_ALLOW_WRITE%"=="" set PANEL_ALLOW_WRITE=0
if "%PANEL_HOST%"=="" set PANEL_HOST=127.0.0.1
if "%PANEL_PORT%"=="" set PANEL_PORT=8787
echo PANEL_MODE=%PANEL_MODE%  PANEL_ALLOW_WRITE=%PANEL_ALLOW_WRITE%  %PANEL_HOST%:%PANEL_PORT%
python panel\server.py
