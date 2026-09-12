# -*- coding: utf-8 -*-
"""风险过滤器：把「事件+行情」模型从“方向预测”改造成**仓位上限乘数**，接入实盘决策。

**为什么这样定位**（证据见 README「5.5 事件→方向模型」/「5.6 风控：从'预测型'转为'反应型'」）：
- 模型的日频方向命中率只有 52–56%（边际薄、2024 型强趋势年会亏），
  直接当成新的评分维度会与量化分重复计价；
- 但它**在躲坏日子上有价值**：按信号做多/空仓的净值在多配置下跑赢买入持有，
  且回撤更小——这正好是“风险过滤器”要干的事。

**接入点**：`strategy.plan` 的 `w_target`（权益目标仓位）只被**下调、永不上调**，
下限由 `risk_overlay_floor` 保护；订单在该目标之后生成，因此口径自洽。

阈值来自训练期样本外预测分布（`risk_overlay_on/neutral/off`），不拍脑袋；
盘前/数据缺失/模型不可用 → 一律返回 scale=1.0（不影响原决策）。
"""

from . import event_model, settings, util

SIGNAL_FILE = "risk_signal.json"


def build_signal(p_up, cfg=None, extra=None, vol_rank=None, ma_align=None):
    """由 P(次日上涨) 生成风险信号（**默认只降不升**，牛市覆盖是唯一例外）。

    1) 默认映射 `flat_0_1_p52`（P≥0.52 满仓，否则空仓）；
    2) **波动率过滤**：`vol_rank` ≥ `risk_overlay_vol_pause`（默认 0.90）→ 暂停过滤器、
       保持满仓（极端情绪下择时容易被反复打脸）；
    3) **牛市覆盖**（2025-26 修复期“让出涨幅”的修正）：`vol_rank < bull_vol_max`
       （默认 0.30，波动率极低）且 `ma_align == 1`（价格站上 20 日线且 20>60>200
       均线多头排列）→ 不降仓，并给出 `floor_target`（默认 0.80）请求仓位跟随趋势上浮；
       刚性风控（单只止损、组合回撤锁、防御冷却、w_cap）仍然优先，本金安全不打折。
    """
    st = (cfg or {}).get("strategy", {}) or {}
    name = str(st.get("risk_overlay_mapping") or "flat_0_1_p52")
    mp = MAPPINGS.get(name)
    pause_th = float(st.get("risk_overlay_vol_pause", 0.90) or 0.0)
    bull_vol = float(st.get("risk_overlay_bull_vol_max", 0.30) or 0.0)
    bull_floor = float(st.get("risk_overlay_bull_floor", 0.80) or 0.0)
    if p_up is None:
        return {"p_up": None, "action": "none", "scale": 1.0, "mapping": name,
                "reason": "模型不可用，不干预仓位"}
    p = float(p_up)
    if (bull_vol > 0 and vol_rank is not None and float(vol_rank) < bull_vol
            and ma_align is not None and float(ma_align) >= 0.5):
        sig = {"p_up": round(p, 4), "action": "bull_on", "scale": 1.0,
               "mapping": name, "vol_rank": round(float(vol_rank), 3),
               "bull_floor": bull_floor,
               "reason": "低波动（{:.0%} 分位<{:.0%}）+ 均线多头排列 → 牛市覆盖："
                         "不降仓且权益目标不低于 {:.0%}".format(
                             float(vol_rank), bull_vol, bull_floor)}
        if extra:
            sig.update(extra)
        return sig
    if pause_th > 0 and vol_rank is not None and float(vol_rank) >= pause_th:
        sig = {"p_up": round(p, 4), "action": "paused", "scale": 1.0,
               "mapping": name, "vol_rank": round(float(vol_rank), 3),
               "vol_pause": pause_th,
               "reason": "波动率处于近 250 日 {:.0%} 分位（≥{:.0%}）→ 暂停风险过滤、"
                         "保持满仓，避免极端情绪下被反复打脸".format(
                             float(vol_rank), pause_th)}
        if extra:
            sig.update(extra)
        return sig
    if mp is not None:
        scale = float(mp(p))
    else:                       # 兼容自定义阈值
        on_th = float(st.get("risk_overlay_on_th", 0.55) or 0.55)
        off_th = float(st.get("risk_overlay_off_th", 0.45) or 0.45)
        scale = (1.0 if p >= on_th else
                 (float(st.get("risk_overlay_neutral", 0.7) or 0.7) if p >= off_th
                  else float(st.get("risk_overlay_off", 0.4) or 0.4)))
    action = ("risk_on" if scale >= 0.999 else
              ("risk_off" if scale <= 0.001 else "neutral"))
    sig = {"p_up": round(p, 4), "action": action, "scale": round(scale, 3),
           "mapping": name, "vol_rank": (None if vol_rank is None
                                         else round(float(vol_rank), 3)),
           "vol_pause": pause_th,
           "reason": "P(次日沪深300上涨)={:.1%} → 权益目标仓位×{:.0%}".format(p, scale)}
    if extra:
        sig.update(extra)
    return sig


def _dataset_cfg(cfg):
    return cfg or settings.load_config()


MAPPINGS = {
    "graded_0.4_0.7_1": lambda p: 1.0 if p >= 0.55 else (0.7 if p >= 0.45 else 0.4),
    "graded_0.3_0.7_1": lambda p: 1.0 if p >= 0.55 else (0.7 if p >= 0.45 else 0.3),
    "flat3_0_0.5_1": lambda p: 1.0 if p >= 0.55 else (0.5 if p >= 0.45 else 0.0),
    "flat_0_1_p50": lambda p: 1.0 if p >= 0.50 else 0.0,
    "flat_0_1_p52": lambda p: 1.0 if p >= 0.52 else 0.0,
    "floor3_flat": lambda p: 1.0 if p >= 0.5 else 0.3,
}


def _sim(nav_and_mdd, obs, mapping, fee, smooth=1, min_hold=1):
    """按映射跑净值。smooth：用近 N 日预测均值决策（抗抖动）；min_hold：两次调仓
    的最小间隔天数（场外基金 T+1 且赎回费随持有期下降，频繁切换会被成本吃光）。"""
    nav, pos, switches, peak, mdd = 1.0, None, 0, 1.0, 0.0
    rets, buf, last_change = [], [], None
    for i, (r, p) in enumerate(obs):
        buf.append(float(p))
        if len(buf) > max(1, int(smooth)):
            buf.pop(0)
        if len(buf) < max(1, int(smooth)):
            continue
        p_use = sum(buf) / len(buf)
        tgt = float(mapping(p_use))
        if pos is None:
            pos, last_change = tgt, i
        elif abs(tgt - pos) > 1e-9:
            if last_change is not None and (i - last_change) < int(min_hold):
                tgt = pos
            else:
                switches += 1
                nav *= (1.0 - fee)
                pos, last_change = tgt, i
        nav *= (1.0 + pos * r["ret"])
        rets.append(pos * r["ret"])
        peak = max(peak, nav)
        mdd = max(mdd, 1.0 - nav / peak)
    n = max(1, len(rets))
    mu = sum(rets) / n
    sd = (sum((x - mu) ** 2 for x in rets) / n) ** 0.5 or 1e-9
    return {"nav": round(nav, 4), "ann": round(nav ** (252.0 / n) - 1.0, 4),
            "mdd": round(mdd, 4), "switches": switches,
            "sharpe": round(mu / sd * (252 ** 0.5), 3), "days": n,
            "exposure": round(sum(1 for x in rets if x != 0) / n, 3)}


def compare_mappings(days=2400, cfg=None, feature_set="market", l2=2.0,
                     refit_every=5, half_life=0, warmup=60, holdout=0.4):
    """仓位映射对照：**选择期挑映射、留出期只报告**（避免挑最好看的）。"""
    cfg = _dataset_cfg(cfg)
    ds = event_model.build_dataset(days=days, cfg=cfg)
    keys = event_model.feature_keys(ds["features"], feature_set)
    ds = {"rows": ds["rows"], "features": keys, "dates": ds["dates"],
          "closes": ds["closes"]}
    rows = ds["rows"]
    preds = event_model.walk_forward(ds, warmup=warmup, refit_every=refit_every,
                                     l2=l2, half_life=half_life)
    obs = [(rows[i], preds[i]) for i in range(len(rows)) if preds[i] is not None]
    if len(obs) < 100:
        return {"ok": False, "message": "样本不足"}
    fee = float((cfg.get("strategy") or {}).get("micro_fee_proxy",
                                                event_model.FEE) or event_model.FEE)
    cut = int(len(obs) * (1.0 - holdout))
    sel_obs, ho_obs = obs[:cut], obs[cut:]
    out = {}
    for name, mp in MAPPINGS.items():
        out[name] = {"select": _sim(None, sel_obs, mp, fee),
                     "holdout": _sim(None, ho_obs, mp, fee)}
    bh_sel = _sim(None, sel_obs, lambda p: 1.0, 0.0)
    bh_ho = _sim(None, ho_obs, lambda p: 1.0, 0.0)
    best = None
    for name, r in out.items():
        s = r["select"]
        score = s["ann"] - 0.5 * s["mdd"]
        if best is None or score > best[1]:
            best = (name, score)
    return {"ok": True, "n": len(obs), "select_days": cut, "holdout_days": len(ho_obs),
            "mappings": out, "buy_hold_select": bh_sel, "buy_hold_holdout": bh_ho,
            "selected": best[0] if best else None,
            "note": "选择期按 年化−0.5×最大回撤 挑映射；留出期仅用于报告"}


SEGMENTS = {
    "2021H2 美联储转鹰": ("2021-07-01", "2021-12-31"),
    "2022 俄乌+加息（年内两波急跌）": ("2022-01-01", "2022-12-31"),
    "2022-04 上海封控急跌": ("2022-03-15", "2022-05-15"),
    "2024-01/02 小微盘流动性危机": ("2024-01-02", "2024-02-29"),
    "2024-09 政策V型反转": ("2024-09-01", "2024-10-31"),
    "2025-2026 修复期": ("2025-01-01", "2026-09-09"),
}


def _series(days=2400, cfg=None, feature_set="market", l2=2.0,
            refit_every=5, half_life=0, warmup=60):
    """一次构建数据集 + walk-forward 预测（含每日 vol_rank，供波动率过滤用）。"""
    cfg = _dataset_cfg(cfg)
    ds = event_model.build_dataset(days=days, cfg=cfg)
    keys = event_model.feature_keys(ds["features"], feature_set)
    ds = {"rows": ds["rows"], "features": keys, "dates": ds["dates"],
          "closes": ds["closes"]}
    preds = event_model.walk_forward(ds, warmup=warmup, refit_every=refit_every,
                                     l2=l2, half_life=half_life)
    return [(ds["rows"][i], preds[i]) for i in range(len(ds["rows"]))
            if preds[i] is not None]


def _sim_seg(obs, mapping, fee, smooth=1, min_hold=1, vol_pause=None,
             bull_vol=None):
    """按日模拟（可选：vol_rank ≥ vol_pause 暂停过滤器；vol_rank < bull_vol 且均线多头
    排列时“牛市覆盖”→ 满仓，用于验证 2025-26 修复期的让出涨幅问题）。
    """
    nav, bh, pos, switches, peak, mdd = 1.0, 1.0, None, 0, 1.0, 0.0
    bh_peak, bh_mdd = 1.0, 0.0
    buf, last_change = [], None
    whipsaw, exits, reentries = 0, 0, 0
    last_exit_i = None
    worst_rets, best_rets = [], []
    bull_days, bull_nav_gain = 0, 1.0
    for i, (r, p) in enumerate(obs):
        bh *= (1.0 + r["ret"])
        bh_peak = max(bh_peak, bh)
        bh_mdd = max(bh_mdd, 1.0 - bh / bh_peak)
        buf.append(float(p))
        if len(buf) > max(1, int(smooth)):
            buf.pop(0)
        if len(buf) < max(1, int(smooth)):
            continue
        f = r.get("f") or {}
        vol_rank = float(f.get("vol_rank") or 0.0)
        ma_align = float(f.get("ma_align") or 0.0)
        paused = bool(vol_pause and vol_rank >= float(vol_pause))
        bull = bool(bull_vol and vol_rank < float(bull_vol) and ma_align >= 0.5)
        if paused or bull:
            tgt = 1.0
        else:
            tgt = float(mapping(sum(buf) / len(buf)))
        if bull:
            bull_days += 1
            if pos is not None and pos > 0:
                bull_nav_gain *= (1.0 + pos * r["ret"])
        if pos is None:
            pos, last_change = tgt, i
        elif abs(tgt - pos) > 1e-9:
            if last_change is not None and (i - last_change) < int(min_hold):
                tgt = pos
            else:
                switches += 1
                nav *= (1.0 - fee)
                if tgt < pos:
                    exits += 1
                    last_exit_i = i
                else:
                    reentries += 1
                    if last_exit_i is not None and (i - last_exit_i) <= 5:
                        whipsaw += 1
                pos, last_change = tgt, i
        nav *= (1.0 + pos * r["ret"])
        worst_rets.append((r["ret"], pos))
        best_rets.append((r["ret"], pos))
        peak = max(peak, nav)
        mdd = max(mdd, 1.0 - nav / peak)
    n = max(1, len(worst_rets))
    worst = sorted(worst_rets, key=lambda t: t[0])[:max(1, n // 20)]
    best = sorted(best_rets, key=lambda t: -t[0])[:max(1, n // 20)]
    w_exp = (sum(p for _r, p in worst) / len(worst)) if worst else 0.0
    b_exp = (sum(p for _r, p in best) / len(best)) if best else 0.0
    w_ret = (sum(r for r, _p in worst) / len(worst) * 100) if worst else 0.0
    b_ret = (sum(r for r, _p in best) / len(best) * 100) if best else 0.0
    return {"days": n, "nav": round(nav, 4), "ann": round(nav ** (252.0 / n) - 1.0, 4),
            "ret_pct": round((nav - 1.0) * 100, 2),
            "mdd": round(mdd, 4), "switches": switches, "exits": exits,
            "reentries": reentries, "whipsaw": whipsaw,
            "bull_days": bull_days,
            "bh_nav": round(bh, 4), "bh_ann": round(bh ** (252.0 / n) - 1.0, 4),
            "bh_ret_pct": round((bh - 1.0) * 100, 2),
            "bh_mdd": round(bh_mdd, 4),
            "exposure_worst5pct": round(w_exp, 3),
            "exposure_best5pct": round(b_exp, 3),
            "worst5pct_avg_ret_pct": round(w_ret, 3),
            "best5pct_avg_ret_pct": round(b_ret, 3)}


def stress_test(days=2400, cfg=None, mapping_name="flat_0_1_p52", fee=0.002,
                smooth=5, min_hold=10, vol_pause=None, segments=None):
    """极端年份压测：每个片段跑 风险过滤 vs 买入持有，并给出“止损及时/踏空/打脸”指标。"""
    obs_all = _series(days=days, cfg=cfg)
    mp = MAPPINGS.get(mapping_name) or MAPPINGS["flat_0_1_p52"]
    out = {}
    for name, (a, b) in (segments or SEGMENTS).items():
        seg = [(r, p) for r, p in obs_all if a <= r["date"] <= b]
        if len(seg) < 15:
            continue
        res = _sim_seg(seg, mp, fee, smooth=smooth, min_hold=min_hold,
                       vol_pause=vol_pause)
        res["window"] = "{} → {}".format(seg[0][0]["date"], seg[-1][0]["date"])
        res["better"] = res["ann"] > res["bh_ann"]
        out[name] = res
    return {"mapping": mapping_name, "fee_bp": fee * 10000, "smooth": smooth,
            "min_hold": min_hold, "vol_pause": vol_pause, "segments": out,
            "all_days": _sim_seg(obs_all, mp, fee, smooth=smooth,
                                 min_hold=min_hold, vol_pause=vol_pause)}


def compare_vol_pause(days=2400, cfg=None, mapping_name="flat_0_1_p52", fee=0.002,
                      smooth=5, min_hold=10, thresholds=(0.0, 0.85, 0.9, 0.95)):
    """波动率过滤对照：vol_rank ≥ 阈值 时**暂停**过滤器（保持满仓）。

    动机：单边下跌里它止损很灵；V 型反转里容易被“暂停/恢复”左右打脸。
    阈值 0.0 表示不启用（永远不暂停）。
    """
    obs = _series(days=days, cfg=cfg)
    mp = MAPPINGS.get(mapping_name) or MAPPINGS["flat_0_1_p52"]
    out = {}
    for th in thresholds:
        pause = None if th <= 0 else th
        out[str(th)] = {
            "all": _sim_seg(obs, mp, fee, smooth=smooth, min_hold=min_hold,
                            vol_pause=pause),
            "2022": _sim_seg([(r, p) for r, p in obs
                              if "2022-01-01" <= r["date"] <= "2022-12-31"],
                             mp, fee, smooth=smooth, min_hold=min_hold,
                             vol_pause=pause),
            "2024crash": _sim_seg([(r, p) for r, p in obs
                                   if "2024-01-02" <= r["date"] <= "2024-02-29"],
                                  mp, fee, smooth=smooth, min_hold=min_hold,
                                  vol_pause=pause),
            "2024v": _sim_seg([(r, p) for r, p in obs
                               if "2024-09-01" <= r["date"] <= "2024-10-31"],
                              mp, fee, smooth=smooth, min_hold=min_hold,
                              vol_pause=pause),
        }
    return out


def latest(cfg=None, force=False, max_age_min=240, feature_set="market",
           l2=2.0, refit_every=5, half_life=500, warmup=60):
    """最新一日的样本外信号（缓存到 data/risk_signal.json，按日期复用）。

    用**除最后一日以外**的全部历史训练（半衰期加权），预测最后一个已收盘日 →
    这份预测是样本外的（回测里同一套代码产生的 walk-forward 预测已在线评估）。
    """
    cfg = _dataset_cfg(cfg)
    path = util.data_file(SIGNAL_FILE)
    cached = util.load_json(path, {}) or {}
    if cached and not force:
        try:
            from datetime import datetime
            age = (util.now_dt() - datetime.fromisoformat(
                str(cached.get("generated_at") or "")[:19])).total_seconds() / 60.0
        except Exception:
            age = 1e9
        if age <= max_age_min and cached.get("date"):
            return cached
    try:
        ds = event_model.build_dataset(days=2400, cfg=cfg)
        keys = event_model.feature_keys(ds["features"], feature_set)
        rows = ds["rows"]
        if len(rows) < warmup + 20:
            raise util.DataError("样本不足，暂不启用风险过滤")
        st = (cfg.get("strategy") or {})
        smooth = max(1, int(st.get("risk_overlay_smooth", 5) or 1))
        # 近 smooth 日的 walk-forward 预测取均值（抗抖动；限频由引擎按 min_hold 执行）
        wf = event_model.walk_forward(ds, warmup=max(0, len(rows) - smooth - 25),
                                      refit_every=5, l2=l2, half_life=half_life)
        recent = [p for p in wf[-smooth:] if p is not None]
        if not recent:
            raise util.DataError("近期预测不足")
        p_up = sum(recent) / len(recent)
        vol_rank = (rows[-1].get("f") or {}).get("vol_rank")
        ma_align = (rows[-1].get("f") or {}).get("ma_align")
        sig = build_signal(p_up, cfg, vol_rank=vol_rank, ma_align=ma_align,
                           extra={
            "date": rows[-1]["date"], "for_date": rows[-1]["next"],
            "generated_at": util.now_iso(), "days": len(rows),
            "feature_set": feature_set, "smooth": smooth,
            "recent_p": [round(p, 4) for p in recent]})
        util.save_json(path, sig)
        return sig
    except Exception as e:
        sig = {"p_up": None, "action": "none", "scale": 1.0,
               "date": util.today_str(), "generated_at": util.now_iso(),
               "reason": "模型不可用（{}），不干预仓位".format(str(e)[:80])}
        return sig


def evaluate(days=2400, cfg=None, feature_set="market", l2=2.0,
             refit_every=5, half_life=0, warmup=60, band=event_model.DEAD_BAND):
    """历史评估：**风险过滤（只降仓）** vs 满仓买入持有 vs 纯信号多空仓。

    过滤口径：P(涨) ≥ on → 满仓；off ≤ P < on → 中性仓；P < off → 低仓；
    与线上 `build_signal` 完全同一套阈值映射。
    """
    cfg = _dataset_cfg(cfg)
    ds = event_model.build_dataset(days=days, cfg=cfg)
    keys = event_model.feature_keys(ds["features"], feature_set)
    ds = {"rows": ds["rows"], "features": keys, "dates": ds["dates"],
          "closes": ds["closes"]}
    rows = ds["rows"]
    preds = event_model.walk_forward(ds, warmup=warmup, refit_every=refit_every,
                                     l2=l2, half_life=half_life)
    obs = [(rows[i], preds[i]) for i in range(len(rows)) if preds[i] is not None]
    if not obs:
        return {"ok": False, "message": "样本不足"}
    st = cfg.get("strategy", {}) or {}
    fee = float(st.get("micro_fee_proxy", event_model.FEE) or event_model.FEE)
    nav_f = nav_b = nav_p = 1.0
    peaks = {"filter": 1.0, "bh": 1.0, "signal": 1.0}
    mdd = {"filter": 0.0, "bh": 0.0, "signal": 0.0}
    switches_f = switches_p = 0
    pos_f = pos_p = 1.0
    cut_days, cut_rets, hold_rets = 0, [], []
    for r, p in obs:
        sig = build_signal(p, cfg)
        tgt = sig["scale"]
        if abs(tgt - pos_f) > 1e-9:
            switches_f += 1
            nav_f *= (1.0 - fee)
            pos_f = tgt
        nav_f *= (1.0 + pos_f * r["ret"])
        nav_b *= (1.0 + r["ret"])
        t2 = 1.0 if p >= 0.5 else 0.0
        if abs(t2 - pos_p) > 1e-9:
            switches_p += 1
            nav_p *= (1.0 - fee)
            pos_p = t2
        nav_p *= (1.0 + pos_p * r["ret"])
        for k, v in (("filter", nav_f), ("bh", nav_b), ("signal", nav_p)):
            peaks[k] = max(peaks[k], v)
            mdd[k] = max(mdd[k], 1.0 - v / peaks[k])
        if sig["scale"] < 1.0:
            cut_days += 1
            cut_rets.append(r["ret"])
        else:
            hold_rets.append(r["ret"])
    n = len(obs)
    ann = lambda v: round(v ** (252.0 / max(1, n)) - 1.0, 4)
    worst = sorted(obs, key=lambda t: t[0]["ret"])[:max(1, n // 20)]
    caught = sum(1 for r, p in worst if build_signal(p, cfg)["scale"] < 1.0)
    return {"ok": True, "days": n,
            "filter": {"nav": round(nav_f, 4), "ann": ann(nav_f),
                       "mdd": round(mdd["filter"], 4), "switches": switches_f},
            "buy_hold": {"nav": round(nav_b, 4), "ann": ann(nav_b),
                         "mdd": round(mdd["bh"], 4)},
            "signal_long_flat": {"nav": round(nav_p, 4), "ann": ann(nav_p),
                                 "mdd": round(mdd["signal"], 4),
                                 "switches": switches_p},
            "cut_days": cut_days,
            "cut_avg_ret": (round(sum(cut_rets) / len(cut_rets) * 100, 3)
                            if cut_rets else None),
            "hold_avg_ret": (round(sum(hold_rets) / len(hold_rets) * 100, 3)
                             if hold_rets else None),
            "worst5pct_caught": round(caught / max(1, len(worst)), 4),
            "note": "风险过滤＝只降仓乘数（口线上同阈值）；纯信号＝P≥0.5 满仓否则空仓"}
