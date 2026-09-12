# -*- coding: utf-8 -*-
"""AI 次日方向判断（自动记录 · 自检命中率）的回归测试。

锁死 2026-09-12 用户反馈的两个缺陷——两者都会让网页「消息与进化」页的
「AI 次日方向判断（自动记录 · 自检命中率）」看起来“不再自动运行”：

1. **记录/结算被 run_daily 的短路整体跳过**：`has_record(D)` 为真时 `run_daily`
   直接 return，而“记录方向 + 结算命中”原先只写在主流程中段 → 当天第二次运行
   （晚上补拍快照、白天先手动跑过一次）既不会记录方向、也不会结算上一条判断。
   现在 `Engine.direction_record()` 是独立入口，短路路径也会跑。
2. **非交易日（周末/节假日）面板必然空白**：`direction_pending` 旧实现要求
   `add_trading_days(date,1) == today`，周六/周日/节假日永远匹配不到 → 面板一直显示
   “今日尚未运行研判”。现在改成“针对交易日 >= 今天且未结算的最新一条”，并把
   `date` 归一化成它针对的交易日。

另外锁死两个口径约束：
3. 判断入库存的是**给出判断的交易日**（非交易日自动归一，避免同一份收盘数据重复计数）；
4. 方向/信心由平滑后合成评分经 `view_of` 推导，不得与首页观点漂移。

全部离线：临时目录里的 SQLite + 内存账本，不联网、不碰真实 data/。
"""
import copy
import json
import unittest

from fundai import analysis, screening, settings
from fundai.datasource import Market
from fundai.engine import Engine
from fundai.ledger import Ledger

# 全部用例走**内存库**（`:memory:`）：不联网、不碰真实 data/、也不依赖临时目录
# （Windows 沙箱/杀软偶尔会让临时目录删除失败，导致用例互相污染）。
class _Rig:
    """一个离线的 Engine：内存账本 + 内存消息库（不碰真实 data/）。"""

    def __init__(self):
        self.cfg = copy.deepcopy(settings.load_config())
        self.cfg["screening"] = dict(self.cfg.get("screening") or {}, enabled=True)
        self.cfg.setdefault("data", {})["zhitu_token"] = ""
        self.ledger = Ledger(":memory:", initial_cash=1000.0)
        self.eng = Engine(self.cfg, self.ledger, market=Market(self.cfg))
        self.screen = screening.ScreeningStore(":memory:")
        self.eng.screen = self.screen

    def close(self):
        for obj in (self.screen, self.ledger):
            try:
                obj.close()
            except Exception:
                pass


class DirectionPendingTest(unittest.TestCase):
    """跨天/跨周末展示：面板不得因为有非交易日而空白。"""

    def setUp(self):
        self.rig = _Rig()
        self.addCleanup(self.rig.close)
        self.scr = self.rig.screen

    def test_weekend_shows_friday_judgment_for_next_trading_day(self):
        # 2026-09-11（周五）收盘给出一条判断 → 周六/周日打开面板必须看得到
        self.scr.set_direction("2026-09-11", "neutral", 0.5)
        for today in ("2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14"):
            d = self.scr.direction_pending(today)
            self.assertIsNotNone(d, "{} 应能看到 2026-09-11 给出的次日判断".format(today))
            self.assertEqual(d["dir"], "neutral")
            self.assertEqual(d["made_on"], "2026-09-11", "made_on 应为给出判断那天")
            self.assertEqual(d["for_date"], "2026-09-14", "for_date 应为判断针对的交易日")
            self.assertEqual(d["date"], "2026-09-14", "date 应归一化为针对的交易日")

    def test_settled_judgment_is_shown_as_settled_not_pending(self):
        # 周五的判断已在周五收盘后结算 → 该判断日再打开面板时，应标记为“已结算”
        # （pending=False），而不是继续显示“等待收盘后自动结算”。
        self.scr.set_direction("2026-09-10", "bear", 0.7)
        self.assertEqual(self.scr.resolve_directions("2026-09-11", -0.005), 1)
        d = self.scr.direction_pending("2026-09-11")
        self.assertIsNotNone(d)
        self.assertEqual(d["made_on"], "2026-09-10")
        self.assertFalse(d["pending"], "已结算的判断不得再标成待结算")
        self.assertEqual(d["resolved_date"], "2026-09-11")
        self.assertEqual(d["hit"], 1)

    def test_older_judgment_not_leaked_after_target_day_passed(self):
        self.scr.set_direction("2026-09-09", "bull", 0.6)
        self.assertIsNone(self.scr.direction_pending("2026-09-14"),
                          "判断针对的那天已过去 → 不应继续显示")


class DirectionRecordTest(unittest.TestCase):
    """独立入口：记录方向 + 结算自检，幂等、可补跑、失败不静默。"""

    def setUp(self):
        self.rig = _Rig()
        self.addCleanup(self.rig.close)
        self.eng, self.ledger, self.scr = (self.rig.eng, self.rig.ledger,
                                           self.rig.screen)

    def _write_record(self, date_s, score, direction=None):
        calc = {}
        if direction:
            calc["direction"] = direction
        self.ledger.add_record(date_s, score, -0.008, 4510.16, "中性震荡",
                               "内置引擎", "分析", "决策",
                               calc=json.dumps(calc, ensure_ascii=False))

    def test_records_direction_for_trading_day(self):
        self._write_record("2026-09-11", -25)
        res = self.eng.direction_record("2026-09-11", settle_with_chg=-0.0084)
        self.assertTrue(res["recorded"])
        self.assertIsNone(res["error"])
        row = self.scr.direction("2026-09-11")
        self.assertEqual(row["dir"], "neutral")
        self.assertAlmostEqual(row["confidence"], 0.5, places=6)

    def test_same_direction_rerun_keeps_first_judgment(self):
        self._write_record("2026-09-11", -25)
        self.eng.direction_record("2026-09-11")
        first = self.scr.direction("2026-09-11")["created"]
        res = self.eng.direction_record("2026-09-11")
        self.assertTrue(res["kept"], "同向重跑应保留首次判断")
        self.assertFalse(res["recorded"])
        self.assertEqual(self.scr.direction("2026-09-11")["created"], first)

    def test_force_overwrites_and_changed_score_overwrites(self):
        self._write_record("2026-09-11", -25, direction={"dir": "neutral",
                                                        "score": -25})
        self.eng.direction_record("2026-09-11")
        self.assertTrue(self.eng.direction_record("2026-09-11", force=True)["recorded"])
        # 研判结论改了（评分从 -25 变 +60）→ 即使不加 force 也必须覆盖
        self._write_record("2026-09-11", 60, direction={"dir": "bull", "score": 60})
        res = self.eng.direction_record("2026-09-11")
        self.assertTrue(res["recorded"])
        self.assertEqual(self.scr.direction("2026-09-11")["dir"], "bull")

    def test_non_trading_day_files_under_last_trading_day(self):
        """周六/周日重复跑不得各留一条：同一份收盘研判只算一个样本。"""
        self._write_record("2026-09-11", -25)
        for d in ("2026-09-12", "2026-09-13"):
            res = self.eng.direction_record(d)
            self.assertEqual(res["filed_as"], "2026-09-11",
                             "{} 应记在 2026-09-11 名下".format(d))
        rows = [dict(r) for r in self.scr.conn.execute(
            "SELECT date FROM direction ORDER BY date")]
        self.assertEqual([r["date"] for r in rows], ["2026-09-11"],
                         "非交易日不得产生额外判断行（会重复计数命中率）")

    def test_settles_pending_judgment_once_target_day_arrives(self):
        self._write_record("2026-09-10", -25)
        self.eng.direction_record("2026-09-10")
        # 2026-09-11 收盘：结算 09-10 的判断（当日跌 0.84%，中性 → 未中）
        res = self.eng.direction_record("2026-09-11", settle_with_chg=-0.0084)
        self.assertEqual(res["settled"], 1)
        row = self.scr.direction("2026-09-10")
        self.assertEqual(row["resolved_date"], "2026-09-11")
        self.assertEqual(row["hit"], 0)
        # 同日再跑：不得重复结算
        self.assertEqual(self.eng.direction_record(
            "2026-09-11", settle_with_chg=-0.0084)["settled"], 0)

    def test_no_record_is_reported_not_silently_ignored(self):
        res = self.eng.direction_record("2026-09-11")
        self.assertTrue(res["skipped"])
        self.assertIsNone(res["error"])

    def test_failure_is_surfaced(self):
        """写库失败必须回传 error（旧实现 except: pass → 日志里毫无痕迹）。"""
        self._write_record("2026-09-11", -25)
        self.scr.set_direction = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("database is locked"))
        res = self.eng.direction_record("2026-09-11", force=True)
        self.assertIn("database is locked", str(res["error"]))


class DirectionCaliberTest(unittest.TestCase):
    """方向/信心必须与首页观点同源（口径不得漂移）。"""

    def test_direction_follows_view_thresholds(self):
        # 注意 −25 这一档：VIEWS 的区间是 `score >= lo` 顺序匹配，(25, …) 与 (−25, …)
        # 两侧不对称 → −25 落在中性、−26 才转看空。这是**既有口径**（首页观点同样如此），
        # 本项目此次不擅自改动算法，只在用例里锁死现状，避免以后被“顺手改回对称”。
        cases = [(55, "bull"), (25, "bull"), (24, "neutral"), (-24, "neutral"),
                 (-25, "neutral"), (-26, "bear"), (-55, "bear"), (-60, "bear"),
                 (0, "neutral"), (-101, "bear")]
        for score, want in cases:
            got, _conf = Engine.ai_direction(score)
            self.assertEqual(got, want, "评分 {} 应为 {}".format(score, want))
            self.assertEqual(analysis.view_of(score)["key"].replace("hot_", ""),
                             want, "方向不得与 view_of 漂移（评分 {}）".format(score))

    def test_confidence_bounds(self):
        self.assertAlmostEqual(Engine.ai_direction(0)[1], 0.5)
        self.assertAlmostEqual(Engine.ai_direction(100)[1], 1.0)
        self.assertAlmostEqual(Engine.ai_direction(-100)[1], 1.0)
        self.assertGreater(Engine.ai_direction(60)[1], Engine.ai_direction(30)[1])

    def test_hit_rule_matches_readme_caliber(self):
        scr = screening.ScreeningStore(":memory:")
        self.addCleanup(scr.close)
        scr.set_direction("2026-09-09", "bull", 0.6)     # 次日涨 → 中
        scr.set_direction("2026-09-10", "bear", 0.6)     # 次日跌 → 中
        scr.set_direction("2026-09-11", "neutral", 0.5)  # 次日 |涨跌|≤0.3% → 中
        self.assertEqual(scr.resolve_directions("2026-09-10", 0.01), 1)
        self.assertEqual(scr.resolve_directions("2026-09-11", -0.01), 1)
        self.assertEqual(scr.resolve_directions("2026-09-14", 0.001), 1)
        for d in ("2026-09-09", "2026-09-10", "2026-09-11"):
            self.assertEqual(scr.direction(d)["hit"], 1, "{} 应判命中".format(d))

    def test_hit_rule_neutral_miss(self):
        scr = screening.ScreeningStore(":memory:")
        self.addCleanup(scr.close)
        scr.set_direction("2026-09-10", "neutral", 0.5)
        scr.resolve_directions("2026-09-11", -0.0084)
        self.assertEqual(scr.direction("2026-09-10")["hit"], 0)


class LedgerRecordTest(unittest.TestCase):
    def test_get_record_returns_none_for_missing(self):
        lg = Ledger(":memory:", initial_cash=1000.0)
        self.addCleanup(lg.close)
        self.assertIsNone(lg.get_record("2026-09-11"))
        lg.add_record("2026-09-11", -25, -0.008, 4510.0, "中性", "内置引擎",
                      "a", "d", calc="{}")
        rec = lg.get_record("2026-09-11")
        self.assertEqual(rec["market_score"], -25)
        self.assertEqual(rec["date"], "2026-09-11")


if __name__ == "__main__":
    unittest.main()
