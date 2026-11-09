@echo off
chcp 65001 >nul
title CNC Agent 操控台 - 打开面板
setlocal
set "URL=http://127.0.0.1:8787"

echo.
echo   CNC Agent 操控台
echo   ------------------------------------------
echo   地址： %URL%
echo.

rem 先探测面板是否已在运行；未运行则自动拉起虚拟机床网关与面板
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri '%URL%/api/health' -UseBasicParsing -TimeoutSec 2) | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo   [启动] 面板未运行，正在拉起虚拟机床网关与面板...
    cd /d "%~dp0.."
    start "CNC Virtual PLC" /min cmd /c "python panel\plc_sim.py"
    timeout /t 2 >nul
    set PANEL_ALLOW_WRITE=1
    set PANEL_HOST=127.0.0.1
    set PANEL_PORT=8787
    start "CNC Panel" /min cmd /c "python panel\server.py"
    timeout /t 2 >nul
) else (
    echo   [就绪] 面板服务已在运行。
)

echo   [打开] 正在用系统默认浏览器打开操控台...
start "" "%URL%"
echo.
echo   若浏览器未自动打开，请手动复制上面地址到浏览器地址栏。
echo   页面默认进入「机床操控」，勾选「我已确认现场安全」后才能下发指令。
echo.
timeout /t 8 >nul
endlocal
