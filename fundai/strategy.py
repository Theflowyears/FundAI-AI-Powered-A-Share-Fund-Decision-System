# -*- coding: utf-8 -*-
"""仓位决策：风控层（止损/止盈）+ 总仓位 + 选基轮动。

纪律（写死，防止乱来）
---------------------
A. 风控层（严格理性，优先级高于一切进攻）：
   - 单只浮亏 ≤ fund_stop_loss_pct(-8%)   → 【止损】清仓该只；
   - 单只浮盈 ≥ fund_take_profit_pct(10%)  → 【止盈】锁定一半；
   - 单只浮盈 ≥ fund_take_profit_full_pct(25%) → 【止盈】全部落袋为安；
   - 总资产 ≤ 本金×(1+portfolio_stop_pct)（默认亏15%）→ 【组合止损】权益≤25%；
   - 总资产自历史峰值回撤 ≥ peak_trailing(10%) 且评分 < reentry(25)
     → 【峰值回撤】强制降到 25% 权益（跟踪止盈）；
   - 组合级风控触发后进入 rearm_days(默认7) 防御冷却期，期间默认只减不加；
     但冷却期内若【再触发单只止损/止盈】或【市场评分 > rearm_bull_score(50)】，
     视为“例外放行日”，允许直接按常规规则调仓（可经基金转换加仓/换基）；
   - 不足7天份额默认等解锁再卖（避开 1.5% 惩罚费）；risk.allow_early_exit_fee=true
     时无条件执行并照常计费。
B. 常规（未触发风控或处于例外放行日）：
   - 总权益仓位 = eq_base + eq_slope×评分，夹在 [eq_floor, eq_cap]；
   - 与当前相差 ≥ regime_step 才加减仓；
   - 进攻池内按 20 日动量选基/轮动（rotate_gap 门槛）；
   - use_fund_convert=true（默认）：赎回与申购同日配对下单，交给平台『基金转换/
     超级转换』一次完成，免去“先赎回→等资金到账→再买”的空窗期；
     改为 false 则回到“两步走”（先赎旧，资金到账后下一交易日再买）。
"""
from . import settings, util

# 主题分组：用于“综合分散”选基，避免同一板块扎堆（同主题最多 theme_max 只）。
# 把相近板块合并成一组，防止“半导体”“芯片”被当成两个不同主题而重复配置。
# 注意顺序：更具体的词排在前面（如“卫星/航天”需在“通信”之前，避免“卫星通信”被误归通信）。
THEME_GROUPS = [
    (["商业航天", "卫星", "航天", "北斗"], "商业航天"),
    (["军工"], "军工"),
    (["半导体", "芯片"], "半导体芯片"),
    (["科创"], "科创"),
    (["证券", "券商"], "证券"),
    (["人工智能", "AI"], "人工智能"),
    (["软件", "计算机", "信息传媒", "电子信息", "传媒"], "计算机软件"),
    (["通信", "5G"], "通信"),
    (["电子", "消费电子"], "电子"),
    (["银行"], "银行"),
    (["黄金"], "黄金"),
    (["医药", "制药", "药品", "生物医药"], "医药"),
    (["医疗"], "医疗"),
    (["科技"], "科技"),
    (["房地产", "地产"], "房地产"),
    (["运输", "交通", "交运"], "交通运输"),
    (["公用事业", "电力", "水务", "供水", "供气", "水电"], "公用事业"),
    (["沪深300", "中证500", "中证1000"], "宽基"),
]


def _theme_of(name):
    for kws, theme in THEME_GROUPS:
        if any(kw in (name or "") for kw in kws):
            return theme
    return None


def _market_reason(theme, mom, market_mom):
    """根据板块属性 + 动量相对强弱，生成一句话市场原因。"""
    if theme in ("黄金", "公用事业", "银行"):
        base = "防御/高股息属性，震荡市避险优选"
    elif theme in ("半导体芯片", "科创", "人工智能", "科技", "计算机软件", "电子", "通信", "商业航天"):
        base = "科技成长赛道，产业趋势与政策催化"
    elif theme == "军工":
        base = "军工景气度与事件驱动"
    elif theme == "证券":
        base = "牛市旗手，对成交活跃度与市场情绪敏感"
    elif theme in ("医药", "医疗"):
        base = "刚需防御叠加创新驱动"
    elif theme in ("房地产", "交通运输", "宽基"):
        base = "低估值顺周期修复"
    else:
        base = "板块轮动资金关注"
    if market_mom is not None and mom is not None:
        rel = mom - market_mom
        base += "，相对沪深300 {:+.1%}".format(rel)
    return base


def _pick_winners(cands, names, top_n, theme_max):
    """从已排序候选里按主题分散选出至多 top_n 只，返回 [(code, mom, theme)]。

    cands 已按收益/风险排序并过滤；这里只做主题分散（同主题至多 theme_max 只），
    用于降低单一板块风险。
    """
    winners, theme_count = [], {}
    for code, mom in cands:
        if len(winners) >= top_n:
            break
        th = _theme_of(names.get(code, ""))
        if th and theme_count.get(th, 0) >= theme_max:
            continue
        winners.append((code, mom, th))
        if th:
            theme_count[th] = theme_count.get(th, 0) + 1
    return winners


def equity_target_weight(cfg, score, confidence=None):
    """目标权益仓位。

    `equity_base_mode="fixed"`（默认，十年组合试错结论）：固定 `equity_base_fixed`
    （默认 0.60）——实验显示"评分驱动仓位"在样本外不如固定适度仓位；
    `equity_base_mode="score"`：eq_base + eq_slope×score（夹在 [eq_floor, eq_cap]），
    confidence(0~1) 为多信号共识度，分歧大时可向中性收缩。
    """
    st = cfg.get("strategy", {})
    if str(st.get("equity_base_mode") or "fixed") == "fixed":
        return util.clamp(float(st.get("equity_base_fixed", 0.60) or 0.60),
                          0.0, 1.0)
    base = float(st.get("eq_base", 0.60))
    slope = float(st.get("eq_slope", 0.005))
    lo = float(st.get("eq_floor", 0.0))
    hi = float(st.get("eq_cap", 0.95))
    raw = base + slope * float(score)
    conf = util.clamp(float(confidence) if confidence is not None else 1.0, 0.0, 1.0)
    if conf < 1.0:
        # 可选：多信号分歧时向中性收缩（借鉴 ai-hedge-fund 多观点投票）。
        # 实证：A股窗口收缩整体略降收益，故默认 confidence_shrink=1.0（不收缩）；
        # 如需更谨慎可调小（如 0.4 表示分歧时最多偏离中性 40%）。
        shrink = float(st.get("confidence_shrink", 1.0))
        shrink = util.clamp(shrink, 0.0, 1.0)
        if shrink < 1.0:
            raw = base + (raw - base) * (shrink + (1.0 - shrink) * conf)
    return util.clamp(raw, lo, hi)


def _risk_cfg(cfg):
    return cfg.get("strategy", {}).get("risk", {}) or {}


def _min_buy(cfg, code):
    it = settings.fund_item(cfg, code) or {}
    return max(float(cfg.get("strategy", {}).get("min_order_yuan", 10.0)),
               float(it.get("min_buy", 10.0)))


def _add(cfg, orders, msgs, code, action, amount, note):
    amount = float(int(amount))
    lo = _min_buy(cfg, code)
    if amount < lo:
        msgs.append("{}：{} {:.0f} 元低于起购点 {:.0f} 元，跳过".format(
            note, "申购" if action == "buy" else "赎回", amount, lo))
        return False
    orders.append({"code": code, "action": action,
                   "amount_yuan": amount, "note": note})
    return True


def _rank_by_factors(codes, fac, weights, eq_hold, names=None, heat_map=None):
    """多因子截面综合打分（借鉴多因子选股思路：动量/加速/低波动/趋势确认/防追高）。

    weights: {"mom":..,"mom5":..,"vol":..,"bias":..,"heat":..,"theme_heat":..}
    - mom  20日动量（正向）
    - mom5 近5日加速（正向）
    - vol  波动率（权重>0 表示偏好低波动）
    - bias 相对20日线乖离（趋势确认，正向）
    - heat RSI>75 的追高惩罚力度
    - theme_heat 短线题材热度（同花顺涨停概念+最强风口，近3日时间衰减；
      借鉴 Cailianpress-Feishu-Bot 的“主流题材/资金关注”维度。heat_map 缺失
      时恒为 +0 —— 回测/演示口径保持逐位不变）
    返回 [(code, mom, score)] 按 score 降序（持仓优先）。
    """
    facs = {c: fac[c] for c in codes if fac.get(c) and fac[c].get("mom") is not None}
    if not facs:
        return []

    def pct(key, rev=False):
        vals = {c: f[key] for c, f in facs.items() if f.get(key) is not None}
        if not vals:
            return {}
        order = sorted(vals.items(), key=lambda x: x[1], reverse=rev)
        n = len(order)
        return {k: (i / (n - 1) if n > 1 else 0.5)
                for i, (k, _) in enumerate(order)}

    mom_r = pct("mom")
    mom5_r = pct("mom5")
    vol_r = pct("vol", rev=True)   # 低波动 → 高分
    bias_r = pct("bias")
    w_mom = float(weights.get("mom", 0))
    w_m5 = float(weights.get("mom5", 0))
    w_vol = float(weights.get("vol", 0))
    w_bias = float(weights.get("bias", 0))
    heat = float(weights.get("heat", 0))
    w_th = float(weights.get("theme_heat", 0) or 0)
    names = names or {}
    heat_map = heat_map or {}
    out = []
    for c, f in facs.items():
        s = (w_mom * mom_r.get(c, 0.5) + w_m5 * mom5_r.get(c, 0.5) +
             w_vol * vol_r.get(c, 0.5) + w_bias * bias_r.get(c, 0.5))
        if heat and f.get("rsi") is not None and f["rsi"] > 75:
            s -= heat * min(1.0, (f["rsi"] - 75) / 25.0)
        if w_th and heat_map:
            th = _theme_of(names.get(c, ""))
            if th and heat_map.get(th):
                s += w_th * util.clamp(float(heat_map[th]), 0.0, 1.0)
        out.append((c, f["mom"], s))
    out.sort(key=lambda x: (-x[2], 0 if x[0] in eq_hold else 1))
    return out


def position_band(cfg, ctx):
    """**动态调整带**：由「市场制度状态 + 情绪位置」驱动，在基础仓位上加/减，±band_max。

    设计（按用户要求，替代“账户收益触发”的旧思路——账户收益是运气指标、不代表市场状态）：
      · 均线多头排列（制度温和上行）      → +50% 带宽
      · 低波动（波动率 < 60% 分位）        → +30% 带宽
      · 情绪分位 > 70%（偏热）             → +20% 带宽
      · 极端波动（≥90% 分位）              → −60% 带宽
      · 情绪分位 < 30%（偏冷）             → −20% 带宽
    上限由 `equity_hard_cap` 兜底（默认 0.80，**永远不满仓**）。
    返回 (band, note)。
    """
    st = (cfg or {}).get("strategy", {}) or {}
    bmax = float(st.get("position_band_max", 0.20) or 0.0)
    if bmax <= 0:
        return 0.0, "调整带已关闭"
    band, notes = 0.0, []
    ma_align = ctx.get("ma_align")
    vol_rank = ctx.get("vol_rank")
    micro_pct = ctx.get("micro_pct")
    if ma_align:
        band += bmax * 0.5
        notes.append("均线多头排列 +{:.0%}".format(bmax * 0.5))
    if vol_rank is not None:
        vr = float(vol_rank)
        if vr < float(st.get("band_vol_low", 0.60) or 0.6):
            band += bmax * 0.3
            notes.append("低波动({:.0%}分位) +{:.0%}".format(vr, bmax * 0.3))
        elif vr >= float(st.get("band_vol_extreme", 0.90) or 0.9):
            band -= bmax * 0.6
            notes.append("极端波动({:.0%}分位) −{:.0%}".format(vr, bmax * 0.6))
    if micro_pct is not None:
        mp = float(micro_pct)
        if mp > float(st.get("band_micro_hot", 0.70) or 0.7):
            band += bmax * 0.2
            notes.append("情绪偏热({:.0%}分位) +{:.0%}".format(mp, bmax * 0.2))
        elif mp < float(st.get("band_micro_cold", 0.30) or 0.3):
            band -= bmax * 0.2
            notes.append("情绪偏冷({:.0%}分位) −{:.0%}".format(mp, bmax * 0.2))
    band = util.clamp(band, -bmax, bmax)
    return band, "；".join(notes) or "无调整（制度/情绪中性）"


def plan(cfg, ctx):
    """ctx 字段见 engine._ctx；另加 score / funds_mom / names /
    fund_pnl {code:{value,cost,pnl_pct,sellable_mv,locked_mv}} / initial / peak_total。
    """
    st = cfg.get("strategy", {})
    rk = _risk_cfg(cfg)
    score = int(ctx.get("score", 0))
    cash = float(ctx.get("cash", 0.0))
    committed = float(ctx.get("committed", 0.0))
    eq_mv = float(ctx.get("eq_mv", 0.0))
    bond_mv = float(ctx.get("bond_mv", 0.0))
    total = float(ctx.get("total", cash + eq_mv + bond_mv)) or cash
    bond_code = ctx.get("bond_code")
    eq_hold = {c: v for c, v in (ctx.get("eq_hold_mv") or {}).items() if v > 0.01}
    sell_eq = dict(ctx.get("sell_eq_by_code") or {})
    locked_eq = dict(ctx.get("locked_eq_by_code") or {})
    sell_bond = float(ctx.get("bond_sellable_mv", 0.0))
    mom = dict(ctx.get("funds_mom") or {})
    vol = dict(ctx.get("funds_vol") or {})
    mom5 = dict(ctx.get("funds_mom5") or {})
    # 未执行/已提交订单的方向集合：反向挂单（昨日卖A未执行→今日又想买A 等）一律排除，
    # 避免同一只基金同日出现互相矛盾的买卖指令（详见 audit P0-5）。
    pend_sell = set(ctx.get("pending_sell_codes") or ())
    pend_buy = set(ctx.get("pending_buy_codes") or ())
    # 候选池：优先用引擎给出的**当日池**（回放里是 point-in-time 重建的池）。
    # 修 BUG（2026-09-11）：旧写法 `ctx.get("equity_codes") or 静态候选池` 会把
    # **空列表**当成"没给"，于是回放首日（预热期没有动量、池为空）退回静态 config 池，
    # 把 700 元买到了在该区间根本没有净值数据的基金上 → 订单永不成交 → `committed`
    # 永久占用全部现金 → 引擎此后无法加仓（实测 188/293 天报"可动用现金不足"，
    # 牛市区间只赚 1.4%，而指数 +40%）。现在只有"键缺失(None)"才回退静态池，
    # 空列表是有效信息（当日无合格标的 → 不买）。
    _pool_codes_ctx = ctx.get("equity_codes")
    if _pool_codes_ctx is None:
        eq_codes = [f["code"] for f in settings.equity_candidates(cfg)]
    else:
        eq_codes = list(_pool_codes_ctx)
    names = ctx.get("names") or {}
    nm_of = lambda c: names.get(c, c)
    fund_pnl = ctx.get("fund_pnl") or {}
    initial = float(ctx.get("initial")
                    or cfg.get("account", {}).get("initial_cash", 500))
    peak = float(ctx.get("peak_total") or 0)

    regime = float(st.get("regime_step", 0.12))
    floor = float(st.get("bond_buy_floor", 0.20))
    max_bond = float(st.get("max_bond_weight", 0.45))
    rotate_gap = float(st.get("rotate_gap", 0.08))
    top_n = max(1, int(st.get("top_n", 4)))
    theme_max = max(1, int(st.get("theme_max", 1)))
    min_mom = float(st.get("min_momentum", 0.0))
    market_mom = ctx.get("market_mom")  # 大盘(沪深300)20日动量，用于相对强弱
    relative = bool(st.get("relative_momentum", True))
    allow_early = bool(rk.get("allow_early_exit_fee", False))
    use_convert = bool(st.get("use_fund_convert", True))  # 基金转换一步完成
    allow_rearm_trade = bool(rk.get("allow_rearm_trade", True))  # 冷却期例外开关
    rearm_bull = int(rk.get("rearm_bull_score", 50))  # 例外：评分高于此值放行
    min_hold = int(st.get("min_hold_days", 7))
    mix = util.clamp(float(st.get("momentum_weight", 0.5)), 0.0, 1.0)

    stop_loss = float(rk.get("fund_stop_loss_pct", -0.08))
    tp_half = float(rk.get("fund_take_profit_pct", 0.10))
    tp_full = float(rk.get("fund_take_profit_full_pct", 0.25))
    pf_stop = float(rk.get("portfolio_stop_pct", -0.15))
    trail = float(rk.get("peak_trailing_pct", 0.10))
    reentry = int(rk.get("reentry_score", 25))
    defensive = bool(ctx.get("defensive"))

    orders, msgs = [], []
    portfolio_risk = False   # 组合止损 / 峰值回撤（会启动防御冷却）
    per_fund_risk = False    # 单只止损/止盈（当日允许继续调仓）
    risk_kind = None
    w_cap = None
    risk_touched = set()     # 今日已触发止损/止盈的基金：当日禁止再买、再卖（冷静到次日）
    risk_sell_amount = 0.0   # 今日单只风控卖出的金额合计（供“转换”同步买入使用）

    def sellable_or_all(info):
        """止损/止盈清仓金额：允许提前则全仓，否则只卖可卖部分。"""
        mv = float((info or {}).get("value", 0) or 0)
        sellable_mv = float((info or {}).get("sellable_mv", 0) or 0)
        locked_mv = float((info or {}).get("locked_mv", 0) or 0)
        if allow_early and locked_mv > 0.01:
            return mv
        return sellable_mv

    # ================= A. 风控层（止损 / 止盈） =================
    # 单遍判断（每只基金只执行一种止盈动作，避免“卖一半+再清仓”被同时生成）：
    #   浮亏 ≤ 止损线        → 清仓止损
    #   浮盈 ≥ 全部落袋线    → 全部赎回落袋为安
    #   浮盈 ≥ 卖一半线      → 赎回一半锁定利润
    for code, info in fund_pnl.items():
        pnl = info.get("pnl_pct")
        if pnl is None:
            continue
        if code in pend_buy:
            # 还有未执行的申购旧单：先让用户处理，避免“还没买成就触发止损卖”的悖论
            msgs.append("{} 存在未执行的申购建议，先处理旧单后再评估止损/止盈".format(
                nm_of(code)))
            continue
        locked_v = float((info or {}).get("locked_mv", 0) or 0)
        if pnl <= stop_loss:
            # A1 单只止损：清仓
            amt = sellable_or_all(info)
            if amt >= _min_buy(cfg, code):
                if _add(cfg, orders, msgs, code, "sell", amt,
                        "【止损】{} 浮亏 {:.1%}（≤{:.0%}），理性止损清仓".format(
                            nm_of(code), pnl, stop_loss)):
                    risk_touched.add(code)
                    per_fund_risk = True
                    risk_sell_amount += amt
            elif locked_v > 0.01:
                msgs.append("{} 止损：持仓不足{}天（{:.0f} 元）暂不可卖，解锁后立即执行".format(
                    nm_of(code), min_hold, locked_v))
        elif pnl >= tp_full:
            # A3 止盈-清仓：全部落袋为安
            amt = sellable_or_all(info)
            if amt >= _min_buy(cfg, code):
                if _add(cfg, orders, msgs, code, "sell", amt,
                        "【止盈落袋】{} 浮盈 {:.1%}（≥{:.0%}），全部赎回落袋为安".format(
                            nm_of(code), pnl, tp_full)):
                    risk_touched.add(code)
                    per_fund_risk = True
                    risk_sell_amount += amt
            elif locked_v > 0.01:
                msgs.append("{} 止盈落袋：持仓不足{}天（{:.0f} 元）暂不可卖，解锁后立即执行".format(
                    nm_of(code), min_hold, locked_v))
        elif pnl >= tp_half:
            # A2 止盈-一半：先锁定一半利润
            half = float(info.get("value", 0) or 0) * 0.5
            amt = min(half, sellable_or_all(info))
            if amt >= _min_buy(cfg, code):
                if _add(cfg, orders, msgs, code, "sell", amt,
                        "【止盈一半】{} 浮盈 {:.1%}（≥{:.0%}），先落袋一半利润".format(
                            nm_of(code), pnl, tp_half)):
                    risk_touched.add(code)  # 当日不再加买/重复卖出，次日重新评估
                    per_fund_risk = True
                    risk_sell_amount += amt
            elif locked_v > 0.01:
                msgs.append("{} 止盈一半：可卖份额不足（持仓不足{}天部分 {:.0f} 元），解锁后执行".format(
                    nm_of(code), min_hold, locked_v))
    # A4 组合级风控（反应型、可再入、带**迟滞**；用户口径："组合回撤 → 冷却 N 天"）
    #   ① 自**峰值**回撤 ≥ |portfolio_stop_pct| → 降权益至 25%，冷却 rearm_days 后自动解除
    #   ② 相对**本金**亏 ≥ |portfolio_stop_pct| 且评分 < reentry_score → 同样只冷却
    # 修 BUG（2026-09-11，两阶段）：
    #   * 旧实现只有②且不看评分 → 总资产跌破本金 85% 后该判据**天天**成立，
    #     永久停留在 25% 防守仓（实测 2019-05 触发后连续 7 年重复触发、权益压到 ~1%、
    #     全历史 −18.6%、仅 66 笔交易；同一引擎在牛市段却能赚 +14.5%）。
    #   * 只加"可再入"还不够：回撤类判据在账户回到峰值前也天天成立，于是冷却一到期
    #     就立刻重新触发。因此再加**迟滞**：触发后进入"未武装"状态，必须等回撤收敛到
    #     安全区（≤ 阈值 −5pp）才重新武装，避免永久锁死。
    rearm_days = int(rk.get("rearm_days", 5) or 0)
    dd_peak = (1.0 - total / peak) if peak and peak > 0 else 0.0
    armed = bool(ctx.get("risk_armed", True))
    risk_armed_after = armed
    if armed and dd_peak >= abs(pf_stop):
        w_cap = 0.25
        risk_kind = "portfolio_stop"
        msgs.append("【组合回撤】总资产 {:.2f} 自峰值 {:.2f} 回撤 {:.1%}（≥{:.0%}），"
                    "权益降至 25% 防守；{} 个交易日后自动解除，回撤收敛后重新武装".format(
                        total, peak, dd_peak, abs(pf_stop), rearm_days))
        portfolio_risk = True
        risk_armed_after = False
    elif armed and total <= initial * (1.0 + pf_stop) and score < reentry:
        w_cap = 0.25
        risk_kind = "portfolio_stop"
        msgs.append("【组合止损】总资产 {:.2f} 较本金 {:.2f} 亏 ≥{:.0%} 且评分 {:+d}<{}，"
                    "权益降至 25% 防守；{} 个交易日后自动解除".format(
                        total, initial, abs(pf_stop), score, reentry, rearm_days))
        portfolio_risk = True
        risk_armed_after = False
    elif armed and peak > initial and total <= peak * (1.0 - trail) and score < reentry:
        w_cap = 0.25
        risk_kind = "trailing"
        msgs.append("【峰值回撤】总资产自峰值 {:.2f} 回撤 ≥{:.0%} 且评分 {:+d}<{}，"
                    "跟踪止盈，权益降至 25%".format(peak, trail, score, reentry))
        portfolio_risk = True
        risk_armed_after = False
    elif not armed:
        # 未武装状态：回撤收敛到"阈值 −5pp"以内才重新武装（迟滞带）
        if dd_peak <= max(0.02, abs(pf_stop) - 0.05):
            risk_armed_after = True

    # 冷却期例外：处于防御期(rearm_days)内且非组合级风控触发当日，若今日再触发单只
    # 止损/止盈，或市场评分高于 rearm_bull_score（默认 50），则当日解除“只防守”限制，
    # 允许按常规规则直接交易（含经基金转换的加仓/轮动）。
    if not portfolio_risk and defensive and allow_rearm_trade and \
            (per_fund_risk or score > rearm_bull):
        why = ("触发单只止损/止盈" if per_fund_risk
               else "市场评分 {:+.0f} > {}".format(score, rearm_bull))
        msgs.append("【防御期例外】冷却期内{}，今日解除『只防守』限制，允许直接调仓"
                    "（评分高于 {} 或触发单只止损/止盈时照常执行，不强制等冷却结束）".format(
                        why, rearm_bull))
        defensive = False

    # 动态调整带：由「制度状态（均线多头/低波动）+ 情绪位置」驱动，±position_band_max
    # （按用户要求：**不用账户收益做触发**——收益是运气指标，不代表市场状态）
    band, band_note = position_band(cfg, ctx)
    band_max = max(1e-6, float((cfg.get("strategy") or {}).get(
        "position_band_max", 0.20) or 0.20))
    # 轮动灵活度由**制度**驱动：温和上行（带 >0）时把防抖门槛最多收窄一半
    rotate_gap_eff = rotate_gap * (1.0 - 0.5 * max(0.0, band) / band_max)
    # 锁定期内转换：仅对**已盈利 ≥ rotate_winner_gain 的持仓**放行（持仓级规则，
    # 不是账户级；用户 2026-09-11 明确要求）
    winner_gain = float((cfg.get("strategy") or {}).get(
        "rotate_winner_gain", 0.05) or 0.05)

    def _fund_gain(code):
        info = fund_pnl.get(code) or {}
        try:
            return float(info.get("pnl_pct"))
        except (TypeError, ValueError):
            return None

    if band:
        msgs.append("动态调整带 {:+.0%}（{}）".format(band, band_note))

    w_now = eq_mv / total if total else 0.0
    w_target = equity_target_weight(cfg, score, ctx.get("confidence"))
    if w_cap is not None:
        w_target = min(w_target, w_cap)
    if defensive:
        w_target = min(w_target, 0.25)
    # 【已降级为参考】预测型风险过滤（事件/行情模型 P(涨)）：默认关闭（README 5.6），
    # 开启时也只降不升
    overlay = ctx.get("risk_overlay") or {}
    try:
        o_scale = float(overlay.get("scale") or 1.0)
    except (TypeError, ValueError):
        o_scale = 1.0
    o_floor = float((cfg.get("strategy") or {}).get("risk_overlay_floor", 0.15) or 0.0)
    if overlay.get("action") != "off" and 0.0 < o_scale < 1.0:
        before = w_target
        w_target = max(w_target * o_scale,
                       o_floor if w_target > o_floor else w_target)
        msgs.append("风险过滤（参考，{}，{}）：权益目标仓位 {:.0%} → {:.0%}".format(
            overlay.get("action") or "neutral", overlay.get("reason") or "",
            before, w_target))
    # 牛市覆盖（可选开关，默认关闭）
    bull_floor = float(overlay.get("bull_floor") or 0.0)
    if overlay.get("action") == "bull_on" and bull_floor > 0:
        before = w_target
        w_target = max(w_target, min(1.0, bull_floor))
        if w_cap is not None:
            w_target = min(w_target, w_cap)
        if defensive:
            w_target = min(w_target, 0.25)
        if w_target > before + 1e-9:
            msgs.append("牛市覆盖（{}）：权益目标仓位 {:.0%} → {:.0%}".format(
                overlay.get("reason") or "低波动+均线多头", before, w_target))

    # ① 动态调整带上浮（制度+情绪驱动；硬上限兜底，永远不满仓）
    hard_cap = float((cfg.get("strategy") or {}).get("equity_hard_cap", 0.80) or 0.80)
    if band > 0:
        before = w_target
        w_target = min(w_target + band, hard_cap)
        if w_cap is not None:
            w_target = min(w_target, w_cap)
        if defensive:
            w_target = min(w_target, 0.25)
        if abs(w_target - before) > 1e-9:
            msgs.append("仓位带 +{:.0%}：权益目标 {:.0%} → {:.0%}（上限 {:.0%}）".format(
                band, before, w_target, hard_cap))
    elif band < 0:
        before = w_target
        w_target = max(0.0, w_target + band)
        msgs.append("仓位带 {:.0%}：权益目标 {:.0%} → {:.0%}".format(
            band, before, w_target))

    # ② 反应型风控（不预测）：跌破关键均线 → 减仓（每跌破一条减 step，封顶 max_cut，
    #    并保留 ma_break_floor 底仓——十年实测显示“减太狠”会把牛市收益砍掉）
    ma_break = ctx.get("ma_break") or {}
    if float(ma_break.get("cut") or 0) > 0:
        before = w_target
        floor = float(ma_break.get("floor") or 0.0)
        w_target = max(min(floor, before), w_target - float(ma_break["cut"]))
        msgs.append("【反应型风控】{}；权益目标 {:.0%} → {:.0%}（站回均线后自动恢复，"
                    "底仓 {:.0%}）".format(
                        ma_break.get("note") or "跌破关键均线", before, w_target, floor))
    # ③ 硬上限：任何路径都不超过 equity_hard_cap（防御冷却/组合风控仍可更低）
    w_target = min(w_target, hard_cap)
    if w_cap is not None:
        w_target = min(w_target, w_cap)
    if defensive:
        w_target = min(w_target, 0.25)

    if portfolio_risk:
        # 组合级风控当日：降到目标防守仓位后收手（进入冷却，由引擎锁定 rearm_days）。
        # 单只止损/止盈卖出已计入减仓，剩余需要再减的仓位减去 risk_sell_amount，避免卖过头。
        cut = max(0.0, eq_mv - risk_sell_amount - w_target * total)
        if cut > 0:
            holders = [c for c in sorted(eq_hold, key=lambda c: -sell_eq.get(c, 0.0))
                       if c not in risk_touched]
            for code in holders:
                if cut <= 0:
                    break
                if code in pend_buy:
                    continue  # 有未执行的申购旧单，暂不动
                a = min(cut, sell_eq.get(code, 0.0))
                if a >= _min_buy(cfg, code):
                    if _add(cfg, orders, msgs, code, "sell", a,
                            "【风控减仓】{} 降至 {:.0%} 权益仓位".format(
                                nm_of(code), w_target)):
                        cut -= a
            leftover = sum(v for c, v in locked_eq.items() if c not in risk_touched)
            if leftover > 0.01 and not allow_early:
                msgs.append("另有 {:.0f} 元不足{}天暂不可卖，解锁后继续执行风控减仓".format(
                    leftover, min_hold))
        msgs.append("风控触发：今日只防守不进攻（不加仓、不换基），进入 {} 个交易日防御冷却；"
                    "冷却期内仅当评分 >{} 或再触发单只止损/止盈时才允许直接调仓。".format(
                        int(rk.get("rearm_days", 7)), rearm_bull))
        return _result(orders, msgs, score, w_now, w_target, None, None,
                       True, False, risk_kind, risk_armed=risk_armed_after)

    if per_fund_risk:
        msgs.append("已触发单只止损/止盈并生成赎回指令：风控后按规则继续调仓；"
                    "满足加仓/轮动条件时将同步经『基金转换』换入强势板块，避免资金空窗。")

    # ================= B. 常规仓位与选基 =================
    # 目标：收益最大化 + 风险最小化平衡。
    # 1) 收益维度：按 20 日动量排序（强者恒强）；可选“风险调整 = 动量/波动率”。
    # 2) 门槛过滤：默认只配正动量（避免追跌）；可选“跑赢大盘”相对门槛。
    # 3) 风险维度：主题分散（不单押一个板块）+ 下方止损止盈/组合风控。
    # 4) 今日已止损/止盈清仓的基金从候选中剔除（避免当日被再次买入，尊重止损纪律）。
    threshold = float(market_mom) if (relative and market_mom is not None) else min_mom
    if relative and market_mom is not None:
        msgs.append("板块配置门槛：跑赢大盘（沪深300 20日动量 {:+.1%}）的板块才配置".format(market_mom))
    risk_adjusted = bool(st.get("risk_adjusted", False))
    factor_weights = st.get("factor_weights")  # 多因子权重（None → 纯动量）
    factors = ctx.get("funds_factors") or {}
    avail_eq = [c for c in eq_codes if c not in risk_touched]
    # 有未执行赎回旧单的基金不进入买入候选（避免与旧卖单方向相反）
    blocked = [c for c in avail_eq if c in pend_sell]
    avail_eq = [c for c in avail_eq if c not in pend_sell]
    for c in blocked:
        msgs.append("{} 存在未执行的赎回旧单，先处理旧单后再纳入买入候选".format(
            nm_of(c)))
    cand_codes = []
    for c in avail_eq:
        m = mom.get(c)
        if m is None or (threshold is not None and m < threshold):
            continue
        cand_codes.append(c)
    ranked = []
    if factor_weights:
        ranked = _rank_by_factors(cand_codes, factors, factor_weights, eq_hold,
                                  names=names, heat_map=ctx.get("theme_heat"))
    else:
        for c in cand_codes:
            m = mom.get(c)
            v = vol.get(c)
            key = (m / v if (v and v > 0) else m) if risk_adjusted else m
            ranked.append((c, m, key))
        ranked.sort(key=lambda x: (-x[2], 0 if x[0] in eq_hold else 1))
    winners = _pick_winners([(c, m) for c, m, _ in ranked], names, top_n, theme_max)
    if not winners and avail_eq:
        winners = [(avail_eq[0], mom.get(avail_eq[0]),
                    _theme_of(names.get(avail_eq[0], "")))]
    winner_codes = {c for c, _, _ in winners}
    winner_code = winners[0][0] if winners else None
    winner_mom = winners[0][1] if winners else None

    avail_cash = max(0.0, cash - committed)
    diff = w_target - w_now

    def amounts_for(total_amt, wlist):
        """按“动量加权 + 等权混合”把 total_amt 切分给 wlist（末位吃取整余数）。

        返回 [(code, mom, theme, amt_yuan)]。
        """
        n = len(wlist)
        if n <= 0 or total_amt <= 0:
            return []
        mws = [max(0.0, (m if m is not None else 0.0)) for _, m, _ in wlist]
        sw = sum(mws)
        eq = 1.0 / n
        if sw > 0:
            ws = [mix * m / sw + (1.0 - mix) * eq for m in mws]
        else:
            ws = [eq] * n  # 全零动量 → 等权兜底
        out, used = [], 0.0
        for i, ((c, m, th), w) in enumerate(zip(wlist, ws)):
            amt = int(total_amt - used) if i == n - 1 else int(total_amt * w)
            used += amt
            out.append((c, m, th, amt))
        return out

    if not defensive and diff >= regime:  # 加仓 → 分散买入综合组合
        need = diff * total
        sold_bond = 0.0
        short = need - avail_cash
        if bond_code and bond_code in pend_buy:
            msgs.append("债基 {} 存在未执行的申购旧单，暂不赎旧买新，先处理旧单".format(
                nm_of(bond_code)))
        elif short > 5.0 and sell_bond >= _min_buy(cfg, bond_code or ""):
            sold_bond = min(short, sell_bond)
            _add(cfg, orders, msgs, bond_code, "sell", sold_bond,
                 "为加仓股基筹措资金，赎回债基" +
                 ("（可与加仓单同步用『基金转换』一次完成）" if use_convert else ""))
        # 基金转换语义：当日止损/止盈赎回的资金视为即时可用（平台垫资转换），
        # 卖出与买入在同一交易日配对下单、T+1 同步确认，无“等资金到账”空窗。
        conv_extra = risk_sell_amount if use_convert else 0.0
        buy_total = min(need, avail_cash + sold_bond + conv_extra)
        if winners and buy_total >= _min_buy(cfg, winners[0][0]):
            for c, m, th, amt in amounts_for(buy_total, winners):
                if amt < _min_buy(cfg, c):
                    continue
                m5 = mom5.get(c)
                reason = _market_reason(th, m, market_mom)
                note = ("配置板块【{}】{}（权重 {:.0%}）；20日动量 {:+.1%}、近5日 {:+.1%}；{}".format(
                    th or "综合", nm_of(c), (amt / buy_total if buy_total else 0),
                    (m if m is not None else 0.0),
                    (m5 if m5 is not None else 0.0), reason))
                _add(cfg, orders, msgs, c, "buy", amt, note)
        else:
            msgs.append("可动用现金不足或无可用进攻标的，权益加仓顺延")
    elif diff <= -regime:  # 减仓
        cut = -diff * total
        holders = [c for c in sorted(eq_hold, key=lambda c: -sell_eq.get(c, 0.0))
                   if c not in risk_touched]
        for code in holders:
            if cut <= 0:
                break
            if code in pend_buy:
                continue  # 有未执行的申购旧单，先处理旧单
            a = min(cut, sell_eq.get(code, 0.0))
            if a >= _min_buy(cfg, code):
                held_m = mom.get(code)
                _add(cfg, orders, msgs, code, "sell", a,
                     "评分{:+d} 走弱，权益目标降至 {:.0%}（当前 {:.0%}）；{} 20日动量 {:+.1%}，"
                     "减仓降低市场风险".format(
                         score, w_target, w_now, nm_of(code),
                         (held_m if held_m is not None else 0.0)))
                cut -= a
        leftover = sum(v for c, v in locked_eq.items() if c not in risk_touched)
        if leftover > 0.01:
            msgs.append("股基 {:.0f} 元持仓不足{}天暂不可卖，解锁后再执行减仓".format(
                leftover, min_hold))

    # 轮动换基：卖出明显落后于“综合组合”的持仓，换入组合。
    # use_fund_convert=true 时，赎回与申购同日配对、交由平台『转换/超级转换』
    # 一次完成（免去“先赎回→资金到账→再买”的真空期）；否则仍拆两步走。
    rotation = False
    rot_proceeds = 0.0
    rot_sold = []
    if winners and not defensive and abs(diff) < regime and score >= -25:
        weakest = min((m for _, m, _ in winners if m is not None), default=None)
        holders = sorted(eq_hold, key=lambda c: -sell_eq.get(c, 0.0))
        for code in holders:
            if code in winner_codes or code in risk_touched:
                continue
            if code in pend_buy:
                msgs.append("{} 存在未执行的申购旧单，先处理旧单，本轮暂不轮动卖出".format(
                    nm_of(code)))
                continue
            sellable = sell_eq.get(code, 0.0)
            held_mom = mom.get(code)
            if sellable < _min_buy(cfg, code):
                locked_v = locked_eq.get(code, 0) or 0.0
                fund_gain = _fund_gain(code)
                # 收益 ≥ rotate_winner_gain 的持仓：在**温和上行制度**（调整带 >0）下
                # 放行锁定期内转换（触发条件是市场状态，不是账户收益）
                if (locked_v > 0.01 and fund_gain is not None
                        and fund_gain >= winner_gain and band > 0):
                    sellable = eq_hold.get(code, 0.0)
                    msgs.append("{} 已盈利 {:+.1%}（≥{:.0%}）且市场处温和上行（调整带 {:+.0%}）："
                                "放行锁定期内转换（{} 元）".format(
                                    nm_of(code), fund_gain, winner_gain, band,
                                    sellable))
                elif locked_v > 0.01:
                    msgs.append("{} 尚有 {:.0f} 元不足{}天，暂不能参与轮动".format(
                        nm_of(code), locked_v, min_hold))
                    continue
                else:
                    continue
            if sellable < _min_buy(cfg, code):
                continue
            # 防抖：持仓动量与组合内最弱相差不足 rotate_gap 时不轻易换，明显落后才轮动
            # （收益越高，rotate_gap_eff 越小 → 轮动越灵活，见“脱离规则权重”）
            if weakest is not None and held_mom is not None and \
                    held_mom >= weakest - rotate_gap_eff:
                msgs.append("{} 不在组合内但动量接近组合最弱，暂不换仓".format(nm_of(code)))
                continue
            if _add(cfg, orders, msgs, code, "sell", sellable,
                    "轮动换基：{} 20日动量 {:+.1%} 落后组合内最弱（top{} 板块），"
                    "赎回换入领涨板块".format(
                        nm_of(code), (held_mom if held_mom is not None else 0.0), top_n)):
                rot_proceeds += sellable
                rot_sold.append(nm_of(code))
                rotation = True
        if rotation:
            combo = "、".join(th or nm_of(c) for c, _, th in winners)
            if use_convert:
                bought = 0.0
                for c, m, th, amt in amounts_for(rot_proceeds, winners):
                    if amt < _min_buy(cfg, c):
                        continue
                    note = ("【基金转换】赎回资金直接转入 {}（{}）；20日动量 {:+.1%}。"
                            "操作：在支付宝/天天基金对旧持仓点『转换/超级转换』→ 目标 {}，"
                            "转出与转入按同一交易时点确认，一步完成、无需等待赎回资金到账。"
                            "（若平台不支持该对基金转换，请按两步执行：先赎回，到账后录入申购）".format(
                                nm_of(c), c, (m if m is not None else 0.0), c))
                    if _add(cfg, orders, msgs, c, "buy", amt, note):
                        bought += amt
                if bought <= 0:
                    msgs.append("轮动赎回金额不足以申购新标的（低于起购点），资金先入现金，"
                                "下一交易日盘后再评估")
                else:
                    msgs.append("换基采用『基金转换』一步完成（约 {:.0f} 元：{} → {}），"
                                "免去“先赎回→等资金到账→再买”的空窗期；"
                                "赎回与申购指令同日落账，确认后可同时录入成交。".format(
                                    bought, "、".join(rot_sold), combo))
            else:
                msgs.append("赎回资金到账后（债基约 T+1、权益基金一般 T+1~T+3，以平台为准），"
                            "请回网页录入成交；AI 会在下一交易日盘后建议买入板块：{}。".format(combo))
    elif winners and not defensive and abs(diff) < regime and score >= -25 and \
            eq_hold and any(c not in eq_hold for c in winner_codes):
        combo = "、".join(th or nm_of(c) for c, _, th in winners)
        msgs.append("观察中：板块组合 {} 相对领先但总仓位未触发调仓，暂不动。".format(combo))

    # 债基/现金打理：现金是“再进攻的流动性缓冲”（债基同样受 7 天锁仓约束，
    # 过早转入债基会卡住再买入权益的资金），因此只在现金确实冗余且债基仍有
    # 容量时小额转入，其余保留现金等待权益机会。防御冷却期只保留现金。
    buys = sum(o["amount_yuan"] for o in orders if o["action"] == "buy")
    sells = sum(o["amount_yuan"] for o in orders if o["action"] == "sell")
    est_cash = avail_cash - buys + sells
    if defensive:
        if est_cash > 0.05 * total:
            msgs.append("防御期：保留现金等待冷却结束，暂不增配债基/权益")
    elif score >= -25 and not rotation:
        cap_room = max(0.0, max_bond * total - bond_mv)
        extra = est_cash - floor * total
        if extra >= _min_buy(cfg, bond_code or "") and extra <= cap_room:
            _add(cfg, orders, msgs, bond_code, "buy", extra,
                 "现金比例偏高(>{}%)，买入债基生息，保留 {:.0%} 机动".format(
                     int(floor * 100), floor))
        elif est_cash > floor * total and cap_room < _min_buy(cfg, bond_code or ""):
            msgs.append("债基已达目标上限 {:.0%}，暂不追加".format(max_bond))
    elif score < -25:
        if est_cash > 0.15 * total:
            msgs.append("观点偏空（评分{:+d}），保留现金等待企稳，不增配债基".format(score))

    winners_out = [{"code": c, "name": nm_of(c), "mom": m, "theme": th}
                   for c, m, th in winners]
    return _result(orders, msgs, score, w_now, w_target, winner_code,
                   winner_mom, bool(per_fund_risk or portfolio_risk),
                   rotation, risk_kind, winners_out,
                   risk_armed=risk_armed_after)


def _result(orders, msgs, score, w_now, w_target, winner_code, winner_mom,
            risk_action, rotation, risk_kind=None, winners=None,
            risk_armed=None):
    return {
        "orders": orders,
        "msgs": msgs,
        "score": score,
        "w_now": round(w_now, 4),
        "w_target": round(w_target, 4),
        "winner_code": winner_code,
        "winner_mom": (round(winner_mom, 4) if winner_mom is not None else None),
        "winners": winners or [],
        "rotation": bool(rotation),
        "risk_action": bool(risk_action),
        "risk_kind": risk_kind,
        # 组合级风控的"迟滞"状态：False = 未武装（须等回撤收敛才允许再次触发）
        "risk_armed": risk_armed,
    }
