@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY_CMD="
where py >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3"
if not defined PY_CMD (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
    echo [ERROR] Python 3.10 or newer was not found.
    echo Install 64-bit Python and enable Add Python to PATH.
    pause
    exit /b 1
)

%PY_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo [ERROR] GPX Ride Renderer requires Python 3.10 or newer.
    pause
    exit /b 1
)

set "VENV_PY=%CD%\.venv_run\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [INFO] First run: creating the project Python environment...
    %PY_CMD% -m venv .venv_run
    if errorlevel 1 goto :setup_failed
)

"%VENV_PY%" -c "import numpy,gpxpy,contextily,xyzservices,requests,PIL,matplotlib,cv2,imageio_ffmpeg" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing or updating required packages...
    "%VENV_PY%" -m pip install --upgrade pip
    if errorlevel 1 goto :setup_failed
    "%VENV_PY%" -m pip install -r requirements-build.txt
    if errorlevel 1 goto :setup_failed
)

"%VENV_PY%" GPX_Ride_Renderer.py
set "APP_EXIT=%errorlevel%"
if not "%APP_EXIT%"=="0" pause
exit /b %APP_EXIT%

:setup_failed
echo.
echo [ERROR] Python package setup failed.
echo Check the network connection and run run_source.bat again.
pause
exit /b 1
