# -*- coding: utf-8 -*-
"""全量 CLI 可执行性审计：逐个真跑 23 个子命令，记录退出码/耗时/输出。

纪律
----
* **绝不动线上数据**：所有会写账本/备选池的命令一律用 `--db <临时文件>`；
  会写消息库/知识库的只跑"最小动作"（1 天 / 3 天 / limit 很小）；
* 每个命令都要真跑（不是只跑 `--help`），因为"参数解析通过但执行报错"正是要查的；
* 结果落 `data/audit_report.json`，便于复现与对比（例如打包后再跑一次）。

用法：python data/audit.py            # 全部
      python data/audit.py --quick    # 跳过联网/耗时命令
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parallel  # noqa: E402
parallel.use_utf8_stdout()  # 已是 UTF-8 就不重包（避免包装器被 GC 关掉底层 buffer）

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def cases(quick=False, tmpdb=None):
    """(名称, 参数, 超时秒, 分类, 期望退出码) —— 分类用于报告分组。

    期望退出码：`None` 表示"必须 0"；给了数字表示"这个非零是**预期行为**"
    （例如给不存在的文件 → 应当优雅报错并返回 1，而不是当成崩溃）。
    """
    out = []
    # 1) 全部子命令的 --help（解析器/参数定义是否健全）
    subs = ["init", "run-daily", "state", "demo", "backtest", "refresh-pool",
            "serve", "news-pull", "search-queue", "search-import", "doctor",
            "vendor-echarts", "oos", "fetch-history", "signals-update",
            "signals-report", "cls-import", "micro-pulse", "news-calibrate",
            "event-model", "news-fetch-db", "news-score-db", "intraday-pulse",
            "direction-check"]
    for s in subs:
        out.append(("help:" + s, [s, "--help"], 60, "help", None))
    # 2) 真跑（安全子集）
    out += [
        ("doctor", ["doctor"], 180, "run", None),
        ("state(临时库)", ["state", "--db", tmpdb], 120, "run", None),
        ("init(临时库)", ["init", "--db", tmpdb, "--cash", "1000"], 120, "run", None),
        ("backtest(2月)", ["backtest", "--months", "2", "--source", "live"], 600, "run", None),
        ("oos(12月)", ["oos", "--months", "12", "--test", "0.35"], 900, "run", None),
        ("refresh-pool(临时库)", ["refresh-pool", "--db", tmpdb], 600, "run", None),
        ("run-daily(临时库,force)", ["run-daily", "--db", tmpdb, "--force"], 900, "run", None),
        ("signals-report", ["signals-report"], 300, "run", None),
        ("signals-update(3天)", ["signals-update", "--days", "3"], 600, "run", None),
        ("direction-check(今日)", ["direction-check"], 300, "run", None),
        ("search-queue", ["search-queue"], 120, "run", None),
        ("search-import(无文件应优雅)", ["search-import"], 180, "run", None),
        ("cls-import(文件不存在→优雅报错)",
         ["cls-import", "--file", str(ROOT / "data" / "__nope__.txt"),
          "--date", "2026-09-10"], 120, "run", 1),
        ("micro-pulse(指定日)", ["micro-pulse", "--date", "2026-09-10"], 600, "run", None),
        ("intraday-pulse", ["intraday-pulse"], 600, "run", None),
        ("news-score-db(limit 50)", ["news-score-db", "--limit", "50"], 600, "run", None),
        ("news-calibrate(60天)", ["news-calibrate", "--days", "60"], 900, "run", None),
    ]
    if not quick:
        out += [
            ("news-fetch-db(1天)", ["news-fetch-db", "--days", "1",
                                    "--max-pages", "3"], 600, "net", None),
            ("fetch-history(1年,并集写回)", ["fetch-history", "--years", "1"], 600, "net", None),
            ("news-pull", ["news-pull"], 900, "net", None),
            ("event-model(120天,不落盘)",
             ["event-model", "--days", "120", "--features", "market",
              "--no-save"], 1200, "net", None),
            ("vendor-echarts", ["vendor-echarts"], 600, "net", None),
        ]
    return out


def run_one(name, args, timeout, expect_rc=None):
    t0 = time.time()
    # ⚠️ 共享状态保护：`refresh-pool` 会把结果写进 **共享的** data/pool_state.json
    # （动态备选池是本部署的真状态，不随 --db 隔离）。审计跑的是临时账本，
    # 其"持仓"为空 → 重建出来的池会**丢掉线上持仓**（2026-09-11 实测：线上持仓
    # 4 只被淘汰出池，界面一度把它们的市值显示为 ¥0）。因此这里先快照、后还原。
    shared = ROOT / "data" / "pool_state.json"
    backup = None
    if shared.exists() and any(a in ("refresh-pool",) for a in args):
        backup = shared.read_bytes()
    try:
        p = subprocess.run([PY, "app.py"] + args, cwd=str(ROOT),
                           capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        rc = p.returncode
    except subprocess.TimeoutExpired:
        return {"name": name, "args": args, "rc": None, "secs": round(time.time() - t0, 1),
                "ok": False, "why": "超时（{}s）".format(timeout),
                "out": "", "err": ""}
    finally:
        if backup is not None:
            try:
                shared.write_bytes(backup)
                print("      （已还原共享状态 data/pool_state.json，避免审计污染线上备选池）")
            except Exception:
                pass
    tail = [ln for ln in (out.strip().splitlines() or []) if ln.strip()][:3]
    etail = [ln for ln in (err.strip().splitlines() or []) if ln.strip()][:4]
    if expect_rc is None:
        ok = (rc == 0)
    else:
        ok = (rc == expect_rc)          # 预期的非零（优雅报错）
    return {"name": name, "args": args, "rc": rc, "secs": round(time.time() - t0, 1),
            "ok": bool(ok), "expected_rc": expect_rc, "out": tail, "err": etail}


def main():
    quick = "--quick" in sys.argv
    jobs = None
    if "--jobs" in sys.argv:
        jobs = int(sys.argv[sys.argv.index("--jobs") + 1])
    with tempfile.TemporaryDirectory() as td:
        tmpdb = str(Path(td) / "audit.db")
        rows = []
        # 分两组：**只读类**并行跑（每个都是独立子进程，等 I/O 时正好占满核）；
        # **写入类**（写账本/消息库/备选池/共享缓存）必须串行，否则互相踩状态。
        cases_all = cases(quick, tmpdb)
        SERIAL_NAMES = ("init(临时库)", "refresh-pool(临时库)",
                        "run-daily(临时库,force)", "news-fetch-db(1天)",
                        "news-score-db(limit 50)", "news-pull",
                        "signals-update(3天)", "direction-check(今日)",
                        "cls-import(文件不存在→优雅报错)",
                        "search-import(无文件应优雅)", "intraday-pulse",
                        "fetch-history(1年,并集写回)", "vendor-echarts")
        par = [c for c in cases_all if c[0] not in SERIAL_NAMES]
        ser = [c for c in cases_all if c[0] in SERIAL_NAMES]
        print("分组：只读 %d 项并行（%s）/ 写入 %d 项串行"
              % (len(par), parallel.describe(jobs), len(ser)))

        def _run(case):
            name, args, timeout, kind, expect_rc = case
            r = run_one(name, args, timeout, expect_rc)
            r["kind"] = kind
            return r

        rows += parallel.tmap(_run, par, jobs=jobs)
        for case in ser:
            rows.append(_run(case))
        order = {c[0]: i for i, c in enumerate(cases_all)}
        rows.sort(key=lambda r: order.get(r["name"], 999))
        for r in rows:
            mark = "OK " if r["ok"] else "FAIL"
            print("[%s] %-28s rc=%-4s %5ss %s" % (
                mark, r["name"], r["rc"], r["secs"],
                ("| " + (r["out"][0][:70] if r["out"] else "")) if r["ok"]
                else ("| " + (r.get("why") or ((r["err"] or r["out"] or [""])[0])[:90]))))
        bad = [r for r in rows if not r["ok"]]
        print("\n合计 %d 项，失败 %d 项" % (len(rows), len(bad)))
        for r in bad:
            print("  ✗ %s → rc=%s %s" % (r["name"], r["rc"],
                                         (r.get("why") or "") or
                                         " / ".join((r["err"] or r["out"])[:2])[:160]))
        with open(ROOT / "data" / "audit_report.json", "w", encoding="utf-8") as f:
            json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "quick": quick, "rows": rows, "failed": len(bad)},
                      f, ensure_ascii=False, indent=1)
        print("已存档 data/audit_report.json")
        return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
