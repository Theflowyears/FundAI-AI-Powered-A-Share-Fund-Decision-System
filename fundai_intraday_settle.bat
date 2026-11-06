@echo off
rem fundai 午间复盘结算：收盘后（15:05）用"收盘 vs 11:30"结算今日午间观点的命中
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Python\bin\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Python\bin\pythonw.exe"
if not exist "%PY%" (
  for /f "delims=!" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
)
echo ---------------- %DATE% %TIME% 午间结算 ---------------- >> data\intraday.log
"%PY%" app.py intraday-pulse --settle >> data\intraday.log 2>&1
