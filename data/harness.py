# -*- coding: utf-8 -*-
"""统一回测评估内核：**全历史 + 牛/熊/震荡三种市场状态独立回测**。

为什么要重建口径（2026-09-11）
------------------------------
旧做法用「前 60% 选组合 / 后 40% 留出」的顺序切分。用户指出这不合理：
顺序切分只会切到**某一种**市场状态（后 40% 恰好偏震荡/偏熊时，结论就只对那种
状态成立），无法回答"策略在牛市/熊市/震荡市里各能赚多少、各回撤多少"。

新口径：
1) **全部历史**（1930 个交易日，2018-09→2026-09）作为主评估；
2) 按**指数客观状态**划分牛市 / 熊市 / 震荡市三段连续窗口，各自独立回测
   （窗口由规则枚举得出，不靠人工挑，见 `regimes()`）；
3) 约束：**最大回撤 ≤ 25%**（每个窗口都满足），再按收益/稳健性排序。

本内核建模的是**权益仓位政策**（基准仓位 + 调整带 + 反应型减仓 + 冷却 + 硬上限）
叠加在指数收益上，含 20bp 摩擦与最短持有 7 天；它**不能**建模选基（哪只基金），
选基与基金级止损止盈由 `Engine.backtest()` 的真实基金级回放覆盖（`data/regime_backtest.py`）。

消息面口径（重要）：`news.daily_score_map()` 按日聚合十年消息库，与线上同日同口径。
2026-09-11 修了两个会让研究失真的 BUG（详见 README）：
  * 键名不匹配（DB 的 label/strength/event vs 代码的 auto_label/...）曾让研究里的
    消息分**恒为 0**；
  * 未按 label 取符号曾让事件型利空算成利多（净情绪十年从未为负）。
"""
import collections
import io
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parallel  # noqa: E402
from fundai import (analysis, calib, indicators, microstructure as ms,  # noqa: E402
                    news as newsmod, settings, strategy, util)

FEE = 0.002          # 单边摩擦（按成交额）
MIN_HOLD = 7         # 最短持有（交易日）
WARMUP = 260         # 指标预热（约一年）
BASE_CACHE = "harness_base.json"
EXTRA_CACHE = "harness_daily.json"


# ---------------- 基础数据 ----------------
def _sig(*paths):
    parts = []
    for p in paths:
        try:
            st = p.stat()
            parts.append("{}-{}".format(int(st.st_mtime), st.st_size))
        except Exception:
            parts.append("na")
    return "|".join(parts)


def build_base(cfg=None, use_cache=True, jobs=None):
    """逐日基础量：量化分 / 均线状态 / 波动分位 / 均线破位减仓量。

    **并行**（2026-09-11）：每个交易日互不依赖（各自取 261 天窗口算指标），
    因此按天铺满 CPU。本机 24 物理核，未命中缓存时从 ~90 秒降到 ~10 秒。
    """
    cfg = cfg or settings.load_config()
    cache_path = util.cache_file(BASE_CACHE)
    idx_cache = util.cache_file("kline_000300.SH.json")
    sig = _sig(idx_cache)
    if use_cache:
        c = util.load_json(cache_path, {}) or {}
        if c.get("sig") == sig and c.get("days"):
            return c["days"]
    closes, dates = calib.load_closes(refresh=False)
    idxs = [i for i in range(len(dates)) if i < len(dates) - 1 and i >= WARMUP]
    days = parallel.pmap(_base_one, idxs, init=_init_base,
                         initargs=(dates, closes), jobs=jobs)
    if use_cache:
        util.save_json(cache_path, {"sig": sig, "updated": util.now_iso(),
                                    "days": days})
    return days


# ---- 逐日基础量的并行 worker（数据放模块全局，避免每个任务都 pickle 整条序列）----
_BASE = {}


def _init_base(dates, closes):
    _BASE["dates"] = dates
    _BASE["closes"] = closes


def _base_one(i):
    """单个交易日的基础量（worker 函数，必须是模块级可 pickle 的名字）。"""
    dates, closes = _BASE["dates"], _BASE["closes"]
    d = dates[i]
    seg = dates[i - WARMUP:i + 1]
    cl = [closes[x] for x in seg]
    stt = indicators.last_stats(seg, cl, [0] * len(seg))
    quant = analysis.score_market(stt)[0]
    cut = sum(1 for n in (60, 120)
              if len(cl) >= n and cl[-1] < sum(cl[-n:]) / n) * 0.2
    return {"date": d, "quant": quant,
            "ma_align": indicators.ma_align_state(cl),
            "vol_rank": indicators.vol_rank(cl),
            "cut": min(cut, 0.4),
            "ret": closes[dates[i + 1]] / closes[d] - 1.0,
            "idx": closes[d]}


def attach_news_micro(days, cfg=None, use_cache=True):
    """把当日消息分 / 情绪分挂到每一天（缺失日期为 None，绝不前视填补）。"""
    cfg = cfg or settings.load_config()
    st = cfg.get("strategy") or {}
    cache_path = util.cache_file(EXTRA_CACHE)
    news_sig = _sig(util.data_file("cls_history.db"))
    sig = "{}|{}|{}".format(news_sig, sorted(
        str(x) for x in (st.get("news_focus_events") or [])),
        st.get("news_net_scale"))
    news_map, micro_map = None, None
    if use_cache:
        c = util.load_json(cache_path, {}) or {}
        if c.get("sig") == sig and c.get("news"):
            news_map, micro_map = c["news"], c.get("micro") or {}
    if news_map is None:
        news_map = newsmod.daily_score_map(cfg)
        micro_map = {}
        for d in days:
            try:
                snap = ms.load_snap(d["date"])
            except Exception:
                snap = None
            if snap and snap.get("score") is not None:
                micro_map[d["date"]] = int(snap["score"])
        if use_cache:
            util.save_json(cache_path, {"sig": sig, "updated": util.now_iso(),
                                        "news": news_map, "micro": micro_map})
    for d in days:
        rec = news_map.get(d["date"]) or {}
        d["news_net"] = rec.get("net")
        d["news"] = rec.get("score")
        d["micro"] = (micro_map or {}).get(d["date"])
    return days


def load_days(cfg=None, use_cache=True):
    cfg = cfg or settings.load_config()
    days = build_base(cfg, use_cache=use_cache)
    return attach_news_micro(days, cfg, use_cache=use_cache)


# ---------------- 市场状态划分 ----------------
def regimes(days, gap=20, min_len_bull_bear=100, min_len_range=120):
    """按指数状态客观划分每一天，并给出连续的牛/熊/震荡窗口。

    规则（只用当日及之前的数据，无前视）：
      * 主判据是 **MA200 的 60 日斜率** slope = MA200[i]/MA200[i-60] − 1
        （慢变量、抗抖动；不用"收盘是否在 MA200 上方"当主判据，否则横盘期
         会因反复穿越而标签抖动，切不出连续的震荡窗口）
      * 牛市：slope > +2%        熊市：slope < −2%        震荡：|slope| ≤ 2%
    再对逐日标签做**形态学闭运算**（合并 ≤gap 个交易日的反向缺口），
    否则 2021-12→2023-01 这类大级别熊市会被中间的反弹打断成碎片窗口。
    """
    closes = [d["idx"] for d in days]
    n = len(closes)
    raw = []
    for i in range(n):
        if i < 260:
            raw.append("warmup")
            continue
        ma200 = sum(closes[i - 199:i + 1]) / 200.0
        ma_prev = sum(closes[i - 259:i - 59]) / 200.0
        slope = ma200 / ma_prev - 1.0 if ma_prev else 0.0
        if slope > 0.02:
            raw.append("bull")
        elif slope < -0.02:
            raw.append("bear")
        else:
            raw.append("range")
    # 形态学闭运算：把被 ≤gap 天其它标签隔开的同类段合并
    labels = list(raw)
    i = 0
    while i < n:
        if labels[i] == "warmup":
            i += 1
            continue
        j = i
        while j < n and labels[j] == labels[i]:
            j += 1
        # 检查 labels[i] 与 labels[j...] 是否同类且缺口 ≤ gap
        k = j
        while k < n and labels[k] != labels[i] and labels[k] != "warmup":
            k += 1
        if k < n and labels[k] == labels[i] and (k - j) <= gap:
            for t in range(j, k):
                labels[t] = labels[i]
        i = j
    runs = collections.defaultdict(list)
    cur, start = None, 0
    for i, lab in enumerate(labels + ["__end__"]):
        if lab != cur:
            if cur in ("bull", "bear", "range"):
                floor = min_len_range if cur == "range" else min_len_bull_bear
                if i - start >= floor:
                    runs[cur].append((start, i - 1))
            cur, start = lab, i
    out = {}
    for kind, spans in runs.items():
        items = []
        for a, b in spans:
            seg = days[a:b + 1]
            ret = seg[-1]["idx"] / seg[0]["idx"] - 1.0
            peak, mdd = -1e18, 0.0
            for d in seg:
                peak = max(peak, d["idx"])
                mdd = max(mdd, 1.0 - d["idx"] / peak)
            items.append({"kind": kind, "start": seg[0]["date"],
                          "end": seg[-1]["date"], "days": len(seg),
                          "idx_ret": round(ret, 4), "idx_mdd": round(mdd, 4),
                          "a": a, "b": b})
        items.sort(key=lambda x: -x["days"])
        out[kind] = items
    return labels, out


def pick_windows(reg):
    """从候选窗口里挑三组代表窗口：各状态里**最长**的那一段（客观、可复现）。

    返回选定窗口 + 全部候选（报告里一起给出，便于审计"挑窗有没有偏心"）。
    """
    chosen = {}
    for kind in ("bull", "bear", "range"):
        cands = reg.get(kind) or []
        chosen[kind] = max(cands, key=lambda x: x["days"]) if cands else None
    return chosen


# ---------------- 仓位政策模拟 ----------------
def variant_cfg(cfg, w_news=None, w_micro=None, band_max=None, base=None,
                cap=None, slope=None):
    """复制一份配置并注入本轮实验的基准权重/调整带/基准仓位。

    为什么必须注入（2026-09-11 修）：`analysis.dynamic_weights()` 是从
    **config 里的 news_weight / micro_weight 出发**再走有界微调的，所以只给
    函数传参而不改 cfg，会让"消息权重 0% vs 45%"这一整维**算出完全相同的数**；
    同理 `strategy.position_band()` 读 `position_band_max`，配置为 0 时
    "调整带开/关"也完全等价。注入后每个组合才真正代表不同的政策。
    """
    import copy as _copy
    c = _copy.deepcopy(cfg)
    st = c.setdefault("strategy", {})
    if w_news is not None:
        st["news_weight"] = float(w_news)
    if w_micro is not None:
        st["micro_weight"] = float(w_micro)
    if band_max is not None:
        st["position_band_max"] = float(band_max)
    if base is not None:
        if str(st.get("equity_base_mode") or "fixed") == "fixed":
            st["equity_base_fixed"] = float(base)
        st["eq_base"] = float(base)
    if cap is not None:
        st["equity_hard_cap"] = float(cap)
        st["eq_cap"] = float(cap)
    if slope is not None:
        st["eq_slope"] = float(slope)
    return c


def score_of(day, w_news, w_micro, cfg, news_on=True, micro_on=True,
             dynamic=True):
    """当日合成分（量化 + 消息 + 情绪），与引擎 `combine_score` 同口径。

    cfg 需是 `variant_cfg()` 注入过基准权重的变体，动态权重才会以该组合的
    基准为起点微调（与线上一致）。
    """
    st = cfg.get("strategy") or {}
    w = w_news if news_on else 0.0
    wm = w_micro if micro_on else 0.0
    nsc = day.get("news") if news_on else None
    msc = day.get("micro") if micro_on else None
    if dynamic and st.get("dynamic_weights", True):
        wn, wm2, _note = analysis.dynamic_weights(
            cfg, news_net=(day.get("news_net") if news_on else None),
            micro_pct=None, vol_rank=day.get("vol_rank"))
        w = wn if news_on else 0.0
        wm = wm2 if micro_on else 0.0
    return _combine(day["quant"], nsc, msc, w, wm, cfg)


def _combine(tech, news_sc, micro_sc, w, wm, cfg):
    """与 `engine.combine_score` 完全一致的合成（含限幅）。"""
    st = cfg.get("strategy") or {}
    cap = float(st.get("news_score_cap", 25) or 25)
    mcap = float(st.get("micro_score_cap", 30) or 0)
    if micro_sc is None or wm <= 0:
        if news_sc is None:
            return int(round(tech))
        nv = float(news_sc)
        if cap > 0:
            nv = max(-cap, min(cap, nv))
        return int(round(tech * (1.0 - w) + nv * w))
    mv = float(micro_sc)
    if mcap > 0:
        mv = max(-mcap, min(mcap, mv))
    wm = max(0.0, min(0.30, wm))
    if news_sc is None:
        return int(round(tech * (1.0 - wm) + mv * wm))
    nv = float(news_sc)
    if cap > 0:
        nv = max(-cap, min(cap, nv))
    return int(round(tech * (1.0 - w - wm) + nv * w + mv * wm))


def sim(days, cfg=None, w_news=0.2, w_micro=0.1, base_mode="fixed", base=0.70,
        band=False, react="none", cap=0.80, cool_cap=0.25, cool_days=5,
        peak_trail=0.10, ma_step=0.2, ma_floor=0.40, news_on=True,
        micro_on=True, dynamic=True, min_hold=MIN_HOLD, fee=FEE, window=None):
    """在 days（可切窗口）上模拟权益仓位政策，返回收益/回撤等指标。

    react: none | ma_cut（均线破位减仓） | cool（回撤冷却降仓） | ma_cut+cool
    所有反应型规则都用**当日及之前**的信息，切换按"单边摩擦×变动幅度"计费。
    """
    cfg = cfg or settings.load_config()
    st = cfg.get("strategy") or {}
    seq = days
    if window:
        seq = [d for d in days if window[0] <= d["date"] <= window[1]]
    if not seq:
        return None
    nav, peak, mdd = 1.0, 1.0, 0.0
    pos = None
    last_i = -999
    cool_until = -1
    switches = 0
    daily = []
    for i, d in enumerate(seq):
        if base_mode == "fixed":
            tgt = float(base)
        else:
            sc = score_of(d, w_news, w_micro, cfg, news_on, micro_on, dynamic)
            tgt = max(0.0, min(float(cap), float(base) + sc / 100.0 * 0.35))
        if band:
            b, _ = strategy.position_band(cfg, {"ma_align": d["ma_align"],
                                                "vol_rank": d["vol_rank"],
                                                "micro_pct": None})
            tgt = max(0.0, min(tgt + b, float(cap)))
        else:
            tgt = min(tgt, float(cap))
        if react in ("ma_cut", "ma_cut+cool") and d["cut"] > 0:
            tgt = max(ma_floor, tgt - d["cut"] * (ma_step / 0.2))
        if react in ("ma_cut+cool", "cool"):
            if cool_until >= i:
                tgt = min(tgt, cool_cap)
            if i > 0 and (nav / peak - 1.0) <= -peak_trail:
                cool_until = i + int(cool_days)
        tgt = max(0.0, min(tgt, float(cap)))
        if pos is None:
            pos = tgt
            last_i = i
        elif abs(tgt - pos) > 0.05 and (i - last_i) >= min_hold:
            nav *= (1.0 - fee * abs(tgt - pos))
            pos = tgt
            last_i = i
            switches += 1
        nav *= (1.0 + pos * d["ret"])
        peak = max(peak, nav)
        mdd = max(mdd, 1.0 - nav / peak)
        daily.append(nav)
    n = max(1, len(seq))
    ret = (nav - 1.0)
    ups = sum(1 for i in range(1, len(daily)) if daily[i] > daily[i - 1])
    return {"days": len(seq), "start": seq[0]["date"], "end": seq[-1]["date"],
            "ret": round(ret * 100, 2), "mdd": round(mdd * 100, 2),
            "ann": round(((nav ** (252.0 / n)) - 1) * 100, 2),
            "ratio": round(ret * 100 / max(1e-6, mdd * 100), 2),
            "switches": switches,
            "up_rate": round(ups / max(1, len(daily) - 1) * 100, 1),
            "nav": round(nav, 4)}


def evaluate_all(days, windows, **kw):
    """同一套参数在「全历史 + 每个 regime 窗口」上分别回测。"""
    out = {"full": sim(days, window=None, **kw)}
    for kind, w in (windows or {}).items():
        if not w:
            out[kind] = None
            continue
        out[kind] = sim(days, window=(w["start"], w["end"]), **kw)
    return out


def fmt_row(name, res, keys=("full", "bull", "bear", "range")):
    cells = []
    for k in keys:
        r = res.get(k)
        cells.append("—" if not r else "%+7.2f%%/%5.1f%%" % (r["ret"], r["mdd"]))
    return "%-34s %s" % (name, " | ".join(cells))


def windows_by_outcome(days, min_len=120, max_len=300, step=5):
    """按**指数区间结果**客观挑选牛市 / 熊市 / 震荡市三组窗口。

    为什么不用"逐日状态标注"直接切窗口：A 股的 MA200 很少长期持平，
    纯斜率标注切不出连续的横盘段（实测 0 个），且会把"MA200 仍在下行、
    但价格已从底部反转"的 2023-11→2024-10 误标成熊市（该段指数 +21%）。
    因此改用"区间结果"定义，规则透明、可复现：

      * 牛市：区间指数收益 ≥ +15% 且区间最大回撤 ≤ 18%
      * 熊市：区间指数收益 ≤ −15%
      * 震荡：区间 |收益| ≤ 6% 且最大回撤 ≤ 15%
    在每类候选里取"最典型"的一段（牛市取收益最高、熊市取跌幅最大、震荡取最平），
    并要求三组窗口**互不重叠**；同时输出全部候选，报告里一并披露（防挑窗偏心）。
    """
    n = len(days)
    cands = {"bull": [], "bear": [], "range": []}
    for L in range(min_len, max_len + 1, 30):
        for i in range(0, n - L, step):
            seg = days[i:i + L]
            ret = seg[-1]["idx"] / seg[0]["idx"] - 1.0
            peak, mdd = -1e18, 0.0
            for d in seg:
                peak = max(peak, d["idx"])
                mdd = max(mdd, 1.0 - d["idx"] / peak)
            rec = {"start": seg[0]["date"], "end": seg[-1]["date"], "days": L,
                   "idx_ret": round(ret, 4), "idx_mdd": round(mdd, 4),
                   "a": i, "b": i + L - 1}
            if ret >= 0.15 and mdd <= 0.18:
                cands["bull"].append(rec)
            elif ret <= -0.15:
                cands["bear"].append(rec)
            elif abs(ret) <= 0.06 and mdd <= 0.15:
                cands["range"].append(rec)
    # 去重（同一段多次命中长度不同）：按 (start,end) 归并，保留天数最长的一条
    def dedupe(items):
        best = {}
        for r in items:
            k = (r["start"], r["end"])
            if k not in best or r["days"] > best[k]["days"]:
                best[k] = r
        return list(best.values())

    for k in cands:
        cands[k] = dedupe(cands[k])
        if k == "bull":
            cands[k].sort(key=lambda r: (-r["idx_ret"], -r["days"]))
        elif k == "bear":
            cands[k].sort(key=lambda r: (r["idx_ret"], -r["days"]))
        else:
            cands[k].sort(key=lambda r: (abs(r["idx_ret"]), -r["days"]))
    chosen = {}
    used = []
    for kind in ("bull", "bear", "range"):
        pick = None
        for r in cands[kind]:
            if any(not (r["b"] < u["a"] or r["a"] > u["b"]) for u in used):
                continue
            pick = r
            break
        chosen[kind] = pick
        if pick:
            used.append(pick)
    return chosen, cands


def pick_windows_by_outcome(days, **kw):
    return windows_by_outcome(days, **kw)[0]


def score_series(days, cfg=None, w_news=0.2, w_micro=0.1, news_on=True,
                 micro_on=True, dynamic=True):
    """按日预算合成分（与引擎 `combine_score` 同口径），供大批量扫描复用。

    cfg 会被 `variant_cfg()` 注入基准权重后再算（否则动态权重会忽略入参）。
    """
    cfg0 = cfg or settings.load_config()
    cfg = variant_cfg(cfg0, w_news=w_news, w_micro=w_micro)
    out = []
    for d in days:
        out.append(score_of(d, w_news, w_micro, cfg, news_on, micro_on, dynamic))
    return out


def sim_series(seq, scores, cfg=None, base_mode="fixed", base=0.70, band=False,
               react="none", cap=0.80, cool_cap=0.25, cool_days=5,
               peak_trail=0.10, ma_floor=0.40, min_hold=MIN_HOLD, fee=FEE):
    """在（日切片, 预存评分）上模拟仓位政策；scores 为 None 时忽略评分。

    评分驱动仓位与引擎 `strategy.equity_target_weight` 同式：
      raw = eq_base + eq_slope × score，夹在 [eq_floor, eq_cap]，再套硬上限。
    指数级风控用**设计值**（均线步长 0.2、冷却阈值 10%、冷却 5 天），
    与线上开关解耦，避免"线上关掉某层 → 实验结论跟着漂移"。
    """
    cfg = cfg or settings.load_config()
    st = cfg.get("strategy") or {}
    if not seq:
        return None
    slope = float(st.get("eq_slope", 0.005) or 0.005)
    floor = float(st.get("eq_floor", 0.0) or 0.0)
    ecap = float(st.get("eq_cap", 0.95) or 0.95)
    nav, peak, mdd = 1.0, 1.0, 0.0
    pos, last_i, cool_until, switches = None, -999, -1, 0
    daily = []
    for i, d in enumerate(seq):
        if base_mode == "fixed" or scores is None:
            tgt = float(base)
        else:
            tgt = max(floor, min(ecap, float(base) + slope * scores[i]))
        if band:
            b, _ = strategy.position_band(cfg, {"ma_align": d["ma_align"],
                                                "vol_rank": d["vol_rank"],
                                                "micro_pct": None})
            tgt = tgt + b
        tgt = max(0.0, min(tgt, float(cap)))
        if react in ("ma_cut", "ma_cut+cool") and d["cut"] > 0:
            tgt = max(ma_floor, tgt - d["cut"])
        if react in ("ma_cut+cool", "cool"):
            if cool_until >= i:
                tgt = min(tgt, cool_cap)
            if i > 0 and (nav / peak - 1.0) <= -peak_trail:
                cool_until = i + int(cool_days)
        tgt = max(0.0, min(tgt, float(cap)))
        if pos is None:
            pos, last_i = tgt, i
        elif abs(tgt - pos) > 0.05 and (i - last_i) >= min_hold:
            nav *= (1.0 - fee * abs(tgt - pos))
            pos, last_i, switches = tgt, i, switches + 1
        nav *= (1.0 + pos * d["ret"])
        peak = max(peak, nav)
        mdd = max(mdd, 1.0 - nav / peak)
        daily.append(nav)
    n = max(1, len(seq))
    ret = nav - 1.0
    ups = sum(1 for i in range(1, len(daily)) if daily[i] > daily[i - 1])
    return {"days": len(seq), "start": seq[0]["date"], "end": seq[-1]["date"],
            "ret": round(ret * 100, 2), "mdd": round(mdd * 100, 2),
            "ann": round(((nav ** (252.0 / n)) - 1) * 100, 2),
            "ratio": round(ret * 100 / max(1e-6, mdd * 100), 2),
            "switches": switches,
            "up_rate": round(ups / max(1, len(daily) - 1) * 100, 1),
            "nav": round(nav, 4)}


def slice_window(days, scores, window):
    """按窗口切（日, 评分）。"""
    if not window:
        return days, scores
    lo, hi = window["start"], window["end"]
    keep = [i for i, d in enumerate(days) if lo <= d["date"] <= hi]
    if not keep:
        return [], []
    return [days[i] for i in keep], (None if scores is None
                                     else [scores[i] for i in keep])


def regime_meta(windows):
    return {k: (None if not v else {"start": v["start"], "end": v["end"],
                                    "days": v["days"],
                                    "idx_ret": v["idx_ret"],
                                    "idx_mdd": v["idx_mdd"]})
            for k, v in (windows or {}).items()}


if __name__ == "__main__":
    parallel.use_utf8_stdout()  # 已是 UTF-8 就不重包（避免包装器被 GC 关掉底层 buffer）
    cfg = settings.load_config()
    days = load_days(cfg)
    print("样本交易日：%d（%s → %s）" % (len(days), days[0]["date"], days[-1]["date"]))
    have_news = sum(1 for d in days if d.get("news") is not None)
    have_micro = sum(1 for d in days if d.get("micro") is not None)
    print("有消息分天数：%d | 有情绪分天数：%d" % (have_news, have_micro))
    labels, reg = regimes(days)
    print("\n逐日状态占比：", {k: labels.count(k) for k in
                              ("bull", "bear", "range", "warmup")})
    chosen, cands = windows_by_outcome(days)
    for kind in ("bull", "bear", "range"):
        print("\n== %s 候选窗口（按典型度排序，前 6）==" % kind)
        for w in (cands.get(kind) or [])[:6]:
            print("  %s → %s  %4d 天  指数 %+7.2f%%  指数回撤 %5.2f%%" % (
                w["start"], w["end"], w["days"], w["idx_ret"] * 100,
                w["idx_mdd"] * 100))
    print("\n== 选定的三组独立留出期（互不重叠）==")
    for k in ("bull", "bear", "range"):
        w = chosen.get(k)
        if w:
            print("  %-6s %s → %s  %3d 天  指数 %+7.2f%%（指数回撤 %.2f%%）" % (
                k, w["start"], w["end"], w["days"], w["idx_ret"] * 100,
                w["idx_mdd"] * 100))
        else:
            print("  %-6s 无候选" % k)
    util.save_json(str(util.data_file("regime_windows.json")),
                   {"meta": regime_meta(chosen),
                    "candidates": {k: v[:20] for k, v in cands.items()},
                    "label_share": {k: labels.count(k) for k in
                                    ("bull", "bear", "range", "warmup")},
                    "days": len(days)})
    print("\n已存档 data/regime_windows.json")
