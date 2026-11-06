# -*- coding: utf-8 -*-
"""午间盘中诊断（12:00 自动任务）：上午行情 + 消息面 + 风险信号 → 短评 + 学习记录。

设计要点：
- **不做尾盘预测**：只说“上午发生了什么、下午要盯什么”，避免与次日研判混淆；
- **可自检**：记录午间观点（看多/看空/震荡 = 对**下午**半场的看法）与 11:30 价位，
  收盘后自动用「收盘 / 11:30 − 1」结算命中 → 与次日方向判断一样累计命中率；
- **可学习**：结构化特征（上午涨跌、宽度、消息净情绪、风险过滤 P(涨)、波动分位）
  落库到 `data/screening.db` 的 `intraday` 表，供后续统计“上午形态 → 下午/次日”的关系。

用法：`python app.py intraday-pulse`（12:00 计划任务调用）→ 收盘后
`engine.run_daily` 会自动 `settle()`；也可 `python app.py intraday-pulse --settle` 手动结算。
"""
import json
import time

from . import news as newsmod, risk_overlay, screening, settings, util

# 午间诊断取数的重试参数（2026-09-16：11:35 那次瞬断导致整天没有诊断）
MIDDAY_QUOTE_ATTEMPTS = 3     # 实时行情最多试 3 次（三家通道各走一遍算 1 次）
MIDDAY_QUOTE_GAP = 2.5        # 每次之间的间隔（秒）


def _quote(cfg, attempts=None):
    """取基准指数实时行情；**带重试**，失败再退回"当日分时线"。

    为什么必须重试（2026-09-16 用户反馈"午间自动诊断还是没有运行"）：
    日志里 11:35:00 那次是 `午间复盘跳过：实时行情不可用（网络/接口）` ——
    旧实现 `_quote()` **只调一次** `indices_quote()`，三家通道同时瞬断就直接放弃，
    当天再也没有第二次机会。而实测同一台机器晚一点再跑就完全正常，
    说明这是**瞬时网络抖动**，不是配置问题。所以：
      1. 重试 `MIDDAY_QUOTE_ATTEMPTS` 次（间隔 `MIDDAY_QUOTE_GAP` 秒）；
      2. 仍失败则用 `index_intraday()` 的**当日分时线**兜底：取最后一个点作为"上午收盘价"、
         用 pre_close 算涨跌幅 —— 分时线与实时行情走的是不同接口，能覆盖前者的故障域。
    返回 (main, quotes, source)；source ∈ live/retry/intraday/""（空 = 彻底拿不到）。
    """
    from .datasource import Market
    mk = Market(cfg)
    n = int(attempts if attempts is not None else MIDDAY_QUOTE_ATTEMPTS)
    main, qs = None, []
    for i in range(max(1, n)):
        try:
            qs = mk.indices_quote(force=True) or []
        except Exception:
            qs = []
        main = next((q for q in qs if q.get("benchmark")), None) or (qs[0] if qs else None)
        if main and main.get("price"):
            return main, qs, ("live" if i == 0 else "retry")
        if i < n - 1:
            time.sleep(MIDDAY_QUOTE_GAP)
    # ---- 兜底：当日分时线（不同接口，故障域不同）----
    try:
        d = mk.index_intraday()
    except Exception:
        d = None
    if d and d.get("ok") and d.get("points"):
        pts = d["points"]
        last = pts[-1]
        pre = d.get("pre_close")
        price = float(last.get("price") or 0)
        chg = float(d.get("chg_pct") or 0)
        if pre and price:
            chg = price / float(pre) - 1.0
        main = {"code": (d.get("code") or "000300"), "name": d.get("name") or "沪深300",
                "price": price, "chg_pct": chg, "chg_amt": None, "benchmark": True,
                "source": "intraday"}
        qs = [main]
        return main, qs, "intraday"
    return None, [], ""


def _morning_news_score(cfg):
    """上午消息面：优先用当日已抓的缓存口径，否则用库内当日电报按当前口径重算。"""
    st = (cfg.get("strategy") or {})
    amp = int(st.get("news_amp") or 8)
    d = util.today_str()
    try:
        p = newsmod.news_cache_file(d)
        if p.exists():
            c = util.load_json(p, {}) or {}
            if c.get("ok") and c.get("scope"):
                sc = c.get("scope") or {}
                return (c.get("score"), c.get("net"), "cache",
                        int(sc.get("n_in_scope") or len(c.get("feed") or [])))
    except Exception:
        pass
    try:
        from datetime import datetime as _dt
        from . import clsdb
        rows = []
        for r in clsdb.scored_items(since=int(util.now_dt().timestamp()) - 2 * 86400):
            ct = r.get("ctime")
            if not ct:
                continue
            if _dt.fromtimestamp(int(ct), util.TZ_CN).strftime("%Y-%m-%d") == d:
                rows.append(r)
        focus = list(st.get("news_focus_events") or newsmod.DEFAULT_FOCUS_EVENTS)
        net, raw, n_in = newsmod.net_score(
            rows, mode=str(st.get("news_net_mode") or "scaled"),
            scale=float(st.get("news_net_scale") or 70.0), focus=focus,
            dict_mode=str(st.get("news_dict_mode") or "off"),
            include_other=bool(st.get("news_include_other_events", False)))
        return (int(max(-100, min(100, net * max(1, amp)))), round(net, 3),
                "db", n_in)
    except Exception:
        return None, None, "none", 0


def _view(mid_chg, p_up, vol_rank, cfg):
    """午间观点（针对**下午**半场）-100~+100 分 → bull/bear/neutral + 文案。"""
    st = cfg.get("strategy") or {}
    score = 0.0
    if mid_chg is not None:
        score += max(-60.0, min(60.0, mid_chg * 100 * 3.0))    # 上午涨跌（反向轻仓）
        score = -score * 0.5                                   # 上午急跌后偏修复、急涨后防回落
    if p_up is not None:
        score += (float(p_up) - 0.5) * 120.0
    if vol_rank is not None:
        score -= max(0.0, float(vol_rank) - 0.8) * 60.0         # 波动极高时降低方向自信
    score = max(-100.0, min(100.0, score))
    if score >= 20:
        view, txt = "bull", "下午倾向修复/走强"
    elif score <= -20:
        view, txt = "bear", "下午倾向继续走弱"
    else:
        view, txt = "neutral", "下午大概率维持震荡"
    return int(round(score)), view, txt


def run_midday(cfg=None, force=False, date_s=None):
    """执行午间诊断并落库（幂等：同日重复运行覆盖）。

    ``date_s``：可指定日期（默认今天）。**只能跑当天** —— 诊断取的是实时行情，
    回补历史日期在物理上不可能（那时的盘中价已经拿不到了），所以显式拒绝并说明，
    而不是悄悄用"现在的价格"冒充历史。
    ``force``：忽略当日缓存重取实时行情。
    """
    cfg = cfg or settings.load_config()
    scr = screening.ScreeningStore()
    if date_s and str(date_s)[:10] != util.today_str():
        return {"ok": False,
                "message": "午间诊断只能取实时行情，无法回补历史日期（{}）".format(
                    str(date_s)[:10])}
    d = util.today_str()
    main, qs, qsrc = _quote(cfg)
    if not main:
        # 取数彻底失败：**不静默**。写一条可查的失败记录（供 intraday-status 与计划任务
        # 重试判断），并给出明确的重试指引——旧实现只返回一句"跳过"，
        # 结果整天没人知道午间诊断根本没跑（用户 2026-09-16 反馈）。
        msg = "实时行情不可用（{} 次尝试 + 分时线兜底都失败）".format(
            MIDDAY_QUOTE_ATTEMPTS)
        try:
            util.save_json(util.data_file("intraday_last_error.json"),
                           {"date": d, "time": util.now_dt().strftime("%H:%M"),
                            "message": msg, "updated": util.now_iso()})
        except Exception:
            pass
        return {"ok": False, "date": d, "retry_able": True,
                "message": msg + "，午间诊断未执行；计划任务会在 11:40/11:45 自动重试，"
                                 "也可手动 python app.py intraday-pulse"}
    mid_price = float(main.get("price") or 0)
    mid_chg = float(main.get("chg_pct") or 0)
    others = [{"name": q.get("name"), "chg": round(float(q.get("chg_pct") or 0), 4)}
              for q in qs if q is not main]
    score, net, src, n_in = _morning_news_score(cfg)
    try:
        sig = risk_overlay.latest(cfg)
    except Exception:
        sig = {}
    vol_rank = sig.get("vol_rank")
    p_up = sig.get("p_up")
    idx_score, view, vtxt = _view(mid_chg, p_up, vol_rank, cfg)
    tone = "普跌" if mid_chg <= -0.01 else ("普涨" if mid_chg >= 0.01 else "震荡")
    parts = ["【午间诊断 {}】".format(d),
             "{} 上午 {:.2%}（{}）".format(main.get("name"), mid_chg, tone)]
    if others:
        parts.append("风格：" + "、".join(
            "{} {:+.2%}".format(o["name"], o["chg"]) for o in others[:3]))
    if score is not None:
        parts.append("消息面净情绪 {:+.2f}（分 {}，口径 {}，{} 条在口径内）".format(
            net if net is not None else 0, score, src, n_in))
    if p_up is not None:
        parts.append("风险过滤 P(次日上涨) {:.0%}（{}）".format(
            p_up, sig.get("action")))
    parts.append("下午观点：{}（{}，{} 分）".format(vtxt, view, idx_score))
    parts.append("盯：{}".format(
        "① 能否收回上午跌幅的一半 ② 跌停/炸板是否扩散 ③ 尾盘量能"
        if mid_chg < 0 else
        "① 能否站稳上午高点 ② 涨停家数是否继续扩张 ③ 是否有放量滞涨"))
    summary = "；".join(parts)
    row = {"date": d, "time": util.now_dt().strftime("%H:%M"),
           "mid_price": mid_price, "mid_chg": round(mid_chg, 6),
           "summary": summary, "dir_view": view, "confidence": 0.6,
           "features": json.dumps({"indices": others, "news_score": score,
                                   "news_net": net, "news_src": src,
                                   "n_in_scope": n_in, "p_up": p_up,
                                   "overlay_action": sig.get("action"),
                                   "vol_rank": vol_rank},
                                  ensure_ascii=False)}
    scr.intraday_upsert(row)
    # 成功即清掉上一次的失败记录，避免状态面板一直显示旧错误
    try:
        p = util.data_file("intraday_last_error.json")
        from pathlib import Path
        if Path(p).exists():
            Path(p).unlink()
    except Exception:
        pass
    return {"ok": True, "date": d, "mid_chg": mid_chg, "view": view,
            "summary": summary, "row": row, "quote_source": qsrc}


def settle(date_s=None, cfg=None):
    """收盘后结算午间观点：命中 = 下午（收盘 vs 11:30）方向与该观点一致（|涨跌|>0.3%）。"""
    from . import calib
    cfg = cfg or settings.load_config()
    scr = screening.ScreeningStore()
    d = str(date_s or util.today_str())[:10]
    row = scr.intraday_get(d)
    if not row or not row.get("mid_price"):
        return {"ok": False, "message": "当日无午间记录"}
    closes, dates = calib.load_closes(cfg=cfg, refresh=True)
    close = closes.get(d)
    if not close:
        return {"ok": False, "message": "还没有 {} 收盘价".format(d)}
    pm = close / float(row["mid_price"]) - 1.0
    view = row.get("dir_view") or "neutral"
    if abs(pm) <= 0.003:
        hit = 1 if view == "neutral" else 0
    else:
        hit = 1 if ((view == "bull" and pm > 0) or (view == "bear" and pm < 0)) else 0
    scr.intraday_settle(d, close, pm, hit)
    return {"ok": True, "date": d, "close": close, "pm_chg": round(pm, 6),
            "hit": bool(hit), "view": view}


def stats(limit=60):
    return screening.ScreeningStore().intraday_stats(limit)


VIEW_TEXT = {"bull": "偏强（下午倾向修复/走强）",
             "bear": "偏弱（下午倾向继续走弱）",
             "neutral": "震荡（下午大概率维持震荡）"}


def status(days=14):
    """午间诊断状态：{ok, today, stats, history, phase_ok, hint}

    给界面/计划任务读的**只读**视图（不触发任何在线请求）：
      * `today`：今日是否已诊断、观点、原文、结算结果；
      * `stats`：累计自检命中率（结算样本）；
      * `history`：最近 N 天（供界面做时间线）；
      * `phase_ok`：现在是否"适合跑诊断"（上午收盘后到收盘前为 True）。
    """
    scr = screening.ScreeningStore()
    st = scr.intraday_stats(int(days))
    today = util.today_str()
    cur = scr.intraday_get(today)
    phase = None
    try:
        from .datasource import _session_phase
        phase = _session_phase()
    except Exception:
        phase = None
    phase_ok = phase in ("noon", "pm", "closed")
    hist = []
    for h in (st.get("history") or []):
        hist.append({"date": h.get("date"), "time": h.get("time"),
                     "view": h.get("dir_view"), "summary": h.get("summary"),
                     "mid_chg": h.get("mid_chg"), "pm_chg": h.get("pm_chg"),
                     "hit": h.get("hit")})
    # 最近一次执行失败的原因（今天失败才透出）：让界面能说清"是没跑还是跑失败了"。
    # 旧的失败路径只返回一句话、什么都没留下，用户只能看到"今日还没有午间复盘"。
    last_error = None
    try:
        from pathlib import Path
        p = Path(util.data_file("intraday_last_error.json"))
        if p.exists():
            err = util.load_json(p, {}) or {}
            if str(err.get("date") or "") == today:
                last_error = "{} {}".format(err.get("time") or "",
                                            err.get("message") or "").strip()
    except Exception:
        last_error = None
    return {
        "ok": True,
        "date": today,
        "phase": phase,
        "phase_ok": phase_ok,
        "last_error": last_error,
        "today": {
            "date": today,
            "time": (cur or {}).get("time"),
            "dir_view": (cur or {}).get("dir_view"),
            "view_text": VIEW_TEXT.get((cur or {}).get("dir_view"), ""),
            "summary": (cur or {}).get("summary"),
            "mid_chg": (cur or {}).get("mid_chg"),
            "pm_chg": (cur or {}).get("pm_chg"),
            "hit": (cur or {}).get("hit"),
            "resolved": bool((cur or {}).get("resolved")),
        } if cur else {},
        "mid_chg": (cur or {}).get("mid_chg"),
        "view_text": VIEW_TEXT.get((cur or {}).get("dir_view"), ""),
        "stats": {"total": st.get("total"), "hit": st.get("hit"),
                  "hit_rate": st.get("hit_rate"), "by_view": st.get("by_view")},
        "history": hist,
        "hint": ("现在适合跑诊断（上午已收盘）" if phase_ok
                 else "现在是非交易时段/盘中前段：诊断仍可跑，但行情取最近一次为准"),
    }
