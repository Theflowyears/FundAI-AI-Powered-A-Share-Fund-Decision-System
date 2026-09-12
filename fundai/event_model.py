# -*- coding: utf-8 -*-
"""事件 + 行情 → 次日方向：SQLite 数据底座、walk-forward 样本外评估、选择性预测、策略回测。

数据底座：`data/cls_history.db`（SQLite，百万行级，支持 4 年跨牛熊回填与多进程并发写）。
行情：主指数（沪深300）日线 + 跨市场风格指数（创业板指/中证500，用于风格与制度特征）。

**“跑赢基线”的定义（本模块一律照此报告）**：
1) **方向精度**：命中率必须高于多数类基线（max(上涨日占比, 1−上涨日占比)）；
2) **策略收益**：按模型信号做多/空仓的净值，必须高于同期买入持有（buy & hold）；
3) 全部为 **walk-forward 样本外**：预测第 t 日只用 < t 的数据；阈值只在训练窗内定；
   报告再按**逐年**切分，避免“某一年运气”蒙过去。

特征（全部为当日收盘时可得）：
- 事件三组（宏观政策/公司业绩与股东/词典兜底）× 多空 × 条数/强度；
- 🔴重要电报条数、事件相对近 20 日的“意外度”（surprise）；
- 主指数制度状态：动量、波动、RSI、乖离、连涨跌、20 日区间位置、均线状态、
  波动率历史分位、距 20 日高低点天数；
- 跨市场：创业板/中证500 相对沪深300 的 5/20 日强弱（风格轮动）。
"""
import math

from . import calib, calib_history, clsdb, news, settings, util

MODEL_FILE = "event_model.json"
GROUP_MACRO = ["cbank_ease", "cbank_tight", "market_policy", "industry_policy",
               "eco_data", "geo_conflict"]
GROUP_COMPANY = ["earnings", "holder_flow"]
GROUP_DICT = ["dict"]
GROUPS = (("macro", GROUP_MACRO), ("comp", GROUP_COMPANY), ("dict", GROUP_DICT))

INDEX_FEATS = ["mom5", "mom10", "mom20", "mom60", "vol20", "volr", "rsi14",
               "bias20", "prev_ret", "streak", "pos20", "ma_state", "vol_rank",
               "days_since_hi", "days_since_lo", "dd60", "up_from_low60"]
CROSS_FEATS = ["rs_cyb5", "rs_cyb20", "rs_zz5", "rs_zz20"]
# 制度交互：均值回归 vs 趋势跟随的切换（2024 强趋势年里纯回归模型大幅落后，
# 这组特征让线性模型也能表达“趋势强时顺势、震荡时逆势”）
INTER_FEATS = ["trend_x_pos20", "trend_x_streak", "vol_x_streak", "ma_x_bias",
               "trend_x_rsi"]
SURPRISE_FEATS = ["surp_macro", "surp_comp", "surp_dict"]
LAG_FEATS = ["macro__net_w", "comp__net_w", "dict__net_w"]
BENCH = {"cyb": "0.399006", "zz": "1.000905"}
DEAD_BAND = 0.003          # 次日 |涨跌| < 0.3% 视为“无方向噪声”
FEE = 0.0005               # 每次调仓成本（诊断用，场外基金实际更高）


# ---------------- 数据：SQLite 打分 + 行情 ----------------
def ensure_scored(limit=None, progress=None):
    """库内电报的线上同口径打分（增量），返回全部已打分记录。"""
    pend = clsdb.unscored(limit=limit)
    if pend:
        news.score_db(limit=limit, progress=progress)
    return clsdb.scored_items()


def scored_items(use_cache=True, cfg=None):
    """兼容旧接口：返回 [{ctime,event,label,strength,important,scope,title}]。"""
    return ensure_scored()


def _index_feats(closes, dates, i):
    """第 i 日收盘后的主指数制度状态（全部已实现）。"""
    c = closes[dates[i]]

    def ret(k):
        if i < k or closes[dates[i - k]] <= 0:
            return 0.0
        return c / closes[dates[i - k]] - 1.0
    mom5, mom10, mom20 = ret(5), ret(10), ret(20)
    mom60 = ret(60)
    # 60 日回撤 / 自低点反弹幅度（崩盘-反弹制度里区分“下跌中继”和“反转”）
    dd60 = up_from_low60 = 0.0
    if i >= 60:
        win60 = [closes[dates[k]] for k in range(i - 59, i + 1)]
        hi60, lo60 = max(win60), min(win60)
        dd60 = c / hi60 - 1.0 if hi60 else 0.0
        up_from_low60 = c / lo60 - 1.0 if lo60 else 0.0
    if i >= 20:
        rs = [closes[dates[k]] / closes[dates[k - 1]] - 1.0
              for k in range(i - 19, i + 1)]
        m = sum(rs) / len(rs)
        vol20 = (sum((x - m) ** 2 for x in rs) / len(rs)) ** 0.5
        win = [closes[dates[k]] for k in range(i - 19, i + 1)]
        hi, lo = max(win), min(win)
        pos20 = (c - lo) / (hi - lo) if hi > lo else 0.5
        r5 = [abs(closes[dates[k]] / closes[dates[k - 1]] - 1.0)
              for k in range(i - 4, i + 1)]
        volr = (sum(r5) / 5.0) / vol20 if vol20 else 1.0
        ma20 = sum(win) / 20.0
    else:
        vol20 = volr = 0.0
        pos20, ma20 = 0.5, c
    ma60 = (sum(closes[dates[k]] for k in range(i - 59, i + 1)) / 60.0
            if i >= 59 else ma20)
    ma200 = (sum(closes[dates[k]] for k in range(i - 199, i + 1)) / 200.0
             if i >= 199 else ma60)
    ma_state = (1.0 if c > ma20 else -1.0) + (0.5 if c > ma60 else -0.5) + \
               (0.5 if c > ma200 else -0.5)
    # 均线多头排列：价格站上 20 日线且 20>60>200（牛市主升浪的典型形态）
    ma_align = 1.0 if (c > ma20 > ma60 > ma200) else 0.0
    # 波动率在过去 250 日中的分位（0~1）
    vol_rank = 0.5
    if i >= 60:
        hist = []
        for k in range(max(20, i - 249), i + 1):
            seg = [closes[dates[j]] / closes[dates[j - 1]] - 1.0
                   for j in range(k - 19, k + 1)]
            mm = sum(seg) / 20.0
            hist.append((sum((x - mm) ** 2 for x in seg) / 20.0) ** 0.5)
        if hist:
            vol_rank = sum(1 for v in hist if v <= vol20) / float(len(hist))
    d_hi = d_lo = 20
    if i >= 20:
        win = [closes[dates[k]] for k in range(i - 19, i + 1)]
        hi, lo = max(win), min(win)
        d_hi = min(k for k in range(20) if win[19 - k] == hi)
        d_lo = min(k for k in range(20) if win[19 - k] == lo)
    rsi = 50.0
    if i >= 15:
        gains = losses = 0.0
        for k in range(i - 13, i + 1):
            d = closes[dates[k]] - closes[dates[k - 1]]
            gains += max(d, 0.0)
            losses += max(-d, 0.0)
        rsi = 100.0 if losses <= 0 else 100.0 - 100.0 / (1 + gains / losses)
    bias20 = (c / ma20 - 1.0) if ma20 else 0.0
    prev = closes[dates[i]] / closes[dates[i - 1]] - 1.0 if i >= 1 else 0.0
    streak = 0
    k = i
    while k >= 1:
        r = closes[dates[k]] - closes[dates[k - 1]]
        if (r > 0) == (prev > 0) and r != 0:
            streak += 1 if r > 0 else -1
            k -= 1
        else:
            break
    return {"mom5": mom5, "mom10": mom10, "mom20": mom20, "mom60": mom60,
            "vol20": vol20, "volr": volr, "rsi14": rsi, "bias20": bias20,
            "prev_ret": prev, "streak": float(streak), "pos20": pos20,
            "ma_state": ma_state, "vol_rank": vol_rank,
            "days_since_hi": float(min(d_hi, 20)),
            "days_since_lo": float(min(d_lo, 20)),
            "dd60": dd60, "up_from_low60": up_from_low60,
            "ma_align": ma_align}


def _bench_closes(cfg=None, years=6):
    """跨市场风格指数收盘 {name: {date: close}}（读缓存，缺失时在线补）。"""
    from .datasource import Market
    try:
        mk = Market(cfg or settings.load_config())
    except Exception:
        return {}
    out = {}
    for name, secid in BENCH.items():
        try:
            bars = mk.index_bars(secid, years=years)
            out[name] = {d: c for d, c, _v in bars}
        except Exception:
            out[name] = {}
    return out


def build_dataset(days=1500, cfg=None, progress=None):
    """每日一行特征 + 次日涨跌（数据来自 SQLite 打分 + 行情缓存）。"""
    closes, dates = calib.load_closes(cfg=cfg, refresh=False)
    if not dates:
        raise util.DataError("没有主指数收盘缓存（先运行 fetch-history）")
    rows_scored = ensure_scored(progress=progress)
    per_day = {}
    for r in rows_scored:
        ct = r.get("ctime")
        if not ct:
            continue
        d = calib_history.trade_date_of(ct, dates)
        if d:
            per_day.setdefault(d, []).append(r)
    bench = _bench_closes(cfg)
    start = max(0, len(dates) - int(days))
    rows, prev_f, hist_net = [], None, {k: [] for k, _ in GROUPS}
    for i in range(start, len(dates)):
        if i + 1 >= len(dates):
            break
        d = dates[i]
        recs = per_day.get(d) or []
        if not recs:
            continue
        f = {}
        for gname, events in GROUPS:
            for lab, tag in (("bull", "b"), ("bear", "s")):
                sel = [r for r in recs if r["event"] in events and r["label"] == lab]
                f["{}__{}_n".format(gname, tag)] = math.log1p(len(sel))
                f["{}__{}_w".format(gname, tag)] = math.log1p(
                    sum(abs(int(r.get("strength") or 0)) for r in sel))
            net_w = (f["{}__b_w".format(gname)] - f["{}__s_w".format(gname)])
            f["{}__net_w".format(gname)] = net_w
            hist = hist_net[gname]
            base = (sum(hist[-20:]) / len(hist[-20:])) if hist else 0.0
            f["surp_{}".format(gname)] = net_w - base
            hist.append(net_w)
        imp = [r for r in recs if r.get("important")]
        f["imp_bull"] = math.log1p(sum(1 for r in imp if r["label"] == "bull"))
        f["imp_bear"] = math.log1p(sum(1 for r in imp if r["label"] == "bear"))
        f.update(_index_feats(closes, dates, i))
        # 跨市场风格强弱
        for name, cols in bench.items():
            def _r(k):
                if i < k or d not in cols or dates[i - k] not in cols:
                    return 0.0
                return cols[d] / cols[dates[i - k]] - 1.0
            f["rs_{}5".format("cyb" if name == "cyb" else "zz")] = \
                _r(5) - (closes[d] / closes[dates[i - 5]] - 1.0 if i >= 5 else 0.0)
            f["rs_{}20".format("cyb" if name == "cyb" else "zz")] = \
                _r(20) - (closes[d] / closes[dates[i - 20]] - 1.0 if i >= 20 else 0.0)
        if prev_f:
            for k in LAG_FEATS:
                f[k + "_lag1"] = 0.5 * float(prev_f.get(k) or 0.0)
        # 制度交互项
        tsign = 1.0 if f["mom20"] > 0 else -1.0
        f["trend_x_pos20"] = tsign * (f["pos20"] - 0.5) * 2.0
        f["trend_x_streak"] = tsign * max(-5.0, min(5.0, f["streak"])) / 5.0
        f["vol_x_streak"] = f["vol_rank"] * max(-5.0, min(5.0, f["streak"])) / 5.0
        f["ma_x_bias"] = f["ma_state"] * max(-0.1, min(0.1, f["bias20"])) * 10.0
        f["trend_x_rsi"] = tsign * (f["rsi14"] - 50.0) / 50.0
        nxt = closes[dates[i + 1]] / closes[dates[i]] - 1.0
        r3 = None
        if i + 3 < len(dates):
            r3 = closes[dates[i + 3]] / closes[dates[i]] - 1.0
        rows.append({"date": d, "next": dates[i + 1], "ret": nxt,
                     "ret3": r3, "y": 1 if nxt > 0 else 0,
                     "y3": (None if r3 is None else (1 if r3 > 0 else 0)),
                     "n_items": len(recs), "f": f})
        prev_f = f
    return {"rows": rows, "dates": dates, "closes": closes,
            "features": sorted(rows[0]["f"].keys()) if rows else []}


# ---------------- 模型 ----------------
try:                      # numpy 可用时向量化（4 年数据的 walk-forward 快 100 倍）
    import numpy as _np
except Exception:         # pragma: no cover
    _np = None


def _sigmoid(z):
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def fit_logistic(X, y, l2=2.0, iters=200, lr=0.6, weights=None):
    """标准化 + L2 逻辑回归（可选样本权重）。返回 (mean, std, w, b)。"""
    if _np is not None and len(X) > 0:
        A = _np.asarray(X, dtype=float)
        yy = _np.asarray(y, dtype=float)
        wts = (_np.ones(len(A)) if weights is None
               else _np.asarray(weights, dtype=float))
        tot = float(wts.sum()) or 1.0
        mean = (A * wts[:, None]).sum(0) / tot
        var = (((A - mean) ** 2) * wts[:, None]).sum(0) / tot
        std = _np.sqrt(_np.maximum(var, 1e-12))
        Z = (A - mean) / std
        w = _np.zeros(A.shape[1])
        b = 0.0
        for _ in range(iters):
            p = 1.0 / (1.0 + _np.exp(-_np.clip(Z @ w + b, -30, 30)))
            e = (p - yy) * wts
            w -= lr * ((Z.T @ e) / tot + l2 * w / tot)
            b -= lr * float(e.sum()) / tot
        return mean.tolist(), std.tolist(), w.tolist(), float(b)
    n, d = len(X), len(X[0])
    wts = list(weights) if weights else [1.0] * n
    tot = sum(wts) or 1.0
    mean = [sum(X[i][j] * wts[i] for i in range(n)) / tot for j in range(d)]
    std = []
    for j in range(d):
        v = sum(((X[i][j] - mean[j]) ** 2) * wts[i] for i in range(n)) / tot
        std.append(v ** 0.5 if v > 1e-12 else 1.0)
    Z = [[(X[i][j] - mean[j]) / std[j] for j in range(d)] for i in range(n)]
    w = [0.0] * d
    b = 0.0
    for _ in range(iters):
        gw = [0.0] * d
        gb = 0.0
        for i in range(n):
            p = _sigmoid(b + sum(w[j] * Z[i][j] for j in range(d)))
            e = (p - y[i]) * wts[i]
            for j in range(d):
                gw[j] += e * Z[i][j]
            gb += e
        for j in range(d):
            w[j] -= lr * (gw[j] / tot + l2 * w[j] / tot)
        b -= lr * gb / tot
    return mean, std, w, b


def _predict(mean, std, w, b, x):
    if _np is not None:
        A = _np.asarray(x, dtype=float)
        z = b + float(((A - _np.asarray(mean)) / _np.asarray(std)) @ _np.asarray(w))
        return float(1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z)))))
    return _sigmoid(b + sum(w[j] * ((x[j] - mean[j]) / std[j])
                            for j in range(len(w))))


def _predict_batch(mean, std, w, b, X):
    if _np is not None and len(X) > 0:
        A = _np.asarray(X, dtype=float)
        z = ((A - _np.asarray(mean)) / _np.asarray(std)) @ _np.asarray(w) + b
        z = _np.clip(z, -30, 30)
        return (1.0 / (1.0 + _np.exp(-z))).tolist()
    return [_predict(mean, std, w, b, x) for x in X]


def _decay_weights(n, half_life=250):
    """近端样本权重更高（几十年数据里“久远制度”参考价值下降）。"""
    if not half_life:
        return None
    return [0.5 ** ((n - 1 - i) / float(half_life)) for i in range(n)]


def walk_forward(ds, warmup=60, refit_every=5, l2=2.0, iters=200,
                 half_life=0):
    rows, feats = ds["rows"], ds["features"]
    X = [[float(r["f"].get(k) or 0.0) for k in feats] for r in rows]
    y = [int(r["y"]) for r in rows]
    preds, params = [], None
    for t in range(len(rows)):
        if t < warmup:
            preds.append(None)
            continue
        if params is None or (t - warmup) % max(1, refit_every) == 0:
            wts = _decay_weights(t, half_life) if half_life else None
            params = fit_logistic(X[:t], y[:t], l2=l2, iters=iters, weights=wts)
        preds.append(_predict(params[0], params[1], params[2], params[3], X[t]))
    return preds


def policy_walk_forward(ds, warmup=60, refit_every=5, l2=2.0,
                        targets=(0.2, 0.3, 0.5), band=DEAD_BAND, min_hist=40,
                        movers_only=False, half_life=0):
    """选择性出手：阈值只由训练窗的 |p−0.5| 分布确定，样本外执行。"""
    rows, feats = ds["rows"], ds["features"]
    X = [[float(r["f"].get(k) or 0.0) for k in feats] for r in rows]
    y = [int(r["y"]) for r in rows]
    params, in_preds = None, []
    stat = {q: {"taken": 0, "movers": 0, "hits": 0} for q in targets}
    daily = []
    keep = [k for k in range(len(rows))
            if (not movers_only) or abs(rows[k]["ret"]) >= band]
    for t in range(len(rows)):
        if t < warmup:
            continue
        if params is None or (t - warmup) % max(1, refit_every) == 0:
            tr = [k for k in keep if k < t]
            if len(tr) < 20:
                tr = list(range(t))
            wts = _decay_weights(len(tr), half_life) if half_life else None
            mean, std, w, b = fit_logistic([X[k] for k in tr], [y[k] for k in tr],
                                           l2=l2, weights=wts)
            params = (mean, std, w, b)
            in_preds = _predict_batch(mean, std, w, b, X[:t])
        p = _predict(params[0], params[1], params[2], params[3], X[t])
        edges = sorted(abs(x - 0.5) for x in in_preds[-120:])
        rec = {"date": rows[t]["date"], "p": round(p, 4),
               "ret": round(rows[t]["ret"], 6), "taken": []}
        for q in targets:
            if len(edges) < min_hist:
                continue
            k = max(0, min(len(edges) - 1, int(len(edges) * (1.0 - q))))
            if abs(p - 0.5) < edges[k]:
                continue
            s = stat[q]
            s["taken"] += 1
            rec["taken"].append(q)
            if abs(rows[t]["ret"]) >= band:
                s["movers"] += 1
                if (rows[t]["ret"] > 0) == (p >= 0.5):
                    s["hits"] += 1
        daily.append(rec)
    n_days = max(1, len(daily))
    out = {}
    for q, s in stat.items():
        out[str(q)] = {"target": q, "taken": s["taken"], "movers": s["movers"],
                       "hits": s["hits"],
                       "acc": (round(s["hits"] / s["movers"], 4) if s["movers"] else None),
                       "coverage": round(s["taken"] / n_days, 4)}
    return {"targets": out, "daily": daily[-20:], "daily_all": daily,
            "n_oos_days": len(daily)}


# ---------------- 指标 / 策略 ----------------
def metrics(rows, preds, band=DEAD_BAND, min_edge=0.02):
    obs = [(rows[i], preds[i]) for i in range(len(rows)) if preds[i] is not None]
    n_all = len(obs)
    if not n_all:
        return {"n": 0}
    up = sum(1 for r, _ in obs if r["ret"] > 0) / n_all
    base_major = max(up, 1 - up)

    def acc(sel):
        mv = [(r, p) for r, p in sel if abs(r["ret"]) >= band]
        if not mv:
            return None, len(sel), 0
        hit = sum(1 for r, p in mv if (r["ret"] > 0) == (p >= 0.5))
        return hit / len(mv), len(sel), len(mv)
    taken = [(r, p) for r, p in obs if abs(p - 0.5) >= min_edge]
    overall, n_take, n_mov = acc(taken)
    brier = sum((p - r["y"]) ** 2 for r, p in obs) / n_all
    big = [(r, p) for r, p in obs if abs(r["ret"]) >= 0.005]
    a_big, _, n_big = acc(big)
    ranks = sorted(obs, key=lambda t: -abs(t[1] - 0.5))
    cover = []
    for frac in (0.1, 0.2, 0.3, 0.5):
        k = max(1, int(len(ranks) * frac))
        a, n, nm = acc(ranks[:k])
        cover.append({"top": "{:.0f}%".format(frac * 100), "n": n, "movers": nm,
                      "acc": (None if a is None else round(a, 4))})
    return {"n": n_all, "up_rate": round(up, 4),
            "baseline_majority": round(base_major, 4),
            "taken": n_take, "movers": n_mov,
            "acc": (None if overall is None else round(overall, 4)),
            "coverage": round(n_take / n_all, 4),
            "acc_big_move": (None if a_big is None else round(a_big, 4)),
            "n_big_move": n_big, "brier": round(brier, 4),
            "edge_vs_baseline": (None if overall is None
                                 else round(overall - base_major, 4)),
            "topk": cover}


def backtest(rows, preds, edge=0.0, cost=FEE, allow_short=False,
             band=DEAD_BAND):
    """按模型信号做多/空仓（可选做空）的净值，对照买入持有。

    edge：|p−0.5| 门槛，超过才出手；cost：每次调仓成本。
    """
    obs = [(rows[i], preds[i]) for i in range(len(rows)) if preds[i] is not None]
    if not obs:
        return {}
    nav = bh = 1.0
    pos = 0
    switches = 0
    for r, p in obs:
        tgt = 1 if p >= 0.5 + edge else (-1 if (allow_short and p <= 0.5 - edge) else 0)
        if tgt != pos:
            switches += 1
            nav *= (1.0 - cost)
            pos = tgt
        nav *= (1.0 + pos * r["ret"])
        bh *= (1.0 + r["ret"])
    navs, peaks, mdd = [], 1.0, 0.0
    n2 = 1.0
    for r, p in obs:
        tgt = 1 if p >= 0.5 + edge else (-1 if (allow_short and p <= 0.5 - edge) else 0)
        n2 *= (1.0 + tgt * r["ret"])
        navs.append(n2)
        peaks = max(peaks, n2)
        mdd = max(mdd, 1.0 - n2 / peaks)
    rets = [r["ret"] for r, _ in obs]
    m = sum(rets) / len(rets)
    sd = (sum((x - m) ** 2 for x in rets) / len(rets)) ** 0.5 or 1e-9
    return {"days": len(obs), "nav": round(nav, 4), "buy_hold": round(bh, 4),
            "excess": round(nav - bh, 4), "switches": switches,
            "max_drawdown": round(mdd, 4),
            "bh_ann_vol": round(sd * (252 ** 0.5), 4),
            "ann_ret": round(nav ** (252.0 / max(1, len(obs))) - 1.0, 4),
            "bh_ann_ret": round(bh ** (252.0 / max(1, len(obs))) - 1.0, 4),
            "win_vs_bh": nav > bh}


def per_year(rows, preds, band=DEAD_BAND):
    """逐年样本外成绩（命中率 vs 该年基线；策略 vs 买入持有）。"""
    idx = {}
    for i, r in enumerate(rows):
        if preds[i] is None:
            continue
        idx.setdefault(r["date"][:4], []).append(i)
    out = []
    for y in sorted(idx):
        ids = idx[y]
        sub_rows = [rows[i] for i in ids]
        sub_preds = [preds[i] for i in ids]
        met = metrics(sub_rows, sub_preds, band=band)
        bt = backtest(sub_rows, sub_preds, edge=0.0)
        out.append({"year": y, "days": met.get("n", 0),
                    "acc": met.get("acc"), "baseline": met.get("baseline_majority"),
                    "edge": met.get("edge_vs_baseline"),
                    "up_rate": met.get("up_rate"),
                    "bt_nav": bt.get("nav"), "bt_bh": bt.get("buy_hold"),
                    "win": bt.get("win_vs_bh")})
    return out


FEATURE_SETS = {
    "all": None,
    "market": lambda k: k in INDEX_FEATS or k in CROSS_FEATS or k in INTER_FEATS,
    "market_plain": lambda k: k in INDEX_FEATS or k in CROSS_FEATS,
    "market_events": lambda k: (k in INDEX_FEATS or k in CROSS_FEATS
                                or k in INTER_FEATS
                                or k.startswith(("macro__", "comp__", "imp_",
                                                 "surp_macro", "surp_comp"))),
    "no_dict": lambda k: (not k.startswith("dict__")) and k != "surp_dict",
    "events": lambda k: k.startswith(("macro__", "comp__", "imp_", "surp_macro",
                                      "surp_comp")),
}


def feature_keys(feats, name="all"):
    """按特征组名取列（默认 all）。"""
    pred = FEATURE_SETS.get(name or "all")
    return list(feats) if pred is None else [k for k in feats if pred(k)]


def feature_variants(ds, warmup=60, band=DEAD_BAND, l2=2.0, refit_every=5,
                     targets=(0.2, 0.3, 0.5)):
    rows, feats = ds["rows"], ds["features"]
    out = {}
    for name in FEATURE_SETS:
        keys = feature_keys(feats, name)
        if not keys:
            continue
        sub = {"rows": rows, "features": keys, "dates": ds["dates"],
               "closes": ds["closes"]}
        pol = policy_walk_forward(sub, warmup=warmup, band=band, l2=l2,
                                  refit_every=refit_every, targets=targets)
        out[name] = pol["targets"]
    return out


def select_and_holdout(ds, warmup=60, band=DEAD_BAND, grid=None, holdout=0.4):
    """配置选择期 / 最终留出期分离：**特征组、正则、重训频率、样本半衰期**都在
    选择期（前 60%）里挑，后 40% 只用于报告——避免“在同一份数据上试来试去”的自欺。"""
    grid = grid or [
        {"l2": 1.0, "refit_every": 5, "half_life": 0, "feature_set": "market"},
        {"l2": 2.0, "refit_every": 5, "half_life": 0, "feature_set": "market"},
        {"l2": 5.0, "refit_every": 5, "half_life": 0, "feature_set": "market"},
        {"l2": 2.0, "refit_every": 5, "half_life": 250, "feature_set": "market"},
        {"l2": 2.0, "refit_every": 10, "half_life": 0, "feature_set": "market"},
        {"l2": 2.0, "refit_every": 5, "half_life": 0,
         "feature_set": "market_plain"},
        {"l2": 2.0, "refit_every": 5, "half_life": 0,
         "feature_set": "market_events"},
        {"l2": 2.0, "refit_every": 5, "half_life": 0, "feature_set": "no_dict"},
        {"l2": 2.0, "refit_every": 5, "half_life": 0, "feature_set": "all"},
    ]
    rows = ds["rows"]
    n = len(rows)
    cut = int(n * (1.0 - holdout))
    if cut < warmup + 20 or n - cut < 20:
        return {"ok": False, "message": "样本不足（需 ≥ {} 个交易日）".format(warmup + 40)}
    results = []
    for g in grid:
        keys = feature_keys(ds["features"], g.get("feature_set", "market"))
        sub = {"rows": rows[:cut], "features": keys, "dates": ds["dates"],
               "closes": ds["closes"]}
        pol = policy_walk_forward(sub, warmup=warmup, band=band,
                                  refit_every=g.get("refit_every", 5),
                                  l2=g.get("l2", 2.0),
                                  half_life=g.get("half_life", 0))
        results.append({"cfg": g, "policy": pol["targets"]})
    best = None
    for r in results:
        sc = r["policy"].get("0.3") or {}
        if sc.get("movers", 0) >= 20 and sc.get("acc") is not None:
            if best is None or sc["acc"] > best[1]:
                best = (r["cfg"], sc["acc"])
    cfg_best = best[0] if best else grid[0]
    keys_best = feature_keys(ds["features"], cfg_best.get("feature_set", "market"))
    full = walk_forward({"rows": rows, "features": keys_best},
                        warmup=warmup, refit_every=cfg_best.get("refit_every", 5),
                        l2=cfg_best.get("l2", 2.0),
                        half_life=cfg_best.get("half_life", 0))
    ho_rows = rows[cut:]
    ho_preds = full[cut:]
    met = metrics(ho_rows, ho_preds, band=band)
    bt = backtest(ho_rows, ho_preds, edge=0.0)
    return {"ok": True, "selected": cfg_best,
            "select_period": {"from": rows[0]["date"], "to": rows[cut - 1]["date"],
                              "days": cut, "candidates": results},
            "holdout_period": {"from": rows[cut]["date"], "to": rows[-1]["date"],
                               "days": n - cut},
            "holdout": {"acc": met.get("acc"), "baseline": met.get("baseline_majority"),
                        "edge": met.get("edge_vs_baseline"), "n": met.get("n"),
                        "movers": met.get("movers"), "up_rate": met.get("up_rate"),
                        "bt_nav": bt.get("nav"), "bt_bh": bt.get("buy_hold"),
                        "win": bt.get("win_vs_bh")},
            "holdout_years": per_year(ho_rows, ho_preds, band=band)}


def _top_weights(ds, l2=2.0, topn=14):
    rows, feats = ds["rows"], ds["features"]
    if len(rows) < 30:
        return []
    X = [[float(r["f"].get(k) or 0.0) for k in feats] for r in rows]
    y = [int(r["y"]) for r in rows]
    mean, std, w, b = fit_logistic(X, y, l2=l2, weights=_decay_weights(len(rows), 500))
    pairs = sorted(zip(feats, w), key=lambda kv: -abs(kv[1]))[:topn]
    return [{"feature": k, "coef": round(v, 3)} for k, v in pairs]


def run(days=1600, warmup=60, band=DEAD_BAND, min_edge=0.02, cfg=None,
        refit_every=5, l2=2.0, half_life=0, save=True, select=True,
        feature_set="market"):
    ds = build_dataset(days=days, cfg=cfg)
    keys = feature_keys(ds["features"], feature_set)
    ds = {"rows": ds["rows"], "features": keys, "dates": ds["dates"],
          "closes": ds["closes"]}
    rows, feats = ds["rows"], ds["features"]
    if len(rows) < warmup + 30:
        raise util.DataError("样本不足（{} 天）：先抓更多历史或降低 --days".format(len(rows)))
    preds = walk_forward(ds, warmup=warmup, refit_every=refit_every, l2=l2,
                         half_life=half_life)
    met = metrics(rows, preds, band=band, min_edge=min_edge)
    pol = policy_walk_forward(ds, warmup=warmup, refit_every=refit_every,
                              l2=l2, band=band, half_life=half_life)
    pol_mv = policy_walk_forward(ds, warmup=warmup, refit_every=refit_every,
                                 l2=l2, band=band, movers_only=True,
                                 half_life=half_life)
    bt = backtest(rows, preds, edge=0.0)
    bt_t = backtest(rows, preds, edge=0.05)
    latest = None
    if len(rows) >= warmup + 20:
        X = [[float(r["f"].get(k) or 0.0) for k in feats] for r in rows]
        y = [int(r["y"]) for r in rows]
        p = fit_logistic(X[:-1], y[:-1], l2=l2,
                         weights=_decay_weights(len(rows) - 1, 500))
        latest = {"date": rows[-1]["date"], "next": rows[-1]["next"],
                  "p_up": round(_predict(p[0], p[1], p[2], p[3], X[-1]), 4),
                  "actual_next_ret": round(rows[-1]["ret"], 6)}
    rep = {
        "generated_at": util.now_iso(),
        "window": {"from": rows[0]["date"], "to": rows[-1]["date"],
                   "days": len(rows)},
        "settings": {"warmup": warmup, "refit_every": refit_every, "l2": l2,
                     "half_life": half_life, "dead_band": band,
                     "min_edge": min_edge, "features": len(feats),
                     "feature_set": feature_set},
        "metrics": met, "policy": pol, "policy_movers_only": pol_mv,
        "backtest": bt, "backtest_thresh": bt_t,
        "per_year": per_year(rows, preds, band=band),
        "variants": feature_variants(ds, warmup=warmup, band=band, l2=l2,
                                     refit_every=refit_every),
        "selection": (select_and_holdout(ds, warmup=warmup, band=band)
                      if select else None),
        "latest": latest,
        "weights": _top_weights(ds, l2=l2),
        "notes": [
            "全部指标为 walk-forward 样本外：预测第 t 日只用 < t 的数据，阈值只在训练窗内定",
            "跑赢基线 = 命中率 > 多数类基线，且策略净值 > 同期买入持有（两条都看）",
            "命中口径：次日 |涨跌| ≥ {:.1%} 的样本里方向判对".format(band),
            "70% 全样本命中率在近有效市场不可得；本模块报告的是可复现的样本外水平",
        ],
    }
    if save:
        util.save_json(util.data_file(MODEL_FILE), rep)
    return rep


def text_report(rep):
    if not rep or not rep.get("metrics"):
        return "（无结果）"
    m, bt = rep["metrics"], rep.get("backtest") or {}
    lines = ["窗口 {} → {}（{} 个交易日，样本外 {} 天）".format(
        rep["window"]["from"], rep["window"]["to"], rep["window"]["days"], m.get("n")),
        "基线：上涨日占比 {:.1%}｜多数类 {:.1%}｜Brier {}".format(
            m.get("up_rate") or 0, m.get("baseline_majority") or 0, m.get("brier")),
        "整体：出手 {} 天（覆盖 {:.0%}）命中 {}（增量 {}）｜波动≥0.5% 的日子 {}".format(
            m.get("taken"), m.get("coverage") or 0,
            ("{:.1%}".format(m["acc"]) if m.get("acc") is not None else "—"),
            ("{:+.1%}".format(m["edge_vs_baseline"])
             if m.get("edge_vs_baseline") is not None else "—"),
            ("{:.1%}".format(m["acc_big_move"])
             if m.get("acc_big_move") is not None else "—")),
        "策略（按信号做多/空仓，费 {}bp）：净值 {} vs 买入持有 {}（{}{}）".format(
            int(FEE * 10000), bt.get("nav"), bt.get("buy_hold"),
            "跑赢" if bt.get("win_vs_bh") else "跑输",
            "，超额 {:+.1%}".format(bt.get("excess")) if bt.get("excess") is not None else ""),
    ]
    lines.append("")
    lines.append("逐年样本外（命中率 vs 该年基线｜策略 vs 买入持有）：")
    lines.append("  {:<6} {:>5} {:>8} {:>9} {:>9} {:>10} {:>10}".format(
        "年份", "天数", "命中率", "基线", "增量", "策略净值", "买入持有"))
    for y in rep.get("per_year") or []:
        lines.append("  {:<6} {:>5} {:>8} {:>9} {:>9} {:>10} {:>10}".format(
            y["year"], y["days"],
            ("{:.1%}".format(y["acc"]) if y.get("acc") is not None else "—"),
            ("{:.1%}".format(y["baseline"]) if y.get("baseline") is not None else "—"),
            ("{:+.1%}".format(y["edge"]) if y.get("edge") is not None else "—"),
            ("{:.3f}".format(y["bt_nav"]) if y.get("bt_nav") is not None else "—"),
            ("{:.3f}".format(y["bt_bh"]) if y.get("bt_bh") is not None else "—")))
    lines.append("")
    lines.append("选择性出手（阈值训练窗内定）：")
    lines.append("  {:<8} {:>5} {:>8} {:>9} {:>9}".format(
        "覆盖目标", "出手", "含波动日", "命中", "命中率"))
    for key in sorted((rep.get("policy") or {}).get("targets") or {},
                      key=lambda k: float(k)):
        c = rep["policy"]["targets"][key]
        lines.append("  {:<8} {:>5} {:>8} {:>9} {:>9}".format(
            "{:.0%}".format(c["target"]), c["taken"], c["movers"], c["hits"],
            ("{:.1%}".format(c["acc"]) if c["acc"] is not None else "—")))
    mv = (rep.get("policy_movers_only") or {}).get("targets") or {}
    if mv:
        lines.append("  （只在有效波动日训练）")
        for key in sorted(mv, key=lambda k: float(k)):
            c = mv[key]
            lines.append("  {:<8} {:>5} {:>8} {:>9} {:>9}".format(
                "{:.0%}".format(c["target"]), c["taken"], c["movers"], c["hits"],
                ("{:.1%}".format(c["acc"]) if c["acc"] is not None else "—")))
    sel = rep.get("selection")
    if sel and sel.get("ok"):
        h = sel["holdout"]
        lines.append("")
        lines.append("选择期挑中配置：feature_set={} l2={} refit={} half_life={}".format(
            sel["selected"].get("feature_set"), sel["selected"].get("l2"),
            sel["selected"].get("refit_every"), sel["selected"].get("half_life")))
        lines.append("留出期（{} → {}，{} 天，仅用于报告）：命中 {} vs 基线 {}（增量 {}）"
                     "｜策略 {} vs 买入持有 {}（{}）".format(
                         sel["holdout_period"]["from"], sel["holdout_period"]["to"],
                         sel["holdout_period"]["days"],
                         ("{:.1%}".format(h["acc"]) if h.get("acc") is not None else "—"),
                         ("{:.1%}".format(h["baseline"]) if h.get("baseline") is not None else "—"),
                         ("{:+.1%}".format(h["edge"]) if h.get("edge") is not None else "—"),
                         h.get("bt_nav"), h.get("bt_bh"),
                         "跑赢" if h.get("win") else "跑输"))
    var = rep.get("variants") or {}
    if var:
        lines.append("")
        lines.append("特征子集对照（覆盖30%档命中率/含波动日）：")
        for name, tg in var.items():
            c = (tg or {}).get("0.3") or {}
            lines.append("  {:<16} {}（{} 天）".format(
                name,
                ("{:.1%}".format(c["acc"]) if c.get("acc") is not None else "—"),
                c.get("movers", 0)))
    if rep.get("latest"):
        lt = rep["latest"]
        lines.append("")
        lines.append("最近一日样本外预测：{} → {} 上涨概率 {:.1%}（实际次日 {:+.2%}）".format(
            lt["date"], lt["next"], lt["p_up"], lt["actual_next_ret"] or 0.0))
    return "\n".join(lines)
