@echo off
rem fundai portable launcher: start the web UI in a hidden window, then open the browser
setlocal
cd /d "%~dp0"
set "PYW=%LOCALAPPDATA%\Python\bin\pythonw.exe"
if not exist "%PYW%" set "PYW=%LOCALAPPDATA%\Python\bin\python.exe"
if not exist "%PYW%" (
  for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
)
if not exist "%PYW%" (
  echo [ERROR] pythonw.exe / python.exe not found. Install Python or add it to PATH.
  pause
  exit /b 1
)
start "" "%PYW%" "app.py" serve
timeout /t 4 /nobreak >nul
start "" http://127.0.0.1:8787/
echo fundai web UI: http://127.0.0.1:8787/   (close the pythonw process to stop)
endlocal
