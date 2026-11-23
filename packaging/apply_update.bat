@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "TARGET_PID=%~1"
set "STAGE_DIR=%~2"
set "APP_DIR=%~3"

if "%STAGE_DIR%"=="" exit /b 2
if "%APP_DIR%"=="" exit /b 2
if "%STAGE_DIR:~-1%"=="\" set "STAGE_DIR=%STAGE_DIR:~0,-1%"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

if not exist "%STAGE_DIR%" exit /b 3

cd /d "%TEMP%"

if "%TARGET_PID%"=="" goto wait_done
if "%TARGET_PID%"=="0" goto wait_done
set /a WAIT_COUNT=0

:wait_loop
tasklist /FI "PID eq %TARGET_PID%" /NH /FO CSV 2>nul | findstr /L /C:",\"%TARGET_PID%\"," >nul
if errorlevel 1 goto wait_done
set /a WAIT_COUNT+=1
if %WAIT_COUNT% GEQ 30 (
    taskkill /F /PID %TARGET_PID% >nul 2>&1
    ping 127.0.0.1 -n 2 >nul
    goto wait_done
)
ping 127.0.0.1 -n 2 >nul
goto wait_loop

:wait_done
if not exist "%APP_DIR%" mkdir "%APP_DIR%" >nul 2>&1

robocopy "%STAGE_DIR%" "%APP_DIR%" /E /XD _internal /R:5 /W:1 /NFL /NDL /NJH /NJS /NP >nul
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 goto cleanup_fail

if not exist "%STAGE_DIR%\_internal" goto skip_internal
robocopy "%STAGE_DIR%\_internal" "%APP_DIR%\_internal" /MIR /R:5 /W:1 /NFL /NDL /NJH /NJS /NP >nul
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 goto cleanup_fail

:skip_internal
if exist "%APP_DIR%\HexHelper.exe" (
    start "" /D "%APP_DIR%" "%APP_DIR%\HexHelper.exe"
)

rmdir /S /Q "%STAGE_DIR%" >nul 2>&1
(goto) 2>nul & del /F /Q "%~f0" >nul 2>&1 & cmd /c exit /b 0

:cleanup_fail
rmdir /S /Q "%STAGE_DIR%" >nul 2>&1
(goto) 2>nul & del /F /Q "%~f0" >nul 2>&1 & cmd /c exit /b %RC%
