# -*- coding: utf-8 -*-
"""组合试错：把已集成的各路模型/规则做成可开关的组合，在同一数据上比收益与回撤。

信号（全部只用到当日及以前）：
  quant   量化技术分（沪深300 技术指标）
  news    消息面分（当前口径：重点事件 + 反向刻度）
  micro   情绪微观分（仅近 ~260 个交易日有快照）
  p_up    事件模型 walk-forward 样本外 P(次日上涨)
  ma_align/vol_rank/ma_cut  制度状态（均线多头、波动分位、跌破均线减仓）
组合维度：
  基础仓位：fixed60（固定 60%）| score（评分驱动）
  调整带：  off | on（制度+情绪 ±10%）
  预测层：  none | model_flat（P≥0.5 满仓否则空仓）| trend_gate（仅确认下跌趋势才降仓）
  反应层：  none | ma_cut（跌破 MA60/120 每档 −20%，底仓 40%）| ma_cut+cool（叠加回撤 10% 冷却 5 天）
评估：前 60% 用于“选组合”，后 40% 只用于报告；费 20bp（按成交额）、最短持有 7 天。
"""
import collections
import io
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
parallel.use_utf8_stdout()  # 已是 UTF-8 就不重包（避免包装器被 GC 关掉底层 buffer）

from fundai import analysis, calib, clsdb, event_model, indicators, settings  # noqa: E402
from fundai import microstructure as ms, strategy, util  # noqa: E402
from fundai import news as newsmod  # noqa: E402

import parallel  # noqa: E402

cfg = settings.load_config()
ST = cfg.get("strategy") or {}
RK = ST.get("risk") or {}
AMP = int(ST.get("news_amp") or 8)
FOCUS = list(ST.get("news_focus_events") or newsmod.DEFAULT_FOCUS_EVENTS)
SCALE = float(ST.get("news_net_scale") or 70.0)
HARD_CAP = float(ST.get("equity_hard_cap", 0.80) or 0.80)
FEE = 0.002
MIN_HOLD = int(ST.get("min_hold_days", 7) or 7)

closes, dates = calib.load_closes(refresh=False)
per_day = collections.defaultdict(list)
for r in clsdb.scored_items():
    ct = r.get("ctime")
    if ct:
        per_day[datetime.fromtimestamp(int(ct), util.TZ_CN).strftime("%Y-%m-%d")].append(r)

# ---- 事件模型样本外预测（一次算好，供组合复用）----
ds = event_model.build_dataset(days=4000, cfg=cfg)
keys = event_model.feature_keys(ds["features"], "market")
ds_m = {"rows": ds["rows"], "features": keys, "dates": ds["dates"],
        "closes": ds["closes"]}
preds = event_model.walk_forward(ds_m, warmup=60, refit_every=5, l2=2.0)
pup = {ds_m["rows"][i]["date"]: preds[i] for i in range(len(ds_m["rows"]))
       if preds[i] is not None}
print("事件模型样本外预测天数:", len(pup))

# ---- 逐日特征 ----
rows = []
peak_total = 1.0
for i, d in enumerate(dates):
    if i + 1 >= len(dates) or i < 260:
        continue
    seg = dates[i - 260:i + 1]
    cl = [closes[x] for x in seg]
    stt = indicators.last_stats(seg, cl, [0] * len(seg))
    quant = analysis.score_market(stt)[0]
    net, _raw, _n = newsmod.net_score(per_day.get(d) or [], mode="scaled",
                                      scale=SCALE, focus=FOCUS, dict_mode="off")
    news = int(max(-100, min(100, net * max(1, AMP))))
    snap = ms.load_snap(d) or {}
    cut = sum(1 for n in (60, 120)
              if len(cl) >= n and cl[-1] < sum(cl[-n:]) / n) * float(
        RK.get("ma_break_step", 0.2) or 0.2)
    cut = min(cut, float(RK.get("ma_break_max", 0.4) or 0.4))
    rows.append({"date": d, "quant": quant, "news": news,
                 "micro": snap.get("score"), "p_up": pup.get(d),
                 "ma_align": indicators.ma_align_state(cl),
                 "vol_rank": indicators.vol_rank(cl), "cut": cut,
                 "ret": closes[dates[i + 1]] / closes[d] - 1.0})
print("回测日数:", len(rows), "|", rows[0]["date"], "→", rows[-1]["date"])


def base_target(r, mode):
    if mode == "fixed60":
        return 0.60
    score = int(round(r["quant"] * (1 - 0.35 - 0.10)
                      + max(-25, min(25, r["news"])) * 0.35
                      + (max(-30, min(30, r["micro"])) * 0.10 if r["micro"] is not None else 0)))
    return strategy.equity_target_weight(cfg, score, None)


def combo_target(r, base, band_on, pred_layer, react_layer):
    tgt = base_target(r, base)
    if band_on:
        b, _ = strategy.position_band(cfg, {"ma_align": r["ma_align"],
                                            "vol_rank": r["vol_rank"],
                                            "micro_pct": None})
        tgt = max(0.0, min(tgt + b, HARD_CAP))
    if pred_layer == "model_flat" and r["p_up"] is not None:
        mult = 1.0 if r["p_up"] >= 0.5 else 0.0
        tgt = tgt * mult
    elif pred_layer == "trend_gate" and r["p_up"] is not None:
        if r["p_up"] < 0.5 and (r["quant"] < 0 or (r["vol_rank"] or 0) > 0.5):
            tgt = min(tgt, 0.30)
    if react_layer in ("ma_cut", "ma_cut+cool") and r["cut"] > 0:
        tgt = max(0.40, tgt - r["cut"])
    return tgt


def sim(base, band_on, pred_layer, react_layer, lo=0, hi=None):
    sub = rows[lo:hi]
    if pred_layer == "hold":            # 满仓持有：无摩擦、无调仓
        nav = 1.0
        peak = 1.0
        mdd = 0.0
        for r in sub:
            nav *= (1.0 + r["ret"])
            peak = max(peak, nav)
            mdd = max(mdd, 1.0 - nav / peak)
        ret = (nav - 1.0) * 100
        return {"ret": ret, "mdd": mdd * 100,
                "ann": (nav ** (252.0 / max(1, len(sub))) - 1) * 100,
                "ratio": ret / max(1e-6, mdd * 100), "switches": 0,
                "nav": round(nav, 4)}
    nav = 1.0
    peak_nav = 1.0
    mdd = 0.0
    pos = 0.6
    last = -999
    cool_until = -1
    switches = 0
    for i, r in enumerate(sub):
        idx = lo + i
        tgt = combo_target(r, base, band_on, pred_layer, react_layer)
        if react_layer == "ma_cut+cool":
            if cool_until >= idx:
                tgt = min(tgt, 0.25)          # 回撤冷却期内降仓
            if i > 0 and (nav / peak_nav - 1.0) <= -float(
                    RK.get("peak_trailing_pct", 0.10) or 0.10):
                cool_until = idx + 5
        if abs(tgt - pos) > 0.05 and (i - last) >= MIN_HOLD:
            nav *= (1.0 - FEE * abs(tgt - pos))
            pos = tgt
            last = i
            switches += 1
        nav *= (1.0 + pos * r["ret"])
        peak_nav = max(peak_nav, nav)
        mdd = max(mdd, 1.0 - nav / peak_nav)
    n = max(1, len(sub))
    ret = (nav - 1.0) * 100
    return {"ret": ret, "mdd": mdd * 100, "ann": (nav ** (252.0 / n) - 1) * 100,
            "ratio": ret / max(1e-6, mdd * 100), "switches": switches,
            "nav": round(nav, 4)}


COMBOS = [
    ("① 满仓持有（参考）", dict(base="fixed60", band_on=False, pred_layer="hold",
                              react_layer="none")),
    ("② 固定60%", dict(base="fixed60", band_on=False, pred_layer="none",
                     react_layer="none")),
    ("③ 评分基础仓位", dict(base="score", band_on=False, pred_layer="none",
                       react_layer="none")),
    ("④ 评分+调整带", dict(base="score", band_on=True, pred_layer="none",
                      react_layer="none")),
    ("⑤ 评分+带+均线减仓", dict(base="score", band_on=True, pred_layer="none",
                         react_layer="ma_cut")),
    ("⑥ 评分+带+均线+冷却", dict(base="score", band_on=True, pred_layer="none",
                          react_layer="ma_cut+cool")),
    ("⑦ 模型多空（P≥0.5）", dict(base="fixed60", band_on=False,
                          pred_layer="model_flat", react_layer="none")),
    ("⑧ 模型多空+均线减仓", dict(base="fixed60", band_on=False,
                          pred_layer="model_flat", react_layer="ma_cut")),
    ("⑨ 评分+模型+均线", dict(base="score", band_on=True,
                        pred_layer="model_flat", react_layer="ma_cut")),
    ("⑩ 评分+趋势闸门+冷却", dict(base="score", band_on=True,
                          pred_layer="trend_gate", react_layer="ma_cut+cool")),
    ("⑪ 固定60%+均线+冷却", dict(base="fixed60", band_on=False, pred_layer="none",
                          react_layer="ma_cut+cool")),
    ("⑫ 固定60%+模型多空", dict(base="fixed60", band_on=False,
                          pred_layer="model_flat", react_layer="ma_cut+cool")),
]

cut = int(len(rows) * 0.6)
print("\n=== 选择期（{} → {}，{} 天）===".format(rows[0]["date"], rows[cut - 1]["date"], cut))
print("%-22s %10s %9s %8s %7s" % ("组合", "累计收益", "最大回撤", "收益/回撤", "调仓"))
select_scores = {}
for name, kw in COMBOS:
    x = sim(lo=0, hi=cut, **kw)
    select_scores[name] = x
    print("%-22s %+9.1f%% %8.1f%% %8.2f %7d" % (
        name, x["ret"], x["mdd"], x["ratio"], x["switches"]))

best = max(select_scores.items(), key=lambda kv: kv[1]["ratio"])
print("\n选择期按“收益/回撤”最优：{}（比值 {:.2f}）".format(best[0], best[1]["ratio"]))

print("\n=== 留出期（{} → {}，{} 天，仅报告）===".format(
    rows[cut]["date"], rows[-1]["date"], len(rows) - cut))
print("%-22s %10s %9s %8s %7s" % ("组合", "累计收益", "最大回撤", "收益/回撤", "调仓"))
holdout_scores = {}
for name, kw in COMBOS:
    x = sim(lo=cut, hi=None, **kw)
    holdout_scores[name] = x
    flag = "  ←选中" if name == best[0] else ""
    print("%-22s %+9.1f%% %8.1f%% %8.2f %7d%s" % (
        name, x["ret"], x["mdd"], x["ratio"], x["switches"], flag))
util.save_json(str(util.data_file("combo_study.json")),
               {"select": select_scores, "holdout": holdout_scores,
                "best": best[0], "select_end": rows[cut - 1]["date"],
                "rows": len(rows)})
print("\n已存档 data/combo_study.json")
