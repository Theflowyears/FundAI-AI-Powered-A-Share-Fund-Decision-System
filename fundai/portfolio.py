# -*- coding: utf-8 -*-
"""约束组合构建：把入选基金变成可执行的目标权重与金额。

为什么单独一层
--------------
`strategy.plan` 原先用“动量加权 + 等权混合”切分新钱，权重是启发式。真实引擎
四窗口实测（README §25）显示：

* 等权/逆波动在「全历史 + 近 12 个月」都优于动量加权；
* 同主题过度集中会显著放大回撤，主题上限 1 比放宽到 2/3 更稳；
* min_var 与等权的差距在噪声内，不能为了“更复杂”而默认它。

因此这一层只做「给定候选、预算与约束，输出权重/金额」，不改变选基、不预测、
不引入新依赖。所有方法都可被配置切回 legacy（继续走旧动量加权），便于回退。
"""
METHODS = ("equal", "inv_vol", "min_var")


def _as_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _norm(raw):
    vals = [max(0.0, _as_float(x)) for x in raw]
    s = sum(vals)
    if s <= 0:
        return None
    return [x / s for x in vals]


def _equal(n):
    return [1.0 / n] * n if n > 0 else []


def _cap_weights(weights, caps, themes, max_theme):
    """在单基金上限与同主题上限下做投影，返回 (weights, notes)。

    迭代「裁掉超限 → 把释放出来的权重按剩余容量分给未饱和的基金」。
    若主题上限不可行（例如候选全部同一主题），放宽主题上限并在 notes 里说明，
    而不是悄悄给出违反约束的权重。
    """
    n = len(weights)
    if n == 0:
        return [], []
    base = list(weights)
    notes = []

    def _project(theme_cap):
        x = list(base)   # 每次都从原始权重出发：否则“放宽主题上限”的第二次
                         # 投影会站在上一次已裁剪的结果上，给出偏低的分散度
        for _ in range(200):
            before = list(x)
            # 1) 单基金上限
            for i in range(n):
                if x[i] > caps[i] + 1e-12:
                    x[i] = caps[i]
            # 2) 同主题合计上限
            groups = {}
            for i, th in enumerate(themes):
                if th:
                    groups.setdefault(th, []).append(i)
            for th, idx in groups.items():
                s = sum(x[i] for i in idx)
                if s > theme_cap + 1e-12:
                    scale = theme_cap / s
                    for i in idx:
                        x[i] *= scale
            # 3) 把不足 1 的部分按剩余容量再分配
            s = sum(x)
            if s < 1.0 - 1e-12:
                free = []
                for i in range(n):
                    if x[i] >= caps[i] - 1e-12:
                        continue
                    th = themes[i]
                    if th:
                        used = sum(x[j] for j in groups.get(th, []))
                        if used >= theme_cap - 1e-12:
                            continue
                    free.append(i)
                if not free:
                    break
                room = [max(0.0, caps[i] - x[i]) for i in free]
                room_sum = sum(room)
                if room_sum <= 0:
                    break
                need = 1.0 - s
                for i, r in zip(free, room):
                    x[i] += need * r / room_sum
            elif s > 1.0 + 1e-12:
                x = [v / s for v in x]
            if max(abs(a - b) for a, b in zip(x, before)) < 1e-12:
                break
        return _norm(x) or _equal(n)

    w = _project(max_theme)
    # 检查主题约束是否实际满足（全同主题时会不可行）
    if max_theme < 1.0:
        groups = {}
        for i, th in enumerate(themes):
            if th:
                groups.setdefault(th, []).append(i)
        bad = [th for th, idx in groups.items()
               if sum(w[i] for i in idx) > max_theme + 1e-6]
        if bad:
            w = _project(1.0)
            notes.append("同主题上限 {:.0%} 不可行（候选集中在 {}），"
                         "已放宽该约束".format(max_theme, "、".join(bad)))
    return w, notes


def _cov_from_rets(rets_list, shrink=0.5):
    """等长收益序列 -> 收缩协方差（非对角×shrink，对角加微小抖动）。"""
    n = len(rets_list)
    if n == 0:
        return None
    k = min(len(r) for r in rets_list)
    if k < 20:
        return None
    xs = [r[-k:] for r in rets_list]
    mus = [sum(r) / k for r in xs]
    cov = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i, n):
            v = sum((xs[i][t] - mus[i]) * (xs[j][t] - mus[j])
                    for t in range(k)) / k
            if i != j:
                v *= shrink
            cov[i][j] = cov[j][i] = v
        cov[i][i] += 1e-10
    return cov


def _min_var(cov):
    """长仓、权重和为 1 的最小方差组合（投影梯度，纯标准库）。"""
    n = len(cov)
    # 把协方差按对角均值归一后再下降：日收益协方差量级 ~1e-4，
    # 不缩放的话固定步长要几万步才收敛，1200 步等于没优化。
    diag = [cov[i][i] for i in range(n)]
    scale = sum(diag) / n
    if scale <= 0:
        return _equal(n)
    c = [[cov[i][j] / scale for j in range(n)] for i in range(n)]
    w = _equal(n)
    for _ in range(1200):
        grad = [sum(c[i][j] * w[j] for j in range(n)) for i in range(n)]
        nxt = [max(0.0, w[i] - 0.5 * grad[i]) for i in range(n)]
        s = sum(nxt)
        if s <= 0:
            return _equal(n)
        nw = [x / s for x in nxt]
        if max(abs(a - b) for a, b in zip(nw, w)) < 1e-12:
            w = nw
            break
        w = nw
    return w


def _raw_weights(method, codes, factors):
    n = len(codes)
    if n == 0:
        return [], "无候选"
    if method == "equal":
        return _equal(n), "等权"
    if method == "inv_vol":
        vols = []
        for c in codes:
            v = _as_float((factors.get(c) or {}).get("vol"), 0.0)
            vols.append(v if v > 1e-8 else None)
        if all(v is None for v in vols):
            return _equal(n), "逆波动（波动数据缺失→等权）"
        # 缺波动的基金按其余基金的中位波动兜底，避免直接掉出组合
        known = sorted(v for v in vols if v is not None)
        med = known[len(known) // 2] if known else 1e-4
        iv = [1.0 / (v if v is not None else med) for v in vols]
        w = _norm(iv)
        if w is None:
            return _equal(n), "逆波动（权重退化→等权）"
        return w, "逆波动"
    if method == "min_var":
        rets = [(factors.get(c) or {}).get("rets") for c in codes]
        if any(not r or len(r) < 20 for r in rets):
            return _raw_weights("inv_vol", codes, factors)
        cov = _cov_from_rets(rets)
        if cov is None:
            return _raw_weights("inv_vol", codes, factors)
        return _min_var(cov), "最小方差（收缩协方差）"
    return _equal(n), "未知方法→等权"


def _split_amounts(weights, codes, amount, min_buy):
    """把整数预算按权重切成整数元；低于起购点的标的剔除后重新归一。"""
    amount = int(round(_as_float(amount)))
    if amount <= 0 or not codes:
        return {}, [], "预算为 0"
    codes = list(codes)
    w = list(weights)
    min_buy = {c: max(1.0, _as_float(min_buy.get(c), 1.0)) for c in codes}
    if amount < sum(min_buy.values()):
        return {}, [], "预算 {:.0f} 元低于全部候选起购点合计 {:.0f} 元".format(
            amount, sum(min_buy.values()))
    for _ in range(len(codes) + 1):
        amts = {}
        used = 0
        for i, c in enumerate(codes):
            if i == len(codes) - 1:
                amts[c] = max(0, amount - used)
            else:
                amts[c] = max(0, int(amount * w[i]))
                used += amts[c]
        low = [c for c in codes if 0 < amts[c] < min_buy[c]]
        if not low:
            return amts, codes, ""
        keep = [c for c in codes if c not in low]
        if not keep:
            return {}, [], "所有候选分配额都低于起购点"
        keep_w = _norm([w[codes.index(c)] for c in keep])
        if keep_w is None:
            return {}, [], "权重退化"
        codes, w = keep, keep_w
    return {}, [], "无法满足起购点"


def allocate(cfg, codes, factors=None, themes=None, min_buy=None, amount=0.0,
             method=None):
    """给入选基金分配目标权重与金额。

    参数
    ----
    cfg       : 配置（读 strategy.portfolio_method / max_fund_weight /
                max_theme_weight / min_order_yuan）
    codes     : 已排序的入选基金代码（顺序即确定性排序基础）
    factors   : {code: {"vol": 日波动, "rets": [日收益...]}}；缺失时回退等权
    themes    : {code: 主题名}；同主题合计受 max_theme_weight 约束
    min_buy   : {code: 起购金额}；缺省用 strategy.min_order_yuan
    amount    : 本轮可分配金额（元）；<=0 时只返回权重
    method    : equal / inv_vol / min_var；缺省读配置，再缺省 equal

    返回
    ----
    dict: ok / method / weights / amounts / ex_ante_vol / hhi / effective_n /
          notes / reason
    """
    cfg = cfg or {}
    st = cfg.get("strategy") or {}
    factors = factors or {}
    themes = themes or {}
    codes = [c for c in (codes or []) if c]
    n = len(codes)
    method = str(method or st.get("portfolio_method") or "equal").lower()
    if method not in METHODS:
        return {"ok": False, "method": method, "weights": {}, "amounts": {},
                "reason": "未知组合方法：{}".format(method), "notes": []}
    if n == 0:
        return {"ok": False, "method": method, "weights": {}, "amounts": {},
                "reason": "无候选", "notes": []}

    raw, why = _raw_weights(method, codes, factors)
    max_fund = _as_float(st.get("max_fund_weight"), 0.45)
    max_fund = max(max_fund, 1.0 / n) if n > 1 else 1.0
    max_fund = min(1.0, max(0.05, max_fund))
    max_theme = _as_float(st.get("max_theme_weight"), 0.70)
    max_theme = min(1.0, max(max_theme, 1.0 / n)) if n > 1 else 1.0
    caps = [max_fund] * n
    th_list = [themes.get(c) for c in codes]
    weights, notes = _cap_weights(raw, caps, th_list, max_theme)
    if any(abs(a - b) > 1e-9 for a, b in zip(weights, raw)):
        notes.append("已按单基金≤{:.0%}、同主题≤{:.0%} 约束调整".format(
            max_fund, max_theme))
    notes.insert(0, why)

    wmap = {c: w for c, w in zip(codes, weights)}
    # 事前波动：有协方差用协方差，否则用对角（加权波动）近似
    ex_vol = None
    rets = [(factors.get(c) or {}).get("rets") for c in codes]
    if all(r and len(r) >= 20 for r in rets):
        cov = _cov_from_rets(rets)
        if cov:
            var = sum(weights[i] * weights[j] * cov[i][j]
                      for i in range(n) for j in range(n))
            ex_vol = (max(0.0, var) ** 0.5) * (252.0 ** 0.5)
    if ex_vol is None:
        vols = [_as_float((factors.get(c) or {}).get("vol"), 0.0) for c in codes]
        if all(v > 0 for v in vols):
            var = sum((weights[i] * vols[i]) ** 2 for i in range(n))
            ex_vol = (max(0.0, var) ** 0.5) * (252.0 ** 0.5)
    hhi = sum(w * w for w in weights)
    min_buy = min_buy or {}
    default_min = _as_float(st.get("min_order_yuan"), 10.0)
    amounts, kept, why_amt = _split_amounts(
        weights, codes, amount,
        {c: max(default_min, _as_float(min_buy.get(c), default_min))
         for c in codes})
    if amount and amount > 0 and not amounts:
        return {"ok": False, "method": method, "weights": wmap, "amounts": {},
                "ex_ante_vol": ex_vol, "hhi": hhi,
                "effective_n": (1.0 / hhi if hhi > 0 else None),
                "reason": why_amt or "无法分配", "notes": notes}
    if amounts:
        # 金额是最终可执行口径；把因取整/起购点产生的偏差反映到展示权重上
        total_amt = sum(amounts.values())
        if total_amt > 0:
            wmap = {c: amounts[c] / total_amt for c in amounts}
    return {"ok": True, "method": method, "weights": wmap,
            "amounts": {c: int(v) for c, v in (amounts or {}).items()},
            "ex_ante_vol": ex_vol, "hhi": hhi,
            "effective_n": (1.0 / hhi if hhi > 0 else None),
            "reason": "", "notes": notes}
