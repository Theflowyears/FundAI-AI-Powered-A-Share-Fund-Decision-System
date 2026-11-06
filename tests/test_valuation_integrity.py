# -*- coding: utf-8 -*-
"""回归测试：**估值不完整时绝不产生假回撤风控**（2026-09-16 事故）。

事故现场（用户原话）：
> 「风险过滤信号虽未触发极端预警，但账户自峰值回撤已达58.2%，触发硬性风控机制，
>   强制要求权益仓位降至25%并进入5日防御冷却期。统计数据模块是不是有问题？
>   1000的资金哪里回撤58了？！」

真实数据：
  * 账本 cash=121.01，5 只持仓市值合计 571.21，债基 299.99 → 真实 total ≈ **992.21**
  * peak_total = **1007.42** → 真实回撤只有 **1.51%**（远不到 15% 阈值）
  * 但那次 run_daily 算出的 total 是 **421.00** = 现金 121.01 + 债基 299.99，
    **正好少了全部股基 571.21**；研判原文里写着"当前股基市值为0" → 铁证
  * 于是 1 - 421/1007.42 = **58.2%** → 误触发"组合回撤 ≥15%" → 强制 25% + 5 天冷却

根因链条（三层，逐层都要堵）：
  ① `nav_latest_map` 取价失败 → 该持仓整条不进 dict（在线失败 + 窗口过滤后缓存为空）
  ② `_valuation` 把"没取到价"当成"价值 0"（`continue`），total 静默缩水
  ③ `strategy.plan` 拿这个被低估的 total 去算回撤 → 假信号 → 触发硬风控
"""
import os
import shutil
import tempfile
import unittest

from fundai import settings, strategy, util
from fundai.engine import Engine
from fundai.ledger import Ledger


class ValuationCompletenessTest(unittest.TestCase):
    """② 估值层：缺价必须**如实报出来**，不能静默当 0。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fundai_val_")
        self.led = Ledger(os.path.join(self.tmp, "l.db"))
        self.cfg = settings.load_config()
        self.eng = Engine(self.cfg, self.led)
        # 账本要有钱才能成交（exec_buy 现金不足会返回 ok=False，不会静默扣款）
        self.led.add_cash(1000.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _buy(self, code, shares, nav):
        res = self.led.exec_buy(code, util.today_str(), shares, nav, 0.0,
                                budget=shares * nav)
        self.assertTrue(res.get("ok"), "建仓失败：{}".format(res))
        self.assertIn(code, self.led.positions(), "建仓后应出现在持仓里")

    def test_missing_nav_marks_valuation_incomplete(self):
        self._buy("002199", 300.0, 1.0)     # 股基
        self._buy("270049", 200.0, 1.0)     # 债基
        full = self.eng._valuation({"002199": ("2026-09-15", 0.881),
                                    "270049": ("2026-09-15", 1.2443)})
        self.assertTrue(full["complete"])
        self.assertEqual(full["missing"], [])
        self.assertAlmostEqual(full["total"],
                               full["cash"] + 300 * 0.881 + 200 * 1.2443, places=2)

        # 股基缺价（模拟取价失败）→ total 缩水，但必须明确标记为不完整
        partial = self.eng._valuation({"270049": ("2026-09-15", 1.2443)})
        self.assertFalse(partial["complete"], "缺价必须标记为不完整")
        self.assertEqual(partial["missing"], ["002199"])
        self.assertLess(partial["total"], full["total"])
        self.assertAlmostEqual(partial["total"],
                               partial["cash"] + 200 * 1.2443, places=2)

    def test_last_known_nav_reads_cache_without_date_filter(self):
        """① 兜底取价：不受 need_from 窗口影响，只认缓存里最后一条净值。"""
        code = "999999"        # 随便挑个不会与真实池冲突的代码
        p = self.eng.market._nav_cache_path(code)
        util.save_json(p, {"items": {"2026-09-10": 1.5, "2026-09-15": 1.6}})
        try:
            d, nav = self.eng.last_known_nav(code)
            self.assertEqual(d, "2026-09-15")
            self.assertAlmostEqual(nav, 1.6)
            # 池 ∪ 持仓之外的代码也要能补上
            out = self.eng._fill_known([code], {})
            self.assertIn(code, out)
            self.assertEqual(out[code], ("2026-09-15", 1.6))
        finally:
            try:
                p.unlink()
            except OSError:
                pass

    def test_valuation_incomplete_suppresses_drawdown_risk(self):
        """③ 风控层：估值不完整时**不准**触发回撤类硬风控（本次事故的核心）。"""
        cfg = settings.load_config()
        cfg["strategy"] = dict(cfg.get("strategy") or {})
        st = cfg["strategy"]
        st["risk_overlay_enable"] = False
        st["position_band_max"] = 0.0
        st["liquidity_reserve_yuan"] = 0.0
        ctx = {"score": 0, "cash": 121.0, "committed": 0.0, "eq_mv": 0.0,
               "bond_mv": 300.0, "total": 421.0, "initial": 1000.0,
               "peak_total": 1007.42, "equity_codes": ["002199"],
               "names": {"002199": "军工"}, "funds_mom": {"002199": 0.05},
               "funds_vol": {"002199": 0.02}, "funds_mom5": {}, "fund_pnl": {},
               "eq_hold_mv": {}, "sell_eq_by_code": {}, "locked_eq_by_code": {},
               "bond_sellable_mv": 300.0, "pending_buy_codes": set(),
               "pending_sell_codes": set(), "defensive": False,
               "risk_armed": True, "date": "2026-09-16",
               "funds_factors": {}, "ma_break": {"cut": 0.0}}
        # 不完整：total=421 看起来像回撤 58.2%，但**必须不触发**
        bad = dict(ctx, valuation_complete=False, valuation_missing=["002199"])
        plan = strategy.plan(cfg, bad)
        joined = " ".join(plan.get("msgs") or [])
        self.assertNotIn("组合回撤", joined,
                         "估值不完整时不该报回撤风控：{}".format(joined))
        self.assertIn("估值不完整", joined, "要明确说明为什么没做风控")
        self.assertIsNone(plan.get("risk_kind"))
        # 完整：同样数字才允许触发（证明闸门是"完整性"而不是"数字大小"）
        good = dict(ctx, valuation_complete=True, valuation_missing=[])
        plan2 = strategy.plan(cfg, good)
        joined2 = " ".join(plan2.get("msgs") or [])
        self.assertIn("组合回撤", joined2,
                      "估值完整时该触发就触发（否则闸门把真信号也挡了）")

    def test_drawdown_message_shows_real_numbers(self):
        """回归断言：正常估值下的回撤应是"真实"的 ~1.5%，而不是 58%。"""
        self._buy("002199", 337.07, 0.881)
        peak = 1007.42
        val = self.eng._valuation({"002199": ("2026-09-15", 0.881)})
        dd = 1.0 - val["total"] / peak
        self.assertLess(abs(dd), 0.15,
                        "真实回撤不该接近风控阈值：total={} dd={:.2%}".format(
                            val["total"], dd))


if __name__ == "__main__":
    unittest.main()
