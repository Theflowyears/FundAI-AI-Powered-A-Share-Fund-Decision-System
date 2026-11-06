# -*- coding: utf-8 -*-
"""量化决策/组合构建的四窗口 + 年度稳健性评估（真实基金级引擎）。

为什么单独一个脚本
------------------
`regime_backtest.py` 是“子系统阉割对比”，参数变体写死在模块里；本脚本面向
**决策/组合层**的可复现评估，统一比较：当前 live、等权/逆波动/最小方差、
基金级风控开关、基准仓位档位。输出一张四窗口表 + 全历史年度分解，并按
「回撤≤25% 前提下收益优先」给出候选推荐；推荐只作为证据，不自动改配置。

口径（与 README §6/§12 一致，必须如实披露）
--------------------------------------------
* 回放：point-in-time 逐日重建备选池，前 60 个交易日预热不计入统计；
* 费用：按配置的申赎费（C 类持有≥7 天通常为 0），不含滑点；
* 窗口：全历史 + 牛/熊/震荡三组独立留出期（`harness.windows_by_outcome`）
  + 近 12 个月；四窗口不是全路径，年度分解用于检查“是否只靠某一年”；
* 结论按“回撤≤25%、收益优先”排序，不承诺未来收益。

用法：
  python data/decision_study.py                 # 全量（较慢，约 10+ 分钟）
  python data/decision_study.py --only live,min_var
  python data/decision_study.py --quick         # 只跑震荡 + 近12个月
  python data/decision_study.py --jobs 4        # 并行（环境不支持会自动串行）
输出：控制台表格 + data/decision_study.json
"""
import io
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parallel  # noqa: E402
parallel.use_utf8_stdout()

import harness  # noqa: E402
from fundai import settings, util  # noqa: E402
import regime_backtest as rb  # noqa: E402

NO_TP_STOP = {"risk.fund_stop_loss_enable": True,
              "risk.fund_take_profit_enable": False}
PURE_MOM = {"factor_weights": {"mom": 1.0, "mom5": 0.0, "vol": 0.0,
                               "bias": 0.0, "heat": 0.0, "theme_heat": 0.0}}

VARIANTS = [
    ("live", "当前配置（adaptive/mom20 + 关基金级风控）", {}),
    ("legacy_live", "旧配置（legacy + 基金级止损/止盈）",
     {"portfolio_method": "legacy",
      "risk.fund_stop_loss_enable": True,
      "risk.fund_take_profit_enable": True}),
    ("adaptive_ma_align", "自适应·均线多头信号",
     {"adaptive_signal": "ma_align"}),
    ("equal_nofundrisk", "等权 + 关基金级风控",
     {"portfolio_method": "equal"}),
    ("inv_vol_nofundrisk", "逆波动 + 关基金级风控",
     {"portfolio_method": "inv_vol"}),
    ("min_var_nofundrisk", "最小方差 + 关基金级风控",
     {"portfolio_method": "min_var"}),
    ("no_tp_equal", "保留止损/关止盈 + 等权",
     dict(NO_TP_STOP, portfolio_method="equal")),
    ("legacy_nofundrisk", "旧动量加权 + 关基金级风控",
     {"portfolio_method": "legacy"}),
    ("pure_mom", "纯 20 日动量因子（adaptive/关风控）", dict(PURE_MOM)),
    ("base_80_adaptive", "adaptive + 固定基准仓位 80%",
     {"equity_base_fixed": 0.80}),
    ("base_70_adaptive", "adaptive + 固定基准仓位 70%",
     {"equity_base_fixed": 0.70}),
    ("min_var_cap35", "最小方差 + 单基金上限 35%",
     {"portfolio_method": "min_var", "max_fund_weight": 0.35}),
    # ---- 仓位档位（README §25.2b：证明档位之间不是单调关系）----
    ("adaptive90_cap95", "自适应 + 基准90%/上限95%",
     {"equity_base_fixed": 0.90, "equity_hard_cap": 0.95}),
    ("adaptive70_cap80", "自适应 + 基准70%/上限80%",
     {"equity_base_fixed": 0.70, "equity_hard_cap": 0.80}),
    ("legacy90_cap95", "旧策略 + 基准90%/上限95%",
     {"portfolio_method": "legacy",
      "risk.fund_stop_loss_enable": True,
      "risk.fund_take_profit_enable": True,
      "equity_base_fixed": 0.90, "equity_hard_cap": 0.95}),
]

DD_LIMIT = 25.0


def yearly(series):
    """按自然年复合收益（只用于检查路径是否靠某一年）。"""
    out = {}
    for i in range(1, len(series or [])):
        y = series[i]["date"][:4]
        prev = series[i - 1]["total"]
        if prev:
            out.setdefault(y, []).append(series[i]["total"] / prev - 1.0)
    return {y: round((math.prod(1 + x for x in v) - 1) * 100, 2)
            for y, v in out.items()}


def _metrics(r):
    return {
        "ret": round(r["ret_pct"] * 100, 2),
        "mdd": round(r["max_dd_pct"] * 100, 2),
        "bench": (round(r["bench_ret_pct"] * 100, 2)
                  if r.get("bench_ret_pct") is not None else None),
        "trades": r["trades"], "days": r["days"],
        "start": r.get("start"), "end": r.get("end"),
        "yearly": yearly(r.get("series")),
    }


_STUDY = {}


def _init_study(cfg, runs):
    _STUDY["cfg"] = cfg
    _STUDY["runs"] = dict(runs)


def _task(task):
    """(variant_key, patch, window_key) -> (variant_key, window_key, metrics)。"""
    vkey, patch, wkey = task
    cfg = rb.apply_patch(_STUDY["cfg"], patch)
    w = _STUDY["runs"][wkey]
    t0 = time.time()
    try:
        r = rb.run_one(cfg, w, end_date=w["end"],
                       months=w.get("months") or rb.months_for(w))
        rec = _metrics(r)
    except Exception as e:
        rec = {"error": "{}: {}".format(type(e).__name__, str(e)[:300])}
    rec["secs"] = round(time.time() - t0, 1)
    return vkey, wkey, rec


def _eligible(res):
    """回撤约束：全历史与每个窗口的最大回撤都不超过 DD_LIMIT。"""
    for k, r in (res or {}).items():
        if r.get("error"):
            return False
        if float(r.get("mdd") or 999) > DD_LIMIT + 1e-9:
            return False
    return True


def _worst_ret(res, keys):
    vals = []
    for k in keys:
        r = (res or {}).get(k) or {}
        if r.get("ret") is not None:
            vals.append(float(r["ret"]))
    return min(vals) if vals else None


def main():
    # 默认串行：多个进程同时回放会共享 K 线缓存写，曾出现 .tmp 竞争；
    # 需要加速时显式 --jobs N（缓存已热、且能接受并发写风险）。
    jobs = 1
    if "--jobs" in sys.argv:
        jobs = int(sys.argv[sys.argv.index("--jobs") + 1])
    only = None
    if "--only" in sys.argv:
        only = {x.strip() for x in sys.argv[sys.argv.index("--only") + 1]
                .split(",") if x.strip()}
    quick = "--quick" in sys.argv
    only_windows = None
    if "--windows" in sys.argv:
        only_windows = {x.strip() for x in
                        sys.argv[sys.argv.index("--windows") + 1].split(",")
                        if x.strip()}

    cfg = settings.load_config()
    days = harness.load_days(cfg)
    chosen, _cands = harness.windows_by_outcome(days)
    runs = {"full": {"start": days[0]["date"], "end": days[-1]["date"],
                     "days": len(days)}}
    for k in ("bull", "bear", "range"):
        if chosen.get(k):
            runs[k] = chosen[k]
    runs["recent"] = {"start": days[-274]["date"], "end": days[-1]["date"],
                      "days": 274, "months": 12}
    if quick:
        runs = {k: runs[k] for k in ("range", "recent") if k in runs}
    if only_windows:
        runs = {k: v for k, v in runs.items() if k in only_windows}
    print("评估窗口：")
    for k, w in runs.items():
        print("  %-6s %s → %s %4d 天" % (k, w["start"], w["end"], w["days"]))

    todo = [v for v in VARIANTS if not only or v[0] in only]
    tasks = [(key, patch, k) for key, _name, patch in todo for k in runs]
    print("变体 %d × 窗口 %d = %d 个任务；并行：%s"
          % (len(todo), len(runs), len(tasks), parallel.describe(jobs)))
    t_all = time.time()
    rows = parallel.pmap(_task, tasks, init=_init_study,
                         initargs=(cfg, dict(runs)), jobs=jobs)
    out = {"generated_at": util.now_iso(),
           "windows": {k: {kk: w[kk] for kk in ("start", "end", "days")}
                       for k, w in runs.items()},
           "dd_limit": DD_LIMIT,
           "variants": {}}
    for key, name, patch in todo:
        out["variants"][key] = {"name": name,
                                "patch": {k: str(v) for k, v in patch.items()},
                                "res": {}}
    for vkey, wkey, rec in rows:
        out["variants"][vkey]["res"][wkey] = rec

    keys = [k for k in ("full", "bull", "bear", "range", "recent") if k in runs]
    print("\n%-20s %s" % ("变体", " ".join("%-20s" % k for k in keys)))
    for key, name, _patch in todo:
        res = out["variants"][key]["res"]
        cells = []
        for k in keys:
            r = res.get(k) or {}
            cells.append("ERR" if r.get("error") else
                         "%+7.2f/%5.1f" % (r.get("ret", 0), r.get("mdd", 0)))
        print("%-20s %s" % (key, " ".join("%-20s" % c for c in cells)))

    # 推荐：回撤全部达标 → 全历史收益优先 → 最差窗口收益 → 交易数少
    eligible = [(key, out["variants"][key]["res"]) for key, _n, _p in todo
                if _eligible(out["variants"][key]["res"])]
    eligible.sort(key=lambda kv: (
        -(kv[1].get("full", {}).get("ret") or -999),
        -(_worst_ret(kv[1], [k for k in keys if k != "full"]) or -999),
        kv[1].get("full", {}).get("trades") or 999))
    out["recommended"] = [k for k, _r in eligible[:3]]
    out["note"] = ("推荐只按四窗口+全历史+回撤约束排序，不代表未来收益；"
                   "单路径回测方差大，必须结合年度分解与样本外复核。")
    print("\n推荐（DD≤%.0f%%、全历史收益优先）：%s"
          % (DD_LIMIT, "、".join(out["recommended"]) or "无达标变体"))
    util.save_json(str(util.data_file("decision_study.json")), out)
    print("已存档 data/decision_study.json；总耗时 %.1f 秒"
          % (time.time() - t_all))


if __name__ == "__main__":
    main()
