# -*- coding: utf-8 -*-
"""组合试错（新口径）：**全历史 + 牛市/熊市/震荡市三组独立留出期**，回撤上限 25%。

与上一轮（`weight_combo_study.py`）的区别
----------------------------------------
1. 验证口径：不再用"前 60% 选 / 后 40% 留出"的顺序切分（用户指出它无法模拟牛熊），
   改为 **全部历史（1930 天）+ 三组客观挑选、互不重叠的市场状态窗口**，
   每个窗口**独立回测**，同时给出收益与最大回撤（`harness.windows_by_outcome`）。
2. 回撤约束：**每个窗口的最大回撤都必须 ≤ 25%**（用户指定），再按收益排序。
3. 消息面口径修正：上一轮扫描把 DB 行直接喂 `net_score`（键名不匹配）→
   消息分**恒为 0**，所谓"消息权重"其实只是评分缩放；本轮用
   `news.daily_score_map()`（与线上同日同口径，且已修"利空算利多"的符号 BUG）。

输出：控制台表格 + `data/regime_combo_study.json`
用法：python data/regime_study.py [--dd 25]
"""
import io
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parallel  # noqa: E402
parallel.use_utf8_stdout()  # 已是 UTF-8 就不重包（避免包装器被 GC 关掉底层 buffer）

import harness  # noqa: E402
from fundai import settings, util  # noqa: E402

DD_LIMIT = 25.0
W_NEWS = (0.0, 0.10, 0.20, 0.30, 0.45)
W_MICRO = (0.0, 0.10, 0.20)
BASES = (0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00)
CAPS = (0.60, 0.70, 0.80, 0.90, 1.00)
BANDS = (False, True)
REACTS = ("none", "ma_cut", "cool", "ma_cut+cool")
BASE_MODES = ("fixed", "score")


def label(c):
    return "消息{:.0%}/情绪{:.0%} {}基准{:.0%} 带{} {} 上限{:.0%}".format(
        c["w_news"], c["w_micro"], c["base_mode"], c["base"],
        "开" if c["band"] else "关",
        {"none": "无风控", "ma_cut": "均线减仓", "cool": "回撤冷却",
         "ma_cut+cool": "均线+冷却"}[c["react"]], c["cap"])


# ---- 并行 worker：每个进程负责一组「消息/情绪权重」，内部遍历其余维度 ----
_STUDY = {}


def _init_study(chosen, cfg):
    _STUDY["chosen"] = chosen
    _STUDY["cfg"] = cfg


def _eval_group(task):
    """评估一个 (w_news, w_micro) 组下的全部组合（worker 函数，模块级可 pickle）。"""
    w_news, w_micro = task
    chosen, cfg = _STUDY["chosen"], _STUDY["cfg"]
    days = harness.load_days(cfg)          # 读磁盘缓存，避免把大对象 pickle 给每个 worker
    sc = harness.score_series(days, cfg, w_news=w_news, w_micro=w_micro)
    out = []
    for base_mode, base, cap, band, react in itertools.product(
            BASE_MODES, BASES, CAPS, BANDS, REACTS):
        if base > cap:
            continue
        cfg_c = harness.variant_cfg(cfg, w_news=w_news, w_micro=w_micro,
                                    band_max=(0.10 if band else 0.0),
                                    base=base, cap=cap)
        combo = {"w_news": w_news, "w_micro": w_micro, "base_mode": base_mode,
                 "base": base, "cap": cap, "band": band, "react": react}
        res, ok = {}, True
        for k in ("full", "bull", "bear", "range"):
            seq, s2 = harness.slice_window(days, sc, chosen.get(k))
            r = harness.sim_series(seq, s2, cfg_c, base_mode=base_mode,
                                   base=base, band=band, react=react, cap=cap)
            res[k] = r
            if r is None or r["mdd"] > DD_LIMIT:
                ok = False
        combo["res"] = res
        combo["dd_ok"] = ok
        combo["ret_full"] = res["full"]["ret"]
        combo["mdd_max"] = max(r["mdd"] for r in res.values() if r)
        combo["ret_min"] = min(r["ret"] for r in res.values() if r)
        combo["label"] = label(combo)
        out.append(combo)
    return out


def main():
    jobs = None
    if "--jobs" in sys.argv:
        jobs = int(sys.argv[sys.argv.index("--jobs") + 1])
    cfg = settings.load_config()
    days = harness.load_days(cfg)
    chosen, cands = harness.windows_by_outcome(days)
    print("样本：%d 个交易日（%s → %s）" % (len(days), days[0]["date"],
                                            days[-1]["date"]))
    print("留出期（客观挑选、互不重叠）：")
    for k in ("bull", "bear", "range"):
        w = chosen[k]
        print("  %-6s %s → %s  %3d 天  指数 %+7.2f%%（指数回撤 %5.2f%%）" % (
            k, w["start"], w["end"], w["days"], w["idx_ret"] * 100,
            w["idx_mdd"] * 100))
    print("并行：%s" % parallel.describe(jobs))

    tasks = list(itertools.product(W_NEWS, W_MICRO))
    groups = parallel.pmap(_eval_group, tasks, init=_init_study,
                           initargs=(chosen, cfg), jobs=jobs)
    results = [c for g in groups for c in g]
    print("组合数：%d（%d 个权重组并行评估）" % (len(results), len(tasks)))

    ok = [c for c in results if c["dd_ok"]]
    print("\n=== 约束：每个窗口最大回撤 ≤ %.0f%% ===" % DD_LIMIT)
    print("满足约束的组合：%d / %d" % (len(ok), len(results)))

    def table(rows, title, n=12, key=None):
        print("\n=== %s ===" % title)
        print("%-46s %16s %16s %16s %16s" % ("组合", "全历史", "牛市", "熊市", "震荡"))
        for c in rows[:n]:
            r = c["res"]
            print("%-46s %+7.2f%%/%5.1f%% %+7.2f%%/%5.1f%% %+7.2f%%/%5.1f%% %+7.2f%%/%5.1f%%"
                  % (c["label"][:46], r["full"]["ret"], r["full"]["mdd"],
                     r["bull"]["ret"], r["bull"]["mdd"],
                     r["bear"]["ret"], r["bear"]["mdd"],
                     r["range"]["ret"], r["range"]["mdd"]))

    best_ret = sorted(ok, key=lambda c: -c["ret_full"])
    best_rob = sorted(ok, key=lambda c: -c["ret_min"])
    best_dd = sorted(ok, key=lambda c: c["mdd_max"])
    table(best_ret, "按全历史收益排序（回撤≤25%%）前 12", 12)
    table(best_rob, "按「最差窗口收益」排序（最稳）前 8", 8)
    table(best_dd, "按「最大回撤最小」排序前 3", 3)

    # ---- 当前线上配置的对照 ----
    st = cfg.get("strategy") or {}
    live = {"w_news": float(st.get("news_weight") or 0.2),
            "w_micro": float(st.get("micro_weight") or 0.1),
            "base_mode": str(st.get("equity_base_mode") or "fixed"),
            "base": float(st.get("equity_base_fixed") or 0.7),
            "cap": float(st.get("equity_hard_cap") or 0.8),
            "band": float(st.get("position_band_max") or 0) > 0,
            "react": "none" if float((st.get("risk") or {}).get(
                "ma_break_step") or 0) <= 0 else "ma_cut"}
    sc_live = harness.score_series(days, cfg, w_news=live["w_news"],
                                   w_micro=live["w_micro"])
    cfg_live = harness.variant_cfg(cfg, w_news=live["w_news"],
                                   w_micro=live["w_micro"],
                                   band_max=(0.10 if live["band"] else 0.0),
                                   base=live["base"], cap=live["cap"])
    live_res = {}
    for k in ("full", "bull", "bear", "range"):
        seq, s2 = harness.slice_window(days, sc_live, chosen.get(k))
        live_res[k] = harness.sim_series(seq, s2, cfg_live,
                                         base_mode=live["base_mode"],
                                         base=live["base"], band=live["band"],
                                         react=live["react"], cap=live["cap"])
    print("\n=== 当前线上配置（固定 70%%/上限 80%%/无指数级择时）===")
    for k in ("full", "bull", "bear", "range"):
        r = live_res[k]
        print("  %-6s 收益 %+7.2f%%  回撤 %5.2f%%  （%s → %s，%d 天）"
              % (k, r["ret"], r["mdd"], r["start"], r["end"], r["days"]))

    util.save_json(str(util.data_file("regime_combo_study.json")), {
        "windows": harness.regime_meta(chosen),
        "candidates": {k: v[:20] for k, v in cands.items()},
        "dd_limit": DD_LIMIT,
        "combos": len(results), "dd_ok": len(ok),
        "top_return": [{"label": c["label"], "res": c["res"], "cfg": {
            k: c[k] for k in ("w_news", "w_micro", "base_mode", "base", "cap",
                              "band", "react")}} for c in best_ret[:20]],
        "top_robust": [{"label": c["label"], "res": c["res"]} for c in best_rob[:10]],
        "top_lowdd": [{"label": c["label"], "res": c["res"]} for c in best_dd[:10]],
        "live": {"cfg": live, "res": live_res},
    })
    print("\n已存档 data/regime_combo_study.json")


if __name__ == "__main__":
    if "--dd" in sys.argv:
        DD_LIMIT = float(sys.argv[sys.argv.index("--dd") + 1])
    main()
