@echo off
rem fundai background service launcher (no console window; exits if already running)
setlocal
set "PYW=%LOCALAPPDATA%\Python\bin\pythonw.exe"
if not exist "%PYW%" (
  for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
)
if not exist "%PYW%" (
  echo [fundai] pythonw not found. Install Python (e.g. to %%LOCALAPPDATA%%\Python) or add it to PATH.
  pause
  exit /b 1
)
start "" "%PYW%" "%~dp0fundai_start_hidden.py"
endlocal
