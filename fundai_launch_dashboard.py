# -*- coding: utf-8 -*-
"""桌面“启动后台服务”入口（无窗口，pythonw 运行）。

行为：
1) 若 8787 端口无服务 → 静默启动 fundai_start_hidden.py（无窗口后台服务）；
2) 轮询等待服务就绪（最多约 25 秒）；
3) 用系统默认浏览器打开仪表盘 http://127.0.0.1:8787。
服务已在运行时则跳过第 1 步，直接打开网页。
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8787"
HOST, PORT = "127.0.0.1", 8787
WRAPPER = PROJECT / "fundai_start_hidden.py"


def port_open(timeout=1.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    if not port_open():
        # 用 pythonw 启动守护入口（无控制台窗口；已在运行则由守护入口自行退出）
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        try:
            subprocess.Popen(
                [pythonw, str(WRAPPER)],
                cwd=str(PROJECT),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
    # 等待服务就绪（守护入口可能因端口占用直接退出，此时多半已就绪）
    ok = port_open()
    for _ in range(50):
        if ok:
            break
        time.sleep(0.5)
        ok = port_open()
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
