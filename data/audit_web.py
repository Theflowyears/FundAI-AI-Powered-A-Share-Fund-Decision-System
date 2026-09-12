# -*- coding: utf-8 -*-
"""Web 接口全量审计：探测 web.py 里声明的**每一个**路由。

安全策略
--------
* 默认打**演示库服务器**（`python app.py serve --demo --port 8799`）——演示库对
  所有写操作返回"只读"错误，因此可以放心地把每个 POST 都打一遍；
* GET 路由全部真取，POST 路由用**最小/空 body** 打，重点看：
  ① 是否 500（未捕获异常）；② 是否返回结构化 JSON；③ 写操作是否被正确拒绝。
* 报告落 `data/audit_web.json`。

用法：python data/audit_web.py [--port 8799]
"""
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import parallel  # noqa: E402
parallel.use_utf8_stdout()  # 已是 UTF-8 就不重包（避免包装器被 GC 关掉底层 buffer）

ROOT = Path(__file__).resolve().parent.parent


def routes():
    """从 web.py 源码里抽出 (方法, 路径) 清单（GET/POST 各自的 elif 分支）。"""
    src = (ROOT / "fundai" / "web.py").read_text(encoding="utf-8")
    out = {"GET": set(), "POST": set()}
    mode = None
    for line in src.splitlines():
        if "def do_GET" in line:
            mode = "GET"
        elif "def do_POST" in line:
            mode = "POST"
        elif re.match(r"\s*def \w+", line) and "do_" not in line:
            mode = None
        if mode:
            m = re.search(r'path == "(/api/[^"]+)"', line)
            if m:
                out[mode].add(m.group(1))
    return {k: sorted(v) for k, v in out.items()}


def call(base, method, path, body=None, timeout=60):
    url = base + path
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return {"status": r.status, "secs": round(time.time() - t0, 2),
                    "json_ok": _json_ok(raw), "body": raw[:200]}
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8", "replace")
        return {"status": e.code, "secs": round(time.time() - t0, 2),
                "json_ok": _json_ok(raw), "body": raw[:200]}
    except Exception as e:
        return {"status": None, "secs": round(time.time() - t0, 2),
                "json_ok": False, "body": "{}: {}".format(type(e).__name__, str(e)[:120])}


def _json_ok(raw):
    try:
        json.loads(raw)
        return True
    except Exception:
        return False


# 每个 POST 的最小 body（写操作在演示库上应被拒绝；这些不打真数据）
POST_BODY = {
    "/api/run-daily": {},
    "/api/pool/refresh": {},
    "/api/orders/confirm": {"id": 1},
    "/api/orders/skip": {"id": 1},
    "/api/orders/fill": {"id": 1, "amount": 100, "shares": 100, "nav": 1.0},
    "/api/positions/refresh": {},
    "/api/backtest": {"months": 2},
    "/api/news/pull": {"date": "2026-09-10"},
    "/api/micro/refresh": {},
    "/api/news/rate": {"date": "2026-09-10", "item_id": "nope", "label": "bull"},
    "/api/calibration/backfill": {"days": 3, "max_pages": 2},
    "/api/event-model/run": {"days": 120, "features": "market"},
    "/api/history/extend": {"years": 1},
    "/api/news/direction": {"date": "2026-09-10", "dir": "neutral"},
    "/api/news/search/export": {"date": "2026-09-10"},
    "/api/news/search/import": {"date": "2026-09-10"},
    "/api/config/set": {"set": {"strategy.news_weight": 0.22}},
    "/api/config/rollback": {},
}

# 重接口单独给足超时（首次调用要生成演示数据/跑回放，60 秒会误判成"异常"）
SLOW_ROUTES = {"/api/backtest": 300, "/api/event-model/run": 300,
               "/api/calibration/backfill": 300, "/api/history/extend": 300,
               "/api/micro/refresh": 180, "/api/positions/refresh": 180,
               "/api/run-daily": 600, "/api/pool/refresh": 300,
               "/api/news/pull": 300}


def main():
    port = 8799
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    base = "http://127.0.0.1:{}".format(port)
    rs = routes()
    rows = []
    print("路由：GET %d 个 / POST %d 个（并行探测，%s）"
          % (len(rs["GET"]), len(rs["POST"]), parallel.describe()))

    def _one(task):
        method, p = task
        r = call(base, method, p,
                 POST_BODY.get(p, {}) if method == "POST" else None,
                 timeout=SLOW_ROUTES.get(p, 90))
        r.update(path=p, method=method)
        return r

    # I/O 密集：GET 与轻量 POST 并发打（被拒绝也是有效结果）；
    # 重接口（回测/建模/历史延伸/刷新）单独串行，避免互相抢服务端的 _busy 锁。
    light = ([("GET", p) for p in rs["GET"]]
             + [("POST", p) for p in rs["POST"] if p not in SLOW_ROUTES]
             + [("GET", p) for p in ("/", "/app.js", "/style.css",
                                     "/vendor/echarts.min.js")])
    rows += parallel.tmap(_one, light)
    for p in [p for p in rs["POST"] if p in SLOW_ROUTES]:
        rows.append(_one(("POST", p)))
    for r in rows:                      # 静态资源标记（报告里单独分组）
        if r["path"] in ("/", "/app.js", "/style.css", "/vendor/echarts.min.js"):
            r["static"] = True

    bad = [r for r in rows if r["status"] is None or (r["status"] or 0) >= 500
           or not r["json_ok"] and not r.get("static")]
    print("\n%-6s %-34s %-4s %-6s %s" % ("方法", "路径", "码", "耗时", "返回"))
    for r in rows:
        flag = "  " if r not in bad else "✗ "
        print("%s%-6s %-34s %-4s %5ss %s" % (flag, r["method"], r["path"],
                                             r["status"], r["secs"],
                                             r["body"][:96].replace("\n", " ")))
    print("\n异常项：%d / %d" % (len(bad), len(rows)))
    for r in bad:
        print("  ✗ %s %s → %s %s" % (r["method"], r["path"], r["status"],
                                     r["body"][:140]))
    with open(ROOT / "data" / "audit_web.json", "w", encoding="utf-8") as f:
        json.dump({"base": base, "rows": rows, "abnormal": len(bad),
                   "generated": time.strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False, indent=1)
    print("已存档 data/audit_web.json")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
