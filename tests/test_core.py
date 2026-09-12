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

    def test_window_beyond_calendar_end(self):
        """窗口越过日历终点（今天→未来截止日）：日历内取精确、终点后工作日近似补齐。

        回归仪表盘 bug：“距截止日已无交易日/还需日均收益显示不出”——K线日历只覆盖
        历史，“今天→2027-03-08”在日历内取不到任何日期，误返回空列表。
        """
        util.set_calendar(["2026-09-07", "2026-09-08", "2026-09-09"])
        # 09-10(四)→09-18(五)：日历终点后全部工作日近似，剔周末 12/13 → 共 7 天
        days = util.trading_days_between("2026-09-10", "2026-09-18")
        self.assertEqual(days, ["2026-09-10", "2026-09-11", "2026-09-14",
                                "2026-09-15", "2026-09-16", "2026-09-17",
                                "2026-09-18"])
        # 混合窗口：日历内保持精确（09-09），终点后 09-10/11 用近似
        days2 = util.trading_days_between("2026-09-09", "2026-09-11")
        self.assertEqual(days2, ["2026-09-09", "2026-09-10", "2026-09-11"])
        # 完全在日历内：不多不少、不混入近似
        days3 = util.trading_days_between("2026-09-07", "2026-09-09")
        self.assertEqual(days3, ["2026-09-07", "2026-09-08", "2026-09-09"])


class BackfillSnapshotTest(unittest.TestCase):
    """资产曲线自愈回填：cash=seed+流水(≤d)，份额=剩余批次+其后卖出；不编造点位。"""

    def setUp(self):
        import os
        import tempfile
        self.db = os.path.join(tempfile.mkdtemp(), "t.db")

    def _engine(self, idx, nav_map, start="2024-09-01", seed="1000.0"):
        from fundai.engine import Engine
        from fundai.ledger import Ledger
        lg = Ledger(self.db, initial_cash=1000.0)
        lg.set_meta("seed_cash", seed)
        e = Engine.__new__(Engine)
        e.ledger = lg
        e.demo = False
        e.account = lambda: {"start_date": start, "initial_cash": 1000.0}
        e._pool_items = lambda: [{"code": "002199", "kind": "equity"},
                                 {"code": "270049", "kind": "bond"}]

        class FakeMarket:
            def index_history(self_):
                return idx

            def fund_history(self_, code):
                return nav_map[code]
        e.market = FakeMarket()
        return e, lg

    @staticmethod
    def _exec(lg, code, act, d, sh, cd, fee=0.0):
        lg.conn.execute("INSERT INTO executed(code,action,date,shares,nav,fee,"
                        "cash_delta,note) VALUES(?,?,?,?,?,?,?,?)",
                        (code, act, d, sh, 1.0, fee, cd, "t"))
        lg.conn.commit()

    def test_reconstruct_and_purge_stale(self):
        idx = [("2024-09-02", 4500.0, 0), ("2024-09-03", 4510.0, 0),
               ("2024-09-04", 4520.0, 0), ("2024-09-05", 4530.0, 0),
               ("2024-09-06", 4540.0, 0)]
        nav = {"002199": [("2024-09-02", 0.89), ("2024-09-03", 0.90),
                           ("2024-09-04", 0.91), ("2024-09-05", 0.92),
                           ("2024-09-06", 0.93)],
               "270049": [("2024-09-02", 1.24), ("2024-09-03", 1.25),
                          ("2024-09-04", 1.25), ("2024-09-05", 1.25),
                          ("2024-09-06", 1.26)]}
        e, lg = self._engine(idx, nav)
        # 脏快照（早于开始日的历史遗留）应被清掉
        lg.add_snapshot("2024-08-01", 500, 0, 0, 500, 0, 4400.0, "旧")
        lg.add_lot("002199", "2024-09-02", 237.07, 0.89, 0.0)   # 当前剩余(已扣卖出)
        lg.add_lot("270049", "2024-09-02", 112.51, 1.24, 0.0)
        self._exec(lg, "002199", "buy", "2024-09-02", 337.07, -300.0)
        self._exec(lg, "270049", "buy", "2024-09-02", 112.51, -140.0)
        self._exec(lg, "002199", "sell", "2024-09-05", 100.0, 89.0)
        n = e.backfill_snapshots(days=7)
        self.assertEqual(n, 5)
        snaps = {r["date"]: r for r in lg.get_snapshots()}
        self.assertNotIn("2024-08-01", snaps)                       # 脏点已清
        s2, s6 = snaps["2024-09-02"], snaps["2024-09-06"]
        self.assertAlmostEqual(s2["cash"], 560.0, places=2)         # 1000-300-140
        self.assertAlmostEqual(s2["mv_eq"], 337.07 * 0.89, places=1)  # 卖出日前的份额
        self.assertAlmostEqual(s2["mv_bond"], 112.51 * 1.24, places=1)
        self.assertAlmostEqual(s2["total"], 560 + 337.07 * 0.89 + 112.51 * 1.24,
                               places=1)
        self.assertEqual(s2["index_close"], 4500.0)
        self.assertAlmostEqual(s6["cash"], 649.0, places=2)        # 卖出回款后
        self.assertAlmostEqual(s6["mv_eq"], 237.07 * 0.93, places=1)  # 卖出后剩余
        # 已有快照的日期不覆盖（幂等）
        self.assertEqual(e.backfill_snapshots(days=7), 0)

    def test_skip_day_without_nav(self):
        idx = [("2024-09-02", 4500.0, 0), ("2024-09-03", 4510.0, 0)]
        nav = {"002199": [("2024-09-03", 0.90)],          # 09-02 无净值
               "270049": [("2024-09-02", 1.24), ("2024-09-03", 1.25)]}
        e, lg = self._engine(idx, nav)
        lg.add_lot("002199", "2024-09-02", 100.0, 0.9, 0.0)
        self._exec(lg, "002199", "buy", "2024-09-02", 100.0, -90.0)
        n = e.backfill_snapshots(days=7)
        self.assertEqual(n, 1)                              # 09-02 跳过，不编造
        self.assertTrue(lg.has_snapshot("2024-09-03"))
        self.assertFalse(lg.has_snapshot("2024-09-02"))


class AutoArchiveNewsTest(unittest.TestCase):
    """例行披露/同模板批量消息：AI 自动归档，不占人工复核额度（2026-09-10 用户反馈）。"""

    def test_holdings_disclosure_event(self):
        from fundai import semantics
        ev = semantics.classify_event(
            "财联社9月10日电，香港交易所信息显示，摩根大通（JPMorgan）在药明康德H股"
            "的持股比例于09月07日从9.90%升至10.01%，购买的平均股价为191.7113港元。", "")
        self.assertEqual(ev["event"], "holdings_disclosure")
        self.assertEqual(ev["label"], "neutral")
        self.assertEqual(ev["strength"], 0)
        # 减持方向也归此类（趋势性、无大盘方向）
        ev2 = semantics.classify_event(
            "香港交易所信息显示，某机构在该股的持股比例从12.3%降至11.1%。", "")
        self.assertEqual(ev2["event"], "holdings_disclosure")
        # 普通的增持/回购公告不受影响（仍走 holder_flow 方向判断）
        ev3 = semantics.classify_event("某公司公告：控股股东拟增持公司股份", "")
        self.assertEqual(ev3["event"], "holder_flow")
        self.assertEqual(ev3["label"], "bull")

    def test_collapse_same_template(self):
        from fundai import screening
        mk = lambda i, title: {
            "item_id": "x%d" % i, "title": title, "auto_label": "neutral",
            "user_label": "", "event_type": "dict"}
        items = [
            mk(1, "财联社9月10日电，香港交易所信息显示，摩根大通（JPMorgan）在中兴通讯H股"
                  "的持股比例于09月07日从5.91%升至6.14%，购买的平均股价为23.6934港元。"),
            mk(2, "财联社9月10日电，香港交易所信息显示，摩根大通（JPMorgan）在中际旭创H股"
                  "的持股比例于09月07日从13.44%升至14.01%，购买的平均股价为1144.5338港元。"),
            mk(3, "财联社9月10日电，香港交易所信息显示，摩根大通（JPMorgan）在药明康德H股"
                  "的持股比例于09月07日从9.90%升至10.01%，购买的平均股价为191.7113港元。"),
            mk(4, "重要通知：今日市场成交显著放量，北向资金净流入超百亿"),
        ]
        kept, dupes = screening.collapse_same_template(items)
        self.assertEqual([k["item_id"] for k in kept], ["x1", "x4"])
        self.assertEqual({d["item_id"] for d in dupes}, {"x2", "x3"})
        self.assertTrue(all(d["_dup_of"] == "x1" for d in dupes))
        # 方向明确的消息不参与归并（该进哪进哪）
        items2 = [dict(items[0], auto_label="bull"), dict(items[1], auto_label="bull")]
        kept2, dupes2 = screening.collapse_same_template(items2)
        self.assertEqual(len(kept2), 2)
        self.assertEqual(dupes2, [])
        # 命中自动归档事件类型的（重新打分后）也不进归并池
        items3 = [dict(items[0], event_type="holdings_disclosure"),
                  dict(items[1], event_type="holdings_disclosure")]
        kept3, dupes3 = screening.collapse_same_template(items3)
        self.assertEqual((len(kept3), len(dupes3)), (2, 0))


class LlmLabelTest(unittest.TestCase):
    """记录来源展示名：provider 是模板默认 deepseek 而 model 实为其他家族时，按真实模型显示。"""

    def test_label_follows_real_model_family(self):
        from fundai import analysis
        self.assertEqual(analysis.llm_label(
            {"llm": {"provider": "deepseek", "model": "qwen3.8-flash"}}), "Qwen")
        self.assertEqual(analysis.llm_label(
            {"llm": {"provider": "deepseek", "model": "deepseek-chat"}}), "DeepSeek")
        # 显式自定义（非默认值）的 provider 原样尊重
        self.assertEqual(analysis.llm_label(
            {"llm": {"provider": "MyGateway", "model": "gpt-4o"}}), "MyGateway")
        self.assertEqual(analysis.llm_label({}), "DeepSeek")
        self.assertEqual(analysis.llm_label(
            {"llm": {"provider": "", "model": "glm-4"}}), "GLM")


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
                                   "schema": news_mod.NEWS_SCHEMA,
                                   "bull": [], "bear": [],
                                   "net": 2, "score": 16, "amplitude": 8})
        obj = news_mod.load_news(self.date_s, amplitude=8, cfg=None)
        self.assertTrue(obj["ok"])
        self.assertEqual(len(obj["feed"]), 1)
        self.assertEqual(obj["schema"], news_mod.NEWS_SCHEMA)


class KlineFinalBarTest(unittest.TestCase):
    def test_bar_is_final(self):
        self.assertFalse(Market._bar_is_final("2026-09-09 12:42:10"))
        self.assertTrue(Market._bar_is_final("2026-09-09 15:05:00"))
        self.assertTrue(Market._bar_is_final("2026-09-09 20:10:00"))
        self.assertTrue(Market._bar_is_final("2026-09-08 23:59:59"))

    def test_drop_partial_today(self):
        # 注意：函数内部用“真实当前时刻”判定 → 用 now_dt 打桩固定时钟，
        # 否则本测试在 15:05 前运行必挂（时间依赖修复）。
        from datetime import datetime
        today = util.today_str()
        base = datetime.strptime(today, "%Y-%m-%d")
        old_now = util.now_dt
        try:
            util.now_dt = lambda: base.replace(hour=12, minute=42)
            items = {"2026-09-08": [4500.0, 170000000.0],
                     today: [4520.0, 95000000.0]}
            # 盘中(15:05 前)抓到的当日 bar → 剔除，回到上一交易日
            out = Market._drop_partial_today(dict(items),
                                             "{} 12:42:10".format(today))
            self.assertNotIn(today, out)
            util.now_dt = lambda: base.replace(hour=20, minute=0)
            # 收盘后抓的当日 bar → 保留
            out2 = Market._drop_partial_today(dict(items),
                                              "{} 20:00:00".format(today))
            self.assertIn(today, out2)
        finally:
            util.now_dt = old_now

    def test_kline_seq_filter(self):
        items = {"2026-09-07": [4500.0, 1.0], "2026-09-08": [4510.0, 2.0]}
        seq = Market._kline_seq(items, "2026-09-08")
        self.assertEqual([x[0] for x in seq], ["2026-09-08"])


class TermLearningTest(unittest.TestCase):
    def test_fin_terms_sanity(self):
        from fundai import lexicon
        self.assertGreater(len(lexicon.FIN_TERMS), 300)
        # 关键术语必须存在
        for t in ("降准", "固态电池", "半导体", "回购", "北向资金",
                  "临时停牌", "二级市场", "参考净值", "溢价", "风险提示"):
            self.assertIn(t, lexicon.FIN_TERMS)
        # 术语化判定（v3 严格版）：只收完整术语；任何滑动窗碎片都不收
        self.assertTrue(lexicon.termness("停牌"))
        self.assertTrue(lexicon.termness("临时停牌"))
        self.assertTrue(lexicon.termness("基金份额"))
        self.assertFalse(lexicon.termness("基金二级市场"))   # 滑动窗碎片
        self.assertFalse(lexicon.termness("盘时基金份额参"))
        self.assertFalse(lexicon.termness("幅度溢价"))
        self.assertFalse(lexicon.termness("慈善基金会"))
        self.assertFalse(lexicon.termness("火车站广场"))

    def test_learn_only_fin_terms(self):
        from fundai import lexicon
        from fundai import screening
        scr = screening.ScreeningStore(":memory:")
        feed = [
            {"id": "d1", "title": "固态电池量产提速 头部企业拿下大额订单",
             "text": "固态电池装车 概念股走强 板块活跃", "auto_label": "bull",
             "auto_strength": 1, "source": "x", "time": "10:00"},
            {"id": "d2", "title": "固态电池产业化项目密集落地 设备厂商受益",
             "text": "固态电池需求旺盛 产业链公司扩产", "auto_label": "bull",
             "auto_strength": 1, "source": "x", "time": "10:01"},
            {"id": "d3", "title": "火车站广场发生爆炸 现场救援进行中",
             "text": "爆炸波及周边地区 伤亡情况不明", "auto_label": "bear",
             "auto_strength": -1, "source": "x", "time": "10:02"},
        ]
        scr.ingest_feed("2026-09-01", feed, skip_recent=False)
        with scr.conn:
            scr.conn.execute("UPDATE items SET user_label='bull',"
                             "user_strength=1 WHERE item_id IN ('d1','d2')")
            scr.conn.execute("UPDATE items SET user_label='bear',"
                             "user_strength=-1 WHERE item_id='d3'")
        n = scr.learn_all()
        learned = {w["word"] for w in scr.lexicon_rows(1000)}
        # 与金融术语无关的碎片（火车站/爆炸/现场）不应被学
        self.assertFalse(learned & {"火车站", "火车站广场", "爆炸", "现场救援"})
        # 术语化的常见表达至少学到一个
        self.assertTrue(learned & {"固态电池", "电池"})
        self.assertGreaterEqual(n, 1)
        scr.close()


class NewsCleanTest(unittest.TestCase):
    def test_fetch_news_filters_irrelevant(self):
        import fundai.news as nm
        from fundai.util import DataError
        items = [
            {"source": "东财快讯", "time": "2026-09-09 09:00",
             "title": "央行开展逆回购操作 维护流动性合理充裕", "text": "央行逆回购 500 亿"},
            {"source": "东财快讯", "time": "2026-09-09 09:01",
             "title": "某地火车站发生爆炸 造成人员受伤", "text": "爆炸 现场 救援"},
            {"source": "新浪7x24", "time": "2026-09-09 09:02",
             "title": "半导体设备板块走强 龙头涨停", "text": "芯片设备 涨停"},
        ]
        old_collect = nm._collect_all
        nm._collect_all = lambda: items
        old_learned = nm.learned_extra
        nm.learned_extra = lambda: {}
        try:
            obj = nm.fetch_news(amplitude=8, cfg=None)
        finally:
            nm._collect_all = old_collect
            nm.learned_extra = old_learned
        self.assertEqual(obj["raw"], 3)
        self.assertEqual(obj["screened_out"], 1)  # 火车站爆炸被预筛
        titles = [e["title"] for e in obj["feed"]]
        self.assertIn("央行开展逆回购操作 维护流动性合理充裕", titles)
        self.assertNotIn("某地火车站发生爆炸 造成人员受伤", titles)

    def test_importance_and_cls_fields_flow_to_store(self):
        """财联社字段：url 保留、重要电报标 🔴，并可入库持久化 important 列。"""
        import fundai.news as nm
        from fundai import screening
        items = [
            {"source": "财联社电报", "time": "10:00",
             "title": "突发！央行宣布降准", "text": "央行降准 释放长期流动性",
             "url": "https://www.cls.cn/detail/1"},
            {"source": "东财快讯", "time": "10:01",
             "title": "某公司召开股东会审议年报议案", "text": "会议正常召开",
             "url": ""},
        ]
        old_collect, old_learned = nm._collect_all, nm.learned_extra
        nm._collect_all = lambda: items
        nm.learned_extra = lambda: {}
        try:
            obj = nm.fetch_news(amplitude=8, cfg=None)
        finally:
            nm._collect_all, nm.learned_extra = old_collect, old_learned
        emap = {e["title"]: e for e in obj["feed"]}
        imp = emap.get("突发！央行宣布降准")
        self.assertIsNotNone(imp)
        self.assertEqual(imp["important"], 1)
        self.assertEqual(imp["url"], "https://www.cls.cn/detail/1")
        # 普通公司公告不标重要
        self.assertEqual(emap["某公司召开股东会审议年报议案"]["important"], 0)
        scr = screening.ScreeningStore(":memory:")
        scr.ingest_feed("2026-09-10", obj["feed"], skip_recent=False)
        rows = {r["title"]: r for r in scr.items_for("2026-09-10")}
        self.assertEqual(rows["突发！央行宣布降准"]["important"], 1)
        scr.close()

    def test_norm_title(self):
        from fundai import screening
        self.assertEqual(screening.norm_title("央行降准0.5个百分点！？ "),
                         "央行降准05个百分点")

    def test_ingest_skip_recent_duplicate(self):
        from fundai import screening
        scr = screening.ScreeningStore(":memory:")
        d1 = [{"id": "a1", "title": "央行降准0.5个百分点 释放长期资金",
               "text": "央行 降准", "auto_label": "bull", "auto_strength": 1,
               "source": "x", "time": "10:00"}]
        n1 = scr.ingest_feed("2026-09-08", d1, skip_recent=True)
        # 次日同样标题（滚动重播）→ 跳过
        d2 = [{"id": "a2", "title": "央行降准0.5个百分点 释放长期资金",
               "text": "央行 降准", "auto_label": "bull", "auto_strength": 1,
               "source": "x", "time": "09:00"}]
        n2 = scr.ingest_feed("2026-09-09", d2, skip_recent=True)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)
        self.assertEqual(len(scr.items_for("2026-09-08")), 1)
        self.assertEqual(len(scr.items_for("2026-09-09")), 0)
        # 同一条目重拉（force 刷新）→ 允许更新
        n3 = scr.ingest_feed("2026-09-08", d1, skip_recent=True)
        self.assertEqual(n3, 1)
        scr.close()

    def test_ingest_fuzzy_cross_source_dup_skipped(self):
        from fundai import screening
        scr = screening.ScreeningStore(":memory:")
        base = "央行开展五千亿元规模的逆回购操作以维护银行体系流动性合理充裕"  # len>=28
        variant = "央行开展五千亿元规模的逆回购操作以维护银行体系流动性宽松充裕"  # 同事件换字
        other = "卫星互联网龙头公司斩获批量订单产业链迎来放量期"  # 不同主题
        mk = lambda i, t: {"id": i, "title": t, "text": t,
                           "auto_label": "bull", "auto_strength": 1,
                           "source": "s", "time": "10:00"}
        scr.ingest_feed("2026-09-08", [mk("a", base)], skip_recent=True)
        n = scr.ingest_feed("2026-09-09",
                            [mk("b", variant), mk("c", other)],
                            skip_recent=True)
        # 换一字的转帖被模糊去重 → 只新增不同主题那条
        self.assertEqual(n, 1)
        self.assertEqual(len(scr.items_for("2026-09-09")), 1)
        self.assertEqual(scr.items_for("2026-09-09")[0]["item_id"], "c")
        scr.close()

    def test_fuzzy_not_merge_distinct_topic(self):
        from fundai import screening
        a = "央行开展五千亿元规模的逆回购操作以维护银行体系流动性合理充裕"
        b = "卫星互联网龙头公司斩获批量订单产业链迎来放量期"
        self.assertFalse(screening.ScreeningStore._fuzzy_dup([a], b, 0.93))

    def test_review_split_caps_at_50(self):
        from fundai import screening
        mk = lambda i, a, u="": {"item_id": i, "auto_label": a, "user_label": u}
        items = [mk(str(i), "neutral") for i in range(60)] + \
                [mk("b1", "bull"), mk("b2", "bear"), mk("d1", "bull", "bull")]
        top, extra, ai, done = screening.ScreeningStore.review_split(items, 50)
        self.assertEqual(len(top), 50)
        self.assertEqual(len(extra), 10)
        self.assertEqual(len(ai), 2)
        self.assertEqual(len(done), 1)

    def test_news_score_cap(self):
        from fundai.engine import combine_score
        # 显式钉住权重，避免随 config 调整而失效（消息 0.35 仅用于本用例）
        cfg = {"strategy": {"news_weight": 0.35, "news_score_cap": 25,
                            "micro_weight": 0.0}}
        self.assertEqual(combine_score(cfg, 0, 64), 9)   # 64 被限到 25：25*0.35≈9
        self.assertEqual(combine_score(cfg, 0, 8), 3)     # 8*0.35=2.8 → 3
        self.assertEqual(combine_score(cfg, 0, -100), -9)


class SemanticsTest(unittest.TestCase):
    def _ev(self, title, text=""):
        from fundai import semantics
        return semantics.classify_event(title, text)

    def test_cbank_cut_infers_bull(self):
        ev = self._ev("央行宣布全面降准0.5个百分点", "释放长期资金约一万亿元")
        self.assertEqual(ev["event"], "cbank_ease")
        self.assertEqual(ev["scope"], "market")
        self.assertEqual(ev["label"], "bull")
        self.assertGreaterEqual(ev["strength"], 3)
        self.assertIn("流动性", ev["reason"])

    def test_geo_conflict_infers_bear(self):
        ev = self._ev("中东冲突加剧 局势再度升级", "双方交火规模扩大")
        self.assertEqual(ev["event"], "geo_conflict")
        self.assertEqual(ev["label"], "bear")
        self.assertEqual(ev["scope"], "market")

    def test_earnings_beat_infers_bull_single(self):
        ev = self._ev("某公司中报预增 净利润同比增长一倍", "业绩超预期")
        self.assertEqual(ev["event"], "earnings")
        self.assertEqual(ev["scope"], "single")
        self.assertEqual(ev["label"], "bull")

    def test_etf_premium_warning_neutral(self):
        ev = self._ev(
            "沪深300ETF盘中临时停牌：二级市场交易价格明显高于基金份额参考净值",
            "出现较大幅度溢价，未有效回落，本基金提示溢价风险")
        self.assertEqual(ev["event"], "fund_premium_warning")
        self.assertEqual(ev["label"], "neutral")
        self.assertEqual(ev["strength"], 0)
        self.assertIn("产品", ev["reason"])

    def test_holder_reduction_infers_bear_single(self):
        ev = self._ev("某控股股东披露减持计划", "拟减持不超过2%股份")
        self.assertEqual(ev["event"], "holder_flow")
        self.assertEqual(ev["label"], "bear")
        self.assertEqual(ev["scope"], "single")

    def test_reapply_auto_updates_legacy_rows(self):
        from fundai import screening
        scr = screening.ScreeningStore(":memory:")
        feed = [{"id": "p1", "title": "沪深300ETF盘中临时停牌：二级市场交易价格高于份额参考净值",
                 "text": "溢价风险提示", "auto_label": "bear", "auto_strength": -1,
                 "auto_net": 0, "event_type": "", "auto_reason": "",
                 "source": "x", "time": "10:00"}]
        scr.ingest_feed("2026-09-09", feed, skip_recent=False)
        # 先把该行写成旧的“误判 bear”状态，再重算
        with scr.conn:
            scr.conn.execute(
                "UPDATE items SET auto_label='bear', auto_strength=-1, "
                "event_type='', auto_reason='' WHERE item_id='p1'")
        n = scr.reapply_auto("2026-09-09")
        self.assertEqual(n, 1)
        row = scr.items_for("2026-09-09")[0]
        self.assertEqual(row["auto_label"], "neutral")
        self.assertEqual(row["event_type"], "fund_premium_warning")
        self.assertTrue(row["auto_reason"])
        scr.close()

    def test_learn_skips_premium_template_rows(self):
        from fundai import screening
        scr = screening.ScreeningStore(":memory:")
        feed = [
            {"id": "a", "title": "沪深300ETF溢价提示：二级市场交易价格高于参考净值",
             "text": "盘中临时停牌 溢价风险", "auto_label": "bear",
             "auto_strength": -1, "auto_net": -1,
             "event_type": "fund_premium_warning", "auto_reason": "产品",
             "source": "x", "time": "10:00"},
            {"id": "b", "title": "沪深300ETF溢价提示：基金份额参考净值低于市价",
             "text": "盘中临时停牌", "auto_label": "bear", "auto_strength": -1,
             "auto_net": -1, "event_type": "fund_premium_warning",
             "auto_reason": "产品", "source": "x", "time": "10:01"},
        ]
        scr.ingest_feed("2026-09-09", feed, skip_recent=False)
        with scr.conn:
            scr.conn.execute("UPDATE items SET user_label='bear',"
                             "user_strength=-1 WHERE item_id IN ('a','b')")
        scr.learn_all()
        learned = {w["word"] for w in scr.lexicon_rows(1000)}
        # 产品风险提示模板的行不参与学习：其高频词不应变成“利空词”
        self.assertFalse(learned & {"停牌", "溢价", "参考净值"})
        scr.close()


class SignalCalibTest(unittest.TestCase):
    def test_evaluate_next_day_hits(self):
        from fundai import calib
        dates = ["2026-09-07", "2026-09-08", "2026-09-09"]
        closes = {"2026-09-07": 100.0, "2026-09-08": 103.0,
                  "2026-09-09": 99.0}
        rows = [
            {"date": "2026-09-07", "auto_label": "bull",
             "event_type": "cbank_ease", "scope": "market", "source": "x",
             "time": "09:00"},
            {"date": "2026-09-08", "auto_label": "bear",
             "event_type": "geo_conflict", "scope": "market", "source": "x",
             "time": "10:00"},
            {"date": "2026-09-08", "auto_label": "neutral",
             "event_type": "fund_premium_warning", "scope": "single",
             "source": "x", "time": "10:01"},
        ]
        out, agg = calib.evaluate(rows, closes, dates)
        # 09-07 bull → 09-08 +3% 命中；09-08 bear → 09-09 -3.9% 命中；
        # 09-08 neutral → 09-09 波动大 → 不命中
        by = {(r["date"], r["event_type"]): r for r in out}
        self.assertEqual(by[("2026-09-07", "cbank_ease")]["hit"], 1)
        self.assertEqual(by[("2026-09-08", "geo_conflict")]["hit"], 1)
        self.assertEqual(
            by[("2026-09-08", "fund_premium_warning")]["hit"], 0)
        self.assertEqual(agg["cbank_ease"]["hit_rate"], 1.0)
        self.assertEqual(agg["geo_conflict"]["hit_rate"], 1.0)

    def test_cls_export_rows(self):
        import tempfile
        from pathlib import Path
        from fundai import calib
        rows = [
            {"time": "2026-09-09 10:00", "title": "央行降准0.5个百分点",
             "text": "释放长期资金"},
            {"time": "2026-09-09 10:01", "title": "某中东国家冲突加剧",
             "text": "局势升级 地缘风险升温"},
        ]
        p = Path(tempfile.gettempdir()) / "_cls_probe.json"
        p.write_text(__import__("json").dumps(rows), encoding="utf-8")
        try:
            feed = calib.cls_export_rows(str(p))
        finally:
            p.unlink(missing_ok=True)
        self.assertEqual(len(feed), 2)
        labels = {f["title"]: f["auto_label"] for f in feed}
        self.assertEqual(labels["央行降准0.5个百分点"], "bull")
        self.assertEqual(labels["某中东国家冲突加剧"], "bear")
        self.assertEqual(feed[0]["source"], "财联社电报(手动导入)")
        self.assertTrue(all(f["event_type"] for f in feed))


class ScoreCalcTest(unittest.TestCase):
    """评分透明化：records.calc 存列/旧库迁移 + 消息分明细口径。"""

    def test_record_calc_roundtrip_with_old_schema(self):
        import os
        import sqlite3
        import tempfile
        from fundai.ledger import Ledger
        db = os.path.join(tempfile.mkdtemp(), "m.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        con.execute("CREATE TABLE records(date TEXT PRIMARY KEY,"
                    "market_score INTEGER, market_chg REAL, index_close REAL,"
                    "view_title TEXT, source TEXT, analysis TEXT, decision TEXT)")
        con.commit()
        con.close()
        lg = Ledger(db, initial_cash=1000.0)   # 旧库缺 calc 列 → 自动 ALTER
        lg.add_record("2026-01-01", -15, 0.01, 4500.0, "观点", "src", "报告",
                      "决策", calc='{"v": 1}')
        r = lg.get_records()[0]
        self.assertEqual(r["calc"], '{"v": 1}')
        self.assertEqual(r["market_score"], -15)

    def test_human_news_meta_scaled_anti_saturation(self):
        """人工消息分同样受抗饱和刻度约束（旧口径打标多了必然顶格、零区分度）。"""
        from fundai import screening
        scr = screening.ScreeningStore(":memory:")
        feed = [{"id": "h%d" % i, "title": "消息%d" % i, "text": "",
                 "auto_label": "neutral", "auto_strength": 0, "auto_net": 0,
                 "event_type": "dict", "auto_reason": "", "source": "x",
                 "time": "09:0%d" % i} for i in range(3)]
        scr.ingest_feed("2026-09-09", feed, skip_recent=False)
        with scr.conn:
            scr.conn.execute("UPDATE items SET user_label='bull', user_strength=1")
        h = scr.human_news_meta("2026-09-09", amplitude=8, mode="scaled",
                                scale=10.0)
        self.assertAlmostEqual(h["net_raw"], 3.0, places=2)
        self.assertAlmostEqual(h["net"], 0.3, places=3)      # 3/10
        self.assertEqual(h["score"], 2)                      # 0.3×8=2.4→2
        old = scr.human_news_meta("2026-09-09", amplitude=8, mode="sum")
        self.assertEqual(old["net"], 3.0)
        self.assertEqual(old["score"], 24)                   # 旧口径线性膨胀
        scr.close()

    def test_news_breakdown_human_and_auto(self):
        from fundai import screening
        scr = screening.ScreeningStore(":memory:")
        feed = [
            {"id": "h1", "title": "央行降准0.5个百分点", "text": "",
             "auto_label": "bull", "auto_strength": 2, "auto_net": 2,
             "event_type": "macro_policy", "auto_reason": "", "source": "x",
             "time": "09:00"},
            {"id": "h2", "title": "某公司拟减持", "text": "", "auto_label": "bear",
             "auto_strength": -1, "auto_net": -1, "event_type": "holder_flow",
             "auto_reason": "", "source": "x", "time": "09:01"},
            {"id": "h3", "title": "交易所调整保证金通知", "text": "",
             "auto_label": "neutral", "auto_strength": 0, "auto_net": 0,
             "event_type": "dict", "auto_reason": "", "source": "x", "time": "09:02"},
        ]
        scr.ingest_feed("2026-09-09", feed, skip_recent=False)
        with scr.conn:
            scr.conn.execute("UPDATE items SET user_label='big_bull',"
                             "user_strength=3 WHERE item_id='h1'")
        auto = scr.news_breakdown("2026-09-09", human_mode=False)
        self.assertEqual(auto["net"], 1.0)          # +2-1，中性不计
        self.assertEqual([i["title"] for i in auto["items"]],
                         ["央行降准0.5个百分点", "某公司拟减持"])   # |强度|降序
        hum = scr.news_breakdown("2026-09-09", human_mode=True)
        self.assertEqual(hum["net"], 3.0)           # 只统计人工方向打标
        self.assertEqual(hum["counts"], {"big_bull": 1})
        scr.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
