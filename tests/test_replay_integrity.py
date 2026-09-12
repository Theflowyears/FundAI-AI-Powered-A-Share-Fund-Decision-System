# -*- coding: utf-8 -*-
"""回放/风控的回归测试：锁死 2026-09-11 修掉的四个会静默毁掉回测结论的缺陷。

1. **候选池回退**：`ctx["equity_codes"] == []` 曾被 `or 静态候选池` 吞掉，
   于是回放首日把 700 元买到了区间内根本没有净值数据的基金上 → 挂单永不成交。
2. **组合级风控永久锁死**：相对本金亏损的判据不看评分也不带迟滞，一旦触发就
   每天都触发（实测 2019-05 之后连续 7 年权益被压到 ~1%，全历史 −18.6%）。
   现在可再入 + 迟滞（回撤收敛到阈值 −5pp 才重新武装）。
3. **挂单作废**：基金中途停止公布净值时，挂单必须到期作废并释放资金占用，
   否则 `committed` 会永久占满现金。
4. **指数缓存双键合并**：主指数K线可能分别存在于智兔键与东财 secid 键下，
   读取必须取并集（事故中单文件缺失曾让 `calib.load_closes()` 归零）。

全部离线；不联网、不碰真实缓存文件。
"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fundai import settings, strategy, util
from fundai.datasource import Market
from fundai.engine import Engine
from fundai.ledger import Ledger


def _base_ctx(**over):
    ctx = {"score": 0, "cash": 1000.0, "committed": 0.0, "eq_mv": 0.0,
           "bond_mv": 0.0, "total": 1000.0, "initial": 1000.0, "peak_total": 1000.0,
           "equity_codes": [], "names": {}, "funds_mom": {}, "funds_vol": {},
           "funds_mom5": {}, "fund_pnl": {}, "eq_hold_mv": {},
           "sell_eq_by_code": {}, "locked_eq_by_code": {}, "bond_sellable_mv": 0.0,
           "pending_buy_codes": set(), "pending_sell_codes": set(),
           "defensive": False, "risk_armed": True, "date": "2026-09-11",
           "funds_factors": {}}
    ctx.update(over)
    return ctx


class PoolFallbackTest(unittest.TestCase):
    """空候选池必须被当成"当日无合格标的"，而不是回退到静态 config 池。"""

    def setUp(self):
        self.cfg = copy.deepcopy(settings.load_config())

    def test_empty_pool_gives_no_equity_buy(self):
        plan = strategy.plan(self.cfg, _base_ctx(equity_codes=[]))
        buys = [o for o in plan["orders"] if o["action"] == "buy"]
        static_codes = {f["code"] for f in settings.equity_candidates(self.cfg)}
        for o in buys:
            self.assertNotIn(o["code"], static_codes,
                             "空池时不应回退静态候选池下单：{}".format(o))

    def test_missing_key_still_allows_static_fallback(self):
        """键完全缺失（旧调用方）时保留静态池回退，避免老代码路径全部买不进。"""
        ctx = _base_ctx()
        ctx.pop("equity_codes")
        plan = strategy.plan(self.cfg, ctx)
        self.assertIn("orders", plan)


class PortfolioStopHysteresisTest(unittest.TestCase):
    """组合级风控必须"可再入 + 带迟滞"，不能天天重复触发把账户锁死。"""

    def setUp(self):
        self.cfg = copy.deepcopy(settings.load_config())
        self.pf = float(((self.cfg.get("strategy") or {}).get("risk") or {})
                        .get("portfolio_stop_pct", -0.15))

    def test_first_breach_triggers_and_disarms(self):
        # 自峰值回撤 20%（> 15%）→ 触发，并给出"未武装"状态
        ctx = _base_ctx(total=800.0, peak_total=1000.0, risk_armed=True)
        plan = strategy.plan(self.cfg, ctx)
        self.assertEqual(plan.get("risk_kind"), "portfolio_stop")
        self.assertIs(plan.get("risk_armed"), False)

    def test_not_retriggered_while_unarmed(self):
        # 已未武装 + 回撤仍很大 → 不得再触发（否则等于永久防守）
        ctx = _base_ctx(total=800.0, peak_total=1000.0, risk_armed=False)
        plan = strategy.plan(self.cfg, ctx)
        self.assertIsNone(plan.get("risk_kind"))
        self.assertIs(plan.get("risk_armed"), False)

    def test_rearms_after_recovery(self):
        # 回撤收敛到阈值 −5pp 以内 → 重新武装
        safe = 1000.0 * (1.0 - max(0.02, abs(self.pf) - 0.05)) + 1.0
        ctx = _base_ctx(total=safe, peak_total=1000.0, risk_armed=False)
        plan = strategy.plan(self.cfg, ctx)
        self.assertIs(plan.get("risk_armed"), True)


class OrderExpiryTest(unittest.TestCase):
    """回放中"基金不再公布净值"时，挂单必须作废并释放资金占用。"""

    def test_unfillable_order_expires(self):
        """直接预置一笔"没有可用净值"的买单：它必须在 ORDER_EXPIRE_DAYS 后作废。"""
        cfg = copy.deepcopy(settings.load_config())
        pool = cfg.get("pool") or []
        eq = next(f for f in pool if f.get("kind") == "equity")
        bond = next(f for f in pool if f.get("kind") == "bond")
        cfg["pool"] = [eq, bond]
        mkt = Market(cfg)
        eng = Engine(cfg, Ledger(":memory:", initial_cash=1000.0), market=mkt)
        expire = Engine.ORDER_EXPIRE_DAYS

        dates = ["2026-01-{:02d}".format(i) for i in range(1, expire + 6)]
        closes = [4000.0 + i for i in range(len(dates))]
        vols = [1e8] * len(dates)
        nav_series = {bond["code"]: {d: 1.0 for d in dates}}   # 股基完全没有净值

        ledger = Ledger(":memory:", initial_cash=1000.0)
        oid = ledger.add_order(dates[0], eq["code"], "buy", 100.0, "测试：无净值挂单")
        with mock.patch.object(eng, "_replay_micro_map", return_value=None), \
                mock.patch.object(eng, "_replay_news_map", return_value=None):
            eng.simulate(ledger, dates, closes, vols, nav_series, warmup_n=0)

        o = ledger.order_by_id(oid)
        self.assertEqual(o["status"], "skipped",
                         "无净值的买单应到期作废，而不是永久 pending（{}）".format(
                             o["status"]))
        pend = [x for x in ledger.orders()
                if x["status"] == "pending" and x["action"] == "buy"]
        self.assertEqual(pend, [])
        self.assertTrue(any("作废" in x for x in getattr(eng, "_replay_expired", [])),
                        "作废应有记录：{}".format(getattr(eng, "_replay_expired", [])))

    def test_fillable_order_still_fills(self):
        """对照组：有净值的买单必须正常成交（别把作废逻辑写过头）。"""
        cfg = copy.deepcopy(settings.load_config())
        pool = cfg.get("pool") or []
        eq = next(f for f in pool if f.get("kind") == "equity")
        bond = next(f for f in pool if f.get("kind") == "bond")
        cfg["pool"] = [eq, bond]
        mkt = Market(cfg)
        eng = Engine(cfg, Ledger(":memory:", initial_cash=1000.0), market=mkt)
        dates = ["2026-02-{:02d}".format(i) for i in range(1, 8)]
        closes = [4000.0 + i for i in range(len(dates))]
        nav_series = {eq["code"]: {d: 1.0 for d in dates},
                      bond["code"]: {d: 1.0 for d in dates}}
        ledger = Ledger(":memory:", initial_cash=1000.0)
        oid = ledger.add_order(dates[0], eq["code"], "buy", 100.0, "测试：可成交")
        with mock.patch.object(eng, "_replay_micro_map", return_value=None), \
                mock.patch.object(eng, "_replay_news_map", return_value=None):
            eng.simulate(ledger, dates, closes, [1e8] * len(dates), nav_series,
                         warmup_n=0)
        self.assertEqual(ledger.order_by_id(oid)["status"], "filled")


class QuotaGuardTest(unittest.TestCase):
    """配额守卫只能看**有配额的那条通道**（智兔），不能被东财/新浪的调用数带偏。

    2026-09-11 审计实测：当天全部通道 154 次（智兔只有个位数）就触发了
    "今日智兔配额不足（已用 154/200）"，`refresh-pool` 直接拒绝服务 ——
    因为计数改成"全通道"后，守卫还在用 `calls_today`。
    """

    def _engine(self):
        cfg = copy.deepcopy(settings.load_config())
        cfg["screening"] = dict(cfg.get("screening") or {}, enabled=True)
        # 显式给 Token：没有数据源时 refresh_pool 会在**配额守卫之前**就返回
        # "需要可用数据源"，本用例要测的是守卫本身（发行包默认没有 Token，
        # 不显式设定会让用例随环境漂移）。
        cfg.setdefault("data", {})["zhitu_token"] = "TEST-TOKEN-FOR-QUOTA-GUARD"
        mkt = Market(cfg)
        eng = Engine(cfg, Ledger(":memory:", initial_cash=1000.0), market=mkt)
        return eng, cfg

    def test_other_channels_do_not_consume_quota(self):
        eng, cfg = self._engine()
        uni = settings.universe_of(cfg) or [{"code": "011609", "name": "x"}]
        n = len(uni)
        usage = {"provider": "x", "calls_today": 190, "daily_limit": 200,
                 "zhitu_today": 5, "akshare_today": 0}
        with mock.patch.object(eng.market, "usage", return_value=usage), \
                mock.patch.object(eng.market, "fund_history",
                                  side_effect=lambda *a, **k: []):
            res = eng.refresh_pool(force=True)
        msg = str(res.get("message") or "")
        self.assertNotIn("配额不足", msg,
                         "智兔只用 5 次却被判配额不足（%d 只候选）：%s" % (n, msg))

    def test_zhitu_exhaustion_still_guarded(self):
        eng, cfg = self._engine()
        usage = {"provider": "x", "calls_today": 190, "daily_limit": 200,
                 "zhitu_today": 195, "akshare_today": 0}
        with mock.patch.object(eng.market, "usage", return_value=usage):
            res = eng.refresh_pool(force=True)
        self.assertFalse(res.get("ok"))
        self.assertIn("配额不足", str(res.get("message")))


class HeldOutsidePoolTest(unittest.TestCase):
    """持仓基金被动态池淘汰后，**仍必须估值并显示名称**（不能显示成 ¥0）。

    2026-09-11 审计实测：动态池每周重建，若某只仍持有的基金动量下滑被淘汰，
    旧实现 `nav_latest_map()` 只遍历池内代码 → 该持仓 nav=None、value=0、名称退化成代码，
    账户总值瞬间从 1007 掉到 421（看起来像钱没了，其实只是没取价）。
    """

    def test_holding_outside_pool_still_valued(self):
        cfg = copy.deepcopy(settings.load_config())
        mkt = Market(cfg)
        # 池子里只放一只债基（模拟"权益持仓已被淘汰出池"）
        bond = next(f for f in (cfg.get("pool") or []) if f.get("kind") == "bond")
        mkt_engine = Engine(cfg, Ledger(":memory:", initial_cash=1000.0), market=mkt)
        mkt_engine._pool_items = lambda: [{"code": bond["code"], "kind": "bond",
                                           "name": bond["name"]}]
        held = [c for c in ("002199", "004643", "006697") if
                settings.universe_item(cfg, c)] or ["002199"]
        # 直接建仓（用账本的公开 API，别手写 SQL 依赖表结构）
        mkt_engine.ledger.add_lot(held[0], "2026-09-01", 100.0, 2.0, 0.0)
        with mock.patch.object(mkt_engine.market, "fund_latest",
                               return_value=("2026-09-11", 2.5)):
            nav_map = mkt_engine.nav_latest_map()
            detail = mkt_engine._positions_detail(nav_map)
        self.assertIn(held[0], nav_map, "池外持仓也必须取到净值")
        row = next(r for r in detail if r["code"] == held[0])
        self.assertGreater(row["value"], 0, "池外持仓的市值不能是 0")
        self.assertNotEqual(row["name"], held[0], "应能解析出基金名称而不是裸代码")


class IndexCacheMergeTest(unittest.TestCase):
    """主指数K线必须合并两个可能的缓存键（事故中单文件缺失曾让校准全废）。"""

    def test_merge_reads_both_keys(self):
        cfg = copy.deepcopy(settings.load_config())
        # 必须显式给 Token：没有 Token 时 `_index_key()` 会返回东财 secid，
        # 两个缓存键**恰好相同**，合并逻辑就退化成单文件读取（发行包里没配 Token，
        # 曾因此让本用例在不同环境下表现不一致 —— 测试要显式控制前提条件）。
        cfg.setdefault("data", {})["zhitu_token"] = "TEST-TOKEN-FOR-MERGE-TEST"
        mkt = Market(cfg)
        with tempfile.TemporaryDirectory() as td:
            key1 = mkt._index_key()
            key2 = (cfg.get("market", {}).get("index") or {})["eastmoney_secid"]
            self.assertNotEqual(str(key1), str(key2), "本用例要求两个缓存键不同")
            p1 = Path(td) / "a.json"
            p2 = Path(td) / "b.json"
            util.save_json(p1, {"items": {"2026-01-01": [1.0, 1],
                                          "2026-01-02": [2.0, 1]}})
            util.save_json(p2, {"items": {"2026-01-02": [9.0, 1],
                                          "2026-01-03": [3.0, 1]}})
            paths = {str(key1): p1, str(key2): p2}
            with mock.patch.object(Market, "_kline_cache_path",
                                   staticmethod(lambda k: paths[str(k)])):
                items = mkt._index_cache_items()
        self.assertEqual(sorted(items), ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertEqual(items["2026-01-02"][0], 2.0)   # 先出现的键优先


if __name__ == "__main__":
    unittest.main()
