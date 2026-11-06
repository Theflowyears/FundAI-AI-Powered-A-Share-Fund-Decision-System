# -*- coding: utf-8 -*-
"""本轮四个修复的回归测试（2026-09-15）。

1. **午间复盘**：状态视图、只能跑当天、结算口径、统计字段；
2. **成交记录基金名称**：名称映射覆盖"持仓/账本里的代码"，池外基金也解析得出名字；
3. **词典学习**：只学**完整金融术语**（不再产生滑窗碎片）、学的是"术语 × 方向"关联、
   且学习速度与消息量近似线性（旧实现是 n-gram 枚举，消息一多就打标卡死）；
4. **配额口径**：只有智兔有 200/日上限，其余通道"不限量"且不占用智兔额度。

全部离线：临时库 + 无网络调用。
"""
import io
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from fundai import api_usage, lexicon, screening, settings, util
from fundai.datasource import Market
from fundai.engine import Engine
from fundai.ledger import Ledger

_TMP = Path(__file__).resolve().parent.parent / "_test_tmp" / "fix4"


def _tmpfile(name):
    _TMP.mkdir(parents=True, exist_ok=True)
    p = _TMP / name
    if p.exists():
        p.unlink()
    return str(p)


class TermScanTest(unittest.TestCase):
    """学习只扫完整金融术语（不再枚举 n-gram）。"""

    def test_only_whitelist_terms(self):
        txt = "央行宣布降准0.5个百分点，基金二级市场交易价格高于参考净值，临时停牌核查"
        terms = lexicon.scan_terms(txt)
        self.assertIn("降准", terms)
        self.assertIn("参考净值", terms)
        self.assertIn("临时停牌", terms)
        for t in terms:
            self.assertIn(t, lexicon.FIN_TERMS,
                          "{} 不在术语白名单里（不应被学出来）".format(t))
        # 旧实现会产出这类碎片
        for bad in ("基金二级市场", "盘时基金份额参", "幅度溢价", "场交易价格"):
            self.assertNotIn(bad, terms)

    def test_no_fragments_from_long_sentence(self):
        txt = "某基金公司公告称旗下产品二级市场交易价格出现较大幅度溢价并临时停牌核查"
        for t in lexicon.scan_terms(txt):
            self.assertIn(t, lexicon.FIN_TERMS)

    def test_longest_match_prefers_longer_term(self):
        # “参考净值” 与 “净值” 都在词表里，重叠时必须留更长的那个
        terms = lexicon.scan_terms("参考净值")
        self.assertEqual(terms, ["参考净值"])

    def test_scan_is_fast(self):
        """2000 条消息应远快于 1 秒（旧 n-gram 实现要十几秒）。"""
        docs = ["央行降准释放流动性，半导体芯片涨停潮，北向资金净流入"] * 2000
        t0 = time.perf_counter()
        for d in docs:
            lexicon.scan_terms(d)
        dt = time.perf_counter() - t0
        self.assertLess(dt, 1.5, "术语扫描太慢：{:.2f}s/2000 条".format(dt))


class LearnAssociationTest(unittest.TestCase):
    """学习的是「术语 × **市场实际反应**」，且异步不阻塞打标。

    口径变更（2026-09-16，用户："自动驾驶这些，利空利多不是每天都在变得吗？
    怎么能这样单一地总结呢？"）：旧实现统计"某词在利多消息里出现几次"，
    于是方向被永久钉在词上；现在统计"含某词的**消息次日市场实际怎么走**"，
    方向由上下文 + AI 判断，学习只提供市场反应先验。
    """

    def setUp(self):
        self.db = _tmpfile("learn.db")
        self.ledger_db = _tmpfile("learn_ledger.db")
        self.scr = screening.ScreeningStore(self.db)
        self.addCleanup(self.scr.close)
        # 学习需要"次日真实涨跌"当老师，而且样本单位是**术语×日期**，
        # 所以至少要 MIN_SAMPLES 天的历史才会产出方向先验。
        # 这里造 8 个交易日、指数每天上涨（1000 → 1080），
        # 于是每个术语的"次日上涨比例"应为 1.0 —— 这是**市场真实反应**，
        # 与"这个词听起来像不像利好"完全无关。
        con = sqlite3.connect(self.ledger_db)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS records("
                        "date TEXT PRIMARY KEY, index_close REAL)")
            self.days = ["2026-09-%02d" % (1 + i) for i in range(9)]
            for i, d in enumerate(self.days):
                con.execute("INSERT OR REPLACE INTO records(date,index_close) "
                            "VALUES(?,?)", (d, 1000.0 + i * 10.0))
            con.commit()
        finally:
            con.close()
        self.scr._ledger_db = self.ledger_db

    def _add(self, iid, title, label, event="dict", date_s=None, text=""):
        with self.scr.conn:
            self.scr.conn.execute(
                "INSERT OR REPLACE INTO items(date,item_id,title,text,user_label,"
                "user_strength,event_type) VALUES(?,?,?,?,?,?,?)",
                (date_s or self.days[0], iid, title, text, label, 0, event))

    def _add_over_days(self, prefix, title, label, n=6):
        """在 n 个不同交易日各放一条，产出 n 个"术语×日期"样本。"""
        for i in range(n):
            self._add("{}{}".format(prefix, i), title, label,
                      date_s=self.days[i])

    def test_learns_market_reaction_not_polarity(self):
        # 用**非基础词、非写作噪声词**的完整术语（"央行/流动性/交易所"这类要么是基础
        # 词要么是措辞词，会被正确排除，不适合做本用例的样本）。
        self._add_over_days("b", "新能源汽车销量超预期，人形机器人量产", "bull")
        self._add_over_days("r", "半导体公司业绩预减，光伏组件价格下滑", "bear")
        self._add_over_days("n", "今日晴，适合出行", "neutral")
        n = self.scr.learn_terms()
        self.assertGreater(n, 0, "应当学到术语")
        rows = self.scr.lexicon_rows(100)
        words = {r["word"] for r in rows}
        self.assertTrue(words, "学习成果为空")
        for w in words:
            self.assertIn(w, lexicon.FIN_TERMS, "学到了非白名单词：{}".format(w))
        # 核心断言：学到的必须是**市场反应统计**，不是"词的方向"
        r0 = rows[0]
        self.assertIn("hit", r0, "学习成果必须带市场反应命中率")
        self.assertIn("docs", r0, "学习成果必须带样本数（docs）")
        self.assertIsNotNone(r0["hit"])
        self.assertGreaterEqual(r0["hit"], 0.0)
        self.assertLessEqual(r0["hit"], 1.0)
        # 指数天天上涨 → 每个术语的"次日上涨比例"应为 1.0（市场真实反应）
        self.assertAlmostEqual(r0["hit"], 1.0, places=3)
        # 兼容入口不再返回"词的方向"（那正是被否掉的做法）
        self.assertEqual(self.scr.learned_extra(), {},
                         "learned_extra 不应再返回词的方向（方向不由词决定）")
        # 打分用的条件统计必须能拿到，且样本足够
        stats = self.scr.term_stats()
        self.assertTrue(stats, "term_stats 应产出可用的条件统计")
        self.assertTrue(all(v["n"] >= screening.ScreeningStore.MIN_SAMPLES
                            for v in stats.values()),
                        "样本不足的术语不应出现在 term_stats 里")
        # 关键：**利多与利空消息里的同一个术语，hit 都等于市场真实反应**，
        # 而不是"利多消息里的词 hit 就高" —— 这正是去掉静态极性的证据。
        self.assertTrue(all(abs(v["hit"] - 1.0) < 1e-6 for v in stats.values()),
                        "所有样本的次日都是上涨 → hit 应全为 1.0：{}".format(
                            {k: v["hit"] for k, v in stats.items()}))

    def test_samples_below_threshold_are_dropped(self):
        """样本不足 → **不给方向**（宁缺勿滥，避免 2 个样本就断言方向）。"""
        self._add("one", "新能源汽车销量超预期", "bull")
        self.scr.learn_terms()
        stats = self.scr.term_stats()
        self.assertEqual(stats, {},
                         "只有 1 个样本时不该产出方向先验：{}".format(stats))

    def test_learn_is_async_and_non_blocking(self):
        self._add_over_days("b", "新能源汽车销量超预期，人形机器人量产", "bull")
        t0 = time.perf_counter()
        self.scr.learn_async(delay=0.05)
        dt = time.perf_counter() - t0
        self.assertLess(dt, 0.2, "learn_async 不应阻塞（打标要立刻返回）")
        self.assertTrue(self.scr.wait_learn(10), "后台学习应在 10 秒内完成")
        self.assertTrue(self.scr.lexicon_rows(10), "后台学习应产出学习成果")

    def test_rate_item_returns_immediately(self):
        self._add("m1", "新能源汽车销量超预期", "")
        t0 = time.perf_counter()
        row = self.scr.rate_item(self.days[0], "m1", "bull")
        dt = time.perf_counter() - t0
        self.assertEqual(row["user_label"], "bull")
        self.assertLess(dt, 0.5, "打标应立刻返回（学习在后台）")


class QuotaSemanticsTest(unittest.TestCase):
    """只有智兔有 200/日上限，其它通道不限量且不占智兔额度。"""

    def setUp(self):
        self.path = _tmpfile("usage.json")
        os.environ["FUNDAI_USAGE_FILE"] = self.path
        self.addCleanup(lambda: os.environ.pop("FUNDAI_USAGE_FILE", None))

    def test_other_channels_do_not_consume_zhitu_quota(self):
        api_usage.reset(self.path)
        for _ in range(150):                     # 免费通道狂调
            api_usage.note("sina", ok=True, purpose="quote")
        for _ in range(3):
            api_usage.note("zhitu", ok=True, purpose="index_kline", limit=200)
        u = api_usage.usage_summary(cfg={}, provider="x", daily_limit=200,
                                   chain={"quote": ("sina", "tencent", "eastmoney")})
        self.assertEqual(u["zhitu_today"], 3)
        self.assertEqual(u["other_calls_today"], 150)
        self.assertEqual(u["zhitu_remaining"], 197, "其它通道不该吃掉智兔剩余额度")
        self.assertFalse(u["exhausted"], "智兔只用了 3 次，不该判成配额耗尽")

    def test_unlimited_sources_are_flagged(self):
        api_usage.reset(self.path)
        api_usage.note("sina", ok=True, purpose="quote")
        api_usage.note("akshare", ok=True, purpose="nav")
        u = api_usage.usage_summary(cfg={}, provider="x", daily_limit=200)
        self.assertIn("sina", u["unlimited_sources"])
        self.assertIn("akshare", u["unlimited_sources"])
        self.assertNotIn("zhitu", u["unlimited_sources"])
        quota = {q["source"]: q for q in u["quota"]}
        self.assertTrue(quota["sina"]["unlimited"])
        self.assertIsNone(quota["sina"]["limit"])
        self.assertEqual(quota["zhitu"]["limit"], 200,
                         "智兔应显示 200 上限（即使今天没调用）")

    def test_zhitu_exhaustion_detected(self):
        api_usage.reset(self.path)
        for _ in range(200):
            api_usage.note("zhitu", ok=True, purpose="index_kline", limit=200)
        u = api_usage.usage_summary(cfg={}, provider="x", daily_limit=200)
        self.assertTrue(u["zhitu_exhausted"])
        self.assertEqual(u["zhitu_remaining"], 0)
        self.assertTrue(u["exhausted"])


class ImmediateRunTest(unittest.TestCase):
    """午间复盘的命令/状态/边界。"""

    def test_status_shape(self):
        from fundai import intraday
        st = intraday.status(days=7)
        for k in ("ok", "date", "phase", "phase_ok", "today", "stats", "history"):
            self.assertIn(k, st, "status 缺少字段 {}".format(k))
        self.assertIn("total", st["stats"])

    def test_rejects_historical_date(self):
        """午间诊断靠实时行情，历史日期必须明确拒绝（而不是用现价冒充）。"""
        from fundai import intraday
        r = intraday.run_midday(date_s="2020-01-02")
        self.assertFalse(r.get("ok"))
        self.assertIn("无法回补", r.get("message") or "")


class PositionCapTest(unittest.TestCase):
    """流动性储备：至少留 reserve 元现金；本金优先做利润。"""

    def _ctx(self, total=1000.0, score=0):
        return {"score": score, "cash": total, "committed": 0.0, "eq_mv": 0.0,
                "bond_mv": 0.0, "total": total, "initial": total,
                "peak_total": total, "equity_codes": ["011609"],
                "names": {"011609": "科创50"}, "funds_mom": {"011609": 0.05},
                "funds_vol": {"011609": 0.02}, "funds_mom5": {}, "fund_pnl": {},
                "eq_hold_mv": {}, "sell_eq_by_code": {}, "locked_eq_by_code": {},
                "bond_sellable_mv": 0.0, "pending_buy_codes": set(),
                "pending_sell_codes": set(), "defensive": False,
                "risk_armed": True, "date": "2026-09-15", "funds_factors": {}}

    def _cfg(self, reserve, base=0.95, cap=0.95):
        """显式钉住本案相关参数，**不依赖用户 config.json 的调参结果**。

        发布包里的 `config.json` 是精简示例（没有 `equity_base_fixed` 等键），
        若直接吃配置默认值，本用例在快照里会得到 0.6 而不是 0.95 —— 那是
        "发布配置不同"，不是"储备逻辑坏了"。所以这里把基准仓位、调整带、
        硬上限全部写死，只让 `liquidity_reserve_yuan` 成为变量。
        """
        cfg = settings.load_config()
        cfg["strategy"] = dict(cfg.get("strategy") or {})
        st = cfg["strategy"]
        st["equity_base_mode"] = "fixed"
        st["equity_base_fixed"] = base
        st["position_band_max"] = 0.0        # 关掉调整带，只观察上限本身
        st["equity_hard_cap"] = cap
        st["risk_overlay_enable"] = False
        st["liquidity_reserve_yuan"] = reserve
        return cfg

    def test_reserve_caps_equity(self):
        from fundai import strategy
        plan = strategy.plan(self._cfg(reserve=200.0), self._ctx(total=1000.0))
        self.assertLessEqual(plan["w_target"], 0.80 + 1e-6,
                             "留 200 元流动池 → 权益目标不应超过 80%")

    def test_no_reserve_allows_full(self):
        from fundai import strategy
        plan = strategy.plan(self._cfg(reserve=0.0), self._ctx(total=1000.0))
        self.assertGreater(plan["w_target"], 0.80,
                           "不保留流动池时应能超过 80%（利润优先）")

    def test_reserve_only_needs_that_much_cash(self):
        """语义确认：reserve 是"至少留这么多现金"，不是"永远 20% 空仓"。

        本金变大时同一笔 200 元占的比例缩小，权益上限应当随之抬升。
        """
        from fundai import strategy
        small = strategy.plan(self._cfg(reserve=200.0), self._ctx(total=1000.0))
        big = strategy.plan(self._cfg(reserve=200.0), self._ctx(total=5000.0))
        self.assertLessEqual(small["w_target"], 0.80 + 1e-6)
        self.assertGreaterEqual(big["w_target"], small["w_target"],
                                "本金更大时同一笔流动池占比更小，上限不应更紧")
        self.assertAlmostEqual(big["w_target"], 0.95, delta=0.01,
                               msg="5000 元留 200 元 → 96% 可用，应顶到硬上限 95%")


if __name__ == "__main__":
    unittest.main()
