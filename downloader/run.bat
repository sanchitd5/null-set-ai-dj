@echo off
title YouTube to MP3 Downloader (Highest Quality)
cd /d "%~dp0"

:: Check if Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not found on your system PATH!
    echo Please ensure Python is installed and added to PATH.
    pause
    exit /b 1
)

:: Run script with any passed arguments or interactive mode
python "%~dp0downloader.py" %*

if %errorlevel% neq 0 (
    echo.
    echo Download finished with notices or errors.
)

:: Pause only if double clicked without arguments
if "%~1"=="" (
    pause
)
