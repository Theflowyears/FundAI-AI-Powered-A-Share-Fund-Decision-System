# -*- coding: utf-8 -*-
"""反应型风控 vs 预测型/满仓：十年指数级验证。

口径：基础仓位（评分驱动）→ 动态调整带（制度+情绪）→ 反应型减仓（跌破均线）
      → 硬上限 80%；每次仓位变动收 20bp 摩擦（场外基金）。
"""
import collections
import io
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
parallel.use_utf8_stdout()  # 已是 UTF-8 就不重包（避免包装器被 GC 关掉底层 buffer）

from fundai import analysis, calib, clsdb, indicators, settings, strategy, util  # noqa: E402
from fundai import news as newsmod  # noqa: E402

import parallel  # noqa: E402

cfg = settings.load_config()
ST = cfg.get("strategy") or {}
AMP = int(ST.get("news_amp") or 8)
FOCUS = list(ST.get("news_focus_events") or newsmod.DEFAULT_FOCUS_EVENTS)
SCALE = float(ST.get("news_net_scale") or 70.0)
HARD_CAP = float(ST.get("equity_hard_cap", 0.80) or 0.80)
FEE = 0.002

closes, dates = calib.load_closes(refresh=False)
per_day = collections.defaultdict(list)
for r in clsdb.scored_items():
    ct = r.get("ctime")
    if ct:
        per_day[datetime.fromtimestamp(int(ct), util.TZ_CN).strftime("%Y-%m-%d")].append(r)

rows = []
for i, d in enumerate(dates):
    if i + 1 >= len(dates) or i < 260:
        continue
    seg = dates[i - 260:i + 1]
    stt = indicators.last_stats(seg, [closes[x] for x in seg], [0] * len(seg))
    quant = analysis.score_market(stt)[0]
    net, _raw, _n = newsmod.net_score(per_day.get(d) or [], mode="scaled",
                                      scale=SCALE, focus=FOCUS, dict_mode="off")
    news = int(max(-100, min(100, net * max(1, AMP))))
    score = int(round(quant * (1 - 0.35 - 0.10) + max(-25, min(25, news)) * 0.35))
    ret = closes[dates[i + 1]] / closes[d] - 1.0
    cl = [closes[x] for x in seg]
    ma = {n: (sum(cl[-n:]) / n if len(cl) >= n else None) for n in (20, 60, 120)}
    cuts = sum(1 for n in (20, 60, 120)
               if ma[n] and cl[-1] < ma[n]) * float(
        (ST.get("risk") or {}).get("ma_break_step", 0.2) or 0.2)
    cuts = min(cuts, float((ST.get("risk") or {}).get("ma_break_max", 0.6) or 0.6))
    rows.append({"date": d, "score": score, "ret": ret, "cut": cuts,
                 "ma_align": indicators.ma_align_state(cl),
                 "vol_rank": indicators.vol_rank(cl)})
print("样本:", len(rows), "天 |", rows[0]["date"], "→", rows[-1]["date"])


def sim(mode, min_hold=7):
    """手续费按**成交额**计（0.2%/次），并遵守 min_hold 个交易日的最短持有（与线上一致）。"""
    nav = bh = 1.0
    peak = 1.0
    mdd = 0.0
    pos = 0.6
    switches = 0
    last_change = -999
    for i, r in enumerate(rows):
        base = strategy.equity_target_weight(cfg, r["score"], None)
        if mode == "buy_hold":
            tgt = 1.0
        elif mode == "base_only":
            tgt = min(base, HARD_CAP)
        elif mode == "base_band":
            band, _ = strategy.position_band(cfg, {"ma_align": r["ma_align"],
                                                   "vol_rank": r["vol_rank"]})
            tgt = max(0.0, min(base + band, HARD_CAP))
        elif mode == "base_band_ma":
            band, _ = strategy.position_band(cfg, {"ma_align": r["ma_align"],
                                                   "vol_rank": r["vol_rank"]})
            tgt = max(0.0, min(base + band, HARD_CAP) - r["cut"])
        elif mode == "static_60":
            tgt = 0.60
        else:
            tgt = 1.0
        if abs(tgt - pos) > 0.05 and (i - last_change) >= min_hold:
            traded = abs(tgt - pos)
            nav *= (1.0 - FEE * traded)
            pos = tgt
            switches += 1
            last_change = i
        nav *= (1.0 + pos * r["ret"])
        bh *= (1.0 + r["ret"])
        peak = max(peak, nav)
        mdd = max(mdd, 1.0 - nav / peak)
    n = len(rows)
    return {"ret": (nav - 1) * 100, "ann": (nav ** (252.0 / n) - 1) * 100,
            "mdd": mdd * 100, "switches": switches, "bh": (bh - 1) * 100}


print("\n%-16s %10s %10s %10s %8s" % ("口径", "累计收益", "年化", "最大回撤", "调仓"))
for label, mode in (("满仓持有", "buy_hold"), ("静态60%", "static_60"),
                    ("基础仓位(评分)", "base_only"), ("基础+调整带", "base_band"),
                    ("基础+带+均线减仓", "base_band_ma")):
    x = sim(mode)
    print("%-16s %+9.1f%% %+9.2f%% %9.1f%% %8d" % (
        label, x["ret"], x["ann"], x["mdd"], x["switches"]))

# 关键年份对照
print("\n关键年份累计收益:")
years = ["2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025"]
print("%-18s %s" % ("口径", "  ".join("%7s" % y for y in years)))
for label, mode in (("满仓持有", "buy_hold"), ("基础+调整带", "base_band"),
                    ("基础+带+均线减仓", "base_band_ma")):
    cell = []
    for y in years:
        sub = [r for r in rows if r["date"].startswith(y)]
        nav = 1.0
        for r in sub:
            base = strategy.equity_target_weight(cfg, r["score"], None)
            if mode == "buy_hold":
                tgt = 1.0
            elif mode == "base_band":
                band, _ = strategy.position_band(cfg, {"ma_align": r["ma_align"],
                                                       "vol_rank": r["vol_rank"]})
                tgt = max(0.0, min(base + band, HARD_CAP))
            else:
                band, _ = strategy.position_band(cfg, {"ma_align": r["ma_align"],
                                                       "vol_rank": r["vol_rank"]})
                tgt = max(0.0, min(base + band, HARD_CAP) - r["cut"])
            nav *= (1 + tgt * r["ret"])
        cell.append("%+6.1f%%" % ((nav - 1) * 100))
    print("%-18s %s" % (label, "  ".join(cell)))
