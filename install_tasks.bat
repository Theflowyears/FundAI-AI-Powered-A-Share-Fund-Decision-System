@echo off
rem ============================================================
rem  fundai 计划任务注册：把保活 / 午间诊断 / 每日研判等任务
rem  按**当前安装目录**注册到 Windows 任务计划程序。
rem  用法：右键 → 以管理员身份运行（注册任务需要权限）
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYW=%LOCALAPPDATA%\Python\bin\pythonw.exe"
if not exist "%PYW%" set "PYW=%LOCALAPPDATA%\Python\bin\python.exe"
if not exist "%PYW%" (
  for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
)
if not exist "%PYW%" (
  echo [错误] 找不到 pythonw.exe / python.exe，请先安装 Python 或加入 PATH。
  pause & exit /b 1
)
echo 使用解释器：%PYW%
echo 安装目录　：%~dp0

echo.
echo [1/6] 保活（每 15 分钟，确保网页服务在线）
schtasks /Create /F /TN "fundai_keepalive" /SC MINUTE /MO 15 ^
  /TR "\"%PYW%\" \"%~dp0fundai_start_hidden.py\""

echo [2/6] 午间诊断（工作日 12:00）
schtasks /Create /F /TN "fundai_intraday" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 12:00 ^
  /TR "\"%PYW%\" \"%~dp0app.py\" intraday-pulse"

echo [3/6] 每日样本结算（工作日 20:30）
schtasks /Create /F /TN "fundai_daily_signals" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 20:30 ^
  /TR "\"%~dp0fundai_daily_signals.bat\""

echo [4/6] 每日研判（每天 21:30）
schtasks /Create /F /TN "fundai_run_daily" /SC DAILY /ST 21:30 ^
  /TR "\"%PYW%\" \"%~dp0app.py\" run-daily"

echo [5/6] 每日研判兜底（每天 22:45，净值晚公布时补跑）
schtasks /Create /F /TN "fundai_run_daily_late" /SC DAILY /ST 22:45 ^
  /TR "\"%PYW%\" \"%~dp0app.py\" run-daily"

echo [6/6] AI 次日方向判断自检（每天 23:05，独立于研判；研判已跑过也能补记+结算命中率）
schtasks /Create /F /TN "fundai_direction_check" /SC DAILY /ST 23:05 ^
  /TR "\"%PYW%\" \"%~dp0app.py\" direction-check"

echo.
echo 完成。查看任务： schtasks /Query /TN "fundai_keepalive"
echo 删除全部：   for %%%%T in (fundai_keepalive fundai_intraday fundai_daily_signals fundai_run_daily fundai_run_daily_late fundai_direction_check) do schtasks /Delete /F /TN "%%%%T"
pause
