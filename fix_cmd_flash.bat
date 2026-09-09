@echo off
chcp 65001 >nul
title fundai CMD 闪框一键修复
echo =====================================================
echo   fundai CMD 闪现问题一键修复
echo   请【右键 → 以管理员身份运行】本脚本
echo =====================================================
echo.
echo 闪框原因：定时保活/每日任务用【有窗口的 cmd/python.exe】
echo 运行，每次触发会闪一个黑框再秒关。
echo.
echo [1/3] 查找并删除 fundai 相关计划任务...
schtasks /Delete /TN "fundai_keepalive" /F >nul 2>&1 && echo    - 已删除 fundai_keepalive
schtasks /Delete /TN "fundai_daily" /F >nul 2>&1 && echo    - 已删除 fundai_daily
schtasks /Delete /TN "fundai_serve" /F >nul 2>&1 && echo    - 已删除 fundai_serve
schtasks /Delete /TN "fundai_keepalive\*" /F >nul 2>&1 && echo    - 已删除 fundai_keepalive\*
schtasks /Delete /TN "AI基金保活" /F >nul 2>&1 && echo    - 已删除 AI基金保活
echo.
echo [2/3] 确认服务仍正常（无窗口 pythonw 常驻）...
tasklist /FI "IMAGENAME eq pythonw.exe" 2>nul | findstr /i pythonw >nul && (
  echo    - pythonw 服务进程在运行（无窗口，正常）
) || (
  echo    - 未发现 pythonw，尝试启动后台服务...
  start "" "%LOCALAPPDATA%\Python\bin\pythonw.exe" "%~dp0fundai_start_hidden.py"
)
echo.
echo [3/3] 完成！
echo.
echo 后续建议：
echo   - 服务崩溃后如想自动拉起，可用【无窗口】方式重建保活任务：
echo       schtasks /Create /TN fundai_keepalive /SC MINUTE /MO 15 /TR
echo         "\"%LOCALAPPDATA%\Python\bin\pythonw.exe\" \"%~dp0fundai_start_hidden.py\""
echo     即用 pythonw.exe（无控制台窗口）运行，不会再闪黑框。
echo   - 彻底不要保活：忽略上一条即可，开机自启已在启动文件夹兜底。
echo.
pause
