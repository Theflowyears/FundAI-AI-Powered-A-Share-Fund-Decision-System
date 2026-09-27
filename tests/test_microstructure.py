# -*- coding: utf-8 -*-
"""微观结构（涨停情绪复盘）纯离线断言：解析/算法/合成/降级（不联网）。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fundai import microstructure as ms
from fundai import settings


class ParseTest(unittest.TestCase):
    def test_high_days(self):
        self.assertEqual(ms.parse_high_days("14天9板"), (14, 9))
        self.assertEqual(ms.parse_high_days("首板"), (1, 1))
        self.assertEqual(ms.parse_high_days("3天2板"), (3, 2))
        self.assertEqual(ms.parse_high_days("5"), (1, 5))
        self.assertEqual(ms.parse_high_days(None, 2), (1, 2))

    def test_st_filter(self):
        self.assertTrue(ms.is_st("*ST某股"))
        self.assertTrue(ms.is_st("ST长投"))
        self.assertFalse(ms.is_st("张江高科"))

    def test_theme_of_concept(self):
        self.assertEqual(ms.theme_of_concept("半导体概念"), "半导体芯片")
        self.assertEqual(ms.theme_of_concept("CPO概念"), "人工智能")
        self.assertEqual(ms.theme_of_concept("券商概念"), "证券")
        self.assertEqual(ms.theme_of_concept("大熊猫保护"), None)


class PulseAlgoTest(unittest.TestCase):
    def _raw(self):
        zt = [
            {"code": "600001", "name": "甲", "reason_type": "存储芯片+机器人",
             "high_days": "3天3板"},
            {"code": "000002", "name": "乙", "reason_type": "存储芯片",
             "high_days": "首板"},
            {"code": "300003", "name": "丙", "reason_type": "券商概念",
             "high_days": "2天2板"},
            {"code": "600004", "name": "ST丁", "reason_type": "存储芯片",
             "high_days": "首板"},
        ]
        return {"zt": zt, "zb": [{"code": "1"}, {"code": "2"}],
                "dt": [{"code": "9"}],
                "blocks": [{"name": "人工智能", "limit_up_num": 5}],
                "ov": {"rise_fall": {"rise": 3800, "fall": 1200,
                                     "deuce": 200, "limit_up": 40,
                                     "limit_down": 5},
                       "turnover": {"now": "1.9万亿", "pre": "2.2万亿"}},
                "_errs": []}

    def test_snapshot_metrics(self):
        snap = ms.snapshot_from_raw("2026-09-09", self._raw(), prev_snap=None)
        self.assertEqual(snap["zt_nonst"], 3)          # ST丁被剔除
        self.assertEqual(snap["zt_codes"], ["000002", "300003", "600001"])
        self.assertEqual(snap["zb_rate"], round(2 / 42, 4))   # 2/(40+2)
        self.assertEqual(snap["max_board"], 3)
        self.assertIn("甲(存储芯片)", snap["ladder"]["3"])
        self.assertEqual(snap["concepts"]["存储芯片"], 2)
        self.assertEqual(snap["theme_counts"].get("半导体芯片"), 2)
        self.assertEqual(snap["theme_counts"].get("人工智能"), 6)  # 1+5(风口)
        self.assertEqual(snap["rise_ratio"], (3800 + 100) / 5200)
        self.assertIsNotNone(snap["score"])

    def test_promotion_and_persistence(self):
        prev = {"date": "2026-09-08",
                "zt_codes": ["600001", "300003", "600999"],
                "concepts": {"存储芯片": 6, "券商概念": 4, "旧题材": 3}}
        snap = ms.snapshot_from_raw("2026-09-09", self._raw(),
                                    prev_snap=prev)
        self.assertEqual(snap["promotion"], 2)         # 600001、300003 晋级
        self.assertAlmostEqual(snap["promotion_rate"], 2 / 3, places=4)
        self.assertIn("存储芯片", snap["persistent"])   # 昨日Top5∩今日Top5
        today_top = [c for c, _ in snap["top_concepts"]]
        self.assertNotIn("旧题材", today_top)

    def test_zb_rate_partial(self):
        raw = self._raw()
        raw["zb"] = None
        snap = ms.snapshot_from_raw("2026-09-09", raw)
        self.assertIsNone(snap["zb_rate"])
        self.assertTrue(snap["partial"]["zb"])

    def test_extreme_damping(self):
        """沸点/冰点两端阻尼（物极必反/亢龙有悔）。"""
        hot = ms.sentiment_score({
            "rise_ratio": 0.95, "zt": 120, "dt": 0,
            "promotion_rate": 0.9, "zb_rate": 0.05})
        self.assertEqual(hot[2], ["overheat"])
        self.assertLessEqual(hot[0], 70)
        cold = ms.sentiment_score({
            "rise_ratio": 0.02, "zt": 10, "dt": 60,
            "promotion_rate": 0.0, "zb_rate": 0.8})
        self.assertEqual(cold[2], ["freezing"])
        self.assertGreaterEqual(cold[0], -70)

    def test_partial_missing_dims(self):
        s, parts, _ = ms.sentiment_score({"rise_ratio": 0.6})
        self.assertIn("breadth", parts)
        self.assertNotIn("promotion", parts)
        self.assertEqual(s, 20)  # (0.6-0.5)*200

    def test_zen(self):
        self.assertIn("亢龙有悔", ms.zen_quote(0.9, 0.6))
        self.assertIn("否极泰来", ms.zen_quote(0.1, 0.4))


class ThemeHeatTest(unittest.TestCase):
    def test_decay_and_normalize(self):
        d1 = {"date": "d1", "theme_counts": {"半导体芯片": 10}}
        d0 = {"date": "d0", "theme_counts": {"半导体芯片": 6, "证券": 2}}
        heat = ms.theme_heat_map([d1, d0])
        self.assertAlmostEqual(heat["半导体芯片"], 1.0, places=2)
        self.assertLess(heat["证券"], heat["半导体芯片"])

    def test_empty(self):
        self.assertEqual(ms.theme_heat_map([]), {})
        self.assertEqual(ms.theme_heat_map([{"date": "x",
                                             "theme_counts": {}}]), {})

    def test_order_independent(self):
        """权重只认 date，不认调用方传参顺序（web 层多反转一次曾让最新日只拿 0.2）。"""
        d1 = {"date": "2026-09-08", "zt": 60, "theme_counts": {"半导体芯片": 12}}
        d0 = {"date": "2026-09-09", "zt": 40,
              "theme_counts": {"半导体芯片": 8, "证券": 6}}
        a = ms.theme_heat_report([d1, d0])
        b = ms.theme_heat_report([d0, d1])
        self.assertEqual(a["map"], b["map"])
        self.assertEqual(a["days"], ["2026-09-08", "2026-09-09"])
        self.assertEqual(a["decay"], [0.3, 0.5])      # 旧 → 新

    def test_share_normalization_favours_new_hotspot(self):
        """按“当日占比”而非绝对家数：小基数日的今日热点不再被大基数旧日压住。"""
        old = {"date": "a", "zt": 100, "theme_counts": {"X": 20}}   # 占比 0.20
        new = {"date": "b", "zt": 10, "theme_counts": {"Y": 5}}     # 占比 0.50，仅今日
        rep = ms.theme_heat_report([old, new])
        self.assertGreater(rep["map"]["Y"], rep["map"]["X"])
        # 旧口径（原始家数加权 0.3×20 vs 0.5×5）会把 X 排在前面
        self.assertEqual(rep["rows"][0]["theme"], "Y")

    def test_persistence_bonus(self):
        """三日连续的中等热度 > 仅今日爆发的一日游。"""
        snaps = [{"date": "a", "zt": 50, "theme_counts": {"持续": 10}},
                 {"date": "b", "zt": 50, "theme_counts": {"持续": 10, "闪": 9}},
                 {"date": "c", "zt": 50, "theme_counts": {"持续": 10, "闪": 12}}]
        rep = ms.theme_heat_report(snaps)
        self.assertGreater(rep["map"]["持续"], rep["map"]["闪"])
        self.assertEqual(rep["rows"][0]["days_seen"], 3)

    def test_block_half_weight_and_legacy_fallback(self):
        """风口按 0.5 计入（同一批涨停股不重复计数）；旧快照无 theme_parts 时回退。"""
        snap = {"date": "a", "zt": 40,
                "theme_parts": {"concepts": {"半导体芯片": 10},
                                "blocks": {"半导体芯片": 10}}}
        rep = ms.theme_heat_report([snap])
        self.assertEqual(rep["rows"][0]["values"][0], 15)     # 10 + 0.5×10
        legacy = {"date": "a", "zt": 40, "theme_counts": {"半导体芯片": 20}}
        self.assertEqual(
            ms.theme_heat_report([legacy])["rows"][0]["values"][0], 20)

    def test_ignition_and_market_heat(self):
        d1 = {"date": "a", "zt": 40, "theme_counts": {"人工智能": 12}}
        d2 = {"date": "b", "zt": 30,
              "theme_counts": {"人工智能": 6, "公用事业": 9}}
        rep = ms.theme_heat_report([d1, d2])
        self.assertIn("公用事业", rep["ignitions"])       # 今日 30% vs 其余 0%
        self.assertNotIn("人工智能", rep["ignitions"])
        self.assertEqual(rep["zt_latest"], 30)
        self.assertEqual(rep["zt_prev"], 40)
        self.assertEqual(rep["trend"], "down")
        self.assertIsNotNone(rep["market_heat"])


class ClsSignTest(unittest.TestCase):
    def test_sign_algorithm(self):
        """签名 = md5(sha1(按key排序参数串))（参考仓库同源算法）。"""
        s = ms_cls_sign({"os": "web", "app": "CailianpressWeb",
                         "sv": "7.7.5"})
        import hashlib
        raw = "app=CailianpressWeb&os=web&sv=7.7.5"
        want = hashlib.md5(hashlib.sha1(raw.encode()).hexdigest()
                           .encode()).hexdigest()
        self.assertEqual(s, want)


def ms_cls_sign(params):
    # news._cls_sign 的镜像（news 模块导入较重，直接引用真函数）
    from fundai import news
    return news._cls_sign(params)


class ConfigCompatTest(unittest.TestCase):
    def test_defaults_present(self):
        cfg = settings.load_config()
        st = cfg["strategy"]
        self.assertIn("micro_weight", st)
        self.assertIn("micro_score_cap", st)
        self.assertIn("theme_heat", st["factor_weights"])

    def test_combine_three_way(self):
        import copy
        from fundai.engine import combine_score
        cfg = copy.deepcopy(settings.load_config())
        st = cfg.setdefault("strategy", {})
        st.update({"news_weight": 0.35, "micro_weight": 0.15,
                   "news_score_cap": 25, "micro_score_cap": 30})
        # 三方：100*0.5 + 20*0.35 + 20*0.15 = 60（取无舍入歧义的值）
        self.assertEqual(combine_score(cfg, 100, 20, 20), 60)
        # 消息缺失：情绪照抽15%、消息份额还给量化 → 100*0.85 + 20*0.15 = 88
        self.assertEqual(combine_score(cfg, 100, None, 20), 88)
        # micro None → 与旧版二方完全一致（向后兼容）
        self.assertEqual(combine_score(cfg, 50, 20, None),
                         int(round(50 * 0.65 + 20 * 0.35)))
        self.assertEqual(combine_score(cfg, 50, None, None), 50)
        # 限幅：micro=100 被压到 cap=30
        self.assertEqual(combine_score(cfg, 0, 0, 100),
                         int(round(30 * 0.15)))

    def test_micro_weight_zero_two_way(self):
        import copy
        from fundai.engine import combine_score
        cfg = copy.deepcopy(settings.load_config())
        st = cfg.setdefault("strategy", {})
        st.update({"news_weight": 0.35, "micro_weight": 0.0,
                   "news_score_cap": 25})
        self.assertEqual(combine_score(cfg, 50, 20, 80),
                         int(round(50 * 0.65 + 20 * 0.35)))

    def _ctx(self, theme_heat=None):
        fac = {"A": {"mom": 0.12, "mom5": 0.01, "vol": 0.02, "bias": 0.0,
                     "rsi": 50},
               "B": {"mom": 0.11, "mom5": 0.01, "vol": 0.02, "bias": 0.0,
                     "rsi": 50},
               "C": {"mom": 0.10, "mom5": 0.01, "vol": 0.02, "bias": 0.0,
                     "rsi": 50}}
        ctx = dict(score=0, cash=1000, committed=0, eq_mv=0, bond_mv=0,
                   total=1000,
                   names={"A": "华夏人工智能", "B": "天弘半导体",
                          "C": "国泰科创"},
                   eq_hold_mv={}, sell_eq_by_code={}, locked_eq_by_code={},
                   pending_sell_codes=set(), pending_buy_codes=set(),
                   funds_mom={"A": 0.12, "B": 0.11, "C": 0.10},
                   funds_vol={}, funds_mom5={},
                   funds_factors=fac, equity_codes=["A", "B", "C"],
                   market_mom=None, confidence=None, defensive=False,
                   peak_total=0, initial=1000, fund_pnl={},
                   bond_code=None, bond_sellable_mv=0)
        if theme_heat is not None:
            ctx["theme_heat"] = theme_heat
        return ctx

    def _plan(self, ctx):
        import copy
        from fundai import strategy
        cfg = copy.deepcopy(settings.load_config())
        cfg["strategy"]["factor_weights"] = {"mom": 0.65, "mom5": 0.0,
                                             "vol": 0.0, "theme_heat": 0.50}
        return strategy.plan(cfg, ctx)

    def test_theme_heat_absent_same_as_off(self):
        """ctx 无 theme_heat → 纯动量排序（回测/演示口径不变）。"""
        r = self._plan(self._ctx())
        self.assertEqual([w["code"] for w in r["winners"]], ["A", "B", "C"])

    def test_theme_heat_reorders(self):
        """题材热度足够高 → 半导体(B)反超动量第一的人工智能(A)。"""
        r = self._plan(self._ctx(theme_heat={"半导体芯片": 1.0}))
        self.assertEqual([w["code"] for w in r["winners"]][0], "B")


if __name__ == "__main__":
    unittest.main(verbosity=2)
