# -*- coding: utf-8 -*-
"""真实引擎的**分市场状态独立回测** + **子系统阉割对比**（基金级，含真实费用）。

与 `regime_study.py` 的分工
---------------------------
* `regime_study.py`：**指数级仓位政策**扫描（快、可扫几千组参数），但它只把
  消息/情绪分用于"评分驱动仓位"，无法体现"消息面影响选基"；
* 本脚本：调用**真实引擎**（`Engine.backtest`，point-in-time 重建备选池、
  真实申赎费、基金级止损止盈、最短持有），在四段数据上分别回测：
    全历史（1930 天）+ 牛市 210 天 + 熊市 210 天 + 震荡市 210 天。
  回放已接入十年消息库的当日消息分（`strategy.replay_news`），
  因此"阉割消息面 / 阉割情绪 / 关掉动态权重 / 开预测型过滤器"等对比第一次可测。

注意（数据边界，必须如实报告）：同花顺涨停快照只有 2025 年后的 260 多个交易日，
所以历史窗口里**情绪维度天然缺失**（记为 0，不做前视填补）；情绪维度的对比只在
全历史口径下有意义，且只覆盖最近一年。

用法：
  python data/regime_backtest.py            # 全部变体
  python data/regime_backtest.py --only live,news_off
输出：控制台表格 + data/regime_backtest.json
"""
import copy
import inspect
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parallel  # noqa: E402
parallel.use_utf8_stdout()  # 已是 UTF-8 就不重包（避免包装器被 GC 关掉底层 buffer）

import harness  # noqa: E402
from fundai import settings, util  # noqa: E402
from fundai.datasource import Market  # noqa: E402
from fundai.engine import Engine  # noqa: E402
from fundai.ledger import Ledger  # noqa: E402


def variant(base_cfg, **patch):
    """深拷贝配置并对 strategy / strategy.risk 打补丁。"""
    c = copy.deepcopy(base_cfg)
    st = c.setdefault("strategy", {})
    rk = st.setdefault("risk", {})
    for k, v in patch.items():
        if k.startswith("risk."):
            rk[k[5:]] = v
        else:
            st[k] = v
    return c


# 阉割 / 开关对比矩阵：每个变体只改一件事
# 注意（2026-09-11 两个坑）：
#  1) `dynamic_weights=True` 时消息权重被**限幅**在 [news_weight_min, news_weight_max]
#     之间（默认 [0.25,0.40]），所以"把消息权重设成 0"并不等于阉割消息面；
#     真正的阉割必须同时 `dynamic_weights=False`（下面的 news_off 就是这么定义的）。
#  2) 旧代码 `analysis.dynamic_weights` 用 `or 0.35` 兜底，会把显式 0 换成 0.35，
#     导致 news_off 与 news_035 给出逐位相同的结论（已修，且这里加了 news_zero_dyn_on
#     作为"只设 0 但不关动态权重"的对照，用来展示限幅的存在）。
VARIANTS = [
    ("live", "线上配置（固定70% + 基金级止损，指数级择时关）", {}),
    ("news_off", "阉割消息面（news_weight=0 且关动态权重）",
     {"news_weight": 0.0, "dynamic_weights": False}),
    ("news_zero_dyn_on", "只把消息权重设 0（动态权重仍开 → 被限幅回 0.25）",
     {"news_weight": 0.0}),
    ("news_035", "静态消息权重 35%（旧默认，关动态权重）",
     {"news_weight": 0.35, "dynamic_weights": False}),
    ("replay_news_off", "回放不带消息面（纯量化，旧口径）",
     {"replay_news": False}),
    ("dict_full", "词典兜底全量参与（news_dict_mode=full）",
     {"news_dict_mode": "full"}),
    ("micro_off", "阉割情绪微观（micro_enable=false）", {"micro_enable": False}),
    ("dynw_off", "关闭动态权重（静态 20%/10%）", {"dynamic_weights": False}),
    ("cooldown_on", "开启回撤冷却（峰值回撤10% → 仓位≤25%，5 天）",
     {"risk.peak_trailing_pct": 0.10}),
    ("ma_cut_on", "开启均线减仓（破 MA60/120 各减 20%）",
     {"risk.ma_break_step": 0.2}),
    ("ma_cut_cool", "均线减仓 + 回撤冷却同时开",
     {"risk.ma_break_step": 0.2, "risk.peak_trailing_pct": 0.10}),
    ("base_60", "基准仓位降到 60%", {"equity_base_fixed": 0.60}),
    ("base_80", "基准仓位升到 80%", {"equity_base_fixed": 0.80}),
    ("band_on", "开启调整带 ±10%（制度+情绪）", {"position_band_max": 0.10}),
    ("theme_off", "阉割题材热度因子（theme_heat=0）",
     {"factor_weights": None}),
    ("overlay_on", "开启预测型过滤器（十年负贡献，默认关）",
     {"risk_overlay_enable": True}),
    # ===== 最终组合测试：在"均线减仓 + 回撤冷却"这个最好的底子上加一层改动 =====
    ("final_B", "最终 B：MA减仓+回撤冷却（设计值 0.2/0.10）",
     {"risk.ma_break_step": 0.2, "risk.peak_trailing_pct": 0.10}),
    ("final_C", "最终 C：B + 基准仓位 60%（更保守）",
     {"risk.ma_break_step": 0.2, "risk.peak_trailing_pct": 0.10,
      "equity_base_fixed": 0.60}),
    ("final_D", "最终 D：B + 关动态权重（静态 20%/10%）",
     {"risk.ma_break_step": 0.2, "risk.peak_trailing_pct": 0.10,
      "dynamic_weights": False}),
    ("final_E", "最终 E：B + 词典兜底全量参与",
     {"risk.ma_break_step": 0.2, "risk.peak_trailing_pct": 0.10,
      "news_dict_mode": "full"}),
    ("final_F", "最终 F：B + 消息 35%/关动态权重",
     {"risk.ma_break_step": 0.2, "risk.peak_trailing_pct": 0.10,
      "news_weight": 0.35, "dynamic_weights": False}),
    ("final_G", "最终 G：B + 基准仓位 80%（更激进）",
     {"risk.ma_break_step": 0.2, "risk.peak_trailing_pct": 0.10,
      "equity_base_fixed": 0.80}),
    ("final_H", "最终 H：B + 均线减仓步长 0.1（更温和）",
     {"risk.ma_break_step": 0.1, "risk.peak_trailing_pct": 0.10}),
    ("final_I", "最终 I：只回撤冷却（不叠均线减仓）",
     {"risk.peak_trailing_pct": 0.10}),
    # ===== 敏感性检验：final_H（MA步长 0.1）看起来"四窗口全面更优"，必须验证
    #       是不是幸运点 —— 扫附近步长，看结论是否稳定（而不是单点最优）=====
    ("sens_ma005", "敏感性：MA步长 0.05 + 回撤冷却",
     {"risk.ma_break_step": 0.05, "risk.peak_trailing_pct": 0.10}),
    ("sens_ma015", "敏感性：MA步长 0.15 + 回撤冷却",
     {"risk.ma_break_step": 0.15, "risk.peak_trailing_pct": 0.10}),
    ("sens_ma020", "敏感性：MA步长 0.20 + 回撤冷却（= final_B）",
     {"risk.ma_break_step": 0.20, "risk.peak_trailing_pct": 0.10}),
    ("sens_ma030", "敏感性：MA步长 0.30 + 回撤冷却",
     {"risk.ma_break_step": 0.30, "risk.peak_trailing_pct": 0.10}),
    ("sens_cool008", "敏感性：MA步长 0.1 + 冷却阈值 8%",
     {"risk.ma_break_step": 0.10, "risk.peak_trailing_pct": 0.08}),
    ("sens_cool015", "敏感性：MA步长 0.1 + 冷却阈值 15%",
     {"risk.ma_break_step": 0.10, "risk.peak_trailing_pct": 0.15}),
    ("sens_no_cut", "敏感性：MA步长 0（关均线减仓）+ 冷却 10%",
     {"risk.ma_break_step": 0.0, "risk.peak_trailing_pct": 0.10}),
]


def months_for(w):
    """窗口天数 → months 参数（按自然月粗算，_prep_replay 再按 end_date 截断）。"""
    return max(1, int(round(w["days"] / 21.0)))


def run_one(cfg, window, end_date=None, months=None):
    mkt = Market(cfg)
    # 兼容 datasource 是否支持 offline 形参（历史区间用本地缓存，不烧配额）
    if "offline" not in inspect.signature(Market.index_history).parameters:
        orig = Market.index_history
        Market.index_history = lambda self, need_from=None, offline=False: \
            orig(self, need_from=need_from)
    eng = Engine(cfg, Ledger(":memory:", initial_cash=1000.0), market=mkt)
    return eng.backtest(months=months or 12, source="live", end_date=end_date)


# ---------------- 并行 worker：(变体 × 回测段) 一个任务，彼此完全独立 ----------------
_RB = {}


def _init_rb(base_cfg, runs):
    _RB["cfg"] = base_cfg
    _RB["runs"] = dict(runs)


def apply_patch(cfg, patch):
    """在配置副本上打补丁（支持 risk.* 前缀；factor_weights=None 表示把 theme_heat 归零）。"""
    c = copy.deepcopy(cfg)
    patch = dict(patch)
    if "factor_weights" in patch and patch["factor_weights"] is None:
        fw = dict((c.get("strategy") or {}).get("factor_weights") or {})
        fw["theme_heat"] = 0.0
        c.setdefault("strategy", {})["factor_weights"] = fw
        patch = {k: v for k, v in patch.items() if k != "factor_weights"}
    st = c.setdefault("strategy", {})
    rk = st.setdefault("risk", {})
    for k, v in patch.items():
        if k.startswith("risk."):
            rk[k[5:]] = v
        else:
            st[k] = v
    return c


def _rb_task(task):
    """跑一个 (变体, 段)：返回 (变体key, 段key, 指标)。异常也返回结构化错误，不炸整池。"""
    key, patch, run_key = task
    cfg = apply_patch(_RB["cfg"], patch)
    w = _RB["runs"][run_key]
    t0 = time.time()
    try:
        r = run_one(cfg, w, end_date=w["end"], months=months_for(w))
        return (key, run_key, {
            "ret": round(r["ret_pct"] * 100, 2),
            "mdd": round(r["max_dd_pct"] * 100, 2),
            "bench": (round(r["bench_ret_pct"] * 100, 2)
                      if r["bench_ret_pct"] is not None else None),
            "days": r["days"], "trades": r["trades"],
            "fees": round(r["fees"], 2), "start": r["start"], "end": r["end"],
            "secs": round(time.time() - t0, 1)})
    except Exception as e:
        return (key, run_key, {"error": "{}: {}".format(type(e).__name__,
                                                        str(e)[:200]),
                               "secs": round(time.time() - t0, 1)})


def main():
    jobs = None
    if "--jobs" in sys.argv:
        jobs = int(sys.argv[sys.argv.index("--jobs") + 1])
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    # 研究跑批**不复用线上配额计数**：并行 worker 同时写同一个计数文件会互相覆盖，
    # 而且历史区间本来就是离线读缓存（offline=True），压根不该记到"今日已用"里。
    os.environ.setdefault("FUNDAI_USAGE_FILE",
                          str(util.data_file("cache/study_usage.json")))
    base_cfg = settings.load_config()
    days = harness.load_days(base_cfg)
    chosen, _cands = harness.windows_by_outcome(days)
    runs = [("full", {"start": days[0]["date"], "end": days[-1]["date"],
                      "days": len(days)})]
    for k in ("bull", "bear", "range"):
        runs.append((k, chosen[k]))
    print("回测段：")
    for k, w in runs:
        print("  %-6s %s → %s  %4d 天" % (k, w["start"], w["end"], w["days"]))

    out = {"windows": {k: {kk: w[kk] for kk in ("start", "end", "days")}
                       for k, w in runs},
           "variants": {}}
    todo = [v for v in VARIANTS if not only or v[0] in only]
    tasks = [(key, patch, k) for key, _n, patch in todo for k, _w in runs]
    print("并行：%s（%d 个变体 × %d 段 = %d 个独立任务）"
          % (parallel.describe(jobs), len(todo), len(runs), len(tasks)))
    t_all = time.time()
    rows = parallel.pmap(_rb_task, tasks, init=_init_rb,
                         initargs=(base_cfg, {k: w for k, w in runs}),
                         jobs=jobs)
    print("矩阵完成，总耗时 %.1f 秒（串行约需 %.0f 秒）"
          % (time.time() - t_all,
             sum((r[2].get("secs") or 0) for r in rows)))
    for key, run_key, res in rows:
        out["variants"].setdefault(key, {"name": dict(
            (k, n) for k, n, _p in VARIANTS).get(key, key),
            "patch": {k2: str(v2) for k2, v2 in
                      dict((k, p) for k, _n, p in VARIANTS)[key].items()},
            "res": {}})
        out["variants"][key]["res"][run_key] = res
    for key, name, _patch in todo:
        v = out["variants"].get(key)
        if not v:
            continue
        print("\n=== %s（%s）===" % (key, name))
        for k, _w in runs:
            r = v["res"].get(k) or {}
            if r.get("error"):
                print("  %-6s 失败：%s" % (k, r["error"]))
            else:
                print("  %-6s 收益 %+7.2f%%  回撤 %5.2f%%  基准 %+7.2f%%  交易 %-3d 费 %6.2f（%.0fs）"
                      % (k, r.get("ret", 0), r.get("mdd", 0), r.get("bench") or 0.0,
                         r.get("trades", 0), r.get("fees", 0.0), r.get("secs", 0)))
    util.save_json(str(util.data_file("regime_backtest.json")), out)

    # ---- 汇总表 ----
    print("\n\n===== 汇总（收益%/回撤%，四段独立回测）=====")
    print("%-18s %-22s %-22s %-22s %-22s" % ("变体", "全历史", "牛市", "熊市", "震荡"))
    for key, name, _p in VARIANTS:
        if key not in out["variants"]:
            continue
        row = []
        for k, _w in runs:
            r = out["variants"][key]["res"].get(k) or {}
            row.append("—" if r.get("error") else "%+7.2f/%5.1f" % (r["ret"],
                                                                    r["mdd"]))
        print("%-18s %s" % (key, " ".join("%-22s" % c for c in row)))
    print("\n已存档 data/regime_backtest.json")


if __name__ == "__main__":
    main()
