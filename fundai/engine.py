# -*- coding: utf-8 -*-
"""引擎：数据 → 研判 → 决策 → 记账 的调度中心。

- run_daily()：真实账号每日例行
    * exec_mode=manual：AI 只输出“今日建议”（订单挂起），由人执行后录入；
    * exec_mode=auto：模拟自动按 T+1 净值成交；
- simulate()：在给定交易日序列上回放同一引擎（回测/演示共用）
- backtest() / run_demo()：包装 simulate
- state_payload() / fund_status()：供 Web 界面取数
"""
import math

from . import analysis, indicators, news as newsmod, screening, settings, strategy, util
from .datasource import Market
from .ledger import Ledger
from .util import DataError

HALF_YEAR_DAYS = 183
POOL_STATE_FILE = util.data_file("pool_state.json")

# 各大板块代表基金（用于每日“板块全景”评价）：每个板块取一只代表性 C 类联接基金
SECTOR_PROXIES = [
    ("军工", "002199"),
    ("半导体芯片", "008888"),
    ("科创50", "011609"),
    ("证券", "012700"),
    ("人工智能", "008586"),
    ("计算机软件", "001630"),
    ("通信", "007818"),
    ("电子", "001618"),
    ("银行", "006697"),
    ("黄金", "002611"),
    ("医药", "007077"),
    ("科技", "007874"),
    ("医疗", "012323"),
    ("房地产", "004643"),
    ("交通运输", "019405"),
    ("公用事业", "027738"),
    ("商业航天", "024195"),
    ("沪深300", "005918"),
]


def combine_score(cfg, tech, news_score):
    """量化分(tech) 与 消息分(news) 按 news_weight 权重合成最终评分。"""
    w = float((cfg.get("strategy", {}) or {}).get("news_weight", 0.35))
    if news_score is None:
        return int(round(tech))
    return int(round(tech * (1.0 - w) + float(news_score) * w))


class Engine:
    def __init__(self, cfg, ledger, market=None, demo=False):
        self.cfg = cfg
        self.ledger = ledger
        self.market = market or Market(cfg)
        self.demo = demo
        self.st = cfg.get("strategy", {})
        self.acct = cfg.get("account", {})
        self.min_hold = int(self.st.get("min_hold_days", 7))
        self.risk_cfg = self.st.get("risk", {}) or {}
        self._pool_state = None      # 动态筛选结果（data/pool_state.json）
        self._pool_override = None   # 临时强制用 config 池（演示回放等）
        self.screen = None           # 消息人工筛选库（惰性创建）

    # ---------------- 账户元信息 ----------------
    def ensure_account(self):
        meta = self.ledger.account_meta() or {}
        changed = False
        if not meta.get("start_date"):
            meta["start_date"] = util.today_str()
            meta["end_date"] = util.add_days(util.today_str(), HALF_YEAR_DAYS)
            changed = True
        if meta.get("target_value") is None:
            meta["target_value"] = float(self.acct.get("target_value", 1500))
            changed = True
        if meta.get("initial_cash") is None:
            meta["initial_cash"] = float(self.acct.get("initial_cash", 1000))
            changed = True
        meta["name"] = self.acct.get("name", "AI基金1000挑战1500实验")
        meta["exec_mode"] = self.acct.get("exec_mode", "manual")
        if changed or meta != (self.ledger.account_meta() or {}):
            self.ledger.set_account_meta(meta)
        return meta

    def account(self):
        return self.ledger.account_meta() or self.ensure_account()

    # ---------------- 基础工具 ----------------
    def _screen(self):
        """消息人工筛选/学习库（惰性创建，回测/演示路径不触碰）。"""
        if self.screen is None:
            self.screen = screening.ScreeningStore()
        return self.screen

    def _idx_name(self):
        return (self.cfg.get("market", {}).get("index", {}) or {}).get("name", "沪深300")

    # ---- 动态备选池层（config 池为兜底；pool_state.json 为动态筛选结果） ----
    def _load_pool_state(self):
        if self._pool_state is None:
            js = util.load_json(POOL_STATE_FILE)
            items = (js or {}).get("items")
            self._pool_state = js if isinstance(items, list) and items else None
        return self._pool_state

    def _save_pool_state(self, items, source="dynamic", extra=None):
        js = {"updated": util.now_iso(), "source": source, "items": items}
        if extra:
            js.update(extra)
        util.save_json(POOL_STATE_FILE, js)
        self._pool_state = js

    def pool_source(self):
        if self.demo or self._pool_override:
            return "config"
        st = self._load_pool_state()
        return (st or {}).get("source", "config")

    def pool_updated(self):
        if self.demo or self._pool_override:
            return None
        st = self._load_pool_state()
        return (st or {}).get("updated")

    def _pool_items(self):
        if self._pool_override:
            return self._pool_override
        if not self.demo:
            st = self._load_pool_state()
            if st and st.get("items"):
                return st["items"]
        return settings.pool_of(self.cfg)

    def pool_item(self, code):
        for f in self._pool_items():
            if f.get("code") == code:
                return f
        return settings.fund_item(self.cfg, code)

    def _name_of(self, code):
        it = self.pool_item(code)
        return (it or {}).get("name") or code

    def _pool_codes(self):
        return [f["code"] for f in self._pool_items()]

    def _eq_codes(self):
        return [f["code"] for f in self._pool_items()
                if f.get("kind") == "equity"]

    def _bond_code(self):
        for f in self._pool_items():
            if f.get("kind") == "bond":
                return f["code"]
        b = settings.bond_item(self.cfg)
        return b["code"] if b else None

    def _kind(self, code):
        it = self.pool_item(code)
        return it.get("kind") if it else "equity"

    def _rate_of(self, code):
        item = self.pool_item(code) or {}
        buy = float(item.get("buy_rate", 0) or 0)
        lt7 = float(item.get("sell_rate_lt7d",
                             self.cfg.get("fees", {}).get("sell_lt7d_default", 0.015)))
        ge7 = float(item.get("sell_rate_ge7d",
                             self.cfg.get("fees", {}).get("sell_ge7d_default", 0.0)))
        return {"buy": buy, "lt7": lt7, "ge7": ge7}

    def _sell_rate_fn(self, code):
        r = self._rate_of(code)
        return lambda days: r["lt7"] if days < self.min_hold else r["ge7"]

    # ---------------- 动态备选池筛选 ----------------
    def refresh_pool(self, force=False):
        """从 universe 候选中按 20 日动量重建备选池。

        保证：总数 >= screening.min_total(默认8)；当前持仓基金与债基永远保留；
        只替换“非持仓”的进攻候选。
        """
        sc = settings.screening_of(self.cfg)
        if not sc.get("enabled") and not force:
            return {"ok": False, "message": "screening.enabled=false，未开启动态筛选（可 --force）"}
        if not (self.market.akshare_on or self.market.token):
            return {"ok": False,
                    "message": "动态筛选需要可用数据源（akshare 或 config.json → data.zhitu_token）"}
        uni = settings.universe_of(self.cfg)
        if not uni:
            uni = [{"code": c, "name": self._name_of(c)} for c in self._eq_codes()]
        min_total = max(1, int(sc.get("min_total", 8)))
        top_eq = max(1, int(sc.get("top_equity", 8)))
        need_days = int(sc.get("min_history_days", 120))
        usage = self.market.usage()
        limit = usage["daily_limit"]
        base_calls = usage["calls_today"]
        if limit is not None and len(uni) > limit - base_calls - 10:
            return {"ok": False,
                    "message": "今日智兔配额不足（已用 {}/{}/日），请明天再筛选".format(
                        base_calls, limit)}
        # 债基（config 池 + 当前持仓）始终保留
        bonds = {}
        for f in list(self._pool_items()) + settings.pool_of(self.cfg):
            if f.get("kind") == "bond":
                bonds[f["code"]] = f
        held = self.ledger.positions()
        rows = []
        today = util.parse_d(util.today_str())
        for u in uni:
            code = u["code"]
            try:
                seq = self.market.fund_history(code)
            except DataError:
                continue
            if not seq:
                continue
            last_d = util.parse_d(seq[-1][0])
            if (today - last_d).days > 20:
                continue  # 疑似停更/新成立异常，跳过
            if (last_d - util.parse_d(seq[0][0])).days < need_days:
                continue
            closes = [n for _, n in seq]
            if len(closes) < 22:
                continue
            b = closes[-22]
            mom = (closes[-1] / b - 1.0) if b else None
            if mom is None:
                continue
            rows.append({"code": code, "name": u.get("name") or code,
                         "mom20": mom, "last_date": seq[-1][0]})
        rows.sort(key=lambda r: -r["mom20"])
        chosen, have = [], set()
        theme_count = {}

        # 同主题最多 2 只（复用 strategy 的统一主题分组，覆盖军工/半导体/证券/银行/
        # 黄金/医药/科技/医疗/房地产/交通/公用/商业航天等全部板块），
        # 池扩大到 14 只权益后仍需保持板块分散，避免动量池过度集中单一板块。
        for r in rows:
            if len(chosen) >= top_eq:
                break
            th = strategy._theme_of(r["name"])
            if th and theme_count.get(th, 0) >= 2:
                continue
            chosen.append(r)
            have.add(r["code"])
            if th:
                theme_count[th] = theme_count.get(th, 0) + 1
        # 仍不足 top_eq 时放宽主题限制补足
        for r in rows:
            if len(chosen) >= top_eq:
                break
            if r["code"] not in have:
                chosen.append(r)
                have.add(r["code"])
        # 保护：当前持有但不在 Top 的权益基金（人工模式可能正在持有）
        for code in held:
            if code in have or self._kind(code) == "bond":
                continue
            extra = next((r for r in rows if r["code"] == code), None)
            if extra:
                chosen.append(extra)
            else:
                chosen.append({"code": code, "name": self._name_of(code),
                               "mom20": None, "last_date": None})
            have.add(code)
        # 兜底：总量不足 min_total 时按动量顺延补齐
        for r in rows:
            if len(chosen) + len(bonds) >= min_total:
                break
            if r["code"] not in have:
                chosen.append(r)
                have.add(r["code"])

        def finalize(it, kind):
            code = it["code"]
            old = self.pool_item(code) or {}
            base = {"code": code, "name": it.get("name") or code, "kind": kind,
                    "role": ("primary" if kind == "bond" else
                             (old.get("role") or "attack")),
                    "buy_rate": 0.0, "sell_rate_lt7d": 0.015,
                    "sell_rate_ge7d": 0.0, "min_buy": 10.0,
                    "mom20": it.get("mom20")}
            for k in ("buy_rate", "sell_rate_lt7d", "sell_rate_ge7d",
                      "min_buy", "role", "name"):
                if k in old and old.get(k) is not None:
                    base[k] = old[k]
            return base

        items = [finalize(r, "equity") for r in chosen]
        items += [finalize(dict(b), "bond") for b in bonds.values()]
        if len(items) < min_total:
            for f in settings.pool_of(self.cfg):
                if len(items) >= min_total:
                    break
                if all(i["code"] != f["code"] for i in items):
                    items.append(dict(f, mom20=None))
        items.sort(key=lambda i: (0 if i["kind"] == "bond" else 1,
                                  -(i.get("mom20") if i.get("mom20") is not None else -9)))
        self._save_pool_state(items, source="dynamic",
                              extra={"universe": len(uni)})
        top3 = [{"code": r["code"], "name": r["name"],
                 "mom20": round(r["mom20"], 4)} for r in chosen[:3]]
        return {
            "ok": True,
            "count": len(items),
            "equity": sum(1 for i in items if i["kind"] == "equity"),
            "bond": sum(1 for i in items if i["kind"] == "bond"),
            "updated": self.pool_updated(),
            "top": top3,
            "message": "已从 {} 只候选中重建备选池（共 {} 只：权益 {} + 债基 {}，含持仓保护）".format(
                len(uni), len(items),
                sum(1 for i in items if i["kind"] == "equity"),
                sum(1 for i in items if i["kind"] == "bond")),
        }

    def _maybe_auto_refresh(self):
        """每日运行前：池过期或不足 8 只时自动重建（配额不足则顺延并提示）。"""
        sc = settings.screening_of(self.cfg)
        if self.demo or not sc.get("enabled"):
            return None
        updated = self.pool_updated()
        stale = True
        if updated:
            try:
                d0 = util.parse_d(updated[:10])
                stale = (util.parse_d(util.today_str()) - d0).days >= \
                    int(sc.get("refresh_days", 7))
            except Exception:
                stale = True
        if not stale and len(self._pool_items()) >= int(sc.get("min_total", 8)):
            return None
        usage = self.market.usage()
        uni_n = len(settings.universe_of(self.cfg))
        if usage["daily_limit"] is not None and \
                usage["daily_limit"] - usage["calls_today"] - uni_n - 20 < 0:
            self.market.warnings.append(
                "备选池需要刷新，但今日智兔配额不足，将自动顺延到明天")
            return None
        try:
            res = self.refresh_pool()
            if res.get("ok"):
                self.market.warnings.append("备选池已自动重建：" + res.get("message", ""))
            return res
        except DataError as e:
            self.market.warnings.append("备选池自动重建失败：" + str(e))
            return None

    def _fund_nav_map(self, code, lo_date):
        try:
            seq = self.market.fund_history(code, need_from=lo_date)
        except DataError:
            return {}
        return {d: nav for d, nav in seq}

    def _nav_on(self, code, d):
        return self._fund_nav_map(code, util.add_days(d, -10)).get(d)

    # ---------------- 组合估值与结构 ----------------
    def _valuation(self, nav_map, ledger=None):
        """nav_map: {code: (nav_date, nav)} 最近可用净值。"""
        ledger = ledger or self.ledger
        cash = ledger.cash()
        pos = ledger.positions()
        mv_by, mv_eq, mv_bond = {}, 0.0, 0.0
        for code, shares in pos.items():
            pair = nav_map.get(code)
            if not pair or not pair[1]:
                continue
            mv = shares * pair[1]
            mv_by[code] = mv
            if self._kind(code) == "bond":
                mv_bond += mv
            else:
                mv_eq += mv
        total = cash + mv_eq + mv_bond
        return {"cash": cash, "mv_by": mv_by, "mv_eq": mv_eq,
                "mv_bond": mv_bond, "total": total}

    def _ctx(self, date_s, valuation, nav_map, ledger=None, mom_map=None):
        """构建 strategy.plan 需要的账户状态（含止损/止盈所需的浮盈浮亏）。"""
        ledger = ledger or self.ledger
        pos = ledger.positions()
        sell_eq, locked_eq, eq_hold_mv = {}, {}, {}
        fund_pnl = {}
        bond_code = self._bond_code()
        bond_sellable = bond_locked = 0.0
        for code, shares in pos.items():
            pair = nav_map.get(code)
            if not pair or not pair[1]:
                continue
            mv = shares * pair[1]
            sh_sell, sh_lock = ledger.sellable(code, date_s, self.min_hold)
            sellv, lockv = sh_sell * pair[1], sh_lock * pair[1]
            if self._kind(code) == "bond":
                bond_sellable += sellv
                bond_locked += lockv
            else:
                eq_hold_mv[code] = mv
                sell_eq[code] = sellv
                locked_eq[code] = lockv
                cost, _ = ledger.basis(code)
                pnl = (mv - cost) / cost if cost > 0 else None
                fund_pnl[code] = {
                    "value": mv, "cost": cost,
                    "pnl_pct": (round(pnl, 6) if pnl is not None else None),
                    "sellable_mv": sellv, "locked_mv": lockv,
                }
        pend_all = ledger.pending_orders() + ledger.submitted_orders()
        committed = sum(o["amount"] for o in pend_all if o["action"] == "buy")
        # 未执行/已提交订单的方向集合：plan 与下单去重时排除“反向挂单”的基金，
        # 避免 昨日卖A未执行 + 今日A重进winners 时同日出现 买A/卖A 互相矛盾的指令。
        pending_buy_codes = {o["fund_code"] for o in pend_all
                             if o["action"] == "buy"}
        pending_sell_codes = {o["fund_code"] for o in pend_all
                              if o["action"] == "sell"}
        meta = self.account() if ledger is self.ledger else (ledger.account_meta() or {})
        return {
            "date": date_s,
            "cash": valuation["cash"],
            "committed": committed,
            "pending_buy_codes": pending_buy_codes,
            "pending_sell_codes": pending_sell_codes,
            "eq_mv": valuation["mv_eq"],
            "bond_mv": valuation["mv_bond"],
            "total": valuation["total"],
            "bond_code": bond_code,
            "bond_sellable_mv": bond_sellable,
            "bond_locked_mv": bond_locked,
            "eq_hold_mv": eq_hold_mv,
            "sell_eq_by_code": sell_eq,
            "locked_eq_by_code": locked_eq,
            "fund_pnl": fund_pnl,
            "initial": float(meta.get("initial_cash")
                             or self.acct.get("initial_cash", 500)),
            "peak_total": ledger.peak_total(),
            "funds_mom": mom_map or {},
            "equity_codes": self._eq_codes(),
            "names": {f["code"]: (f.get("name") or f["code"])
                      for f in self._pool_items()},
        }

    def _fund_factors(self, date_s):
        """进攻池内各基金的多因子（借鉴“动量+波动+均线乖离+RSI”的多因子思路）。

        返回 {code: {"mom": 20日动量, "mom5": 近5日涨跌, "vol": 20日波动率,
                     "bias": 相对20日线乖离, "rsi": RSI14}}；数据不足的略过。
        """
        out = {}
        for code in self._eq_codes():
            try:
                seq = self.market.fund_history(code,
                                               need_from=util.add_days(date_s, -80))
            except DataError:
                continue
            closes = [nav for d, nav in seq if d <= date_s]
            if len(closes) < 22:
                continue
            base = closes[-22]
            if not base:
                continue
            mom = closes[-1] / base - 1.0
            mom5 = (closes[-1] / closes[-6] - 1.0) if len(closes) >= 6 and closes[-6] else None
            rets = [closes[i] / closes[i - 1] - 1.0 for i in range(-21, 0)]
            mean = sum(rets) / len(rets)
            vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
            ma20 = sum(closes[-20:]) / 20.0
            bias = closes[-1] / ma20 - 1.0 if ma20 else 0.0
            rsi = indicators.rsi(closes, 14)
            out[code] = {"mom": mom, "mom5": mom5, "vol": vol,
                         "bias": bias, "rsi": rsi}
        return out

    def _sector_brief(self, date_s):
        """各大板块总体情况：对每个板块代表基金算 20 日动量（+ 近 5 日涨跌），
        按动量排序输出领涨/走弱两栏，用于每日研判的板块全景。"""
        date_s = date_s or util.today_str()
        rows = []
        for sector, code in SECTOR_PROXIES:
            try:
                seq = self.market.fund_history(code,
                                               need_from=util.add_days(date_s, -60))
            except DataError:
                continue
            closes = [nav for d, nav in seq if d <= date_s]
            if len(closes) < 22:
                continue
            base = closes[-22]
            if not base:
                continue
            mom20 = closes[-1] / base - 1.0
            mom5 = (closes[-1] / closes[-6] - 1.0) if len(closes) >= 6 else None
            rows.append({"sector": sector, "mom20": mom20, "mom5": mom5})
        if not rows:
            return ["【板块全景】数据不足，暂无板块评价"]
        rows.sort(key=lambda r: -r["mom20"])
        strong = [r for r in rows if r["mom20"] > 0]
        weak = [r for r in rows if r["mom20"] <= 0]

        def fmt(r):
            s = "{} {:+.1%}".format(r["sector"], r["mom20"])
            if r["mom5"] is not None:
                s += "（5日 {:+.1%}）".format(r["mom5"])
            return s

        lines = ["【板块全景】各大板块 20 日动量（红=领涨，由强到弱）："]
        lines.append("领涨：" + ("；".join(fmt(r) for r in strong) if strong else "无（全线走弱）"))
        lines.append("走弱：" + ("；".join(fmt(r) for r in weak) if weak else "无（全线走强）"))
        return lines

    # ---------------- 评分平滑与防御锁 ----------------
    def _smooth_score(self, ledger, raw):
        """对每日合成评分做 EMA 平滑，用于仓位控制，降低噪声导致的追涨杀跌换手。"""
        st = self.cfg.get("strategy", {}) or {}
        if not st.get("score_ema_enabled", False):
            return int(round(raw))
        alpha = util.clamp(float(st.get("score_ema_alpha", 0.4) or 0.4), 0.05, 1.0)
        prev = ledger.score_ema()
        if prev is None:
            prev = float(raw)
        ema = alpha * float(raw) + (1.0 - alpha) * prev
        ledger.set_score_ema(ema)
        return int(round(ema))

    def _defensive(self, ledger, date_s, score=None):
        """是否处于组合止损/峰值回撤后的防御锁定期（纯时间冷却）。"""
        until = ledger.risk_lock_until()
        return bool(until) and date_s <= until

    def _update_risk_lock(self, plan, date_s, score=None, ledger=None):
        """组合级风控触发后，锁定 rearm_days（默认 7）个交易日为防御期（期间权益≤25%）。

        冷却到期后自动解除、恢复正常仓位规则；评分平滑（若开启）本身即可
        避免到期瞬间满仓追涨。冷却期内若触发单只止损/止盈或评分 > rearm_bull_score，
        strategy.plan 会按“防御期例外”允许直接调仓。单只止损/止盈不触发组合级防御锁。
        """
        ledger = ledger or self.ledger
        kind = plan.get("risk_kind")
        if kind in ("portfolio_stop", "trailing"):
            days = max(0, int(self.risk_cfg.get("rearm_days", 7)))
            ledger.set_risk_lock(util.add_trading_days(date_s, days))
            return
        until = ledger.risk_lock_until()
        if until and date_s > until:
            ledger.clear_risk_lock()

    # ---------------- 下单执行 ----------------
    def _auto_confirm(self, date_s, ledger=None):
        """模拟支付宝 T+1：处理已提交(submitted)的订单。

        规则：提交时刻 15:00 前 → 当日净值成交；15:00 后 → 下一交易日净值成交（V）；
        份额确认日 C = V 的下一交易日。date_s 到 C 且 V 净值已公布 → 自动确认份额入账。
        lots 锁仓从确认日 C 起算。
        """
        ledger = ledger or self.ledger
        fills = []
        for o in ledger.submitted_orders():
            code = o["fund_code"]
            submit_at = o.get("submit_at") or o.get("created") or util.now_iso()
            V = util.nav_value_date(submit_at)
            C = util.add_trading_days(V, 1)
            if date_s < C:
                continue  # 尚未到份额确认日
            nav = self._nav_on(code, V)
            if nav is None:
                continue  # V 净值尚未公布，等下次运行
            if o["action"] == "buy":
                amount = float(o["amount"])
                rate = self._rate_of(code)["buy"]
                fee = (amount - amount / (1.0 + rate)) if rate > 0 else 0.0
                net = amount - fee
                shares = math.floor(net / nav * 100) / 100.0
                if shares < 0.01:
                    ledger.skip_order(o["id"], "确认后金额过小，无法形成有效份额")
                    continue
                res = ledger.exec_buy(code, C, shares, nav, fee, budget=amount)
                if not res["ok"]:
                    ledger.skip_order(o["id"], res.get("reason") or "买入失败")
                    continue
                ledger.complete_order(o["id"], V, nav, shares, res["debit"], fee,
                                      "T+1 自动确认：{} 净值成交，{} 份额确认".format(V, C))
                fills.append({"status": "filled", "id": o["id"], "action": "buy",
                              "code": code, "date": V, "shares": shares,
                              "amount": res["debit"], "fee": util.r2(fee),
                              "nav": nav, "confirm_date": C})
            else:  # sell
                allow_early = bool(self.risk_cfg.get("allow_early_exit_fee", False))
                note_txt = o.get("note") or ""
                forced = allow_early and any(
                    k in note_txt for k in ("止损", "止盈", "风控"))
                if forced:
                    available = ledger.position_shares(code)
                else:
                    available, _ = ledger.sellable(code, C, self.min_hold)
                if available <= 0.001:
                    ledger.skip_order(o["id"],
                                      "确认时无可卖份额或持有不足{}天".format(self.min_hold))
                    continue
                target = min(float(o["amount"]) / nav, available)
                res = ledger.exec_sell(code, C, target, nav, self._sell_rate_fn(code))
                if res["executed"] <= 0:
                    ledger.skip_order(o["id"], "自动确认卖出失败")
                    continue
                ledger.complete_order(o["id"], V, nav, res["executed"], res["net"],
                                      res["fee"],
                                      "T+1 自动确认：{} 净值成交，{} 资金到账".format(V, C))
                fills.append({"status": "filled", "id": o["id"], "action": "sell",
                              "code": code, "date": V,
                              "shares": res["executed"],
                              "amount": res["net"], "fee": res["fee"],
                              "nav": nav, "confirm_date": C})
        return fills

    def _fill_order(self, o, fill_date, nav, ledger=None):
        ledger = ledger or self.ledger
        code = o["fund_code"]
        oid = o["id"]
        if o["action"] == "buy":
            amount = float(o["amount"])
            rate = self._rate_of(code)["buy"]
            fee = (amount - amount / (1.0 + rate)) if rate > 0 else 0.0
            net = amount - fee
            shares = math.floor(net / nav * 100) / 100
            if shares < 0.01:
                ledger.skip_order(oid, "金额过小，无法形成有效份额")
                return {"status": "skipped", "id": oid, "why": "金额过小"}
            res = ledger.exec_buy(code, fill_date, shares, nav, fee, budget=amount)
            if not res["ok"]:
                ledger.skip_order(oid, res.get("reason") or "买入失败")
                return {"status": "skipped", "id": oid,
                        "why": res.get("reason") or "买入失败"}
            ledger.complete_order(oid, fill_date, nav, shares, res["debit"],
                                  fee, "自动成交")
            return {"status": "filled", "id": oid, "action": "buy",
                    "code": code, "shares": shares,
                    "amount": res["debit"],
                    "fee": util.r2(fee), "date": fill_date}
        risk_cfg = (self.cfg.get("strategy", {}) or {}).get("risk", {}) or {}
        allow_early = bool(risk_cfg.get("allow_early_exit_fee", False))
        note_txt = o.get("note") or ""
        forced = allow_early and any(k in note_txt for k in ("止损", "止盈", "风控"))
        if forced:
            available = ledger.position_shares(code)
        else:
            available, _ = ledger.sellable(code, fill_date, self.min_hold)
        if available <= 0.001:
            ledger.skip_order(oid, "无可卖份额或持有不足{}天".format(self.min_hold))
            return {"status": "skipped", "id": oid,
                    "why": "无可卖份额(持有不足{}天)".format(self.min_hold)}
        target_shares = min(float(o["amount"]) / nav, available)
        res = ledger.exec_sell(code, fill_date, target_shares, nav,
                               self._sell_rate_fn(code))
        if res["executed"] <= 0:
            ledger.skip_order(oid, "卖出失败")
            return {"status": "skipped", "id": oid, "why": "卖出失败"}
        ledger.complete_order(oid, fill_date, nav, res["executed"],
                              res["net"], res["fee"],
                              "自动成交，按{}净值{:.4f}".format(fill_date, nav))
        return {"status": "filled", "id": oid, "action": "sell",
                "code": code, "shares": res["executed"],
                "amount": res["net"], "fee": res["fee"], "date": fill_date}

    def _place_orders(self, date_s, plan, ledger=None):
        """下达计划订单（人工模式去重），返回已创建订单。

        去重升级为双向：同一只基金存在未执行的旧单（无论买卖方向）都不再重复
        下达，避免跨日反向挂单并存。
        """
        ledger = ledger or self.ledger
        created = []
        pending = ledger.pending_orders()
        for o in plan.get("orders") or []:
            dup = [p for p in pending if p["fund_code"] == o["code"]]
            if dup:
                p = dup[0]
                same = (p["action"] == o["action"])
                if same:
                    plan.setdefault("msgs", []).append(
                        "已有未执行的{}建议（{}），请先执行或跳过，不再重复下达".format(
                            "申购" if o["action"] == "buy" else "赎回", o["code"]))
                else:
                    plan.setdefault("msgs", []).append(
                        "基金 {} 已有未执行的{}旧单，方向相反的{}建议不再下达，"
                        "请先处理旧单（执行/录入成交或放弃）".format(
                            o["code"],
                            "申购" if p["action"] == "buy" else "赎回",
                            "申购" if o["action"] == "buy" else "赎回"))
                continue
            oid = ledger.add_order(date_s, o["code"], o["action"],
                                   o["amount_yuan"], o["note"])
            created.append({"id": oid, **o})
        return created

    def _news_evolve(self, date_s, news_obj, chg, msgs_all):
        """进化闭环：当天消息入库 → 结算人工方向 → 登记信息缺口 → 合并搜索回填。"""
        try:
            scr = self._screen()
            if news_obj and news_obj.get("ok"):
                scr.ingest_feed(date_s, news_obj.get("feed") or [])
            if chg is not None:
                hit_n = scr.resolve_directions(date_s, chg)
                if hit_n:
                    msgs_all.append("已自动结算 {} 条 AI 次日方向判断（对照 {} 大盘走势），"
                                    "命中率见“消息与进化”页".format(hit_n, date_s))
            before = len(screening.open_queue_items())
            queue = screening.ensure_daily_news_queue(
                date_s, bool(news_obj and news_obj.get("ok")),
                int((news_obj or {}).get("items") or 0))
            if len(queue) > before:
                q = next((x for x in queue if x.get("date") == date_s), None)
                msgs_all.append("【搜索补全】在线消息不足，已登记待补清单（data/search_queue.json，"
                                "理由：{}）；运行 python app.py search-queue 查看，由 DSH 本地搜索"
                                "回填 data/search_results.json 后系统自动合并。".format(
                                    (q or {}).get("reason", "")))
            m = screening.import_search_results(cfg=self.cfg)
            if m.get("added"):
                msgs_all.append("【本地搜索回填】" + m.get("note", ""))
        except Exception as e:
            msgs_all.append("消息筛选/学习处理异常（不影响主流程）：{}".format(e))

    # ================= 每日例行（真实/在线） =================
    def run_daily(self, force=False):
        if self.demo:
            return {"status": "error", "message": "演示库只读，请使用正式库运行每日研判"}
        meta = self.ensure_account()
        exec_mode = meta.get("exec_mode", "manual")
        self._maybe_auto_refresh()  # 池过期/不足时自动重建（配额不足自动顺延）
        try:
            idx = self.market.index_history(
                need_from=util.add_days(util.today_str(), -175))
        except DataError as e:
            return {"status": "error", "message": "指数数据获取失败：" + str(e)}
        if not idx:
            return {"status": "error", "message": "未获取到指数K线"}
        dates = [x[0] for x in idx]
        closes = [x[1] for x in idx]
        vols = [x[2] for x in idx]
        D = dates[-1]
        st = indicators.last_stats(dates, closes, vols)
        msgs_all = []
        # 0) 先结算到期 T+1 订单（manual 模式）：即使当日已有研判（noop 短路）也要先把
        #    已提交(submitted)且到确认日的订单按净值入账，避免被 has_record(D) 饿死
        #    （audit P1-8）。
        fills = []
        if exec_mode == "manual":
            fills = self._auto_confirm(D)
            if fills:
                msgs_all.append("已自动确认 {} 笔到期 T+1 订单按净值入账".format(len(fills)))
        if not force and self.ledger.has_record(D):
            return {"status": "noop", "date": D, "fills": fills,
                    "message": "{} 已完成研判（非交易日或今日已运行）。如需重跑：python app.py run-daily --force".format(D)}
        # 1) force 重跑：撤销当日 pending
        if force:
            for o in [x for x in self.ledger.pending_orders() if x["order_date"] == D]:
                self.ledger.skip_order(o["id"], "force 重跑撤销")
        if exec_mode == "auto":
            for o in self.ledger.pending_orders():
                if o["order_date"] >= D:
                    continue
                nav = self._nav_on(o["fund_code"], D)
                if nav is None:
                    continue  # 该基金今日净值未公布，等下一交易日
                fills.append(self._fill_order(o, D, nav))
        # 2) 组合估值（按 <=D 的最近净值）
        nav_map = {}
        stale = []
        for code in self._pool_codes():
            try:
                seq = self.market.fund_history(code,
                                               need_from=util.add_days(D, -55))
            except DataError as e:
                msgs_all.append("{} 净值获取失败：{}".format(code, e))
                continue
            latest = [x for x in seq if x[0] <= D]
            if not latest:
                continue
            nav_map[code] = (latest[-1][0], latest[-1][1])
            if latest[-1][0] != D:
                stale.append("{} 净值更新至 {}，今日尚未公布".format(code, latest[-1][0]))
        val = self._valuation(nav_map)
        close = st.get("close")
        chg = st.get("chg_pct")
        # 3) 研判：量化分 + 消息面分 → 合成 + 风控 + 选基
        tech, sigs = analysis.score_market(st)
        source = "内置引擎"
        # 消息面（多源广域 + 自动词典分；若用户已人工打标则优先用“人工消息分”）
        st_cfg = self.cfg.get("strategy", {}) or {}
        news_amp = int(st_cfg.get("news_amp", 8) or 8)
        news_obj = newsmod.load_news(D, amplitude=news_amp, cfg=self.cfg)
        news_from = "自动词典"
        if news_obj.get("ok"):
            try:
                scr = self._screen()
                scr.ingest_feed(D, news_obj.get("feed") or [])
                human = scr.human_news_meta(D, amplitude=news_amp)
            except Exception as e:
                human = None
                msgs_all.append("消息人工筛选库异常：{}".format(e))
            if human:
                news_sc = human["score"]
                news_from = "人工筛选（{}条方向打标）".format(human["directional"])
            else:
                news_sc = news_obj.get("score")
        else:
            news_sc = None
        score = combine_score(self.cfg, tech, news_sc)
        if news_sc is not None:
            source = "量化+消息面"
            msgs_all.append("评分合成：量化 {:+.0f} ×{}% + 消息面[{}] {:+.0f} ×{}% = {:+.0f}".format(
                tech, int((1 - float(st_cfg.get("news_weight", 0.35))) * 100),
                news_from, news_sc, int(float(st_cfg.get("news_weight", 0.35)) * 100),
                score))
        else:
            msgs_all.append("消息面获取失败或为空，本次按纯量化评分运行")
        news_block = self._news_lines(news_obj, news_from)
        factors = self._fund_factors(D)
        mom_map = {c: f["mom"] for c, f in factors.items()}
        ctx = self._ctx(D, val, nav_map, mom_map=mom_map)
        ctx["funds_vol"] = {c: f["vol"] for c, f in factors.items()}
        ctx["funds_mom5"] = {c: f["mom5"] for c, f in factors.items()}
        ctx["funds_factors"] = factors
        raw_score = score
        smooth = self._smooth_score(self.ledger, raw_score)
        ctx["score"] = smooth
        ctx["raw_score"] = raw_score
        # AI 自动记录“次日方向”并用于自检命中率（用户不参与预测，只做消息打标过滤）
        try:
            view = analysis.view_of(smooth)["key"]
            ai_dir = ("bull" if view in ("hot_bull", "bull")
                      else "bear" if view in ("hot_bear", "bear") else "neutral")
            ai_conf = (0.5 if ai_dir == "neutral" else
                       float(util.clamp(abs(smooth) / 100.0, 0.0, 1.0) * 0.5 + 0.5))
            self._screen().set_direction(D, ai_dir, ai_conf)
        except Exception:
            pass
        ctx["defensive"] = self._defensive(self.ledger, D, smooth)
        ctx["market_mom"] = st.get("mom20")  # 大盘(沪深300)20日动量，用于板块相对强弱
        # 多信号共识度（借鉴 ai-hedge-fund 多观点投票）：技术信号内部一致性，
        # 消息面方向作为独立“一票”（-1~+1）参与；分歧大 → 仓位保守
        news_view = (news_sc / 100.0) if news_sc is not None else None
        ctx["confidence"] = analysis.signal_confidence(sigs, news_view)
        # 决策透明度：展示“多信号投票”共识度（借鉴 ai-hedge-fund 的多观点模型）
        bull_n = sum(1 for s in sigs if s.get("impact", 0) > 0)
        bear_n = sum(1 for s in sigs if s.get("impact", 0) < 0)
        news_dir = "利多" if (news_sc or 0) > 0 else ("利空" if (news_sc or 0) < 0 else "中性")
        msgs_all.append("多信号共识度 {:.0%}：技术面 {}/{} 票看多、{}/{} 票看空，消息面{}（{:+d}）。"
                        "共识越高越顺势，分歧大则少追涨杀跌。".format(
                            ctx["confidence"], bull_n, len(sigs), bear_n, len(sigs),
                            news_dir, news_sc or 0))
        if smooth != raw_score:
            msgs_all.append("评分平滑：原始 {:+.0f} → 平滑 {:+.0f}（仅用于仓位控制，降低换手）".format(
                raw_score, smooth))
        plan = strategy.plan(self.cfg, ctx)
        self._update_risk_lock(plan, D, smooth)
        extra = news_block + self._sector_brief(D) + self._plan_lines(plan, exec_mode, D)
        report = analysis.build_report(D, self._idx_name(), close, chg, st,
                                       score, sigs, source,
                                       extra_lines=extra)
        # 可选 LLM 深度研判
        llm = self.cfg.get("llm") or {}
        title = analysis.view_of(score)["title"]
        if llm.get("enabled") and llm.get("api_key"):
            pack = {
                "日期": D, "指数": self._idx_name(),
                "收盘点位": close,
                "当日涨跌%": (None if chg is None else round(chg * 100, 2)),
                "量化评分": tech, "消息面分": news_sc,
                "合成评分": score,
                "技术指标": {k: (round(v, 3) if isinstance(v, float) else v)
                           for k, v in st.items() if k not in ("date", "n")},
                "当日市场消息": {
                    "利好": [n["title"] for n in news_obj.get("bull", [])[:3]],
                    "利空": [n["title"] for n in news_obj.get("bear", [])[:3]],
                } if news_obj.get("ok") else "（无）",
                "账户": {"现金": val["cash"], "股基市值": val["mv_eq"],
                        "债基市值": val["mv_bond"], "总资产": val["total"]},
                "进攻池20日动量": {c: (round(m * 100, 2) if m is not None else None)
                              for c, m in mom_map.items()},
                "AI选基": [{"代码": w.get("code"), "名称": w.get("name"),
                          "20日动量": (round(w.get("mom") * 100, 2) if w.get("mom") is not None else None)}
                         for w in plan.get("winners", [])],
                "风控建议": plan["msgs"],
                "实验目标": "1000元 半年内挑战 1500 元（只能买卖场外基金，不能直接买股票）",
            }
            r = analysis.llm_analyze(self.cfg, pack)
            if r:
                score = r["score"]
                source = "LLM·" + r.get("provider", "ai")
                title = r["title"]
                report = r["text"]
                msgs_all.append("【LLM研判已启用】观点由 {} 生成".format(source))
                if abs(score - plan["score"]) > 15:
                    msgs_all.append("提示：LLM 观点（{:+d}）与合成评分（{:+d}）分歧较大，"
                                    "仓位按规则引擎执行以保持纪律".format(score, plan["score"]))
        # 4) 下达今日建议（去重）
        created = self._place_orders(D, plan)
        # 5) 记录
        decision = self._decision_text(plan, fills, exec_mode, D)
        self.ledger.add_record(D, score, chg, close, title, source,
                               report, decision)
        # 6) 快照
        held = self.ledger.positions()
        fresh = True
        for code in held:
            if not (nav_map.get(code) and nav_map[code][0] == D):
                fresh = False
                break
        snap_written = False
        if fresh:
            self.ledger.add_snapshot(D, val["cash"], val["mv_eq"], val["mv_bond"],
                                     val["total"], self.ledger.fees(), close,
                                     "manual" if exec_mode == "manual" else "auto")
            self.ledger.bump_peak(val["total"])
            snap_written = True
        elif not self.ledger.has_snapshot(D):
            msgs_all.append("部分基金今日净值未公布，待齐备后再次运行生成快照")
        # 7) 进化闭环：人工筛选入库→方向结算→信息缺口队列→本地搜索回填
        self._news_evolve(D, news_obj, chg, msgs_all)
        # 8) 盘后策略时机与数据完整性提示（场外基金按收盘净值成交，信号依赖收盘数据）
        pool_codes = self._pool_codes()
        fresh_n = sum(1 for c in pool_codes if nav_map.get(c) and nav_map[c][0] == D)
        total_n = len(pool_codes) or 1
        now = util.now_dt()
        if D == util.today_str() and now.strftime("%H:%M") < "20:00":
            msgs_all.append(
                "【运行时机】场外基金按收盘净值成交、策略信号依赖收盘数据，盘后策略最优："
                "建议在 A 股收盘、基金净值公布后（约 20:00）运行；当前 {} 早于净值公布时间，"
                "今日净值可能尚未出全。".format(now.strftime("%H:%M")))
        if fresh_n < total_n:
            msgs_all.append(
                "【数据提示】今日净值已更新 {}/{} 只，其余仍为前一日净值，决策可能滞后；"
                "建议净值公布后重跑（python app.py run-daily --force）。".format(fresh_n, total_n))
        if stale:
            msgs_all.append("净值明细：" + "；".join(stale))
        # 8.5) 资金守恒断言（尾差/扣款口径修正后的兜底；老库首次运行会自动建立基线）
        try:
            chk = self.ledger.check_consistency(raise_on_mismatch=False)
            if chk.get("problems"):
                msgs_all.append("【账本一致性异常】" + "；".join(chk["problems"]))
        except Exception as e:
            msgs_all.append("【账本一致性检查失败】{}".format(e))
        out = {
            "status": "ok", "date": D, "score": score, "view": title,
            "source": source, "fills": fills, "orders": created,
            "snapshot": snap_written, "total": val["total"],
            "index_close": close, "chg_pct": chg,
            "exec_mode": exec_mode,
            "messages": msgs_all + self.market.warnings[-5:],
        }
        return out

    # ---------------- 文案辅助 ----------------
    def _news_lines(self, news_obj, news_from="自动词典"):
        """把当日消息面整理成可读段落（供研判全文使用）。"""
        if not news_obj or not news_obj.get("ok"):
            return []
        bull = news_obj.get("bull", []) or []
        bear = news_obj.get("bear", []) or []
        lines = ["【消息面】当日快讯情绪：利好 {} 条 / 利空 {} 条，净情绪 {}（消息分 {:+.0f}；来源：{}）。".format(
            len(bull), len(bear), news_obj.get("net", 0), news_obj.get("score", 0),
            news_from)]
        if bull:
            lines.append("利好：①" + " ②".join(
                "{}（{}）".format(n["title"][:44], n.get("time", "")[-5:])
                for n in bull[:3]))
        if bear:
            lines.append("利空：①" + " ②".join(
                "{}（{}）".format(n["title"][:44], n.get("time", "")[-5:])
                for n in bear[:3]))
        return lines

    def _next_trade_day(self, d):
        """估算下一个交易日（工作日近似；法定节假日自动顺延，由平台规则为准）。"""
        days = util.trading_days_between(util.add_days(d, 1), util.add_days(d, 10))
        return days[0] if days else None

    def _plan_lines(self, plan, exec_mode="manual", suggest_date=None):
        """把 AI 建议组织成“执行窗口 + 分步操作”的清晰文本。"""
        lines = []
        nd = self._next_trade_day(suggest_date or util.today_str())
        if nd:
            lines.append(
                "【执行窗口】本建议于 {} 盘后生成：请在下一交易日（约 {}，如遇法定节假日自动顺延）"
                "15:00 前在支付宝/天天基金提交，将按该日收盘净值成交（场外基金 T+1 确认生效）；"
                "15:00 后提交则顺延到再下一个交易日。".format(suggest_date or "今日", nd))
        lines.extend(plan.get("msgs") or [])
        winners = plan.get("winners") or []
        if winners:
            parts = []
            for w in winners:
                m = w.get("mom")
                mom_txt = ("20日动量 {:+.2%}".format(m)) if m is not None else "动量—"
                parts.append("{}（{}）".format(w.get("name") or self._name_of(w.get("code")), mom_txt))
            lines.append("AI 综合进攻组合（{} 只不同板块）：{}；权益目标仓位 {:.0%}（当前 {:.0%}）。".format(
                len(winners), "、".join(parts), plan.get("w_target", 0), plan.get("w_now", 0)))
        n = 0
        conv_note = any("基金转换" in (o.get("note") or "") for o in plan.get("orders") or [])
        if conv_note:
            lines.append("【基金转换】本轮包含赎回与申购配对单：请在支付宝/天天基金对旧持仓用"
                         "『转换/超级转换』一次操作（转出与转入按同一交易时点确认，T+1 同步生效），"
                         "无需先赎回等资金到账再买；若平台不支持该对基金转换，则按两步执行"
                         "（先赎回，资金到账后再录入申购）。")
        for o in plan.get("orders") or []:
            n += 1
            name = self._name_of(o["code"])
            code = o["code"]
            is_conv = "基金转换" in (o.get("note") or "")
            if o["action"] == "buy":
                if is_conv:
                    lines.append(
                        "步骤{}｜转换买入约 {:.0f} 元：{}（{}）。操作：支付宝/天天基金持有页 → "
                        "『转换/超级转换』→ 目标基金 {}，金额约 {:.0f} 元 → 一次完成，"
                        "转出与转入同步确认（无需等待赎回资金到账）。原因：{}".format(
                            n, o["amount_yuan"], name, code, code,
                            o["amount_yuan"], o["note"]))
                else:
                    lines.append(
                        "步骤{}｜申购 {:.0f} 元：{}（{}）。操作：支付宝/天天基金搜“{}”或“{}”→ "
                        "买入 {:.0f} 元（C类无申购费）→ 按下一交易日收盘净值成交，T+1 晚可查确认份额。"
                        "提示：确认后至少持有 7 个自然日再赎回，否则有 1.5% 惩罚性赎回费。"
                        "原因：{}".format(n, o["amount_yuan"], name, code, code, name,
                                          o["amount_yuan"], o["note"]))
            else:
                lines.append(
                    "步骤{}｜赎回约 {:.0f} 元：{}（{}）。操作：支付宝/天天基金“持有”→ "
                    "赎回/转换。按下一交易日收盘净值确认；资金到账以平台为准（债基约 T+1，"
                    "权益基金一般 T+1~T+3）。{}原因：{}".format(
                        n, o["amount_yuan"], name, code,
                        ("本单可与本轮申购单用『基金转换』一次完成，无需先赎回到账再买。"
                         if conv_note else
                         "到账后请回网页录入本单成交，AI 会在下一交易日盘后给出后续买入建议。"),
                        o["note"]))
        if not n:
            lines.append("今日无需操作：维持现有仓位。" +
                         ("（若你持有轮动中的旧标的且资金已到账但 AI 尚未建议买入，"
                          "说明当日已超调仓阈值，请等下一交易日 AI 重新研判）"
                          if exec_mode == "manual" else ""))
        return lines

    def _decision_text(self, plan, fills, exec_mode="manual", suggest_date=None):
        lines = []
        for f in fills:
            nm = self._name_of(f["code"])
            if f["action"] == "buy":
                lines.append("已按 {} 净值成交：申购 {} {:.2f} 份，含费 {:.2f} 元".format(
                    f["date"], nm, f["shares"], f["amount"]))
            else:
                lines.append("已按 {} 净值成交：赎回 {}，到账 {:.2f} 元（费 {:.2f}）".format(
                    f["date"], nm, f["amount"], f["fee"]))
        if exec_mode == "manual":
            lines.append("▼ AI 今日建议（执行后请到【指令与成交】页点“已执行”录入实际成交）")
        lines.extend(self._plan_lines(plan, exec_mode, suggest_date))
        return "\n".join(lines)

    # ================= 回放（回测 / 演示共用） =================
    def _replay_meta(self, codes):
        """为回放候选代码补齐 名称/种类 元信息（universe + config池 + 动态池兜底）。"""
        meta = {}
        bond_seen = set()
        for f in list(settings.pool_of(self.cfg)) + list(self._pool_items()):
            c = f.get("code")
            if not c:
                continue
            if c not in meta:
                meta[c] = {"name": f.get("name") or c, "kind": f.get("kind") or "equity"}
            if f.get("kind") == "bond":
                bond_seen.add(c)
        for u in settings.universe_of(self.cfg):
            c = u.get("code")
            if c and c not in meta:
                meta[c] = {"name": u.get("name") or c,
                           "kind": "bond" if c in bond_seen else "equity"}
        # 其余（历史动态池里的代码）：名称兜底
        names = strategy._theme_of  # noqa（保留引用，避免误删导入）
        for c in codes:
            if c not in meta:
                meta[c] = {"name": c, "kind": "bond" if c in bond_seen else "equity"}
        return meta

    def _replay_pool_for_day(self, win, meta, eq_all, bond_all, held, top_eq,
                             theme_max=2):
        """用“截至当日可见净值”的 20 日动量重建回放池（point-in-time，无前视）。

        规则与 refresh_pool 对齐：按动量降序 + 同主题至多 theme_max 只 + 持仓保护；
        债基始终保留。返回带 kind/name/mom20 的池列表。
        """
        def mom_of(code):
            w = win.get(code) or []
            if len(w) >= 22 and w[-22]:
                return w[-1] / w[-22] - 1.0
            return None

        rows = [{"code": c, "mom20": mom_of(c)} for c in eq_all
                if mom_of(c) is not None]
        rows.sort(key=lambda r: -r["mom20"])
        chosen, have, theme_count = [], set(), {}
        for r in rows:
            if len(chosen) >= top_eq:
                break
            th = strategy._theme_of(meta.get(r["code"], {}).get("name", ""))
            if th and theme_count.get(th, 0) >= theme_max:
                continue
            chosen.append(r)
            have.add(r["code"])
            theme_count[th] = theme_count.get(th, 0) + 1
        for r in rows:  # 仍不足 top_eq 时放宽主题限制
            if len(chosen) >= top_eq:
                break
            if r["code"] not in have:
                chosen.append(r)
                have.add(r["code"])
        # 持仓保护（人工回放中可能正持有旧池标的）
        for code in held:
            if code in have or meta.get(code, {}).get("kind") == "bond":
                continue
            r = next((x for x in rows if x["code"] == code), None)
            chosen.append(r if r else {"code": code, "mom20": None})
            have.add(code)
        # 债基始终保留（防御底仓，不参与动量排名）
        for code in bond_all:
            if code not in have:
                chosen.append({"code": code, "mom20": None})
                have.add(code)
        items = []
        for r in chosen:
            m = meta.get(r["code"], {})
            items.append({"code": r["code"], "name": m.get("name") or r["code"],
                          "kind": m.get("kind") or "equity",
                          "role": "primary" if m.get("kind") == "bond" else "attack",
                          "buy_rate": 0.0, "sell_rate_lt7d": 0.015,
                          "sell_rate_ge7d": 0.0, "min_buy": 10.0,
                          "mom20": r.get("mom20")})
        items.sort(key=lambda i: (0 if i["kind"] == "bond" else 1,
                                  -(i.get("mom20") if i.get("mom20") is not None else -9)))
        return items

    def simulate(self, ledger, dates, index_close, index_vol, nav_series,
                 warmup_n=0):
        """在给定交易日序列上回放同一引擎（回测/演示共用）。

        - 候选池按“当日及之前可见净值”的 20 日动量**逐日重建**（point-in-time），
          不再用“今天挑好的池”回放历史，消除前视/幸存者偏差（audit P0-3）；
        - warmup_n>0 时，前 warmup_n 个交易日只“预热”指标/动量，不进入统计行
          （其产生的持仓作为后续统计的起始状态）。
        返回 rows 列表（每个元素是一个“统计日”快照）。
        """
        res = []
        closes = index_close
        univ_codes = sorted((nav_series or {}).keys())
        meta = self._replay_meta(univ_codes)
        eq_all = [c for c in univ_codes if meta.get(c, {}).get("kind") != "bond"]
        bond_all = [c for c in univ_codes if meta.get(c, {}).get("kind") == "bond"]
        sc = settings.screening_of(self.cfg)
        top_eq = max(1, int(sc.get("top_equity", 8)))
        win = {c: [] for c in univ_codes}
        for i, d in enumerate(dates):
            # 0) 当日“已公布”净值入窗（ffill 序列：每个指数交易日取<=d 的最新净值）
            for c in univ_codes:
                v = (nav_series.get(c) or {}).get(d)
                if v is not None:
                    win[c].append(v)
            pool = self._replay_pool_for_day(
                win, meta, eq_all, bond_all, set(ledger.positions()), top_eq)
            saved_override = self._pool_override
            self._pool_override = pool
            try:
                # 1) 成交历史挂单
                for o in list(ledger.pending_orders()):
                    if o["order_date"] >= d:
                        continue
                    nav = (nav_series.get(o["fund_code"]) or {}).get(d)
                    if nav:
                        self._fill_order(o, d, nav, ledger=ledger)
                # 2) 估值与指标（覆盖“当日池 + 全部持仓”，避免轮动出池的持仓被漏估）
                nav_map = {}
                val_codes = set(self._pool_codes()) | set(ledger.positions())
                for code in val_codes:
                    nav = (nav_series.get(code) or {}).get(d)
                    if nav:
                        nav_map[code] = (d, nav)
                st = indicators.last_stats(dates[:i + 1], closes[:i + 1],
                                           index_vol[:i + 1])
                val = self._valuation(nav_map, ledger=ledger)
                score, sigs = analysis.score_market(st)
                close = st.get("close")
                chg = st.get("chg_pct")
                # 3) 决策（回放：无当日消息面，按纯量化评分运行止损止盈等规则）
                factors = {}
                mom_map = {}
                for c in self._eq_codes():
                    w = win.get(c) or []
                    if len(w) >= 22 and w[-22]:
                        mom_map[c] = w[-1] / w[-22] - 1.0
                        rets = [w[i] / w[i - 1] - 1.0 for i in range(-21, 0)]
                        mean = sum(rets) / len(rets)
                        vol = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
                        mom5 = (w[-1] / w[-6] - 1.0) if len(w) >= 6 and w[-6] else None
                        ma20 = sum(w[-20:]) / 20.0
                        bias = w[-1] / ma20 - 1.0 if ma20 else 0.0
                        factors[c] = {"mom": mom_map[c], "mom5": mom5, "vol": vol,
                                      "bias": bias, "rsi": indicators.rsi(w, 14)}
                ctx = self._ctx(d, val, nav_map, ledger=ledger, mom_map=mom_map)
                ctx["names"].update({c: meta.get(c, {}).get("name", c)
                                     for c in ledger.positions() if c in meta})
                ctx["funds_vol"] = {c: f["vol"] for c, f in factors.items()}
                ctx["funds_mom5"] = {c: f["mom5"] for c, f in factors.items()}
                ctx["funds_factors"] = factors
                raw_score = score
                smooth = self._smooth_score(ledger, raw_score)
                ctx["score"] = smooth
                ctx["raw_score"] = raw_score
                ctx["defensive"] = self._defensive(ledger, d, smooth)
                ctx["market_mom"] = st.get("mom20")  # 大盘20日动量，用于板块相对强弱
                ctx["confidence"] = analysis.signal_confidence(sigs)  # 回放无消息面
                plan = strategy.plan(self.cfg, ctx)
                self._update_risk_lock(plan, d, smooth, ledger=ledger)
                self._place_orders(d, plan, ledger=ledger)
                # 4) 记录
                src = "内置引擎·纯量化（回放，不含消息面）"
                report = analysis.build_report(d, self._idx_name(), close, chg, st,
                                               score, sigs, src,
                                               extra_lines=self._plan_lines(plan, "auto", d))
                ledger.add_record(d, score, chg, close,
                                  analysis.view_of(score)["title"], src, report,
                                  self._decision_text(plan, [], "auto", d))
                ledger.add_snapshot(d, val["cash"], val["mv_eq"], val["mv_bond"],
                                    val["total"], ledger.fees(), close, "回放")
                ledger.bump_peak(val["total"])
            finally:
                self._pool_override = saved_override
            if i >= warmup_n:
                res.append({"date": d, "score": score, "total": val["total"],
                            "cash": val["cash"], "mv_eq": val["mv_eq"],
                            "mv_bond": val["mv_bond"], "chg_pct": chg,
                            "index_close": close})
        return res

    def _ffill_navs(self, trading_dates, hist_map):
        out = {}
        for code, m in hist_map.items():
            carry = None
            d_map = {}
            for d in trading_dates:
                if d in m:
                    carry = m[d]
                if carry:
                    d_map[d] = carry
            out[code] = d_map
        return out

    def backtest(self, months=6, source="auto", end_date=None):
        """回测最近 months 个月（source: auto/live/demo）。

        修复口径（audit P0-3）：
        - 回放池按“当日及之前”的动量逐日重建（point-in-time），不再用今天挑好的池回放历史；
        - 前 warmup_n(默认60) 个交易日只用于预热指标/池选择，不计入收益统计；
        - 输出增加同期沪深300 基准 buy&hold（bench_ret_pct）与说明字段。
        """
        demo_flag, dates, closes, vols, hist, warmup_n, skipped = \
            self._prep_replay(months, source, end_date, warmup_n=60)
        nav_series = self._ffill_navs(dates, hist)
        sub = Ledger(":memory:",
                     initial_cash=float(self.acct.get("initial_cash", 500)))
        rows = self.simulate(sub, dates, closes, vols, nav_series, warmup_n)
        # 账本一致性断言：资金守恒偏差超限直接失败（audit P0-4）
        chk = sub.check_consistency(raise_on_mismatch=True, tol=0.02)
        totals = [r["total"] for r in rows]
        start_v = totals[0] if totals else 0
        end_v = totals[-1] if totals else 0
        peak = -1e18
        max_dd = 0.0
        for t in totals:
            peak = max(peak, t)
            if peak > 0:
                max_dd = max(max_dd, (peak - t) / peak)
        trades = [o for o in sub.orders() if o["status"] == "filled"]
        bench_ret = None
        if warmup_n and len(closes) > warmup_n and closes[warmup_n - 1]:
            bench_ret = closes[-1] / closes[warmup_n - 1] - 1.0
        return {
            "ok": True,
            "source": "demo" if demo_flag else "live",
            "demo": demo_flag,
            "start": dates[warmup_n] if warmup_n < len(dates) else
                     (dates[-1] if dates else None),
            "end": dates[-1] if dates else None,
            "days": len(rows),
            "warmup_days": warmup_n,
            "consistency": chk,
            "initial": float(self.acct.get("initial_cash", 500)),
            "end_value": round(end_v, 2),
            "ret_pct": (end_v / start_v - 1) if start_v else 0,
            "bench_ret_pct": (round(bench_ret, 6) if bench_ret is not None else None),
            "max_dd_pct": max_dd,
            "trades": len(trades),
            "fees": sub.fees(),
            "skipped_codes": len(skipped),
            "target": float(self.acct.get("target_value", 800)),
            "goal_hit": bool(end_v >= float(self.acct.get("target_value", 800))),
            "note": ("回测口径：point-in-time 逐日重建池 + 前 {} 个交易日预热不计入统计"
                     "（不含消息面，非投资建议）".format(warmup_n)),
            "series": rows,
        }

    def backtest_split(self, months=12, test_frac=0.35, source="auto",
                       end_date=None):
        """按时间顺序切分的回测（缓解样本内自证）。

        - 整窗连续回放（单段 simulate、不各自从 1000 重放），前 warmup_n(默认60) 个
          交易日只预热不计入统计 —— 修复原 test 段“从头累积指标、前 74% 残缺”的问题
          （audit P0-3）；
        - train/test 以“切分日的前一日总资产”为基准计算段内收益，避免重复计利；
        - full 等价 backtest()；demo 数据仅用于流程验证，不构成结论。
        结果写 data/backtest_oos.json。
        """
        demo_flag, dates, closes, vols, hist, warmup_n, skipped = \
            self._prep_replay(months, source, end_date, warmup_n=60)
        nav_series = self._ffill_navs(dates, hist)
        sub = Ledger(":memory:",
                     initial_cash=float(self.acct.get("initial_cash", 500)))
        rows = self.simulate(sub, dates, closes, vols, nav_series, warmup_n)
        sub.check_consistency(raise_on_mismatch=True, tol=0.02)
        n_stats = len(rows)  # 已剔除 warmup
        k = int(n_stats * (1.0 - test_frac))
        k = max(5, min(n_stats - 5, k))

        def seg_stats(t0, t1):
            """t0..t1 为 rows 下标（统计行）。基准资产 = 段首前一交易日的 total。"""
            seg = rows[t0:t1]
            if not seg:
                return None
            end_v = seg[-1]["total"]
            if t0 - 1 >= 0:
                base_v = rows[t0 - 1]["total"]  # warmup 最后一行的 total 或上一段末
            else:
                base_v = seg[0]["total"]
            start_v = seg[0]["total"]
            peak = -1e18
            max_dd = 0.0
            for r in seg:
                t = r["total"]
                peak = max(peak, t)
                if peak > 0:
                    max_dd = max(max_dd, (peak - t) / peak)
            return {
                "start": seg[0]["date"], "end": seg[-1]["date"],
                "days": len(seg),
                "end_value": round(end_v, 2),
                "ret_pct": (end_v / start_v - 1) if start_v else 0,
                "seg_ret_pct": (end_v / base_v - 1) if base_v else 0,
                "max_dd_pct": max_dd,
            }

        full = seg_stats(0, n_stats)
        train = seg_stats(0, k)
        test = seg_stats(k, n_stats)
        # 同期沪深300基准（warmup 结束日收盘为起点，buy&hold）
        bench_full = bench_test = bench_train = None
        if not demo_flag and warmup_n and len(closes) > warmup_n and \
                closes[warmup_n - 1]:
            bench_full = closes[-1] / closes[warmup_n - 1] - 1.0
        if not demo_flag and warmup_n and len(closes) > k + warmup_n and \
                closes[k + warmup_n - 1]:
            bench_train = closes[k + warmup_n - 1] / closes[warmup_n - 1] - 1.0
            bench_test = closes[-1] / closes[k + warmup_n - 1] - 1.0
        out = {
            "ok": True,
            "source": "demo" if demo_flag else "live",
            "demo": demo_flag,
            "months": months, "test_frac": round(test_frac, 2),
            "warmup_days": warmup_n,
            "split_date": rows[k]["date"] if 0 <= k < n_stats else
                          (rows[-1]["date"] if rows else None),
            "initial": float(self.acct.get("initial_cash", 500)),
            "target": float(self.acct.get("target_value", 800)),
            "full": full, "train": train, "test": test,
            "bench": {"full": bench_full, "train": bench_train,
                      "test": bench_test,
                      "note": "同期沪深300 buy&hold，以 warmup 结束日收盘为起点"},
            "caveat": ("口径：point-in-time 逐日重建池 + 前 {} 个交易日预热不计统计；"
                       "test 仍可能受“参数曾在后段被人工调过”影响，仅是近似样本外；"
                       "真正的样本外=实验期逐日实盘。".format(warmup_n)
                       if not demo_flag else "演示合成数据，仅用于流程验证"),
            "skipped_codes": len(skipped),
        }
        return out

    # ---------------- 回放数据准备（共用） ----------------
    def _replay_codes(self):
        """回放候选代码集合：config 池 + universe + 动态池 + 磁盘已有净值缓存。"""
        codes = set()
        for u in list(settings.universe_of(self.cfg)) + \
                list(settings.pool_of(self.cfg)) + list(self._pool_items()):
            if u.get("code"):
                codes.add(u["code"])
        try:
            from pathlib import Path
            for p in Path(str(util.CACHE_DIR)).glob("nav_*.json"):
                codes.add(p.stem[4:])
        except Exception:
            pass
        return sorted(codes)

    def _replay_hist(self, codes, need_from):
        """为回放批量取净值序列 {code: {date: nav}}；个别代码失败跳过并记录。

        只读本地缓存（allow_online=False）：历史窗口够用即返回，不触发在线拉取。
        """
        hist, skipped = {}, []
        for code in codes:
            try:
                seq = self.market.fund_history(code, need_from=need_from,
                                               allow_online=False)
            except DataError:
                skipped.append(code)
                continue
            if seq:
                hist[code] = {d: nav for d, nav in seq}
            else:
                skipped.append(code)
        return hist, skipped

    def _prep_replay(self, months, source, end_date, warmup_n=60):
        """组装连续回放数据（含 warmup 预热前缀）。

        返回 (demo_flag, dates, closes, vols, hist, warmup_n_实际, skipped_codes)。
        demo 数据较短（合成 126 个交易日），不做 warmup。
        """
        mkt = self.market
        months = max(1, int(months))
        window_days = int(months * 30.4)
        demo_flag = False
        skipped = []
        if source == "demo":
            series = mkt.ensure_demo_series()
            demo_flag = True
            dates = series["dates"]
            closes = series["index_close"]
            vols = series["index_vol"]
            hist = {code: {d: nav for d, nav in zip(dates, series["funds"][code])}
                    for code in series["funds"]}
        else:
            try:
                need_from = util.add_days(util.today_str(),
                                          -(months * 31 + int(warmup_n * 1.6) + 30))
                idx = mkt.index_history(need_from=need_from)
                if len(idx) < warmup_n + 20:
                    raise DataError("在线K线不足（{}/{} 个交易日）".format(
                        len(idx), warmup_n + 20))
                codes = self._replay_codes()
                need0 = util.add_days(idx[0][0], -50)
                hist, skipped = self._replay_hist(codes, need0)
                if end_date:
                    idx = [x for x in idx if x[0] <= end_date]
                dates = [x[0] for x in idx]
                closes = [x[1] for x in idx]
                vols = [x[2] for x in idx]
                if end_date:
                    pairs = list(zip(dates, closes, vols))
                    pairs = [p for p in pairs if p[0] <= end_date]
                    dates, closes, vols = ([p[0] for p in pairs],
                                           [p[1] for p in pairs],
                                           [p[2] for p in pairs])
            except DataError as e:
                if source == "live":
                    raise
                if not mkt.demo_available():
                    raise DataError("在线数据不可用，且无演示数据：" + str(e))
                series = mkt.ensure_demo_series()
                demo_flag = True
                dates = series["dates"]
                closes = series["index_close"]
                vols = series["index_vol"]
                hist = {code: {d: nav for d, nav in
                               zip(dates, series["funds"][code])}
                        for code in series["funds"]}
        if demo_flag:
            warmup_n = 0  # 合成数据较短，不做预热（原有口径）
        else:
            # 只保留最近 window_days+warmup_n 个交易日（含预热），控制回放规模
            keep = window_days + warmup_n
            if len(dates) > keep:
                dates, closes, vols = dates[-keep:], closes[-keep:], vols[-keep:]
            if len(dates) < warmup_n + 20:
                raise DataError("有效交易日不足（{} 天），需至少预热 {} 天 + 20 个统计日".format(
                    len(dates), warmup_n))
        return demo_flag, dates, closes, vols, hist, warmup_n, skipped

    # ================= 演示库 =================
    def run_demo(self, reset=True):
        series = self.market.ensure_demo_series(force=reset)
        if reset:
            self.ledger.reset(float(self.acct.get("initial_cash", 500)))
        dates = series["dates"]
        closes = series["index_close"]
        vols = series["index_vol"]
        hist = {code: {d: nav for d, nav in zip(dates, series["funds"][code])}
                for code in series["funds"]}
        nav_series = self._ffill_navs(dates, hist)
        rows = self.simulate(self.ledger, dates, closes, vols, nav_series)
        meta = self.account()
        meta["start_date"] = dates[0]
        meta["end_date"] = util.add_days(dates[0], HALF_YEAR_DAYS)
        meta["demo"] = True
        self.ledger.set_account_meta(meta)
        totals = [r["total"] for r in rows]
        return {
            "ok": True, "source": "demo", "dates": len(rows),
            "start": dates[0], "end": dates[-1],
            "start_value": round(totals[0], 2) if totals else 0,
            "end_value": round(totals[-1], 2) if totals else 0,
            "note": series.get("note", "演示合成数据"),
        }

    # ================= 界面取数 =================
    def fund_status(self):
        out = []
        pool_items = self._pool_items()
        for item in pool_items:
            code = item["code"]
            entry = dict(item)
            entry["source"] = self.pool_source()
            try:
                if self.demo:
                    s = self.market.demo_series() or {}
                    fs = s.get("funds", {}).get(code)
                    entry["nav"] = fs[-1] if fs else None
                    entry["nav_date"] = s.get("dates", [None])[-1] if fs else None
                else:
                    d, nav = self.market.fund_latest(code)
                    entry["nav_date"], entry["nav"] = d, nav
                entry["ok"] = bool(entry.get("nav"))
                if not entry.get("name"):
                    entry["name"] = self.market.fund_name(code)
            except DataError as e:
                entry["ok"] = False
                entry["error"] = str(e)
            out.append(entry)
        out.sort(key=lambda x: (x.get("kind") != "bond",
                                -(x.get("mom20") if x.get("mom20") is not None else -9)))
        return out

    def nav_latest_map(self):
        out = {}
        for f in self._pool_items():
            code = f["code"]
            try:
                if self.demo:
                    s = self.market.demo_series() or {}
                    fs = s.get("funds", {}).get(code)
                    if fs:
                        out[code] = (s.get("dates", [None])[-1], fs[-1])
                else:
                    d, nav = self.market.fund_latest(code)
                    if d:
                        out[code] = (d, nav)
            except DataError:
                pass
        return out

    def state_payload(self):
        meta = self.account()
        today = util.today_str()
        nav_map = self.nav_latest_map()
        val = self._valuation(nav_map)
        pos = self.ledger.positions()
        target = float(meta.get("target_value", 800))
        initial = float(meta.get("initial_cash", 500))
        start = meta.get("start_date") or today
        end = meta.get("end_date") or util.add_days(start, HALF_YEAR_DAYS)
        days_left = max(0, (util.parse_d(end) - util.parse_d(today)).days)
        est_td = len(util.trading_days_between(today, end)) if days_left else 0
        total = val["total"]
        need_ret = None
        if total > 0 and est_td > 0:
            need_ret = (target / total) ** (1.0 / est_td) - 1.0
        positions = []
        for code, shares in pos.items():
            item = self.pool_item(code)
            pair = nav_map.get(code)
            value = round(shares * pair[1], 2) if pair and pair[1] else 0.0
            cost, _sh = self.ledger.basis(code)
            cost = round(cost, 2)
            pnl = round(value - cost, 2)
            pnl_pct = round((value - cost) / cost, 4) if cost > 0 else None
            positions.append({
                "code": code,
                "name": (item or {}).get("name") or code,
                "kind": self._kind(code),
                "role": (item or {}).get("role", ""),
                "shares": shares,
                "nav": pair[1] if pair else None,
                "nav_date": pair[0] if pair else None,
                "value": value,
                "cost": cost,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            })
        positions.sort(key=lambda x: -x["value"])
        # 待执行(pending) + 已提交待T+1确认(submitted) 一并返回，前端按状态展示
        pending = self.ledger.pending_orders() + self.ledger.submitted_orders()
        for o in pending:
            o["fund_name"] = self._name_of(o["fund_code"])
            if o["status"] == "submitted":
                submit_at = o.get("submit_at") or o.get("created") or util.now_iso()
                o["nav_value_date"] = util.nav_value_date(submit_at)
                o["confirm_date"] = util.add_trading_days(o["nav_value_date"], 1)
        last_rec = self.ledger.get_records(1)
        last_snap = self.ledger.latest_snapshot()
        online = None
        try:
            online = self.market.probe_online()
        except DataError:
            online = False
        if self.demo:
            ds_label = "演示合成数据"
        else:
            lbl = self.market.provider_label()
            ds_label = lbl if online else lbl + "（离线缓存）"
        usage = self.market.usage()
        pool_items = self._pool_items()
        pool_info = {
            "count": len(pool_items),
            "equity": sum(1 for f in pool_items if f.get("kind") == "equity"),
            "bond": sum(1 for f in pool_items if f.get("kind") == "bond"),
            "source": self.pool_source(),
            "updated": self.pool_updated(),
            "min_total": int((settings.screening_of(self.cfg)
                              or {}).get("min_total", 8)),
            "screening_enabled": bool((settings.screening_of(self.cfg)
                                       or {}).get("enabled")),
        }
        return {
            "name": meta.get("name"),
            "demo": bool(self.demo),
            "exec_mode": meta.get("exec_mode", "manual"),
            "data_source": ds_label,
            "today": today,
            "start": start,
            "end": end,
            "days_left": days_left,
            "est_trading_days": est_td,
            "initial": initial,
            "target": target,
            "cash": val["cash"],
            "mv_eq": val["mv_eq"],
            "mv_bond": val["mv_bond"],
            "total": total,
            "fees": self.ledger.fees(),
            "gain": total - initial,
            "gain_pct": total / initial - 1 if initial else 0,
            "gap": target - total,
            "need_daily_ret": need_ret,
            "api_usage": usage,
            "pool_info": pool_info,
            "positions": positions,
            "pending_orders": pending,
            "last_record": last_rec[0] if last_rec else None,
            "last_snapshot": last_snap,
            "warnings": self.market.warnings[-8:],
        }
