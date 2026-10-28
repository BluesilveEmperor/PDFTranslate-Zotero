@echo off
rem ===========================================================================
rem  Zotero PDF2zh Server portable launcher (Windows)
rem  Keep this file ASCII-only: cmd.exe misparses UTF-8 batch files after
rem  switching code pages, so all user-facing Chinese messages are printed by
rem  server.py instead. Chinese console output still works because
rem  PYTHONIOENCODING/PYTHONUTF8 are set below.
rem
rem  Usage: start_server.bat [server.py options]
rem ===========================================================================
chcp 65001 >nul
setlocal

cd /d "%~dp0"

if not exist "runtime\python.exe" (
    echo [ERROR] runtime\python.exe not found.
    echo         This folder is not a complete Server portable package.
    pause
    exit /b 1
)
if not exist "server.py" (
    echo [ERROR] server.py not found.
    echo         This folder is not a Server portable package root.
    pause
    exit /b 1
)

rem The embeddable Python *_pth file already lists ..\site-packages and ..;
rem this is a second safety net for manually replaced runtimes.
set "PYTHONPATH=%~dp0site-packages;%~dp0"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

echo [start] runtime: %~dp0runtime\python.exe
echo [start] site-packages: %~dp0site-packages
echo.

"runtime\python.exe" server.py %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Server exited with code %EXIT_CODE%
    echo         Please copy the log above when asking for help.
    pause
)
exit /b %EXIT_CODE%