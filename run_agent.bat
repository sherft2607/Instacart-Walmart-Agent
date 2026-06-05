@echo off
title Instacart Walmart Grocery Agent
cd /d "%~dp0"

:: Check if Chrome is listening on 127.0.0.1:9223
netstat -ano | findstr "127.0.0.1:9223" | findstr "LISTENING" >nul
if %errorlevel% neq 0 (
    echo Chrome debugging port 127.0.0.1:9223 is not active.
    echo Launching Chrome with remote debugging enabled...
    start "" "C:\Users\sherft\AppData\Local\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9223 --user-data-dir="C:\Users\sherft\chrome-debug" "https://www.instacart.com/store/walmart/storefront"
    echo Waiting for Chrome to initialize...
    timeout /t 3 >nul
) else (
    echo Chrome debugging port 9223 is active.
)

echo.
echo ==========================================
echo Select interface mode:
echo   [1] Web App Dashboard (Recommended)
echo   [2] Terminal CLI Mode
echo ==========================================
set /p mode="Choose option [1 or 2, default is 1]: "

if "%mode%"=="2" (
    python grocery_agent.py
) else (
    echo Starting local web server on http://127.0.0.1:5000...
    :: Browser launch is now handled dynamically inside app.py after Flask binds to port 5000
    python app.py
)
pause
