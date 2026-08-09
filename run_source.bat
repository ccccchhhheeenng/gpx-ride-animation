@echo off
chcp 65001 >nul
cd /d "%~dp0"
python GPX_Ride_Renderer.py
if errorlevel 1 pause
