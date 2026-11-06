# -*- coding: utf-8 -*-
"""2026-09-16 三块重做的回归测试。

对应三条用户反馈：
1. **情绪/方向不能把方向钉在词上**
   > 「同一个东西，自动驾驶这些，利空利多不是每天都在变得吗？怎么能这样单一地总结呢？」
   → 覆盖 `context.assess()`：同一术语在不同语境/不同市场状态下得到**不同**方向；
     结果动词主导、否定反转、转折后半句加权、个股级事件折价、写作措辞词不产生方向。

2. **每天人工介入一共 ≤50 次**
   > 「尽可能让 AI 进行判断，要我人工介入的每天不超过五十次！是一起五十次！」
   → 覆盖 `interventions`：human 占额度、auto 不占、超限可发现；
     以及 `review_split` 只把**低把握度**消息排进人工队列。

3. **"数据源降级提示"不能永远挂着**
   > 「⚠️ 数据源降级提示 近期有失败，这个就没成功过！」
   → 覆盖 `api_usage` 的告警口径：连续失败阈值、失败新鲜度、超时自愈、空判不算降级；
     并覆盖真实探活 `probe_source` 让安静的源能"证明自己已恢复"。

全部离线：不发网络请求（探活用 mock），不依赖 config.json。
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from fundai import api_usage, context, interventions, lexicon


# ===========================================================================
# ① 上下文条件化：方向由上下文决定，不由词决定
# ===========================================================================
class ContextDirectionTest(unittest.TestCase):
    """用户举的例子必须成立：同一个"自动驾驶"，语境不同方向相反。"""

    def _a(self, txt, term_stats=None, regime=None, ai_label=None, ai_conf=None):
        return context.assess(txt, base=lexicon.score_text(txt),
                              term_stats=term_stats, regime=regime,
                              ai_label=ai_label, ai_conf=ai_conf)

    def test_same_term_opposite_direction_by_context(self):
        bad = self._a("某车企自动驾驶系统发生严重事故，宣布召回 3 万辆，股价大跌")
        good = self._a("自动驾驶法规正式落地，多家车企获批量产，产业链订单大幅增长")
        self.assertEqual(bad["label"], "bear",
                         "事故语境应为利空：{}".format(bad))
        self.assertEqual(good["label"], "bull",
                         "落地语境应为利多：{}".format(good))
        self.assertIn("自动驾驶", "某车企自动驾驶系统发生严重事故" +
                      "自动驾驶法规正式落地")
        self.assertNotEqual(bad["strength"], good["strength"])

    def test_contract_sign_vs_cancel(self):
        self.assertEqual(self._a("公司与客户签约 4.03 亿元订单")["label"], "bull")
        self.assertEqual(self._a("公司与客户解除 4.03 亿元合同")["label"], "bear")

    def test_negation_reverses_direction(self):
        v = self._a("公司未能按期签约，订单不再纳入本年计划")
        self.assertEqual(v["label"], "bear")
        self.assertIn("!签约", v["evidence"]["hits"],
                      "证据里要标出这次命中是被否定的")

    def test_turn_clause_wins(self):
        """「营收增长但净利润预减」应看转折后半句 → 利空。"""
        self.assertEqual(
            self._a("公司营收增长但净利润预减 40%，毛利率下滑")["label"], "bear")

    def test_wording_terms_do_not_create_direction(self):
        """「交易所/公告/披露/成交额」是新闻措辞，不是方向（旧词典的错就在这里）。"""
        for txt in ("上交所公告：本周新增 3 家上市公司披露申报材料",
                    "两市成交额 1.2 万亿，板块分化明显"):
            v = self._a(txt)
            self.assertEqual(v["label"], "neutral",
                             "措辞词不该产生方向：{}".format(txt))
            self.assertLess(v["confidence"], 0.3, "把握度应很低")

    def test_stock_level_event_is_discounted(self):
        """单只个股的减持不该被读成大盘利空（历史污染源）。"""
        v = self._a("某股份公司股东减持 2% 股份")
        self.assertNotEqual(v["label"], "bear",
                            "个股级事件不应给出大盘利空方向：{}".format(v))
        self.assertEqual(v["evidence"]["level"], "stock")

    def test_market_level_event_gets_full_weight(self):
        v = self._a("证监会立案调查该公司信息披露违规，罚款并处罚相关责任人")
        self.assertEqual(v["label"], "bear")
        self.assertEqual(v["evidence"]["level"], "market")

    def test_reaction_stats_are_conditional_on_samples(self):
        """术语统计必须有足够样本才给方向（宁缺勿滥）。"""
        txt = "半导体板块走强，芯片需求回升"
        # 样本不足：即便命中率很高也不参与判断
        weak = self._a(txt, term_stats={"半导体": {"n": 2, "hit": 1.0, "chg": 3.0}})
        strong = self._a(txt, term_stats={"半导体": {"n": 60, "hit": 0.9, "chg": 1.2}})
        self.assertNotIn("半导体", [w for w, _h, _n in weak["evidence"]["react_detail"]],
                         "样本不足不该参与")
        self.assertIn("半导体", [w for w, _h, _n in strong["evidence"]["react_detail"]],
                      "样本充足应参与")
        self.assertGreater(strong["confidence"], weak["confidence"],
                           "有市场反应支持时把握度应更高")

    def test_regime_changes_the_read(self):
        """同一句话在不同行情下应有不同解读（"利好出尽"）。"""
        txt = "央行宣布全面降准 0.5 个百分点，释放长期流动性"
        calm = self._a(txt, regime={"chg": 0.1})
        hot = self._a(txt, regime={"chg": 2.5})
        self.assertEqual(calm["label"], "bull")
        self.assertIn("利好出尽", json.dumps(hot["evidence"], ensure_ascii=False))

    def test_ai_takes_priority(self):
        """AI 为主：给了方向且把握度够，就直接采纳，不让本地算法覆盖。"""
        v = self._a("某公司发布日常经营公告", ai_label="bear", ai_conf=0.8)
        self.assertEqual(v["source"], "ai")
        self.assertEqual(v["label"], "bear")
        low = self._a("某公司发布日常经营公告", ai_label="bear", ai_conf=0.1)
        self.assertNotEqual(low["source"], "ai",
                            "把握度不足的 AI 标签不该被采纳（留给人工抽检）")

    def test_explain_is_human_readable(self):
        v = self._a("公司与客户解除 4.03 亿元合同")
        s = context.explain(v)
        self.assertTrue(s)
        self.assertIn("解除", s)


# ===========================================================================
# ② 人工介入预算：一共 50 次
# ===========================================================================
class InterventionBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fundai_iv_")
        self.db = os.path.join(self.tmp, "screening.db")
        self.b = interventions.Budget(self.db, budget=50)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_auto_does_not_consume_budget(self):
        self.b.auto("search_merge", n=3, detail=[{"why": "自动合并"}])
        self.b.auto("order", n=2)
        st = self.b.status()
        self.assertEqual(st["used"], 0, "自动完成不占人工额度")
        self.assertEqual(st["auto"], 5, "但要计入 AI 自动完成量（可量化）")
        self.assertEqual(st["left"], 50)

    def test_human_consumes_and_can_exceed(self):
        self.b.human("rate", n=48, detail=[{"why": "抽检"}])
        self.assertEqual(self.b.status()["left"], 2)
        self.assertFalse(self.b.status()["over"])
        self.b.human("rate", n=5)
        st = self.b.status()
        self.assertTrue(st["over"], "超过 50 次应能被发现（说明有动作没被自动化）")
        self.assertEqual(st["left"], 0, "剩余不能为负")

    def test_kind_labels_and_needs_human_flags(self):
        st = self.b.status()
        self.assertTrue(st["kinds"]["rate"]["needs_human"])
        self.assertFalse(st["kinds"]["search_merge"]["needs_human"],
                         "合并搜索回填应当已经自动化，不再需要人工")
        self.assertFalse(st["kinds"]["pool"]["needs_human"])
        self.assertTrue(st["kinds"]["order"]["why"])

    def test_low_confidence_queue_only_lists_uncertain(self):
        """人工额度只花在 AI 没把握的消息上。"""
        # 关键：把 FUNDAI_HOME 指到临时目录，否则 `util.data_file("screening.db")`
        # 会解析到**真实项目库**，用例就会拿真实消息来断言（实测踩到过）。
        home = os.path.join(self.tmp, "home")
        os.makedirs(home, exist_ok=True)
        old_home = os.environ.get("FUNDAI_HOME")
        os.environ["FUNDAI_HOME"] = home
        self.addCleanup(lambda: os.environ.__setitem__("FUNDAI_HOME", old_home)
                        if old_home is not None
                        else os.environ.pop("FUNDAI_HOME", None))
        import sqlite3
        db = os.path.join(home, "screening.db")
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE IF NOT EXISTS items(
            date TEXT, item_id TEXT, title TEXT, auto_label TEXT,
            auto_strength REAL, confidence REAL, decided_by TEXT,
            auto_reason TEXT, user_label TEXT DEFAULT '',
            PRIMARY KEY(date,item_id))""")
        day = time.strftime("%Y-%m-%d")
        for i, conf in enumerate((0.10, 0.30, 0.80, 0.95)):
            con.execute("INSERT OR REPLACE INTO items(date,item_id,title,"
                        "auto_label,confidence,decided_by,user_label) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (day, "i%d" % i, "消息 %d" % i, "neutral", conf,
                         "ai" if conf >= 0.45 else "context", ""))
        con.commit()
        con.close()
        b = interventions.Budget(db, budget=50)
        q = b.low_confidence(limit=10, threshold=0.45)
        ids = {r["id"] for r in q}
        self.assertIn("i0", ids)
        self.assertIn("i1", ids)
        self.assertNotIn("i2", ids, "把握度够高的不该占用人工额度")
        self.assertNotIn("i3", ids)

    def test_history_is_recorded(self):
        self.b.human("rate", n=2)
        self.b.auto("pool", n=1)
        hist = self.b.status()["history"]
        self.assertTrue(hist)
        self.assertEqual(hist[0]["human"], 2)
        self.assertEqual(hist[0]["auto"], 1)


# ===========================================================================
# ③ 数据源降级告警：必须能自动消失
# ===========================================================================
class DegradationAlertTest(unittest.TestCase):
    """告警的三个必要条件缺一不可，任何一个不满足都不该弹。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fundai_alert_")
        self.path = os.path.join(self.tmp, "usage.json")
        os.environ["FUNDAI_USAGE_FILE"] = self.path
        self.addCleanup(lambda: os.environ.pop("FUNDAI_USAGE_FILE", None))
        api_usage.reset()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    CHAIN = {"quote": ("sina", "tencent", "eastmoney")}

    def _snap(self, chain=None):
        return api_usage.snapshot(chain=chain or self.CHAIN)

    def test_single_transient_failure_does_not_alert(self):
        api_usage.note("sina", ok=False, error="timed out", purpose="quote")
        s = self._snap()
        self.assertNotIn("sina", s["stale_sources"],
                         "失败 1 次不足以定性为'这个源有问题'")
        self.assertEqual(s["stale_detail"]["_cleared"]["sina"],
                         "连续失败 1 次，未达阈值 3（多为限流/抖动）")

    def test_consecutive_failures_alert_then_recover(self):
        for _ in range(3):
            api_usage.note("sina", ok=False, error="timed out", purpose="quote")
        s = self._snap()
        self.assertIn("sina", s["stale_sources"])
        self.assertTrue(s["has_recent_trouble"])
        # 恢复一次成功 → 连续计数清零、告警消失（不需要等时间窗）
        api_usage.note("sina", ok=True, purpose="quote")
        s2 = self._snap()
        self.assertNotIn("sina", s2["stale_sources"])
        self.assertFalse(s2["has_recent_trouble"])

    def test_stale_failure_expires_without_any_success(self):
        """**这是"这个就没成功过"的根因**：失败后再没被调用的源，
        旧实现靠"最后一次是失败"判定，该条件永真 → 提示永不消失。"""
        for _ in range(3):
            api_usage.note("eastmoney", ok=False, error="boom", purpose="quote")
        self.assertIn("eastmoney", self._snap()["stale_sources"])
        # 把 last_fail 改成 2 小时前（模拟"之后一直没被调用"）
        data = json.loads(open(self.path, encoding="utf-8").read())
        day = sorted(data["days"])[-1]
        data["days"][day]["sources"]["eastmoney"]["last_fail"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 7200))
        open(self.path, "w", encoding="utf-8").write(
            json.dumps(data, ensure_ascii=False))
        s = self._snap()
        self.assertNotIn("eastmoney", s["stale_sources"],
                         "失败超时后应视为已自愈，不再告警")
        self.assertFalse(s["has_recent_trouble"])

    def test_empty_degradation_is_not_reported(self):
        """首选源当天 0 次调用 → 不算"故障降级"（空判）。"""
        api_usage.note("tencent", ok=True, purpose="quote")
        api_usage.note_active("quote", "tencent")
        s = self._snap()
        self.assertFalse(s["degraded"])
        self.assertIn("非故障降级", s["degraded_reason"] or "")

    def test_real_degradation_is_reported(self):
        api_usage.note("sina", ok=False, error="timeout", purpose="quote")
        api_usage.note("tencent", ok=True, purpose="quote")
        api_usage.note_active("quote", "tencent")
        s = self._snap()
        self.assertTrue(s["degraded"])
        reason = s["degraded_reason"] or ""
        self.assertIn("tencent", reason, "要写明当前是谁在供货")
        self.assertIn("失败 1 次", reason, "要写明首选源失败过几次")

    def test_probe_lets_a_quiet_source_prove_recovery(self):
        """探活是"让安静的源证明自己已恢复"的手段，且**失败也记账**。"""
        for _ in range(3):
            api_usage.note("sina", ok=False, error="boom", purpose="quote")
        self.assertIn("sina", self._snap()["stale_sources"])

        # 探活成功（mock 掉网络层）
        class _R(object):
            status = 200

            def read(self, _n=None):
                return b'var hq_str_s_sh000300="...";'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        # 把切换记录与失败时间挪到"旧"（模拟"很久以前失败、之后一直静默"）
        old = time.strftime("%Y-%m-%dT%H:%M:%S",
                            time.localtime(time.time() - 7200))
        data = json.loads(open(self.path, encoding="utf-8").read())
        day = sorted(data["days"])[-1]
        src = data["days"][day]["sources"]["sina"]
        src["last_fail"] = old
        for it in (data.get("failover") or []):
            it["ts"] = old
        open(self.path, "w", encoding="utf-8").write(
            json.dumps(data, ensure_ascii=False))
        # 旧失败已超时 → 静默；此时探活成功应当把该源彻底复位
        self.assertNotIn("sina", self._snap()["stale_sources"])
        with mock.patch("urllib.request.urlopen", return_value=_R()):
            res = api_usage.probe_source("sina", cfg={})
        self.assertTrue(res["ok"])
        s = self._snap()
        self.assertNotIn("sina", s["stale_sources"])
        self.assertFalse(s["has_recent_trouble"], "探活成功后不该再有告警")

        # 探活失败同样记账（否则状态永远不会更新）
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
            api_usage.probe_source("sina", cfg={})
        rec = (self._snap()["today"]["sources"] or {}).get("sina") or {}
        self.assertGreaterEqual(rec.get("calls") or 0, 4,
                                "探活（成功与失败）都必须计入调用次数")

    def test_probe_records_failure_too(self):
        class _Bad(object):
            status = 503

            def read(self, _n=None):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_Bad()):
            res = api_usage.probe_source("tencent", cfg={})
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
