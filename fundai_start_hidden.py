# -*- coding: utf-8 -*-
"""后台守护入口（无控制台窗口）。

用途：
- 开机自启 / 计划任务 keepalive / 双击重启 时由 pythonw 调用；
- 若 8787 端口已有服务在跑，且**代码没变** → 静默退出（不做任何事）；
- 若在跑的服务是**旧代码**（stamp 不一致，或老版本根本没有 /api/version）→
  自动停掉旧进程并重启，保证"改完代码不用手工重启，15 分钟内自动换新"；
- 否则启动 fundai 可视化服务并持续运行（不退出）；
- 事件写入 data/server_autostart.log 便于排查。
"""
import json
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


def request_self_restart(host, port, timeout=6.0):
    """请旧服务自己退出并拉起新进程（旧版本没有该接口 → 返回 False）。"""
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://{}:{}/api/restart".format(host, port), data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            js = json.loads(r.read().decode("utf-8", "replace"))
        return bool(js.get("ok"))
    except Exception as e:
        log("旧服务不支持自重启（{}）".format(str(e)[:80]))
        return False


def wait_port_free(host, port, seconds=20):
    """等端口释放；返回 True 表示已经空出来。"""
    end = time.time() + seconds
    while time.time() < end:
        if not port_open(host, port):
            return True
        time.sleep(0.5)
    return False


def wait_port_ready(host, port, seconds=30):
    """等端口被（新进程）接管。"""
    end = time.time() + seconds
    while time.time() < end:
        if port_open(host, port):
            return True
        time.sleep(0.5)
    return False


def http_stamp(host, port, timeout=2.0):
    """读在跑服务的代码指纹；拿不到（旧版没有该接口/网络问题）返回 None。"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(
                "http://{}:{}/api/version".format(host, port),
                timeout=timeout) as r:
            js = json.loads(r.read().decode("utf-8", "replace"))
        return js.get("stamp")
    except Exception:
        return None


def find_listen_pid(port):
    """找到监听该端口的进程 PID（Windows）。找不到返回 None。

    两条路都试：`netstat -ano` 最通用；个别受限环境（沙箱/裁剪过的系统）
    拿不到 netstat 输出，则退回 PowerShell 的 `Get-NetTCPConnection`。
    都拿不到就返回 None —— 调用方据此放弃自动重启（宁可让用户手动重启，
    也不能去猜 PID 误杀进程）。
    """
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15)
        for line in (out.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper().startswith("TCP") \
                    and parts[3].upper() == "LISTENING" \
                    and parts[1].endswith(":" + str(port)):
                try:
                    return int(parts[4])
                except ValueError:
                    continue
    except Exception:
        pass
    try:
        ps = ("(Get-NetTCPConnection -State Listen -LocalPort {p} "
              "-ErrorAction SilentlyContinue | Select-Object -First 1 "
              "-ExpandProperty OwningProcess)").format(p=int(port))
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=25)
        pid = (out.stdout or "").strip().splitlines()
        if pid and pid[0].strip().isdigit():
            return int(pid[0].strip())
    except Exception:
        pass
    return None


def stop_stale_server(host, port):
    """停掉"跑着旧代码"的服务；只在确认是 python 进程时才动手。

    返回 True 表示已停（调用方随后自行启动新服务）。
    """
    import subprocess
    pid = find_listen_pid(port)
    if not pid:
        log("未找到监听 {}:{} 的进程，按“无服务”处理".format(host, port))
        return True
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=15)
        row = (out.stdout or "").strip().splitlines()
        name = (row[1].split(",")[0].strip('"').lower() if len(row) > 1 else "")
    except Exception:
        name = ""
    if "python" not in name:
        log("端口 {} 被非 python 进程占用（PID {} / {}），不自动重启".format(
            port, pid, name or "未知"))
        return False
    log("检测到旧版本服务（PID {} / {}），正在停止以便加载新代码…".format(pid, name))
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=20)
    except Exception as e:
        log("停止旧服务失败（{}），请手动结束该进程后重试".format(str(e)[:120]))
        return False
    time.sleep(1.5)
    for _ in range(10):
        if not port_open(host, port):
            log("旧服务已停止")
            return True
        time.sleep(0.5)
    log("旧服务仍未退出，放弃自动重启（请手动处理）")
    return False


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
    relaunch = "--relaunch" in sys.argv
    if relaunch:
        # 由旧服务的自重启拉起：等它释放端口后直接接管
        log("以 --relaunch 启动：等待旧服务释放 {}:{}…".format(host, port))
        wait_port_free(host, port, 25)
    log("检查 {}:{} 是否已在运行…".format(host, port))
    if port_open(host, port) and not relaunch:
        want = None
        try:
            want = web.code_stamp()
        except Exception as e:
            log("计算代码指纹失败（{}），按“需要换新”处理".format(str(e)[:80]))
        got = http_stamp(host, port)
        if want and got == want:
            log("服务已在运行且代码为最新（stamp {}），本进程退出".format(got))
            return 0
        log("在跑的服务是旧代码（磁盘 {} / 在跑 {}）→ 自动重启".format(
            want or "?", got or "无该接口"))
        # 首选：请旧服务**自己**退出并拉起新进程（不需要 taskkill 权限，最稳）
        if request_self_restart(host, port):
            log("已请求旧服务自重启，等待新进程接管…")
            if wait_port_free(host, port, 20) and wait_port_ready(host, port, 30):
                log("新服务已接管 http://{}:{}".format(host, port))
                return 0
            log("自重启后端口未按预期接管，改用启动逻辑兜底")
        # 老版本没有自重启接口 → 退回"停掉旧进程再启动"
        if not stop_stale_server(host, port):
            return 0
    log("开始启动 fundai 服务（db=data/fund200.db）…")
    try:
        srv, _app = web.make_server(cfg, str(DATA / "fund200.db"))
        log("服务已监听 http://{}:{}（stamp {}）{}".format(
            host, port, web.code_stamp(),
            "（relaunch）" if relaunch else ""))
        srv.serve_forever()
    except Exception as e:
        log("服务异常退出：{}\n{}".format(e, traceback.format_exc()))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
