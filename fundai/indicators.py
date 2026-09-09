# -*- coding: utf-8 -*-
"""技术指标（纯函数，仅依赖标准库 math）。"""


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
