# -*- coding: utf-8 -*-
"""风险过滤器：仓位映射与实盘联动的测试（不联网、不训练）。"""
import unittest

from fundai import risk_overlay as ro


CFG = {"strategy": {"risk_overlay_mapping": "floor3_flat",
                    "risk_overlay_floor": 0.15}}


class BuildSignalTest(unittest.TestCase):
    def test_floor3_flat_mapping(self):
        self.assertEqual(ro.build_signal(0.60, CFG)["scale"], 1.0)
        self.assertEqual(ro.build_signal(0.50, CFG)["scale"], 1.0)
        self.assertEqual(ro.build_signal(0.49, CFG)["scale"], 0.3)
        self.assertEqual(ro.build_signal(0.10, CFG)["scale"], 0.3)
        self.assertEqual(ro.build_signal(0.60, CFG)["action"], "risk_on")
        self.assertEqual(ro.build_signal(0.30, CFG)["action"], "neutral")

    def test_flat_mapping_option(self):
        cfg = {"strategy": {"risk_overlay_mapping": "flat_0_1_p50"}}
        self.assertEqual(ro.build_signal(0.51, cfg)["scale"], 1.0)
        self.assertEqual(ro.build_signal(0.49, cfg)["scale"], 0.0)
        self.assertEqual(ro.build_signal(0.49, cfg)["action"], "risk_off")

    def test_never_scales_up(self):
        for name in ro.MAPPINGS:
            for p in (0.0, 0.25, 0.49, 0.5, 0.51, 0.75, 1.0):
                s = ro.build_signal(p, {"strategy": {"risk_overlay_mapping": name}})
                self.assertLessEqual(s["scale"], 1.0)
                self.assertGreaterEqual(s["scale"], 0.0)

    def test_custom_thresholds_fallback(self):
        cfg = {"strategy": {"risk_overlay_mapping": "nope",
                            "risk_overlay_on_th": 0.60,
                            "risk_overlay_off_th": 0.40,
                            "risk_overlay_neutral": 0.8,
                            "risk_overlay_off": 0.2}}
        self.assertEqual(ro.build_signal(0.65, cfg)["scale"], 1.0)
        self.assertEqual(ro.build_signal(0.50, cfg)["scale"], 0.8)
        self.assertEqual(ro.build_signal(0.20, cfg)["scale"], 0.2)

    def test_missing_model_does_not_intervene(self):
        s = ro.build_signal(None, CFG)
        self.assertEqual(s["scale"], 1.0)
        self.assertEqual(s["action"], "none")
        self.assertIn("不干预", s["reason"])

    def test_vol_pause_at_extreme_volatility(self):
        """波动率 ≥ 阈值（默认 0.90）→ 暂停过滤器、保持满仓（防极端情绪打脸）。"""
        cfg = {"strategy": {"risk_overlay_mapping": "flat_0_1_p52",
                            "risk_overlay_vol_pause": 0.90}}
        quiet = ro.build_signal(0.30, cfg, vol_rank=0.50)
        self.assertEqual(quiet["scale"], 0.0)
        self.assertEqual(quiet["action"], "risk_off")
        wild = ro.build_signal(0.30, cfg, vol_rank=0.95)
        self.assertEqual(wild["scale"], 1.0)
        self.assertEqual(wild["action"], "paused")
        self.assertIn("暂停", wild["reason"])
        # 边界：正好等于阈值 → 暂停；阈值设 0 → 永不暂停
        self.assertEqual(ro.build_signal(0.30, cfg, vol_rank=0.90)["action"], "paused")
        off = {"strategy": {"risk_overlay_mapping": "flat_0_1_p52",
                            "risk_overlay_vol_pause": 0.0}}
        self.assertEqual(ro.build_signal(0.30, off, vol_rank=0.99)["scale"], 0.0)

    def test_reason_is_readable(self):
        s = ro.build_signal(0.42, CFG)
        self.assertIn("P(次日", s["reason"])
        self.assertIn("×30%", s["reason"])


class SimTest(unittest.TestCase):
    def test_sim_reduces_drawdown_when_signal_avoids_down_days(self):
        rows = []
        for i in range(40):
            down = i % 2 == 0
            rows.append({"ret": -0.02 if down else 0.01})
            rows[-1]["y"] = 0 if down else 1
        # 完美预测：下跌日空仓、上涨日满仓
        preds = [0.4 if r["ret"] < 0 else 0.6 for r in rows]
        obs = list(zip(rows, preds))
        res = ro._sim(None, obs, ro.MAPPINGS["flat_0_1_p50"], 0.0)
        self.assertGreater(res["nav"], 1.0)
        self.assertEqual(res["mdd"], 0.0)

    def test_sim_counts_switches(self):
        rows = [{"ret": 0.0, "y": 1} for _ in range(4)]
        preds = [0.6, 0.4, 0.6, 0.4]
        res = ro._sim(None, list(zip(rows, preds)),
                      ro.MAPPINGS["flat_0_1_p50"], 0.0)
        self.assertGreaterEqual(res["switches"], 3)
        self.assertLessEqual(res["exposure"], 1.0)


if __name__ == "__main__":
    unittest.main()
