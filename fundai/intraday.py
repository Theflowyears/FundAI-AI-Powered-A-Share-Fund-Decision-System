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

from . import news as newsmod, risk_overlay, screening, settings, util


def _quote(cfg):
    from .datasource import Market
    mk = Market(cfg)
    qs = mk.indices_quote(force=True) or []
    main = next((q for q in qs if q.get("benchmark")), None) or (qs[0] if qs else None)
    return main, qs


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


def run_midday(cfg=None, force=False):
    """执行午间诊断并落库（幂等：同日重复运行覆盖）。"""
    cfg = cfg or settings.load_config()
    scr = screening.ScreeningStore()
    d = util.today_str()
    main, qs = _quote(cfg)
    if not main:
        return {"ok": False, "message": "实时行情不可用（网络/接口），午间诊断跳过"}
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
    return {"ok": True, "date": d, "mid_chg": mid_chg, "view": view,
            "summary": summary, "row": row}


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
