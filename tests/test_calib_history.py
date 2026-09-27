# -*- coding: utf-8 -*-
"""事件命中率校准的纯函数测试（不联网）。"""
import unittest
from datetime import datetime

from fundai import calib_history as cal
from fundai import util


class NewsNetScoreTest(unittest.TestCase):
    """净情绪聚合：抗饱和刻度换算 vs 旧口径（旧口径天天顶格、零区分度）。"""

    def test_scaled_vs_sum(self):
        from fundai import news
        feed = [{"auto_label": "bull", "auto_strength": 1}] * 240
        net, raw, n = news.net_score(feed, scale=240.0)
        self.assertAlmostEqual(net, 1.0, places=3)      # 240 点强度 = 1 个 net 单位
        self.assertEqual(raw, 240.0)
        self.assertEqual(n, 240)
        self.assertEqual(news.net_score(feed, mode="sum")[0], 8.0)   # 旧口径顶格

    def test_sign_neutral_and_clamp(self):
        from fundai import news
        feed = [{"auto_label": "bear", "auto_strength": -2},
                {"auto_label": "neutral", "auto_strength": 5}]
        net, raw, n = news.net_score(feed, scale=10.0)
        self.assertAlmostEqual(net, -0.2, places=3)
        self.assertEqual(n, 1)                          # 中性不计入
        big = [{"auto_label": "bull", "auto_strength": 8}] * 100
        self.assertEqual(news.net_score(big, scale=10.0)[0], 8.0)     # 仍限幅 ±8


class CalibrationHelperTest(unittest.TestCase):
    def _ts(self, d, hm):
        return int(datetime.strptime("{} {}".format(d, hm), "%Y-%m-%d %H:%M")
                   .replace(tzinfo=util.TZ_CN).timestamp())

    def test_trade_date_after_close_moves_to_next_day(self):
        tdays = ["2026-09-09", "2026-09-10", "2026-09-11"]
        self.assertEqual(
            cal.trade_date_of(self._ts("2026-09-09", "10:30"), tdays), "2026-09-09")
        self.assertEqual(  # 收盘后电报 → 次日可得信息（规避未来函数）
            cal.trade_date_of(self._ts("2026-09-09", "20:30"), tdays), "2026-09-10")

    def test_trade_date_skips_weekend(self):
        tdays = ["2026-09-11", "2026-09-14"]          # 周五 → 下周一
        self.assertEqual(
            cal.trade_date_of(self._ts("2026-09-11", "21:00"), tdays), "2026-09-14")
        self.assertEqual(
            cal.trade_date_of(self._ts("2026-09-12", "10:00"), tdays), "2026-09-14")

    def test_trade_date_out_of_window(self):
        self.assertIsNone(cal.trade_date_of(self._ts("2026-09-30", "10:00"),
                                            ["2026-09-09"]))

    def test_multiplier_for(self):
        c = {"suggest_multipliers": {"cbank_ease|bull": 1.15}}
        self.assertAlmostEqual(cal.multiplier_for(c, "cbank_ease", "bull"), 1.15)
        self.assertEqual(cal.multiplier_for(c, "earnings", "bear"), 1.0)
        self.assertEqual(cal.multiplier_for(None, "cbank_ease", "bull"), 1.0)

    def test_p_two_sided(self):
        self.assertGreater(cal._p_two_sided(0.0), 0.99)
        self.assertLess(cal._p_two_sided(2.0), 0.05)

    def test_pearson(self):
        self.assertAlmostEqual(cal._pearson([1, 2, 3], [2, 4, 6]), 1.0, places=6)
        self.assertIsNone(cal._pearson([1, 2], [2, 4]))

    def test_text_report_shape(self):
        rep = cal.text_report({"window": {"from": "a", "to": "b"},
                               "day_level": {"n": 1}, "baseline_up_rate": 0.5,
                               "events": [{"event": "earnings", "event_cn": "业绩",
                                           "label": "bull", "n": 6, "hit_rate": 0.67,
                                           "baseline": 0.5, "edge": 0.17,
                                           "avg_ret_pct": 0.2, "p": 0.03,
                                           "suggest_mult": 1.12}]})
        self.assertIn("业绩", rep)
        self.assertIn("增量", rep)


if __name__ == "__main__":
    unittest.main()
