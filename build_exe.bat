@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

title GPX Ride Renderer - Windows Build

echo ============================================================
echo GPX Ride Renderer V1.0.0 - Portable Windows EXE Builder
echo ============================================================
echo.

set "PY_CMD="
where py >nul 2>&1
if not errorlevel 1 (
    py -3.10 -c "import sys; print(sys.version)" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3.10"
)
if not defined PY_CMD (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
    echo [ERROR] Python 3.10+ not found.
    echo Install 64-bit Python and run this script again.
    pause
    exit /b 1
)

echo [INFO] Python command: %PY_CMD%
%PY_CMD% -c "import sys,struct; print('[INFO] Python:', sys.version.split()[0], '|', struct.calcsize('P')*8, 'bit'); raise SystemExit(0 if sys.version_info >= (3,10) and struct.calcsize('P')*8 == 64 else 2)"
if errorlevel 1 (
    echo [ERROR] 64-bit Python 3.10 or newer is required.
    pause
    exit /b 1
)

echo.
echo [1/6] Creating isolated build environment...
if not exist ".venv_build\Scripts\python.exe" (
    %PY_CMD% -m venv .venv_build
    if errorlevel 1 goto :fail
)
set "VPY=%CD%\.venv_build\Scripts\python.exe"
set "VPIP=%CD%\.venv_build\Scripts\pip.exe"

echo.
echo [2/6] Installing build dependencies...
"%VPY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :fail
"%VPIP%" install -r requirements-build.txt
if errorlevel 1 goto :fail

echo.
echo [3/6] Locating FFmpeg...
if not exist "_build_assets" mkdir "_build_assets"
set "FFMPEG_SRC="

if exist "%CD%\ffmpeg.exe" (
    set "FFMPEG_SRC=%CD%\ffmpeg.exe"
) else (
    for /f "delims=" %%F in ('where ffmpeg 2^>nul') do (
        if not defined FFMPEG_SRC set "FFMPEG_SRC=%%F"
    )
)

if not defined FFMPEG_SRC (
    for /f "usebackq delims=" %%F in (`"%VPY%" -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"`) do (
        if not defined FFMPEG_SRC set "FFMPEG_SRC=%%F"
    )
    echo [WARN] System FFmpeg was not found. Using imageio-ffmpeg fallback.
    echo [WARN] The fallback binary may not support NVIDIA NVENC.
)

if not defined FFMPEG_SRC (
    echo [ERROR] FFmpeg could not be located.
    goto :fail
)

copy /Y "%FFMPEG_SRC%" "_build_assets\ffmpeg.exe" >nul
if errorlevel 1 goto :fail

echo [INFO] FFmpeg: %FFMPEG_SRC%
"_build_assets\ffmpeg.exe" -hide_banner -encoders 2>nul | findstr /i "h264_nvenc" >nul
if errorlevel 1 (
    echo [WARN] h264_nvenc was not found in this FFmpeg build.
    echo [WARN] The application can still fall back to libx264 CPU encoding.
) else (
    echo [OK] NVIDIA h264_nvenc encoder found.
)

"_build_assets\ffmpeg.exe" -hide_banner -encoders 2>nul | findstr /i "libx264" >nul
if errorlevel 1 (
    echo [WARN] libx264 was not found in this FFmpeg build.
) else (
    echo [OK] libx264 encoder found.
)

echo.
echo [4/6] Building PyInstaller onedir package...
echo [INFO] Multiprocessing targets use late freeze_support for PyInstaller spawn compatibility.
echo [INFO] The EXE uses the Windows noconsole bootloader and captures renderer logs through pipes.
if exist "build" rmdir /S /Q "build"
if exist "dist\GPX Ride Renderer" rmdir /S /Q "dist\GPX Ride Renderer"
"%VPY%" -m PyInstaller --noconfirm --clean GPX_Ride_Renderer.spec
if errorlevel 1 goto :fail

if not exist "dist\GPX Ride Renderer\GPX Ride Renderer.exe" (
    echo [ERROR] Build finished but the EXE was not found.
    goto :fail
)

echo.
echo [5/6] Adding portable FFmpeg...
copy /Y "_build_assets\ffmpeg.exe" "dist\GPX Ride Renderer\ffmpeg.exe" >nul
if errorlevel 1 goto :fail

echo.
echo [6/6] Creating release ZIP...
if not exist "release" mkdir "release"
if exist "release\GPX_Ride_Renderer_Windows_x64.zip" del /Q "release\GPX_Ride_Renderer_Windows_x64.zip"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\GPX Ride Renderer\*' -DestinationPath 'release\GPX_Ride_Renderer_Windows_x64.zip' -Force"
if errorlevel 1 (
    echo [WARN] ZIP creation failed, but the onedir build is still usable.
)

echo.
echo ============================================================
echo [DONE] Build completed.
echo.
echo EXE folder:
echo   %CD%\dist\GPX Ride Renderer\
echo.
echo Main executable:
echo   %CD%\dist\GPX Ride Renderer\GPX Ride Renderer.exe
echo.
echo Release ZIP:
echo   %CD%\release\GPX_Ride_Renderer_Windows_x64.zip
echo.
echo IMPORTANT: When sharing the portable build, share the entire folder
 echo or the ZIP. Do not copy only the EXE.
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo ============================================================
echo [ERROR] Build failed. Review the messages above.
echo ============================================================
echo.
pause
exit /b 1
