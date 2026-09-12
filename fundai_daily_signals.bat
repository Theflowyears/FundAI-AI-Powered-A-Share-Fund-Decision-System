@echo off
rem fundai 每日样本结算：每交易日 20:30 由计划任务 fundai_daily_signals 调用
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Python\bin\pythonw.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Python\bin\python.exe"
"%PY%" app.py signals-update --days 7 >> data\signals_update.log 2>&1
