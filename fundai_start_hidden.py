# -*- coding: utf-8 -*-
"""后台守护入口（无控制台窗口）。

用途：
- 开机自启 / 计划任务 keepalive / 双击重启 时由 pythonw 调用；
- 若 8787 端口已有服务在跑 → 静默退出（不做任何事）；
- 否则启动 fundai 可视化服务并持续运行（不退出）；
- 事件写入 data/server_autostart.log 便于排查。
"""
import os
import socket
import sys
import time
import traceback
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
DATA = PROJECT / "data"


def log(msg):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "server_autostart.log", "a", encoding="utf-8") as f:
            f.write("{} {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def port_open(host, port, timeout=1.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    if "windowsapps" in sys.executable.lower():
        log("拒绝使用 Windows 应用商店 python 存根启动（请用 %LOCALAPPDATA%\\Python）")
        return 2
    from fundai import settings, web  # 延迟导入
    try:
        cfg = settings.load_config()
        host = str(cfg.get("server", {}).get("host", "127.0.0.1"))
        port = int(cfg.get("server", {}).get("port", 8787))
    except Exception as e:
        log("配置读取失败：{}".format(e))
        return 1
    log("检查 {}:{} 是否已在运行…".format(host, port))
    if port_open(host, port):
        log("服务已在运行，本进程退出")
        return 0
    log("开始启动 fundai 服务（db=data/fund200.db）…")
    try:
        srv, _app = web.make_server(cfg, str(DATA / "fund200.db"))
        log("服务已监听 http://{}:{}".format(host, port))
        srv.serve_forever()
    except Exception as e:
        log("服务异常退出：{}\n{}".format(e, traceback.format_exc()))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
