@echo off
cd /d "%~dp0"

:: Terminate any existing instance of main.py to prevent Telegram polling conflict
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

:: Small delay to ensure Telegram network socket clean close
timeout /t 1 /nobreak >nul 2>&1

:: Launch pythonw in background (no CMD window)
start "" pythonw main.py

exit /b 0
