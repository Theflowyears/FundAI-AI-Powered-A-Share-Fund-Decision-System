@echo off
rem fundai 午间复盘（午间诊断）：上午收盘后由计划任务 fundai_intraday 调用
rem 想手动跑一次：双击本文件，或在命令行执行 fundai_intraday.bat
rem 日志：data\intraday.log（每次追加，便于排查"到底跑没跑"）
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Python\bin\python.exe"
if not exist "%PY%" set "PY=%LOCALAPPDATA%\Python\bin\pythonw.exe"
if not exist "%PY%" (
  for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
)
echo ================ %DATE% %TIME% 午间复盘 ================ >> data\intraday.log
"%PY%" app.py intraday-pulse >> data\intraday.log 2>&1
echo 退出码 %ERRORLEVEL% >> data\intraday.log
