@echo off
rem ============================================================
rem  fundai 计划任务注册：把保活 / 午间诊断 / 每日研判等任务
rem  按**当前安装目录**注册到 Windows 任务计划程序（共 7 个）。
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
echo [1/7] 保活（每 15 分钟，确保网页服务在线）
schtasks /Create /F /TN "fundai_keepalive" /SC MINUTE /MO 15 ^
  /TR "\"%PYW%\" \"%~dp0fundai_start_hidden.py\""

echo [2/8] 午间复盘（工作日 11:35，上午收盘后）
schtasks /Create /F /TN "fundai_intraday" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 11:35 ^
  /TR "\"%~dp0fundai_intraday.bat\""

echo [2c/8] 午间复盘补跑（工作日 11:40 与 11:45）
rem  为什么要补跑（2026-09-16）：11:35 那次遇到三家行情通道同时瞬断，旧实现只试一次
rem  就直接跳过 → 一整天没有午间诊断。现在 intraday-pulse 是**幂等**的（当天已诊断
rem  就立刻退出，不重复取数），所以多挂两班只是"万一失败了再试"，成功时几乎零成本。
schtasks /Create /F /TN "fundai_intraday_retry1" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 11:40 ^
  /TR "\"%~dp0fundai_intraday.bat\""
schtasks /Create /F /TN "fundai_intraday_retry2" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 11:45 ^
  /TR "\"%~dp0fundai_intraday.bat\""

echo [2b/8] 午间复盘结算（工作日 15:05，用"收盘 vs 11:30"结算命中）
schtasks /Create /F /TN "fundai_intraday_settle" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:05 ^
  /TR "\"%~dp0fundai_intraday_settle.bat\""

echo [3/8] 每日样本结算（工作日 20:30）
schtasks /Create /F /TN "fundai_daily_signals" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 20:30 ^
  /TR "\"%~dp0fundai_daily_signals.bat\""

echo [4/8] 每日研判（每天 21:30）
schtasks /Create /F /TN "fundai_run_daily" /SC DAILY /ST 21:30 ^
  /TR "\"%PYW%\" \"%~dp0app.py\" run-daily"

echo [5/8] 每日研判兜底（每天 22:45，净值晚公布时补跑）
schtasks /Create /F /TN "fundai_run_daily_late" /SC DAILY /ST 22:45 ^
  /TR "\"%PYW%\" \"%~dp0app.py\" run-daily"

echo [6/8] AI 次日方向判断自检（每天 23:05，独立于研判；研判已跑过也能补记+结算命中率）
schtasks /Create /F /TN "fundai_direction_check" /SC DAILY /ST 23:05 ^
  /TR "\"%PYW%\" \"%~dp0app.py\" direction-check"

echo.
echo 完成。查看任务： schtasks /Query /TN "fundai_keepalive"
echo 删除全部：   for %%%%T in (fundai_keepalive fundai_intraday fundai_intraday_retry1 fundai_intraday_retry2 fundai_intraday_settle fundai_daily_signals fundai_run_daily fundai_run_daily_late fundai_direction_check) do schtasks /Delete /F /TN "%%%%T"
pause
