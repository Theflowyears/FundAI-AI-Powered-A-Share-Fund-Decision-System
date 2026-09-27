# -*- coding: utf-8 -*-
"""发行包验证：**从产物本身**（不是从源码树）跑起来，证明包是自包含可用的。

检查项
------
A. 便携 ZIP：解压到临时目录后
   1) 目录结构完整（fundai/、web/static/、app.py、启动脚本、示例配置）；
   2) **全新数据目录**（模拟新用户）下 `app.py state` 能自动生成 config.json 并成功；
   3) `app.py doctor` 正常；
   4) **不含敏感/体积文件**（config.json、*.db、data/cache、十年电报库）；
   5) 起服务并访问 `/`、`/app.js`、`/api/state`；
   6) 在解压目录里跑**测试套件**（证明测试也随包发布且通过）。
B. wheel：解压到临时 site 后
   1) `import fundai.web` 且静态资源解析到**包内** `fundai/static`；
   2) 用 PYTHONPATH 起服务并访问首页与 `/api/state`。

用法：python tools/verify_release.py [--skip-tests]
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PY = sys.executable
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %-46s %s" % ("OK " if ok else "FAIL", name, detail))
    return ok


def free_port():
    """要一个**真的能用**的端口：Windows 会保留若干端口段，硬编码会 WinError 10013。"""
    import socket
    for _ in range(20):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            if port > 1024:
                return port
        except OSError:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    return 8837


def http_json(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_bytes(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def wait_server(port, proc, tries=30):
    for _ in range(tries):
        time.sleep(1)
        if proc.poll() is not None:
            return False
        try:
            http_json("http://127.0.0.1:%d/api/state" % port, timeout=4)
            return True
        except Exception:
            continue
    return False


def same_path(a, b):
    """把 8.3 短路径与长路径视为同一路径（Windows 的 Path.resolve 可能给短名）。"""
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))


def verify_zip(fresh_home, skip_tests):
    zips = sorted(DIST.glob("fundai-portable-*.zip"))
    if not zips:
        return check("便携 ZIP 存在", False, "dist/ 下没有 fundai-portable-*.zip")
    z = zips[-1]
    check("便携 ZIP 存在", True, z.name)
    tmp = Path(tempfile.mkdtemp(prefix="fundai_zip_"))
    try:
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
            zf.extractall(tmp)
        root = tmp / names[0].split("/")[0]
        need = ["app.py", "fundai/__init__.py", "fundai/web.py",
                "web/static/index.html", "web/static/app.js",
                "web/static/vendor/echarts.min.js", "config.example.json",
                "requirements.txt", "README.md", "pyproject.toml",
                "fundai_start.bat", "install_tasks.bat"]
        missing = [n for n in need if not (root / n).exists()]
        check("ZIP 结构完整", not missing, "缺 %s" % missing if missing else
              "%d 个文件" % len(names))
        leaked = [n for n in names
                  if n.endswith(("config.json", ".db", ".db-wal", ".db-shm"))
                  or "/data/cache/" in n or "cls_history.json" in n]
        check("ZIP 不含敏感/大文件", not leaked,
              "泄漏 %s" % leaked[:3] if leaked else "无 config.json/DB/缓存")

        env = dict(os.environ, FUNDAI_HOME=str(fresh_home))
        p = subprocess.run([PY, "app.py", "state"], cwd=str(root), env=env,
                           capture_output=True, timeout=300)
        check("全新环境 state", p.returncode == 0,
              "rc=%d %s" % (p.returncode,
                            (p.stderr or b"").decode("utf-8", "replace")[-120:]))
        check("自动生成 config.json", (fresh_home / "config.json").exists())
        p2 = subprocess.run([PY, "app.py", "doctor"], cwd=str(root), env=env,
                            capture_output=True, timeout=300)
        check("doctor 正常", p2.returncode == 0, "rc=%d" % p2.returncode)

        port = free_port()
        proc = subprocess.Popen([PY, "app.py", "serve", "--port", str(port)],
                                cwd=str(root), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            up = wait_server(port, proc)
            if up:
                html = http_bytes("http://127.0.0.1:%d/" % port)
                js = http_bytes("http://127.0.0.1:%d/app.js" % port)
                st = http_json("http://127.0.0.1:%d/api/state" % port)
                check("ZIP 内起服务（端口 %d）" % port,
                      len(html) > 3000 and len(js) > 10000,
                      "首页 %d 字节 / app.js %d 字节 / 账户 %.2f"
                      % (len(html), len(js), st["state"]["total"]))
            else:
                check("ZIP 内起服务（端口 %d）" % port, False,
                      (proc.stderr.read() or b"").decode("utf-8", "replace")[-200:])
        finally:
            proc.terminate()
        if not skip_tests:
            pt = subprocess.run([PY, "-m", "unittest", "discover", "-s", "tests",
                                 "-p", "test_*.py"], cwd=str(root), env=env,
                                capture_output=True, timeout=1800)
            out = ((pt.stdout or b"") + (pt.stderr or b"")).decode("utf-8", "replace")
            lines = [ln for ln in out.strip().splitlines() if ln.strip()]
            detail = " / ".join(lines[-3:])[:150] if pt.returncode else (lines[-1] if lines else "")
            check("ZIP 内测试套件", pt.returncode == 0, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def verify_wheel(fresh_home):
    whls = sorted(DIST.glob("fundai-*.whl"))
    if not whls:
        return check("wheel 存在", False, "dist/ 下没有 .whl")
    w = whls[-1]
    check("wheel 存在", True, w.name)
    tmp = Path(tempfile.mkdtemp(prefix="fundai_whl_"))
    try:
        site = tmp / "site"
        with zipfile.ZipFile(w) as z:
            names = z.namelist()
            z.extractall(site)
        check("wheel 含包内静态资源",
              any(n.endswith("fundai/static/index.html") for n in names),
              "%d 个条目" % len(names))
        env = dict(os.environ, PYTHONPATH=str(site), FUNDAI_HOME=str(fresh_home))
        code = ("import fundai.web as w, app; print(w.STATIC_DIR)")
        p = subprocess.run([PY, "-c", code], cwd=str(site), env=env,
                           capture_output=True, timeout=300)
        static_dir = (p.stdout or b"").decode("utf-8", "replace").strip()
        want = site / "fundai" / "static"
        ok = (p.returncode == 0 and static_dir
              and os.path.isdir(static_dir) and same_path(static_dir, want))
        check("wheel import 且静态目录在包内", ok,
              "静态目录=%s" % static_dir)
        port = free_port()
        proc = subprocess.Popen([PY, "-m", "app", "serve", "--port", str(port)],
                                cwd=str(site), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            up = wait_server(port, proc)
            if up:
                html = http_bytes("http://127.0.0.1:%d/" % port)
                check("wheel 内起服务（端口 %d）" % port, len(html) > 3000,
                      "首页 %d 字节" % len(html))
            else:
                check("wheel 内起服务（端口 %d）" % port, False,
                      (proc.stderr.read() or b"").decode("utf-8", "replace")[-200:])
        finally:
            proc.terminate()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    skip_tests = "--skip-tests" in sys.argv
    print("=== 发行包验证（dist/）===")
    fresh = Path(tempfile.mkdtemp(prefix="fundai_home_"))
    try:
        verify_zip(fresh, skip_tests)
        verify_wheel(Path(tempfile.mkdtemp(prefix="fundai_home_w_")))
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
    bad = [r for r in RESULTS if not r[1]]
    print("\n合计 %d 项，失败 %d 项" % (len(RESULTS), len(bad)))
    for n, _ok, d in bad:
        print("  ✗ %s %s" % (n, d))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
