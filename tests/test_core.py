# -*- coding: utf-8 -*-
"""核心断言测试（纯标准库 unittest，不联网）。

运行：  python -m unittest discover -s tests -p 'test_*.py'
覆盖：资金守恒/尾差/余额校验、交易日历(K线)、跨日反向挂单去重、
      news 缓存 schema、指数K线收盘bar判定、URL token 脱敏。
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fundai import ledger as ledger_mod
from fundai import news as news_mod
from fundai import settings, util
from fundai.datasource import Market
from fundai.engine import Engine
from fundai.ledger import Ledger

# 收盘判定时刻（与 datasource 保持一致）
CLOSE_TIME = "15:05"


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self._old_cal = util.calendar_dates()

    def tearDown(self):
        util._CALENDAR = self._old_cal

    def test_weekday_fallback(self):
        """未注册日历时按工作日近似（旧行为）。"""
        self.assertEqual(util.add_trading_days("2024-09-30", 1), "2024-10-01")
        self.assertEqual(util.prev_trading_day("2024-10-08"), "2024-10-07")

    def test_kline_calendar_skips_holiday(self):
        """注册K线日历后，国庆长假被正确跳过（audit 举证例）。"""
        util.set_calendar(["2024-09-27", "2024-09-30", "2024-10-08",
                           "2024-10-09", "2024-10-10", "2024-10-11",
                           "2024-10-14"])
        self.assertEqual(util.add_trading_days("2024-09-30", 1), "2024-10-08")
        self.assertEqual(util.prev_trading_day("2024-10-08"), "2024-09-30")
        self.assertEqual(util.prev_trading_day("2024-10-11"), "2024-10-10")

    def test_nav_value_date_over_holiday(self):
        util.set_calendar(["2024-09-30", "2024-10-08", "2024-10-09"])
        # 15:00 前提交 → 当日净值；节假日 10-01~10-07 不存在，10-08 15:01 提交顺延 10-09
        self.assertEqual(util.nav_value_date("2024-09-30 09:00:00"), "2024-09-30")
        self.assertEqual(util.nav_value_date("2024-10-08 15:01:00"), "2024-10-09")
        self.assertEqual(util.nav_value_date("2024-10-08 14:59:00"), "2024-10-08")

    def test_trading_days_between_uses_calendar(self):
        util.set_calendar(["2024-09-30", "2024-10-08", "2024-10-09",
                           "2024-10-10", "2024-10-11", "2024-10-14"])
        days = util.trading_days_between("2024-10-01", "2024-10-14")
        self.assertEqual(days, ["2024-10-08", "2024-10-09", "2024-10-10",
                                "2024-10-11", "2024-10-14"])


class RedactTest(unittest.TestCase):
    def test_token_redacted(self):
        s = util._redact("GET https://api.zhituapi.com/a?token=SECRET-AB-1 err")
        self.assertNotIn("SECRET-AB-1", s)
        self.assertIn("token=***", s)

    def test_bearer_redacted(self):
        s = util._redact("Authorization: Bearer sk-12345 -> 401")
        self.assertNotIn("sk-12345", s)


class LedgerMoneyTest(unittest.TestCase):
    def setUp(self):
        self.lg = Ledger(":memory:", initial_cash=1000.0)

    def test_balance_gate_no_side_effect(self):
        r = self.lg.exec_buy("008888", "2026-09-01", shares=100.0, nav=20.0,
                             fee=0.0, budget=5000.0)
        self.assertFalse(r["ok"])
        self.assertEqual(self.lg.positions(), {})
        self.assertEqual(self.lg.cash(), 1000.0)
        chk = self.lg.check_consistency(raise_on_mismatch=True)
        self.assertTrue(chk["ok"])

    def test_buy_tail_refunded(self):
        # 100 元、净值 1.2345 → 份额 81.00，占用 = 81*1.2345 = 99.9945 → 扣 99.99，
        # 0.01 尾差留在现金（旧代码整额扣款后静默蒸发）
        import math
        shares = math.floor(100.0 / 1.2345 * 100) / 100
        r = self.lg.exec_buy("008888", "2026-09-01", shares=shares, nav=1.2345,
                             fee=0.0, budget=100.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["debit"], 99.99)
        self.assertEqual(self.lg.cash(), 900.01)
        cost, sh = self.lg.basis("008888")
        self.assertAlmostEqual(cost, 99.9945, places=2)

    def test_fee_included_in_debit(self):
        r = self.lg.exec_buy("008888", "2026-09-01", shares=100.0, nav=1.0,
                             fee=0.15, budget=200.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["debit"], 100.15)
        self.assertEqual(self.lg.cash(), 899.85)
        self.assertEqual(self.lg.fees(), 0.15)

    def test_consistency_zero_after_sequence(self):
        r = self.lg.exec_buy("008888", "2026-09-01", shares=81.0, nav=1.2345,
                             fee=0.0, budget=100.0)
        sell = self.lg.exec_sell("008888", "2026-09-20", 40.0, 1.30,
                                 lambda d: 0.0)
        self.assertGreater(sell["net"], 0)
        chk = self.lg.check_consistency(raise_on_mismatch=True)
        self.assertEqual(chk["diff"], 0.0)
        self.assertTrue(chk["ok"])

    def test_sellable_lock_min_hold(self):
        self.lg.exec_buy("008888", "2026-09-01", shares=100.0, nav=1.0,
                         fee=0.0, budget=200.0)
        sellable, locked = self.lg.sellable("008888", "2026-09-05", 7)
        self.assertEqual(sellable, 0.0)
        self.assertAlmostEqual(locked, 100.0, places=2)
        sellable, locked = self.lg.sellable("008888", "2026-09-09", 7)
        self.assertAlmostEqual(sellable, 100.0, places=2)

    def test_reset_clears_flow(self):
        self.lg.exec_buy("008888", "2026-09-01", shares=10.0, nav=1.0,
                         fee=0.0, budget=100.0)
        self.lg.reset(500.0)
        self.assertEqual(self.lg.cash(), 500.0)
        self.assertEqual(self.lg.positions(), {})
        chk = self.lg.check_consistency(raise_on_mismatch=True)
        self.assertTrue(chk["ok"])
        self.assertEqual(chk["seed"], 500.0)

    def test_legacy_baseline_no_false_alarm(self):
        # 老库首次一致性检查以当前现金为基线（此前历史无流水，不误报）
        lg2 = Ledger(":memory:", initial_cash=500.0)
        chk = lg2.check_consistency(raise_on_mismatch=True)
        self.assertTrue(chk["ok"])
        self.assertEqual(chk["seed"], 500.0)


class OrderDedupeTest(unittest.TestCase):
    def _engine(self):
        cfg = settings.load_config()
        lg = Ledger(":memory:", initial_cash=1000.0)
        return Engine(cfg, lg), lg

    def test_opposite_pending_blocked(self):
        eng, lg = self._engine()
        # 昨日卖 A 挂起未执行
        lg.add_order("2026-09-08", "008888", "sell", 300.0, "昨日卖出")
        created = eng._place_orders(
            "2026-09-09", {"orders": [{"code": "008888", "action": "buy",
                                       "amount_yuan": 300.0, "note": "今日买入"}]},
            ledger=lg)
        self.assertEqual(created, [])
        self.assertEqual(len(lg.pending_orders()), 1)

    def test_same_direction_dedupe_kept(self):
        eng, lg = self._engine()
        lg.add_order("2026-09-08", "008888", "buy", 200.0, "旧买")
        created = eng._place_orders(
            "2026-09-09", {"orders": [{"code": "008888", "action": "buy",
                                       "amount_yuan": 200.0, "note": "重复"}]},
            ledger=lg)
        self.assertEqual(created, [])
        self.assertEqual(len(lg.pending_orders()), 1)


class NewsCacheTest(unittest.TestCase):
    def setUp(self):
        self.date_s = "2099-01-05"
        self.path = news_mod.news_cache_file(self.date_s)

    def tearDown(self):
        for p in (self.path,):
            try:
                if Path(str(p)).exists():
                    os.remove(str(p))
            except OSError:
                pass

    def test_old_cache_without_feed_invalid_but_offline_fallback(self):
        # 旧缓存（无 feed）：不应直接判有效；离线失败时回退旧缓存（保留 bull/bear）
        util.save_json(self.path, {"ok": True, "date": self.date_s, "items": 10,
                                   "net": 0, "score": 0, "amplitude": 8,
                                   "bull": [], "bear": []})
        old_get = util.http_get_json

        def stub(*a, **k):
            from fundai.util import DataError
            raise DataError("stub offline")

        util.http_get_json = stub
        try:
            obj = news_mod.load_news(self.date_s, amplitude=8, cfg=None)
        finally:
            util.http_get_json = old_get
        self.assertTrue(obj.get("ok"))  # 回退旧缓存供文案
        self.assertNotIsInstance(obj.get("feed"), list)  # 无 feed 不能打标

    def test_new_cache_with_feed_served_without_fetch(self):
        feed = [{"id": "abc123", "title": "测试", "text": "利好",
                 "auto_label": "bull", "auto_strength": 2,
                 "source": "x", "time": "09:00", "sectors": [], "funds": []}]
        util.save_json(self.path, {"ok": True, "date": self.date_s,
                                   "items": 1, "feed": feed,
                                   "schema": 2, "bull": [], "bear": [],
                                   "net": 2, "score": 16, "amplitude": 8})
        obj = news_mod.load_news(self.date_s, amplitude=8, cfg=None)
        self.assertTrue(obj["ok"])
        self.assertEqual(len(obj["feed"]), 1)
        self.assertEqual(obj["schema"], 2)


class KlineFinalBarTest(unittest.TestCase):
    def test_bar_is_final(self):
        self.assertFalse(Market._bar_is_final("2026-09-09 12:42:10"))
        self.assertTrue(Market._bar_is_final("2026-09-09 15:05:00"))
        self.assertTrue(Market._bar_is_final("2026-09-09 20:10:00"))
        self.assertTrue(Market._bar_is_final("2026-09-08 23:59:59"))

    def test_drop_partial_today(self):
        today = util.today_str()
        items = {"2026-09-08": [4500.0, 170000000.0], today: [4520.0, 95000000.0]}
        # 盘中(15:05 前)抓到的当日 bar → 剔除，回到上一交易日
        out = Market._drop_partial_today(dict(items), "{} 12:42:10".format(today))
        self.assertNotIn(today, out)
        # 收盘后抓的当日 bar → 保留
        out2 = Market._drop_partial_today(dict(items), "{} 20:00:00".format(today))
        self.assertIn(today, out2)

    def test_kline_seq_filter(self):
        items = {"2026-09-07": [4500.0, 1.0], "2026-09-08": [4510.0, 2.0]}
        seq = Market._kline_seq(items, "2026-09-08")
        self.assertEqual([x[0] for x in seq], ["2026-09-08"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
