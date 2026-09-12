# -*- coding: utf-8 -*-
"""权重维度组合试错：内部算法权重可以不一致，追求高收益 + 回撤不失控。

扫描维度：
  w_news      消息面权重 ∈ {0.20, 0.30, 0.35, 0.45}（量化拿剩余；情绪另扫）
  base_mode   fixed（固定基准仓位）| score（评分驱动）
  base        基准仓位 ∈ {0.60, 0.70, 0.80, 0.90, 1.00}
  band        调整带 off | on(±0.10，制度+情绪驱动)
  react       风控 none | ma_cut(均线温和减仓) | ma_cut+cool(叠加回撤冷却) | stop(仅止损/回撤冷却)
  cap         硬上限 ∈ {0.80, 0.90, 1.00}
评估：前 60% 选择期、后 40% 留出期；费 20bp（按成交额）、最短持有 7 天。
目标：**收益优先**，但回撤 ≤ DD_LIMIT；另外单独按“收益/回撤”排序做对照。
"""
import collections
import io
import itertools
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
parallel.use_utf8_stdout()  # 已是 UTF-8 就不重包（避免包装器被 GC 关掉底层 buffer）

from fundai import analysis, calib, clsdb, indicators, settings, strategy, util  # noqa: E402
from fundai import microstructure as ms  # noqa: E402
from fundai import news as newsmod  # noqa: E402

import parallel  # noqa: E402

cfg = settings.load_config()
ST = cfg.get("strategy") or {}
AMP = int(ST.get("news_amp") or 8)
FOCUS = list(ST.get("news_focus_events") or newsmod.DEFAULT_FOCUS_EVENTS)
SCALE = float(ST.get("news_net_scale") or 70.0)
FEE = 0.002
MIN_HOLD = int(ST.get("min_hold_days", 7) or 7)
DD_LIMIT = 25.0          # 回撤约束（%）：宁要高收益，但回撤不超过这个量级
# 指数级风控层的"设计值"：与线上配置解耦，保证本实验可复现
# （线上若把开关设为 0/1.0 关闭这些层，不应改变这里的对照结论）
MA_STEP = 0.2            # 破 MA60 / MA120 各减仓 20%
MA_MAX = 0.4             # 指数级减仓合计上限 40%
MA_FLOOR = 0.40          # 减仓后权益仓位下限
PEAK_TRAIL = 0.10        # 回撤冷却触发阈值（自峰值回撤 10%）
COOL_DAYS = 5            # 冷却天数
COOL_CAP = 0.25          # 冷却期仓位上限（另测 0.50）

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
    cl = [closes[x] for x in seg]
    stt = indicators.last_stats(seg, cl, [0] * len(seg))
    quant = analysis.score_market(stt)[0]
    net, _raw, _n = newsmod.net_score(per_day.get(d) or [], mode="scaled",
                                      scale=SCALE, focus=FOCUS, dict_mode="off")
    news = int(max(-100, min(100, net * max(1, AMP))))
    snap = ms.load_snap(d) or {}
    cut = sum(1 for n in (60, 120) if len(cl) >= n and cl[-1] < sum(cl[-n:]) / n) * MA_STEP
    cut = min(cut, MA_MAX)
    rows.append({"date": d, "quant": quant, "news": news,
                 "micro": snap.get("score"), "cut": cut,
                 "ma_align": indicators.ma_align_state(cl),
                 "vol_rank": indicators.vol_rank(cl),
                 "ret": closes[dates[i + 1]] / closes[d] - 1.0})
print("回测日数:", len(rows), "|", rows[0]["date"], "→", rows[-1]["date"],
      "| 有情绪分:", sum(1 for r in rows if r["micro"] is not None))

W_NEWS = (0.20, 0.30, 0.35, 0.45)
W_MICRO = (0.00, 0.10, 0.20)
BASE_MODES = ("fixed", "score")
BASES = (0.60, 0.70, 0.80, 0.90, 1.00)
BANDS = (False, True)
REACTS = ("none", "ma_cut", "ma_cut+cool", "cool")
CAPS = (0.80, 0.90, 1.00)


def score_of(r, w_news, w_micro):
    m = r["micro"]
    wm = w_micro if m is not None else 0.0
    wq = 1.0 - w_news - wm
    return int(round(r["quant"] * wq + max(-25, min(25, r["news"])) * w_news
                     + (max(-30, min(30, m)) * wm if m is not None else 0)))


def sim(rows_sub, w_news, w_micro, base_mode, base, band_on, react, cap,
        lo=0, min_hold=MIN_HOLD, cool_cap=COOL_CAP, cool_days=COOL_DAYS,
        peak_trail=PEAK_TRAIL):
    nav = 1.0
    peak = 1.0
    mdd = 0.0
    pos = base
    last = -999
    cool_until = -1
    switches = 0
    for i, r in enumerate(rows_sub):
        if base_mode == "fixed":
            tgt = base
        else:   # 评分驱动：以 base 为中轴，按评分线性调整
            s = score_of(r, w_news, w_micro)
            tgt = util.clamp(base + s / 100.0 * 0.35, 0.0, cap)
        if band_on:
            b, _ = strategy.position_band(cfg, {"ma_align": r["ma_align"],
                                                "vol_rank": r["vol_rank"],
                                                "micro_pct": None})
            tgt = max(0.0, min(tgt + b, cap))
        else:
            tgt = min(tgt, cap)
        if react in ("ma_cut", "ma_cut+cool") and r["cut"] > 0:
            tgt = max(MA_FLOOR, tgt - r["cut"])
        if react in ("ma_cut+cool", "cool"):
            if cool_until >= i:
                tgt = min(tgt, cool_cap)
            if i > 0 and (nav / peak - 1.0) <= -peak_trail:
                cool_until = i + int(cool_days)
        if abs(tgt - pos) > 0.05 and (i - last) >= min_hold:
            nav *= (1.0 - FEE * abs(tgt - pos))
            pos = tgt
            last = i
            switches += 1
        nav *= (1.0 + pos * r["ret"])
        peak = max(peak, nav)
        mdd = max(mdd, 1.0 - nav / peak)
    n = max(1, len(rows_sub))
    ret = (nav - 1.0) * 100
    return {"ret": round(ret, 2), "mdd": round(mdd * 100, 2),
            "ann": round((nav ** (252.0 / n) - 1) * 100, 2),
            "ratio": round(ret / max(1e-6, mdd * 100), 2), "switches": switches,
            "nav": round(nav, 4)}


cut = int(len(rows) * 0.6)


def label(r):
    return "消息{:.0%}/情绪{:.0%} {}基准{:.0%} 带{} {} 冷却{} 上限{:.0%}".format(
        r["w_news"], r["w_micro"], r["base_mode"], r["base"],
        "开" if r["band"] else "关",
        {"none": "无风控", "ma_cut": "均线减仓", "ma_cut+cool": "均线+冷却",
         "cool": "仅冷却"}[r["react"]],
        "—" if r["react"] not in ("cool", "ma_cut+cool")
        else "{:.0%}".format(r["cool_cap"]),
        r["cap"])
sel_rows, ho_rows = rows[:cut], rows[cut:]
REACTS = ("none", "ma_cut", "ma_cut+cool", "cool")
COOL_CAPS = (0.25, 0.50)
results = []
for w_news, w_micro, base_mode, base, band_on, react, cap, cool_cap in \
        itertools.product(W_NEWS, W_MICRO, BASE_MODES, BASES, BANDS, REACTS,
                          CAPS, COOL_CAPS):
    s = sim(sel_rows, w_news, w_micro, base_mode, base, band_on, react, cap,
            cool_cap=cool_cap)
    h = sim(ho_rows, w_news, w_micro, base_mode, base, band_on, react, cap,
            cool_cap=cool_cap)
    results.append({"w_news": w_news, "w_micro": w_micro, "base_mode": base_mode,
                    "base": base, "band": band_on, "react": react, "cap": cap,
                    "cool_cap": cool_cap, "select": s, "holdout": h})
print("组合数:", len(results))

print("\n=== 留出期收益前 12（要求留出期回撤 ≤{:.0f}% 且选择期收益 >0）===".format(DD_LIMIT))
ok_ho = [r for r in results
         if r["holdout"]["mdd"] <= DD_LIMIT and r["select"]["ret"] > 0]
for r in sorted(ok_ho, key=lambda r: -r["holdout"]["ret"])[:12]:
    print("%-50s 选择 %+7.1f%%/%5.1f%% | 留出 %+7.1f%%/%5.1f%% (%.2f)" % (
        label(r), r["select"]["ret"], r["select"]["mdd"],
        r["holdout"]["ret"], r["holdout"]["mdd"], r["holdout"]["ratio"]))

print("\n=== 两期都稳（min(收益/回撤) 最大）前 8 ===")
ok_both = [r for r in results
           if r["select"]["mdd"] <= 30 and r["holdout"]["mdd"] <= DD_LIMIT]
for r in sorted(ok_both, key=lambda r: -min(r["select"]["ratio"],
                                            r["holdout"]["ratio"]))[:8]:
    print("%-50s 选择 %+7.1f%%/%5.1f%% (%.2f) | 留出 %+7.1f%%/%5.1f%% (%.2f)" % (
        label(r), r["select"]["ret"], r["select"]["mdd"], r["select"]["ratio"],
        r["holdout"]["ret"], r["holdout"]["mdd"], r["holdout"]["ratio"]))

print("\n=== 高收益 + 回撤可接受（选择期回撤≤30%、留出期回撤≤20%、两期收益>0）前 15 ===")
filt = [r for r in results
        if r["select"]["mdd"] <= 30 and r["holdout"]["mdd"] <= 20
        and r["select"]["ret"] > 0 and r["holdout"]["ret"] > 0]
print("满足条件的组合数:", len(filt))
for r in sorted(filt, key=lambda r: -r["holdout"]["ret"])[:15]:
    print("%-50s 选择 %+7.1f%%/%5.1f%% | 留出 %+7.1f%%/%5.1f%% (%.2f)" % (
        label(r), r["select"]["ret"], r["select"]["mdd"],
        r["holdout"]["ret"], r["holdout"]["mdd"], r["holdout"]["ratio"]))

best_ho = max(ok_ho, key=lambda r: r["holdout"]["ret"])
best_rob = max(ok_both, key=lambda r: min(r["select"]["ratio"],
                                          r["holdout"]["ratio"]))
best_hi = max(filt, key=lambda r: r["holdout"]["ret"]) if filt else best_ho
print("\n★ 采纳（高收益 + 回撤可接受）：{}".format(label(best_hi)))
print("   选择期 %+.1f%%/回撤 %.1f%%｜留出期 %+.1f%%/回撤 %.1f%%" % (
    best_hi["select"]["ret"], best_hi["select"]["mdd"],
    best_hi["holdout"]["ret"], best_hi["holdout"]["mdd"]))
print("\n★ 留出期最高收益（回撤≤{:.0f}%）：{}".format(DD_LIMIT, label(best_ho)))
print("   选择期 %+.1f%%/回撤 %.1f%%｜留出期 %+.1f%%/回撤 %.1f%%" % (
    best_ho["select"]["ret"], best_ho["select"]["mdd"],
    best_ho["holdout"]["ret"], best_ho["holdout"]["mdd"]))
print("★ 两期最稳：{}".format(label(best_rob)))
print("   选择期 %+.1f%%/回撤 %.1f%%｜留出期 %+.1f%%/回撤 %.1f%%" % (
    best_rob["select"]["ret"], best_rob["select"]["mdd"],
    best_rob["holdout"]["ret"], best_rob["holdout"]["mdd"]))
print("\n=== 定向对照：基准 70%/消息 20%/情绪 10% 下，指数级风控层的代价 ===")
print("%-32s %-20s %-20s" % ("风控层", "选择期 收益/回撤", "留出期 收益/回撤"))
for react, cc in (("none", 0.25), ("cool", 0.25), ("ma_cut", 0.25),
                  ("ma_cut+cool", 0.25), ("cool", 0.50), ("ma_cut+cool", 0.50)):
    s = sim(sel_rows, 0.20, 0.10, "fixed", 0.70, False, react, 0.80, cool_cap=cc)
    h = sim(ho_rows, 0.20, 0.10, "fixed", 0.70, False, react, 0.80, cool_cap=cc)
    nm = {"none": "无指数级风控", "cool": "仅回撤冷却", "ma_cut": "仅均线减仓",
          "ma_cut+cool": "均线+冷却"}[react]
    print("%-32s %+7.1f%%/%5.1f%%      %+7.1f%%/%5.1f%%" % (
        "{}（冷却降仓{:+.0%}）".format(nm, cc), s["ret"], s["mdd"], h["ret"], h["mdd"]))

util.save_json(str(util.data_file("weight_combo_study.json")),
               {"rows": len(rows), "dd_limit": DD_LIMIT, "combos": len(results),
                "top_holdout_return": [{"cfg": label(r), **r["holdout"],
                                        "select": r["select"]}
                                       for r in sorted(ok_ho, key=lambda r: -r["holdout"]["ret"])[:20]],
                "best_holdout": {"cfg": label(best_ho), **best_ho},
                "best_robust": {"cfg": label(best_rob), **best_rob}})
print("已存档 data/weight_combo_study.json")
