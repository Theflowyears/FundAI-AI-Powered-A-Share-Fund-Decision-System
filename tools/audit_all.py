# -*- coding: utf-8 -*-
"""一次跑完两套审计：CLI（23 子命令）+ Web（30 路由），并汇总退出码。

**审计对象是"当前这套部署"**（本仓库的 config.json + data/），因此**不设** FUNDAI_HOME：
`refresh-pool` 需要已有净值缓存、`news-calibrate` 需要十年电报库，这些正是本部署的资产。
（"全新空环境能否跑起来"由另一条线负责：`tools/verify_release.py` 会解压便携包、
用**空数据目录**验证首次运行。）

流程：生成/复用演示库 → 起演示服务（Web 审计的写操作会被安全拒绝）→
跑 `data/audit.py`（快速组或全量）+ `data/audit_web.py` → 汇总。

用法：python tools/audit_all.py [--full]
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def run(cmd, env, timeout=2400):
    return subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                          timeout=timeout)


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    full = "--full" in sys.argv
    env = dict(os.environ)          # 审计"当前部署"：用本仓库的 config.json 与 data/
    env.pop("FUNDAI_HOME", None)
    print("=== fundai 全量审计（当前部署：%s）===" % ROOT)

    print("\n[0/3] 生成/复用演示库（Web 审计打演示服务，写操作会被安全拒绝）…")
    t0 = time.time()
    r = run([PY, "app.py", "demo"], env, timeout=1200)
    print("      demo rc=%d（%.0fs）" % (r.returncode, time.time() - t0))

    port = free_port()
    print("[1/3] 启动演示服务（端口 %d）…" % port)
    proc = subprocess.Popen([PY, "app.py", "serve", "--demo", "--port", str(port)],
                            cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    up = False
    for _ in range(30):
        time.sleep(1)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port,
                                        timeout=4) as resp:
                json.loads(resp.read().decode("utf-8"))
            up = True
            break
        except Exception:
            continue
    print("      服务就绪：%s" % up)

    codes = {}
    try:
        print("\n[2/3] CLI 审计（%s）…" % ("全量" if full else "快速组"))
        cmd = [PY, "data/audit.py"] + ([] if full else ["--quick"])
        rc = run(cmd, env, timeout=3600)
        out = ((rc.stdout or b"") + (rc.stderr or b"")).decode("utf-8", "replace")
        codes["cli"] = rc.returncode
        print("\n".join([ln for ln in out.strip().splitlines() if ln.strip()][-3:]))

        print("\n[3/3] Web 审计（演示服务端口 %d 上的全部路由）…" % port)
        rw = run([PY, "data/audit_web.py", "--port", str(port)], env, timeout=3600)
        outw = ((rw.stdout or b"") + (rw.stderr or b"")).decode("utf-8", "replace")
        codes["web"] = rw.returncode
        print("\n".join([ln for ln in outw.strip().splitlines() if ln.strip()][-3:]))
    finally:
        proc.terminate()

    print("\n=== 汇总 ===")
    for k, v in codes.items():
        print("  %-4s 退出码 %d → %s" % (k, v, "通过" if v == 0 else "有异常"))
    print("报告：data/audit_report.json（CLI）、data/audit_web.json（Web）")
    return 1 if any(v != 0 for v in codes.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
