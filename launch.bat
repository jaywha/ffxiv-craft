@echo off
title FFXIV Craft Planner

echo.
echo  ============================================
echo   FFXIV Craft Planner
echo  ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Please install Python 3.10+ from python.org
    pause
    exit /b 1
)

:: Install dependencies if needed
echo  Checking dependencies...
pip show flask >nul 2>&1
if errorlevel 1 (
    echo  Installing required packages...
    pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo  [ERROR] Failed to install packages. Check your internet connection.
        pause
        exit /b 1
    )
    echo  Done.
)

:: Open browser after a short delay (runs in background)
echo  Starting server...
start "" /b cmd /c "timeout /t 2 >nul && start http://localhost:5000"

:: Start Flask
echo  Server running at http://localhost:5000
echo  Close this window to stop the server.
echo.
python app.py

pause
