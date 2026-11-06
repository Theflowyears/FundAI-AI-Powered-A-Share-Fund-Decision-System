# -*- coding: utf-8 -*-
"""组合构建层：权重约束、金额取整、回退与计划集成。"""
import copy
import unittest

from fundai import portfolio, settings, strategy


def _cfg(**over):
    cfg = {
        "strategy": {
            "portfolio_method": "equal",
            "max_fund_weight": 0.45,
            "max_theme_weight": 0.70,
            "min_order_yuan": 10.0,
        }
    }
    cfg["strategy"].update(over)
    return cfg


def _factors(mom=None, vol=None, rets=None):
    mom = mom or {}
    vol = vol or {}
    rets = rets or {}
    return {c: {"mom": mom.get(c), "vol": vol.get(c), "rets": rets.get(c)}
            for c in set(list(mom) + list(vol) + list(rets))}


class AllocateTest(unittest.TestCase):
    def test_equal_weights_sum_one(self):
        a = portfolio.allocate(_cfg(), ["a", "b", "c"], amount=900)
        self.assertTrue(a["ok"])
        self.assertAlmostEqual(sum(a["weights"].values()), 1.0, places=9)
        self.assertEqual(sum(a["amounts"].values()), 900)
        self.assertTrue(all(v >= 10 for v in a["amounts"].values()))

    def test_inv_vol_prefers_low_vol(self):
        fac = _factors(vol={"a": 0.01, "b": 0.02, "c": 0.04},
                       mom={"a": 0.1, "b": 0.1, "c": 0.1})
        a = portfolio.allocate(_cfg(portfolio_method="inv_vol"),
                               ["a", "b", "c"], factors=fac, amount=1000)
        self.assertTrue(a["ok"])
        self.assertGreater(a["weights"]["a"], a["weights"]["b"])
        self.assertGreater(a["weights"]["b"], a["weights"]["c"])
        self.assertAlmostEqual(sum(a["weights"].values()), 1.0, places=9)

    def test_missing_vol_falls_back_equal(self):
        a = portfolio.allocate(_cfg(portfolio_method="inv_vol"),
                               ["a", "b", "c"], amount=300)
        self.assertTrue(a["ok"])
        for v in a["weights"].values():
            self.assertAlmostEqual(v, 1.0 / 3.0, places=9)

    def test_fund_cap(self):
        a = portfolio.allocate(_cfg(max_fund_weight=0.40),
                               ["a", "b", "c"], amount=1000)
        self.assertTrue(a["ok"])
        self.assertLessEqual(max(a["weights"].values()), 0.40 + 1e-9)

    def test_theme_cap(self):
        a = portfolio.allocate(_cfg(max_theme_weight=0.55), ["a", "b", "c"],
                               themes={"a": "半导体", "b": "半导体",
                                       "c": "证券"}, amount=1000)
        self.assertTrue(a["ok"])
        s = a["weights"]["a"] + a["weights"]["b"]
        self.assertLessEqual(s, 0.55 + 1e-6)
        self.assertGreater(a["weights"]["c"], 0.4)

    def test_all_same_theme_relaxes_cap(self):
        a = portfolio.allocate(_cfg(max_theme_weight=0.55), ["a", "b"],
                               themes={"a": "半导体", "b": "半导体"},
                               amount=1000)
        self.assertTrue(a["ok"])
        self.assertAlmostEqual(sum(a["weights"].values()), 1.0, places=9)
        self.assertTrue(any("不可行" in n for n in a["notes"]))

    def test_min_amount_rejects(self):
        a = portfolio.allocate(_cfg(), ["a", "b", "c"], amount=25)
        self.assertFalse(a["ok"])
        self.assertEqual(a["amounts"], {})
        self.assertIn("起购点", a["reason"])

    def test_amounts_integer_and_sum(self):
        a = portfolio.allocate(_cfg(), ["a", "b", "c"], amount=1001)
        self.assertTrue(a["ok"])
        self.assertEqual(sum(a["amounts"].values()), 1001)
        for v in a["amounts"].values():
            self.assertIsInstance(v, int)
            self.assertGreaterEqual(v, 10)

    def test_deterministic(self):
        fac = _factors(vol={"a": 0.01, "b": 0.02, "c": 0.03})
        x = portfolio.allocate(_cfg(portfolio_method="inv_vol"),
                               ["a", "b", "c"], factors=fac, amount=800)
        y = portfolio.allocate(_cfg(portfolio_method="inv_vol"),
                               ["a", "b", "c"], factors=fac, amount=800)
        self.assertEqual(x["weights"], y["weights"])
        self.assertEqual(x["amounts"], y["amounts"])

    def test_min_var_sums_and_nonnegative(self):
        rets = {
            "a": [0.01, -0.01, 0.012, -0.008, 0.005] * 6,
            "b": [0.02, -0.02, 0.025, -0.018, 0.012] * 6,
            "c": [0.0, 0.001, -0.001, 0.002, -0.001] * 6,
        }
        a = portfolio.allocate(_cfg(portfolio_method="min_var"),
                               ["a", "b", "c"],
                               factors=_factors(rets=rets), amount=1000)
        self.assertTrue(a["ok"])
        self.assertAlmostEqual(sum(a["weights"].values()), 1.0, places=8)
        self.assertTrue(all(v >= 0 for v in a["weights"].values()))
        self.assertGreater(a["weights"]["c"], a["weights"]["a"])

    def test_unknown_method(self):
        a = portfolio.allocate(_cfg(portfolio_method="mvo_full"),
                               ["a", "b"], amount=100)
        self.assertFalse(a["ok"])
        self.assertIn("未知组合方法", a["reason"])


def _plan_ctx(codes):
    return {
        "score": 10, "cash": 1000.0, "committed": 0.0, "eq_mv": 0.0,
        "bond_mv": 0.0, "total": 1000.0, "initial": 1000.0,
        "peak_total": 1000.0, "bond_code": "270049",
        "bond_sellable_mv": 0.0, "bond_locked_mv": 0.0,
        "eq_hold_mv": {}, "sell_eq_by_code": {}, "locked_eq_by_code": {},
        "fund_pnl": {}, "funds_mom": {c: 0.10 - 0.02 * i
                                      for i, c in enumerate(codes)},
        "funds_mom5": {c: 0.01 for c in codes},
        "funds_vol": {c: 0.02 for c in codes},
        "funds_factors": {c: {"mom": 0.10 - 0.02 * i, "mom5": 0.01,
                              "vol": 0.02, "bias": 0.01, "rsi": 50,
                              "rets": [0.001] * 60}
                          for i, c in enumerate(codes)},
        "equity_codes": list(codes),
        "names": {codes[0]: "科创50", codes[1]: "半导体芯片",
                  codes[2]: "证券公司"},
        "confidence": 0.6, "defensive": False, "theme_heat": {},
        "risk_overlay": {"action": "off", "scale": 1.0, "reason": "已关闭"},
        "pending_sell_codes": set(), "pending_buy_codes": set(),
        "risk_armed": True, "date": "2026-10-27",
    }


class PlanIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(settings.load_config())
        st = self.cfg["strategy"]
        st.update({
            "equity_base_mode": "fixed", "equity_base_fixed": 0.90,
            "position_band_max": 0.0, "equity_hard_cap": 0.95,
            "liquidity_reserve_yuan": 0.0, "dynamic_weights": False,
            "top_n": 3, "theme_max": 1, "relative_momentum": False,
            "min_momentum": 0.0,
        })
        st["risk"]["fund_stop_loss_pct"] = -1.0
        st["risk"]["fund_take_profit_pct"] = 1.0
        st["risk"]["fund_take_profit_full_pct"] = 1.0
        self.codes = ["011609", "008888", "012700"]

    def test_legacy_has_no_portfolio_payload(self):
        self.cfg["strategy"]["portfolio_method"] = "legacy"
        plan = strategy.plan(self.cfg, _plan_ctx(self.codes))
        self.assertIsNone(plan["portfolio"])
        self.assertTrue(plan["orders"], "应有加仓单")

    def test_equal_plan_returns_weights_and_amounts(self):
        self.cfg["strategy"]["portfolio_method"] = "equal"
        plan = strategy.plan(self.cfg, _plan_ctx(self.codes))
        pf = plan["portfolio"]
        self.assertIsNotNone(pf)
        self.assertEqual(pf["method"], "equal")
        self.assertEqual(set(pf["weights"]), set(self.codes))
        buys = [o for o in plan["orders"] if o["action"] == "buy"]
        self.assertEqual(sum(o["amount_yuan"] for o in buys),
                         sum(pf["amounts"].values()))
        self.assertAlmostEqual(sum(pf["weights"].values()), 1.0, places=6)

    def test_inv_vol_plan_uses_vol(self):
        self.cfg["strategy"]["portfolio_method"] = "inv_vol"
        ctx = _plan_ctx(self.codes)
        ctx["funds_vol"] = {"011609": 0.01, "008888": 0.03, "012700": 0.02}
        for c, v in ctx["funds_vol"].items():
            ctx["funds_factors"][c]["vol"] = v
        plan = strategy.plan(self.cfg, ctx)
        w = plan["portfolio"]["weights"]
        self.assertGreater(w["011609"], w["008888"])

    def _held_ctx(self, pnl):
        ctx = _plan_ctx(self.codes)
        ctx.update({"cash": 500.0, "eq_mv": 500.0, "total": 1000.0,
                    "eq_hold_mv": {"011609": 500.0},
                    "sell_eq_by_code": {"011609": 500.0},
                    "locked_eq_by_code": {"011609": 0.0}})
        ctx["fund_pnl"] = {"011609": {
            "value": 500.0, "cost": 500.0 / (1.0 + pnl),
            "pnl_pct": pnl, "sellable_mv": 500.0, "locked_mv": 0.0}}
        return ctx

    def test_fund_stop_loss_toggle(self):
        st = self.cfg["strategy"]
        st["portfolio_method"] = "legacy"
        st["risk"].update({"fund_stop_loss_pct": -0.08,
                           "fund_stop_loss_enable": True,
                           "fund_take_profit_enable": False})
        plan = strategy.plan(self.cfg, self._held_ctx(-0.20))
        self.assertIn("011609", [o["code"] for o in plan["orders"]
                                if o["action"] == "sell"])
        st["risk"]["fund_stop_loss_enable"] = False
        plan2 = strategy.plan(self.cfg, self._held_ctx(-0.20))
        self.assertNotIn("011609", [o["code"] for o in plan2["orders"]
                                   if o["action"] == "sell"])

    def test_fund_take_profit_toggle(self):
        st = self.cfg["strategy"]
        st["portfolio_method"] = "legacy"
        st["risk"].update({"fund_stop_loss_enable": False,
                           "fund_take_profit_enable": True,
                           "fund_take_profit_pct": 0.10,
                           "fund_take_profit_full_pct": 0.25})
        plan = strategy.plan(self.cfg, self._held_ctx(0.30))
        self.assertIn("011609", [o["code"] for o in plan["orders"]
                                if o["action"] == "sell"])
        st["risk"]["fund_take_profit_enable"] = False
        plan2 = strategy.plan(self.cfg, self._held_ctx(0.30))
        self.assertNotIn("011609", [o["code"] for o in plan2["orders"]
                                   if o["action"] == "sell"])

    def test_adaptive_uses_legacy_in_uptrend(self):
        self.cfg["strategy"].update({"portfolio_method": "adaptive",
                                     "adaptive_signal": "mom20",
                                     "adaptive_bull_score": 25})
        ctx = _plan_ctx(self.codes)
        ctx["ma_align"] = True
        ctx["market_mom"] = 0.05
        ctx["score"] = 30
        plan = strategy.plan(self.cfg, ctx)
        pf = plan["portfolio"]
        self.assertIsNotNone(pf)
        self.assertEqual(pf["method"], "adaptive/legacy")
        self.assertAlmostEqual(sum(pf["weights"].values()), 1.0, places=6)
        self.assertTrue(any("趋势上行" in m for m in plan["msgs"]))

    def test_adaptive_uses_equal_when_not_uptrend(self):
        self.cfg["strategy"].update({"portfolio_method": "adaptive",
                                     "adaptive_bull_score": 25})
        ctx = _plan_ctx(self.codes)
        ctx["ma_align"] = False
        ctx["score"] = 10
        plan = strategy.plan(self.cfg, ctx)
        pf = plan["portfolio"]
        self.assertIsNotNone(pf)
        self.assertEqual(pf["method"], "adaptive/equal")
        for v in pf["weights"].values():
            self.assertAlmostEqual(v, 1.0 / 3.0, places=6)


if __name__ == "__main__":
    unittest.main()
