@echo off
setlocal
REM Push to GitHub through a mirror channel.
REM Credentials are read at runtime from Git Credential Manager.
REM No token is stored in this file, in the remote URL, or in .git/config.
set "SH=%~dp0push_mirror.sh"
set "SH=%SH:\=/%"
where bash >nul 2>nul
if %errorlevel%==0 (
  bash "%SH%" %*
  exit /b %errorlevel%
)
if exist "%ProgramFiles%\Git\bin\bash.exe" (
  "%ProgramFiles%\Git\bin\bash.exe" "%SH%" %*
  exit /b %errorlevel%
)
echo [push_mirror] Git Bash not found. Run scripts\push_mirror.sh from Git Bash.
exit /b 1
