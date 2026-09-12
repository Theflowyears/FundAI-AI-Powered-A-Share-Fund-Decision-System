# -*- coding: utf-8 -*-
"""情绪维度研究：在当前「量化 50% + 消息 35% + 情绪微观 15%」体系下，
情绪分该怎么用才能提高择时/选基效率、降低风险。

做法（全部样本外思维：只用当日及以前信息）：
1) 逐日重建三路分数：
   - 量化：analysis.score_market(indicators.last_stats(…到当日…))
   - 消息：clsdb 当日电报按当前口径（重点事件 + 刻度换算）聚合
   - 情绪微观：回补的历史情绪快照（microstructure snap["score"]）
2) 对若干「用法变体」比较次日方向命中率与做多/空仓策略表现：
   A 不含情绪（把权重归还量化/消息）        B 现状（15%，±30 限幅）
   C 情绪 25%                              D 情绪 10%
   E 情绪当风控闸门：情绪 ≤ −40 时目标仓位 ×0.5
   F 情绪 ±60 限幅（放大极端情绪的力度）
3) 另做「情绪 → 次日个股基金相对指数」的检验（选基效率）。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
parallel.use_utf8_stdout()  # 已是 UTF-8 就不重包（避免包装器被 GC 关掉底层 buffer）

from fundai import analysis, calib, clsdb, indicators, microstructure as ms  # noqa: E402
from fundai import news as newsmod, settings, util  # noqa: E402

cfg = settings.load_config()
ST = cfg.get("strategy") or {}
AMP = int(ST.get("news_amp") or 8)
FOCUS = list(ST.get("news_focus_events") or newsmod.DEFAULT_FOCUS_EVENTS)
DICT_MODE = str(ST.get("news_dict_mode") or "off")
SCALE = float(ST.get("news_net_scale") or 70.0)
DEAD = 0.003

closes, dates = calib.load_closes(refresh=False)
vols_map = {}
try:
    from fundai.datasource import Market
    for d, c, v in (Market(cfg).index_history() or []):
        vols_map[d] = v
except Exception:
    pass

# ---- 每日消息净情绪（按当前口径）----
from datetime import datetime  # noqa: E402
import collections  # noqa: E402

import parallel  # noqa: E402

per_day = collections.defaultdict(list)
for r in clsdb.scored_items():
    ct = r.get("ctime")
    if not ct:
        continue
    d = datetime.fromtimestamp(int(ct), util.TZ_CN).strftime("%Y-%m-%d")
    per_day[d].append(r)

snap_days = []
for d in dates:
    if ms.load_snap(d):
        snap_days.append(d)
snap_days = snap_days[-260:]
print("可用于研究的历史情绪日数:", len(snap_days),
      "|", snap_days[0] if snap_days else "—", "→", snap_days[-1] if snap_days else "—")

rows = []
for d in snap_days:
    i = dates.index(d)
    if i + 1 >= len(dates):
        continue
    start = max(0, i - 260)
    seg_d = dates[start:i + 1]
    st = indicators.last_stats(seg_d, [closes[x] for x in seg_d],
                               [vols_map.get(x) or 0 for x in seg_d])
    quant = analysis.score_market(st)[0]
    net, _raw, _n = newsmod.net_score(per_day.get(d) or [], mode="scaled",
                                      scale=SCALE, focus=FOCUS, dict_mode=DICT_MODE)
    ns = int(max(-100, min(100, net * max(1, AMP))))
    snap = ms.load_snap(d) or {}
    micro = snap.get("score")
    rows.append({"date": d, "quant": quant, "news": ns, "micro": micro,
                 "ret": closes[dates[i + 1]] / closes[d] - 1.0,
                 "vol_rank": (st.get("vol20") or 0)})
print("样本日数:", len(rows), "| 有情绪分的:", sum(1 for r in rows if r["micro"] is not None))


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def composite(r, variant):
    q, n, m = r["quant"], r["news"], r["micro"]
    wn = float(ST.get("news_weight", 0.35))
    if variant == "A_no_micro":
        wq = 1.0 - wn
        return q * wq + clamp(n, -25, 25) * wn
    if m is None:                      # 无情绪分 → 退回 A
        return q * (1.0 - wn) + clamp(n, -25, 25) * wn
    if variant == "B_current":
        wm, cap = 0.15, 30
    elif variant == "C_25pct":
        wm, cap = 0.25, 30
    elif variant == "D_10pct":
        wm, cap = 0.10, 30
    elif variant == "F_cap60":
        wm, cap = 0.15, 60
    else:
        wm, cap = 0.15, 30
    wq = 1.0 - wn - wm
    return q * wq + clamp(n, -25, 25) * wn + clamp(m, -cap, cap) * wm


def evaluate(variant):
    hits = n_dir = 0
    nav = bh = 1.0
    pos = None
    peak = 1.0
    mdd = 0.0
    fee = 0.0005
    for r in rows:
        s = composite(r, variant)
        base = 1.0 if s > 0 else 0.0
        if variant == "E_micro_gate" and r["micro"] is not None and r["micro"] <= -40:
            pos = 0.5 * base
        else:
            pos = base
        if pos != base:
            pass
        nav *= (1.0 + pos * r["ret"])
        bh *= (1.0 + r["ret"])
        peak = max(peak, nav)
        mdd = max(mdd, 1.0 - nav / peak)
        if abs(r["ret"]) >= DEAD:
            n_dir += 1
            if (r["ret"] > 0) == (s > 0):
                hits += 1
    return {"acc": (hits / n_dir) if n_dir else None, "n": n_dir,
            "nav": round(nav, 4), "bh": round(bh, 4), "mdd": round(mdd, 4)}


print("\n%-16s %8s %8s %8s %8s" % ("用法变体", "命中率", "样本", "策略净值", "最大回撤"))
for v in ("A_no_micro", "B_current", "C_25pct", "D_10pct", "E_micro_gate", "F_cap60"):
    e = evaluate(v)
    print("%-16s %7s%% %8d %8.4f %7.1f%%" % (
        v, ("%.1f" % (e["acc"] * 100)) if e["acc"] is not None else "—",
        e["n"], e["nav"], e["mdd"] * 100))
bh_final = 1.0
for _r in rows:
    bh_final *= (1.0 + _r["ret"])
print("买入持有累计: %.4f" % bh_final)

# ---- 情绪极端值分档：它该当“线性权重”还是“极端闸门”？ ----
print("\n情绪分分档 → 次日表现（全部样本 %d 天）:" % len(rows))
buckets = [(-101, -40, "情绪≤−40（冰凉）"), (-40, -15, "−40~−15"),
           (-15, 15, "−15~+15（中性）"), (15, 40, "+15~+40"), (40, 101, "情绪≥+40（过热）")]
for lo, hi, name in buckets:
    sel = [r for r in rows if r["micro"] is not None and lo < r["micro"] <= hi]
    if not sel:
        print("  %-16s 无样本" % name)
        continue
    up = sum(1 for r in sel if r["ret"] > 0) / len(sel)
    avg = sum(r["ret"] for r in sel) / len(sel) * 100
    mov = [r for r in sel if abs(r["ret"]) >= DEAD]
    hit = (sum(1 for r in mov if r["ret"] > 0) / len(mov)) if mov else None
    print("  %-16s n=%3d | 次日上涨占比 %5.1f%% | 平均 %+6.3f%% | 大波动日上涨占比 %s" % (
        name, len(sel), up * 100, avg,
        ("%.1f%%" % (hit * 100)) if hit is not None else "—"))
# 情绪分的独立预测力（与次日涨跌的相关系数）
xs = [r["micro"] for r in rows if r["micro"] is not None]
ys = [r["ret"] for r in rows if r["micro"] is not None]
mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
vx = sum((x - mx) ** 2 for x in xs)
vy = sum((y - my) ** 2 for y in ys)
ic = (sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs)))
      / (vx ** 0.5 * vy ** 0.5)) if vx > 0 and vy > 0 else None
print("\n情绪分与次日涨跌的相关系数 IC = %s" % (
    ("%+.3f" % ic) if ic is not None else "n/a"))
