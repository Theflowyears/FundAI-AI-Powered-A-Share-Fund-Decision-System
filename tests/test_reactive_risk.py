# -*- coding: utf-8 -*-
"""反应型风控与动态调整带的测试（不联网、不依赖行情）。"""
import unittest

from fundai import strategy

from _env import needs_config  # 发行包不含 config.json，相关用例自动跳过


CFG = {"strategy": {
    "position_band_max": 0.10,
    "equity_hard_cap": 0.80,
    "band_vol_low": 0.60, "band_vol_extreme": 0.90,
    "band_micro_hot": 0.70, "band_micro_cold": 0.30,
}}


class PositionBandTest(unittest.TestCase):
    def _band(self, **ctx):
        return strategy.position_band(CFG, ctx)

    def test_bull_regime_positive(self):
        """温和上行（均线多头+低波动+情绪偏热）→ 正调整带，且不超过 band_max。"""
        b, note = self._band(ma_align=1.0, vol_rank=0.30, micro_pct=0.85)
        self.assertGreater(b, 0)
        self.assertLessEqual(b, 0.10 + 1e-9)
        self.assertIn("均线多头", note)
        self.assertIn("低波动", note)

    def test_extreme_vol_negative(self):
        b, note = self._band(ma_align=0.0, vol_rank=0.95, micro_pct=0.5)
        self.assertLess(b, 0)
        self.assertIn("极端波动", note)

    def test_cold_sentiment_negative(self):
        # 波动率取 0.75（既非低波动加分、也非极端扣分）→ 只剩“情绪偏冷”一项
        b, note = self._band(ma_align=0.0, vol_rank=0.75, micro_pct=0.1)
        self.assertLess(b, 0)
        self.assertIn("偏冷", note)

    def test_neutral_is_zero(self):
        b, note = self._band(ma_align=0.0, vol_rank=0.7, micro_pct=0.5)
        self.assertEqual(b, 0.0)
        self.assertIn("中性", note)

    def test_band_clamped(self):
        b, _ = self._band(ma_align=1.0, vol_rank=0.1, micro_pct=0.95)
        self.assertLessEqual(abs(b), 0.10 + 1e-9)

    def test_disabled(self):
        cfg = {"strategy": {"position_band_max": 0.0}}
        b, note = strategy.position_band(cfg, {"ma_align": 1.0})
        self.assertEqual(b, 0.0)
        self.assertIn("关闭", note)


class ReactiveRiskConfigTest(unittest.TestCase):
    @needs_config
    def test_config_expectations(self):
        """口径（2026-09-11 四窗口独立回测后定稿）：
        基金级反应型风控保留（−8% 止损 / 止盈 / 组合 −15%），
        指数级择时层**开启**（均线减仓 0.10 + 回撤冷却 0.10）——
        基金级真实回测显示它们同时提高收益并压低回撤（README 6.4/6.5）。"""
        import json
        from pathlib import Path
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
        rk = cfg["strategy"]["risk"]
        self.assertEqual(rk["fund_stop_loss_pct"], -0.08)
        self.assertEqual(rk["portfolio_stop_pct"], -0.15)
        self.assertEqual(rk["rearm_days"], 5)
        # 指数级层：采纳档（0.10 / 0.10）；平台区间 0.10–0.20 见 README 6.5 敏感性表
        self.assertEqual(rk["ma_break_step"], 0.10)
        self.assertEqual(rk["peak_trailing_pct"], 0.10)
        self.assertEqual(rk["ma_break_max"], 0.4)
        self.assertTrue(rk["ma_break_skip_ma20"])
        self.assertEqual(rk["ma_break_floor"], 0.4)
        # 预测型过滤默认关闭（十年回测不支持；且回放没有历史信号序列，无法验证）
        self.assertFalse(cfg["strategy"]["risk_overlay_enable"])
        # 最终引用组合：固定 70% 基准 + 消息 20%/情绪 10% + 硬上限 80%
        self.assertEqual(cfg["strategy"]["equity_base_mode"], "fixed")
        self.assertEqual(cfg["strategy"]["equity_base_fixed"], 0.7)
        self.assertEqual(cfg["strategy"]["equity_hard_cap"], 0.8)
        self.assertEqual(cfg["strategy"]["news_weight"], 0.2)
        self.assertEqual(cfg["strategy"]["micro_weight"], 0.1)
        # 回放接入十年消息库（否则消息面在回测里完全不参与，见 README 6.4 结论 1）
        self.assertTrue(cfg["strategy"]["replay_news"])


if __name__ == "__main__":
    unittest.main()
