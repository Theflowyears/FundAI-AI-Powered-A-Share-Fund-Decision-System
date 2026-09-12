# -*- coding: utf-8 -*-
"""事件命中率校准（历史回填版）：财联社历史电报 → 次日涨跌命中率。

与 `calib.py`（从今天起逐日积累样本）互补：本模块**一次性回填历史**，
把过去 N 天的电报用与线上完全相同的口径重新打分，立刻得到足够样本量。

数据通道（2026-09 实测，详见 README「四、数据底座」）：
- `https://www.cls.cn/nodeapi/telegraphList` **已 404 下线**（该 node 接口不再提供）；
- 可用通道仍是本项目一直在用的 `/v1/roll/get_roll_list`（签名 md5(sha1(排序参数串))）：
  `rn` 上限 50，带 `refresh_type=1` 时 `last_time` 是**时间游标**，可跳到任意历史日期
  并逐页往回翻（实测 1 / 30 / 90 / 365 天前均可取）。

校准方法：
- 打分口径与线上**完全一致**（`news.score_item`：词典情绪 + 事件语义推断 + 🔴重要度加权）；
- 电报归属交易日：收盘 15:00 之后的电报算作**次日**可得信息（否则会用到当天收盘
  之后才出现的信息，属未来函数）；
- 目标：次一交易日沪深300涨跌方向；
- 输出分事件类型的样本数 / 命中率 / 显著性（正态近似 p）与**收缩后的建议权重**
  （`strategy.event_calibration=true` 时才生效，默认只报告不自动改分）。
"""
import math
from datetime import datetime

from . import news, settings, util

CAL_FILE = "event_calibration.json"
MIN_SAMPLES = 5      # 少于此样本数不给建议权重
SHRINK_K = 20.0      # 收缩强度：mult = 1 + (raw-1) * n/(n+K)
CLOSE_HOUR = 15      # 收盘后电报算次日

EVENT_CN = {
    "cbank_ease": "央行宽松", "cbank_tight": "央行收紧",
    "market_policy": "市场政策", "industry_policy": "产业政策",
    "eco_data": "经济数据", "earnings": "业绩", "holder_flow": "股东增减持",
    "geo_conflict": "地缘冲突", "fund_premium_warning": "产品风险提示",
    "holdings_disclosure": "权益披露", "dict": "词典兜底", "none": "其他",
}


def load_calibration():
    return util.load_json(util.data_file(CAL_FILE), {}) or {}


def save_calibration(cal):
    util.save_json(util.data_file(CAL_FILE), cal)
    return cal


def _p_two_sided(z):
    """双侧 p 值（正态近似），不引入 scipy。"""
    return math.erfc(abs(z) / math.sqrt(2.0))


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    return cov / math.sqrt(vx * vy)


def trade_date_of(ctime, tdays):
    """电报时间 → 归属交易日（15:00 后算次日可得信息，规避未来函数）。"""
    dt = datetime.fromtimestamp(int(ctime), util.TZ_CN)
    d = dt.strftime("%Y-%m-%d")
    after_close = dt.hour >= CLOSE_HOUR
    for cand in tdays:
        if after_close:
            if cand > d:
                return cand
        elif cand >= d:
            return cand
    return None


def fetch_only(days=365, max_pages=4000, delay=0.3, progress=None,
               use_cache=True, until=None, cache_name=None):
    """只抓历史电报入缓存（不跑校准）——适合长时间大批量回填/分段并行。"""
    return news.cls_history(days=days, max_pages=max_pages, delay=delay,
                            progress=progress, use_cache=use_cache,
                            force=True, until=until, cache_name=cache_name)


def calibrate(days=10, max_pages=600, delay=0.3, amplitude=8, cfg=None,
              min_samples=MIN_SAMPLES, progress=None, use_cache=True,
              source="db"):
    """历史电报 → 同口径打分 → 按日聚合 → 对照次日涨跌统计命中率。

    source="db"（默认）：直接读 `data/cls_history.db` 的**已打分**记录（`news.score_db`
    用的是与线上完全相同的 `score_item`），因此支持十年量级、零网络、秒级完成；
    source="fetch"：走 JSON 缓存/在线翻页（早期实现，保留备查）。
    返回报告 dict（同时写入 data/event_calibration.json）。
    """
    cfg = cfg or settings.load_config()
    amp = int(amplitude)
    st_cfg = cfg.get("strategy") or {}
    net_mode = str(st_cfg.get("news_net_mode") or "scaled")
    net_scale = float(st_cfg.get("news_net_scale") or 240.0)
    if source == "fetch":
        hist = news.cls_history(days=days, max_pages=max_pages, delay=delay,
                                progress=progress, use_cache=use_cache)
        raw_items = [it for it in (hist.get("items") or []) if it.get("ctime")]
    else:
        from . import clsdb
        since = int(__import__("time").time()) - int(days) * 86400
        rows = clsdb.scored_items(since=since)
        raw_items = [{"ctime": r.get("ctime"), "event": r.get("event"),
                      "label": r.get("label"), "strength": r.get("strength"),
                      "important": r.get("important"),
                      "scope": r.get("scope"), "title": r.get("title")}
                     for r in rows if r.get("ctime")]
        hist = {"items": raw_items, "pages": 0, "from_cache": True,
                "cache_total": len(raw_items)}
    closes = {}
    try:
        from .datasource import Market
        mkt = Market(cfg)
        idx = mkt.index_history(
            need_from=util.add_days(util.today_str(), -(int(days) + 40))) or []
        closes = {d: c for d, c, _v in idx}
    except Exception as e:
        raise util.DataError("指数数据不可用，无法校准：{}".format(e))
    tdays = sorted(closes)
    if not tdays:
        raise util.DataError("指数K线为空，无法校准")
    extra = news.learned_extra()

    per_day = {}
    for it in raw_items:
        d = trade_date_of(it["ctime"], tdays)
        if d:
            per_day.setdefault(d, []).append(it)

    day_rows, ev_stat = [], {}
    for d in sorted(per_day):
        i = tdays.index(d)
        if i + 1 >= len(tdays):
            continue                                  # 还没有次日数据
        nxt = tdays[i + 1]
        ret = closes[nxt] / closes[d] - 1.0
        entries, lab_n = [], {"bull": 0, "bear": 0, "neutral": 0}
        for it in per_day[d]:
            if source == "fetch":
                e = news.score_item(it, extra=extra, pool=[])
                if not e:
                    continue
                e = {"auto_label": e["auto_label"], "event_type": e["event_type"],
                     "auto_strength": e["auto_strength"],
                     "important": e["important"], "title": e["title"]}
            else:
                e = {"auto_label": it.get("label") or "neutral",
                     "event_type": it.get("event") or "dict",
                     "auto_strength": int(it.get("strength") or 0),
                     "important": it.get("important"),
                     "title": it.get("title") or ""}
            entries.append(e)
            lab = e["auto_label"]
            if lab in lab_n:
                lab_n[lab] += 1
            if lab in ("bull", "bear"):
                st = int(e["auto_strength"] or 0)
                key = "{}|{}".format(e["event_type"], lab)
                s = ev_stat.setdefault(key, {"event": e["event_type"], "label": lab,
                                             "n": 0, "hits": 0, "ret_sum": 0.0,
                                             "strength_sum": 0, "titles": []})
                s["n"] += 1
                s["ret_sum"] += ret
                s["strength_sum"] += st
                hit = (ret > 0) if lab == "bull" else (ret < 0)
                s["hits"] += 1 if hit else 0
                if len(s["titles"]) < 3:
                    s["titles"].append((e["title"] or "")[:60])
        # 与线上完全同一口径聚合（含抗饱和刻度换算）
        net, net_raw, n_dir = news.net_score(entries, mode=net_mode,
                                             scale=net_scale)
        day_rows.append({"date": d, "next": nxt, "ret": ret, "net": net,
                         "net_raw": net_raw, "n_dir": n_dir,
                         "score": int(net * amp), "n_news": len(per_day[d]),
                         "labels": lab_n})
    if not day_rows:
        raise util.DataError("窗口内没有可用样本（检查缓存/交易日窗口）")

    rets = [r["ret"] for r in day_rows]
    up_rate = sum(1 for r in rets if r > 0) / len(rets)
    hits = sum(1 for r in day_rows
               if (r["score"] > 0 and r["ret"] > 0) or
                  (r["score"] < 0 and r["ret"] < 0))
    dir_days = sum(1 for r in day_rows if r["score"] != 0)
    events = []
    mults = {}
    p_up = up_rate
    for key, s in sorted(ev_stat.items(), key=lambda kv: -kv[1]["n"]):
        if s["n"] < 1:
            continue
        hr = s["hits"] / s["n"]
        avg = s["ret_sum"] / s["n"]
        # 相对基线的**增量**命中：利多基准=窗口上涨日占比，利空基准=1-上涨日占比
        # （否则下跌窗口里所有利多信号都会被判“差”，上涨窗口里所有利空都被判“好”）
        null = p_up if s["label"] == "bull" else (1.0 - p_up)
        null = min(max(null, 0.02), 0.98)
        edge = hr - null
        se = math.sqrt(null * (1.0 - null) / s["n"])
        z = edge / se if se > 0 else 0.0
        row = {"event": s["event"], "event_cn": EVENT_CN.get(s["event"], s["event"]),
               "label": s["label"], "n": s["n"], "hit_rate": round(hr, 4),
               "baseline": round(null, 4), "edge": round(edge, 4),
               "avg_ret_pct": round(avg * 100, 4), "z": round(z, 2),
               "p": round(_p_two_sided(z), 4),
               "avg_strength": round(s["strength_sum"] / s["n"], 2),
               "samples": s["titles"]}
        if s["n"] >= int(min_samples):
            raw = util.clamp(1.0 + edge * 2.0, 0.6, 1.4)
            row["suggest_mult"] = round(
                1.0 + (raw - 1.0) * (s["n"] / (s["n"] + SHRINK_K)), 3)
            mults[key] = row["suggest_mult"]
        events.append(row)
    bull_rets = [r["ret"] for r in day_rows if r["score"] > 0]
    bear_rets = [r["ret"] for r in day_rows if r["score"] < 0]
    cal = {
        "generated_at": util.now_iso(),
        "window": {"days": int(days), "from": day_rows[0]["date"],
                   "to": day_rows[-1]["date"]},
        "data": {"telegrams": len(raw_items), "classified_days": len(day_rows),
                 "source": source,
                 "pages": hist.get("pages"), "from_cache": hist.get("from_cache"),
                 "cache_total": hist.get("cache_total"), "errors": hist.get("errors") or []},
        "baseline_up_rate": round(up_rate, 4),
        "day_level": {
            "n": len(day_rows), "directional_days": dir_days,
            "hit_rate": round(hits / dir_days, 4) if dir_days else None,
            "ic": (None if _pearson([r["score"] for r in day_rows], rets) is None
                   else round(_pearson([r["score"] for r in day_rows], rets), 4)),
            "avg_ret_bull": (round(sum(bull_rets) / len(bull_rets) * 100, 4)
                             if bull_rets else None),
            "avg_ret_bear": (round(sum(bear_rets) / len(bear_rets) * 100, 4)
                             if bear_rets else None),
            "n_bull": len(bull_rets), "n_bear": len(bear_rets),
        },
        "day_rows": [{"date": r["date"], "next": r["next"],
                      "score": r["score"], "net": r["net"],
                      "next_ret_pct": round(r["ret"] * 100, 3),
                      "hit": (1 if ((r["score"] > 0 and r["ret"] > 0) or
                                    (r["score"] < 0 and r["ret"] < 0))
                              else (0 if r["score"] != 0 else None)),
                      "n_news": r["n_news"], "labels": r["labels"]}
                     for r in day_rows],
        "events": events,
        "suggest_multipliers": mults,
        "net_mode": net_mode, "net_scale": net_scale,
        "notes": ["打分口径与线上一致（news.score_item + news.net_score）",
                  "净情绪口径：{}（刻度 {}）——旧口径 clamp(Σ强度,±8) 实测天天顶格、零区分度"
                  .format(net_mode, net_scale),
                  "收盘 15:00 后电报算次日可得信息，规避未来函数",
                  "建议权重经 n/(n+{:.0f}) 收缩，默认只报告；"
                  "strategy.event_calibration=true 时才生效".format(SHRINK_K)],
    }
    save_calibration(cal)
    return cal


def multiplier_for(cal, event_type, label):
    """按校准结果取某事件类型 + 方向建议的强度倍数（无建议则 1.0）。"""
    if not cal:
        return 1.0
    m = (cal.get("suggest_multipliers") or {}).get("{}|{}".format(event_type, label))
    if m is None:
        return 1.0
    return float(m)


def text_report(cal):
    """CLI 文本表。"""
    if not cal:
        return "（无校准结果）"
    dd = cal.get("day_level") or {}
    lines = ["窗口 {} → {}（{} 个交易日，电报 {} 条）".format(
        cal["window"]["from"], cal["window"]["to"], dd.get("n"),
        (cal.get("data") or {}).get("telegrams")),
        "基线：窗口内上涨日占比 {:.1%}｜日级方向命中率 {}｜IC {}".format(
            cal.get("baseline_up_rate") or 0,
            ("{:.1%}".format(dd["hit_rate"]) if dd.get("hit_rate") is not None else "—"),
            dd.get("ic") if dd.get("ic") is not None else "—"),
        "日级均值：净利多 {:+.3f}% / 净利空 {:+.3f}%".format(
            dd.get("avg_ret_bull") if dd.get("avg_ret_bull") is not None else 0.0,
            dd.get("avg_ret_bear") if dd.get("avg_ret_bear") is not None else 0.0),
        "", "{:<12} {:<4} {:>5} {:>8} {:>8} {:>8} {:>9} {:>7} {:>9}".format(
            "事件类型", "方向", "样本", "命中率", "基准", "增量", "次日均值", "p值", "建议倍数")]
    for e in cal.get("events") or []:
        lines.append("{:<12} {:<4} {:>5} {:>8} {:>8} {:>8} {:>8.3f}% {:>7} {:>9}".format(
            (e["event_cn"] or e["event"])[:10], "利多" if e["label"] == "bull" else "利空",
            e["n"], "{:.1%}".format(e["hit_rate"]),
            "{:.1%}".format(e.get("baseline") or 0.5),
            "{:+.1%}".format(e.get("edge") or 0.0),
            e["avg_ret_pct"], e["p"], e.get("suggest_mult", "—")))
    lines.append("注：增量 = 命中率 − 基准（利多基准=窗口上涨日占比，利空基准=1−上涨日占比）；"
                 "增量>0 才代表该事件方向有超出市场漂移的额外预测力。")
    return "\n".join(lines)
