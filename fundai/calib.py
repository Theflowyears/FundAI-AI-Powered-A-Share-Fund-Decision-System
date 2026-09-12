# -*- coding: utf-8 -*-
"""消息→次日大盘涨跌 的样本库与命中率校准（“训练”的可执行近似）。

为什么这样设计（审计对话中的说明）：
- （**已更新**：财联社电报可用 `refresh_type=1` + `last_time` 时间游标回溯历史，
  见 `calib_history.py` 与 README「四、数据底座」——历史回填由该模块负责）
- 因此“十年新闻语料训练”不可直接做到，改为：从**今天起逐日积累**
  「当日已抓消息（AI/人工标签 + 事件类型）→ 下一交易日真实涨跌」样本，
  累积 N 天后即可对每类事件（央行宽松/地缘冲突/业绩/产品风险提示…）输出
  真实命中率，据此微调语义规则的方向与强度 —— 与规则层一样可审计；
- 若你在浏览器里从财联社手动导出电报 JSON，用 cls-import 命令并入样本。

样本表 signals（存在 data/screening.db，与消息库同库）：
  date/item_id 主键（幂等，重复更新不新增）
  event_type / scope / pred_label / pred_strength  <- 事件语义层预测
  next_date / next_chg / hit                       <- 下一交易日真实结算
"""
import sqlite3

from . import util


def _conn():
    c = sqlite3.connect(str(util.data_file("screening.db")))
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS signals(
        date TEXT NOT NULL,
        item_id TEXT NOT NULL,
        source TEXT DEFAULT '',
        time TEXT DEFAULT '',
        event_type TEXT DEFAULT '',
        scope TEXT DEFAULT '',
        pred_label TEXT DEFAULT '',
        pred_strength REAL DEFAULT 0,
        next_date TEXT,
        next_chg REAL,
        hit INTEGER,
        created TEXT,
        PRIMARY KEY(date, item_id)
    )""")
    return c


def load_closes(cfg=None, refresh=False):
    """从本地长K线缓存读 {date: close}（不触发网络；需先 fetch-history 延伸）。

    refresh=True：先走一次 Market.index_history()（仅在当日 bar 缺失/为盘中快照时
    才会在线拉一次收盘bar），保证结算时能拿到“今日收盘”。

    2026-09-11 加固：改走 `Market._index_cache_items()`（合并主指数可能存在的两个
    缓存键）。事故中主缓存被兜底结果覆盖/缺失，直接读单一文件会让本函数静默归零，
    进而让**所有**基于指数历史的校准与回测一起失效。
    """
    from .datasource import Market
    from . import settings
    cfg = cfg or settings.load_config()
    mkt = Market(cfg)
    if refresh:
        try:
            mkt.index_history()
        except Exception:
            pass
    try:
        items = mkt._index_cache_items()
    except Exception:
        items = {}
    if not items:
        cache = util.load_json(mkt._kline_cache_path(mkt._index_key()),
                               {"items": {}})
        items = cache.get("items") or {}
    return {d: it[0] for d, it in items.items()}, sorted(items)


def evaluate(item_rows, closes, dates_sorted, hit_th=0.003):
    """纯评估：给一批消息行（含 date/auto_label/event_type/scope/…），
    结合收盘价序列算出每条“下一交易日涨跌”与命中，返回逐条+汇总。
    """
    pos = {d: i for i, d in enumerate(dates_sorted)}
    out = []
    agg = {}
    for r in item_rows:
        d = str(r.get("date") or "")[:10]
        i = pos.get(d)
        if i is None or i + 1 >= len(dates_sorted):
            continue
        nd = dates_sorted[i + 1]
        c0, c1 = closes.get(d), closes.get(nd)
        if not c0 or not c1:
            continue
        chg = c1 / c0 - 1.0
        pred = r.get("auto_label") or r.get("pred_label") or "neutral"
        if pred == "bull":
            hit = 1 if chg > hit_th else 0
        elif pred == "bear":
            hit = 1 if chg < -hit_th else 0
        else:
            hit = 1 if abs(chg) <= hit_th else 0
        ev = r.get("event_type") or "dict"
        row = {
            "date": d, "item_id": r.get("item_id") or r.get("id") or "",
            "source": r.get("source", ""), "time": r.get("time", ""),
            "event_type": ev, "scope": r.get("scope") or "",
            "pred_label": pred, "pred_strength": float(r.get("auto_strength") or 0),
            "next_date": nd, "next_chg": round(chg, 6), "hit": hit,
        }
        out.append(row)
        key = ev or "dict"
        a = agg.setdefault(key, {"n": 0, "hit": 0, "bull": 0, "bear": 0,
                                 "neutral": 0})
        a["n"] += 1
        a["hit"] += hit
        a[pred] = a.get(pred, 0) + 1
    for a in agg.values():
        a["hit_rate"] = (a["hit"] / a["n"]) if a["n"] else None
    return out, agg


def update_samples(item_rows, closes=None, dates_sorted=None, cfg=None):
    """把消息行结算写库（幂等）。返回写入/更新的条数。"""
    closes = closes or {}
    dates_sorted = dates_sorted or []
    if not closes:
        closes, dates_sorted = load_closes(cfg)
    rows, _agg = evaluate(item_rows, closes, dates_sorted)
    c = _conn()
    n = 0
    with c:
        for r in rows:
            c.execute(
                "INSERT INTO signals(date,item_id,source,time,event_type,scope,"
                "pred_label,pred_strength,next_date,next_chg,hit,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(date,item_id) DO UPDATE SET "
                "event_type=excluded.event_type,scope=excluded.scope,"
                "pred_label=excluded.pred_label,"
                "pred_strength=excluded.pred_strength,next_date=excluded.next_date,"
                "next_chg=excluded.next_chg,hit=excluded.hit",
                (r["date"], r["item_id"], r["source"], r["time"], r["event_type"],
                 r["scope"], r["pred_label"], r["pred_strength"], r["next_date"],
                 r["next_chg"], r["hit"], util.now_iso()))
            n += 1
    c.close()
    return n


def report(cfg=None):
    """从样本库输出 事件类型 × 命中率 汇总（写 data/signal_calibration.json）。"""
    c = _conn()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM signals WHERE hit IS NOT NULL").fetchall()]
    c.close()
    agg2 = {}
    for r in rows:
        key = r.get("event_type") or "dict"
        a = agg2.setdefault(key, {"n": 0, "hit": 0, "bull": 0, "bear": 0,
                                  "neutral": 0, "avg_chg": 0.0})
        a["n"] += 1
        a["hit"] += int(r.get("hit") or 0)
        lab = r.get("pred_label") or "neutral"
        a[lab] = a.get(lab, 0) + 1
        a["avg_chg"] += float(r.get("next_chg") or 0)
    for a in agg2.values():
        a["hit_rate"] = (a["hit"] / a["n"]) if a["n"] else None
        a["avg_chg"] = round(a["avg_chg"] / a["n"], 6) if a["n"] else None
    out = {
        "total": len(rows),
        "by_event": {k: v for k, v in sorted(
            agg2.items(), key=lambda kv: -kv[1]["n"])},
        "note": "样本逐日积累：每交易日收盘后运行 app.py signals-update 结算当日"
                "消息的下一交易日涨跌；样本充足后据此微调语义规则强度",
    }
    util.save_json(util.data_file("signal_calibration.json"), out)
    return out


def cls_export_rows(path):
    """读取财联社手动导出文件（list[{time,title,text}] 或 JSONL）→ 标准 feed 行。"""
    import json as _json
    from . import semantics
    raw = util.load_json(path) if str(path).endswith(".json") else None
    if raw is None and str(path).endswith(".jsonl"):
        raw = []
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                raw.append(_json.loads(line))
    items = raw.get("items") if isinstance(raw, dict) else raw
    out = []
    for i, it in enumerate(items or []):
        title = str(it.get("title") or it.get("text") or "")[:200]
        text = str(it.get("text") or it.get("summary") or "")
        if not title:
            continue
        sem = semantics.infer_news(title, text)
        out.append({
            "id": "CLS-{}-{}".format(str(it.get("time") or "")[:10], i),
            "source": "财联社电报(手动导入)",
            "time": str(it.get("time") or "")[:16],
            "title": title, "text": text,
            "auto_label": sem["label"], "auto_strength": sem["strength"],
            "auto_net": sem["strength"],
            "event_type": sem["event"] if sem["event"] != "none" else "dict",
            "auto_reason": sem.get("reason", ""),
            "sectors": [], "funds": [],
            "user_label": "", "user_strength": 0,
        })
    return out
