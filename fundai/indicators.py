# -*- coding: utf-8 -*-
"""技术指标（纯函数，仅依赖标准库 math）。"""


def ma_align_state(closes):
    """均线多头排列：收盘 > MA20 > MA60 > MA200 → 1，否则 0（数据不足返回 None）。"""
    n = len(closes)
    if n < 200:
        return None
    c = closes[-1]
    ma20 = sum(closes[-20:]) / 20.0
    ma60 = sum(closes[-60:]) / 60.0
    ma200 = sum(closes[-200:]) / 200.0
    return 1.0 if (c > ma20 > ma60 > ma200) else 0.0


def vol_rank(closes, window=250, lookback=20):
    """当前 20 日波动率在过去 window 日中的分位（0~1）。数据不足返回 None。

    供动态权重与风险过滤判断“是否处在极端波动”使用（有界、可复现、不依赖外部库）。
    """
    n = len(closes)
    if n < lookback + 10:
        return None

    def vol_at(end):
        seg = [closes[i] / closes[i - 1] - 1.0
               for i in range(end - lookback + 1, end + 1)]
        m = sum(seg) / len(seg)
        return (sum((x - m) ** 2 for x in seg) / len(seg)) ** 0.5
    cur = vol_at(n - 1)
    hist = []
    for end in range(max(lookback, n - window), n):
        try:
            hist.append(vol_at(end))
        except (IndexError, ZeroDivisionError):
            continue
    if not hist:
        return None
    return sum(1 for v in hist if v <= cur) / float(len(hist))


def ma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def rsi(closes, n=14):
    """Wilder RSI。"""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_g = gains / n
    avg_l = losses / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def last_stats(dates, closes, vols):
    """取最后一个交易日的各项指标。

    dates/closes/vols 为升序列表。返回 dict（无值字段为 None）。
    """
    closes = [float(x) for x in closes]
    vols = [float(x) for x in vols]
    n = len(closes)
    out = {
        "date": dates[-1] if dates else None,
        "n": n,
        "close": closes[-1] if n else None,
        "chg_pct": None,
        "ma5": ma(closes, 5),
        "ma10": ma(closes, 10),
        "ma20": ma(closes, 20),
        "ma60": ma(closes, 60),
        "rsi14": rsi(closes, 14),
        "mom5": None,
        "mom20": None,
        "vol_ratio": None,
        "vol": vols[-1] if vols else None,
    }
    if n >= 2:
        prev = closes[-2]
        if prev:
            out["chg_pct"] = closes[-1] / prev - 1.0
    if n >= 6 and closes[-6]:
        out["mom5"] = closes[-1] / closes[-6] - 1.0
    if n >= 21 and closes[-21]:
        out["mom20"] = closes[-1] / closes[-21] - 1.0
    if n >= 20:
        v20 = sum(vols[-20:]) / 20
        if v20:
            out["vol_ratio"] = vols[-1] / v20
    return out
