# -*- coding: utf-8 -*-
"""数据源用量计数/降级状态的持久化测试（全程离线，不发任何网络请求）。

覆盖本次 BUG 的四个关键点：
1. **重启不清零**：计数落盘 data/api_usage.json，新构造 Market / 新读文件仍在；
2. 日期翻转：今天→明天后，昨日计数仍可读（用 mock.patch 替换 util.today_str）；
3. 失败与降级可见：fail 计数 + failover 记录 + active 记录；
4. 坏文件容错：data/api_usage.json 写坏 JSON 也不抛异常；
5. Market 层面：假的 util.http_get_json 伪造智兔失败/成功，断言 attempts/ok/fail
   与 note_failover 都进了持久化存储。

日期注入方式说明
----------------
本模块统一用 ``mock.patch("fundai.util.today_str", ...)`` 注入「今天」——
``api_usage`` 与 ``Market`` 都通过 ``util.today_str()`` 取当天日期（模块属性查找，
patch 生效），因此不需要给业务函数加日期参数。
"""
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fundai import api_usage as au
from fundai import datasource, util
from fundai.datasource import Market


class _IsolatedUsageFile(unittest.TestCase):
    """把计数文件重定向到临时目录（FUNDAI_USAGE_FILE),避免污染 data/api_usage.json。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fundai_usage_")
        self.path = os.path.join(self.tmp, "api_usage.json")
        self._old_env = os.environ.get(au.ENV_PATH)
        os.environ[au.ENV_PATH] = self.path
        # 本模块构造 Market 时会用**假 K线**注册交易日历（util.set_calendar 是全局
        # 进程状态），会污染其它用例（如 test_core.CalendarTest）。这里把它挡住，
        # 不改变被测的计数逻辑。
        patcher = mock.patch.object(util, "set_calendar",
                                    lambda *a, **kw: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop(au.ENV_PATH, None)
        else:
            os.environ[au.ENV_PATH] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 便捷断言/工具 ----
    def _snap(self, **kw):
        kw.setdefault("provider", "测试源")
        return au.snapshot(**kw)

    def _src(self, source, site=None):
        site = site or self._snap()["today"]["sources"]
        return site.get(source) or {}

    def _read_file(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)


class PersistAcrossRestartTest(_IsolatedUsageFile):
    """核心回归：进程重启（重新构造 Market / 重新读模块）后计数不清零。"""

    def test_counts_survive_restart(self):
        cfg = {"data": {"provider": "zhitu", "zhitu_token": "T", "zhitu_daily_limit": 200}}
        m1 = Market(cfg)
        # 真实路径写入：1 次成功的智兔请求（limit 由 Market 自己带上）
        with mock.patch.object(util, "http_get_json",
                               return_value=[{"jzrq": "2026-09-10", "dwjz": "1.0"}]):
            m1._zt_fund_navs("000001")
        # 再补 1 次失败尝试（异常路径也要落盘）
        m1._note("zhitu", ok=False, error="网络超时", purpose="index_kline")
        before = m1.usage()
        self.assertEqual(before["calls_today"], 2)
        self.assertEqual(before["zhitu_today"], 2)
        self.assertEqual(before["daily_limit"], 200)
        # 计数已落盘（不是内存 dict）
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(
            self._read_file()["days"][util.today_str()]["sources"]["zhitu"]["calls"], 2)

        # “重启”：重新构造 Market（全新的内存状态）
        m2 = Market(cfg)
        after = m2.usage()
        self.assertEqual(after["calls_today"], 2, "重启后今日计数被清零（回归）")
        self.assertEqual(after["zhitu_today"], 2)
        self.assertEqual(after["akshare_today"], 0)
        src = (after["sources"] or {}).get("zhitu") or {}
        self.assertEqual(src.get("calls"), 2)
        self.assertEqual(src.get("ok"), 1)
        self.assertEqual(src.get("fail"), 1)
        self.assertEqual(src.get("limit"), 200)
        self.assertIn("网络超时", src.get("last_error") or "")

    def test_new_module_instance_reads_same_file(self):
        """另一个 ApiUsage 实例（相当于新进程）读同一文件，计数仍在。"""
        au.note("eastmoney", ok=True)
        au.note("eastmoney", ok=False, error="boom")
        other = au.ApiUsage()          # 不传 path：动态解析 FUNDAI_USAGE_FILE
        snap = other.snapshot()
        self.assertEqual(snap["today"]["calls"], 2)
        self.assertEqual(snap["today"]["sources"]["eastmoney"]["fail"], 1)

    def test_atomic_write_helper_roundtrip(self):
        """util.write_json_atomic 与 read 往返一致（内部使用，非抛异常）。"""
        p = os.path.join(self.tmp, "atomic.json")
        self.assertTrue(util.write_json_atomic(p, {"a": "中文", "b": 1}))
        self.assertEqual(util.load_json(p), {"a": "中文", "b": 1})


class DayRolloverTest(_IsolatedUsageFile):
    """日期翻转：把「今天」变成下一天后，昨日计数仍可读。"""

    def test_yesterday_kept_after_rollover(self):
        with mock.patch("fundai.util.today_str", return_value="2026-09-11"):
            au.note("zhitu", ok=True, limit=200)
            au.note("akshare", ok=True)
            au.note("akshare", ok=False, error="x")
            # 注意：mock.patch 只在 with 内生效，期间的计数写在 2026-09-11
        with mock.patch("fundai.util.today_str", return_value="2026-09-12"):
            snap = au.snapshot()
            self.assertEqual(snap["date"], "2026-09-12")
            self.assertEqual(snap["today"]["calls"], 0, "新的一天应从 0 开始")
            y = snap["yesterday"]
            self.assertEqual(y["date"], "2026-09-11")
            self.assertEqual(y["calls"], 3)
            self.assertEqual(y["fail"], 1)
            self.assertEqual((y["sources"]["zhitu"] or {})["calls"], 1)
            days = {d["date"]: d for d in snap["days"]}
            self.assertEqual(days["2026-09-11"]["calls"], 3)
            self.assertIn("2026-09-12", days)

    def test_usage_summary_yesterday_visible(self):
        with mock.patch("fundai.util.today_str", return_value="2026-09-11"):
            au.note("sina", ok=True)
        with mock.patch("fundai.util.today_str", return_value="2026-09-12"):
            summ = au.usage_summary(provider="P")
            self.assertEqual(summ["calls_today"], 0)
            self.assertEqual(summ["yesterday"]["calls"], 1)


class FailoverVisibleTest(_IsolatedUsageFile):
    """失败与降级必须可见：fail 计数 + failover 记录 + active 记录。"""

    def test_fail_and_failover_recorded(self):
        au.note("akshare", ok=False, error="AKShare 瞬时失败(1/4)：连接重置",
                purpose="nav")
        au.note_failover("nav", "akshare", "eastmoney", "AKShare 瞬时失败(1/4)")
        au.note("eastmoney", ok=True, purpose="nav")
        snap = self._snap()
        ak = self._src("akshare")
        self.assertEqual(ak["fail"], 1)
        self.assertEqual(ak["ok"], 0)
        self.assertIn("瞬时失败", ak["last_error"])
        em = self._src("eastmoney")
        self.assertEqual((em["calls"], em["ok"], em["fail"]), (1, 1, 0))
        self.assertEqual(snap["active"].get("nav"), "eastmoney")
        self.assertEqual(len(snap["failover"]), 1)
        fo = snap["failover"][0]
        self.assertEqual((fo["purpose"], fo["from"], fo["to"]),
                         ("nav", "akshare", "eastmoney"))
        self.assertIn("瞬时失败", fo["reason"])
        # 降级判定：nav 链首选 akshare，当前 eastmoney → degraded
        self.assertTrue(snap["degraded"])
        self.assertIn("eastmoney", snap["degraded_reason"])

    def test_active_first_choice_is_not_degraded(self):
        au.note("eastmoney", ok=True, purpose="quote")
        au.note_active("quote", "eastmoney")
        snap = self._snap(chain={"quote": ("eastmoney", "sina", "tencent")})
        self.assertEqual(snap["active"].get("quote"), "eastmoney")
        self.assertFalse(snap["degraded"])
        self.assertIsNone(snap["degraded_reason"])

    def test_error_text_is_truncated(self):
        au.note("tencent", ok=False, error="E" * 5000)
        err = self._src("tencent")["last_error"]
        self.assertLessEqual(len(err), au.ERROR_MAX)

    def test_token_is_redacted(self):
        au.note("zhitu", ok=False, error="请求失败 https://x?token=SECRET123 boom")
        err = self._src("zhitu")["last_error"]
        self.assertNotIn("SECRET123", err)
        self.assertIn("***", err)

    def test_failover_cap_and_day_prune(self):
        """failover 只留最近 100 条；更早的日期桶会被清掉（保留 30 天）。"""
        for i in range(au.MAX_FAILOVER + 20):
            au.note_failover("nav", "akshare", "eastmoney", "第{}次".format(i))
        data = self._read_file()
        self.assertEqual(len(data["failover"]), au.MAX_FAILOVER)
        self.assertIn("第{}次".format(au.MAX_FAILOVER + 19),
                      data["failover"][-1]["reason"])


class RetentionTest(_IsolatedUsageFile):
    """保留策略：只留最近 30 天；更早的日期桶会被删掉。"""

    def test_old_days_are_pruned(self):
        # 用 mock.patch 注入连续 40 天，每天记 1 次调用
        base = "2026-08-01"
        days = [util.add_days(base, i) for i in range(40)]
        self.assertEqual(len(days), 40)
        for ds in days:
            with mock.patch("fundai.util.today_str", return_value=ds):
                au.note("zhitu", ok=True, limit=200)
        data = self._read_file()
        kept = sorted(data["days"])
        self.assertEqual(len(kept), au.KEEP_DAYS, "旧日期桶未被裁剪")
        self.assertEqual(kept[-1], days[-1])
        self.assertEqual(kept[0], days[-au.KEEP_DAYS])
        self.assertNotIn(days[0], data["days"])

    def test_many_failovers_pruned_to_cap(self):
        for i in range(au.MAX_FAILOVER + 30):
            au.note_failover("quote", "sina", "tencent", "第{}次".format(i))
        data = self._read_file()
        self.assertEqual(len(data["failover"]), au.MAX_FAILOVER)
        self.assertEqual(data["failover"][0]["reason"], "第30次")
        self.assertEqual(data["failover"][-1]["reason"],
                         "第{}次".format(au.MAX_FAILOVER + 29))


class BadFileToleranceTest(_IsolatedUsageFile):
    """坏 JSON / 字段缺失：snapshot 不抛异常并返回可用结构。"""

    def _broken(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{坏 JSON")

    def test_broken_json_is_tolerated(self):
        self._broken()
        snap = self._snap()                       # 不应抛异常
        self.assertEqual(snap["today"]["calls"], 0)
        self.assertEqual(snap["today"]["limit"], None)
        self.assertEqual(snap["failover"], [])
        self.assertIn("zhitu", snap["today"]["sources"])
        self.assertIsNone(snap["error"])
        # 坏文件之后仍能正常写入（丢弃重建）
        au.note("sina", ok=True)
        self.assertEqual(self._snap()["today"]["calls"], 1)

    def test_broken_json_market_usage_still_works(self):
        self._broken()
        m = Market({"data": {"provider": "zhitu", "zhitu_token": "T"}})
        usage = m.usage()                          # 不应抛异常
        for k in ("provider", "calls_today", "daily_limit", "zhitu_today",
                  "akshare_today"):
            self.assertIn(k, usage)
        self.assertEqual(usage["calls_today"], 0)
        self.assertEqual(usage["daily_limit"], 200)

    def test_garbage_structure_is_tolerated(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"version": 1, "days": {"2026-09-11": "不是字典"},
                                "failover": "不是列表", "active": 42}))
        snap = self._snap()
        self.assertEqual(snap["today"]["calls"], 0)
        self.assertEqual(snap["active"], {})
        au.note("ths", ok=True)
        self.assertEqual(self._snap()["today"]["calls"], 1)


class MarketZhituCountingTest(_IsolatedUsageFile):
    """Market 层面：伪造智兔失败/成功（不联网），断言计数与降级记录。"""

    def _market(self):
        return Market({"data": {"provider": "zhitu", "zhitu_token": "T",
                                "zhitu_daily_limit": 200}})

    def _zhitu_rec(self, m):
        return (m.usage()["sources"] or {}).get("zhitu") or {}

    def test_zhitu_failure_then_success_recorded(self):
        m = self._market()
        # ---- 第一次：失败（异常）——旧实现完全不计数，本次必须记 fail ----
        with mock.patch.object(util, "http_get_json",
                               side_effect=util.DataError("模拟智兔网络失败")):
            with self.assertRaises(util.DataError):
                m._zt_get("/jh/hb/lsjz/000001", purpose="nav")
        rec = self._zhitu_rec(m)
        self.assertEqual(rec["calls"], 1, "失败的智兔请求也必须计数")
        self.assertEqual(rec["ok"], 0)
        self.assertEqual(rec["fail"], 1)
        self.assertEqual(rec["limit"], 200)
        self.assertIn("模拟智兔网络失败", rec["last_error"])
        # ---- 第二次：成功 ----
        rows = [{"jzrq": "2026-09-10", "dwjz": "1.2345"}]
        with mock.patch.object(util, "http_get_json", return_value=rows):
            navs = m._zt_fund_navs("000001")
        self.assertEqual(navs, [("2026-09-10", 1.2345)])
        rec = self._zhitu_rec(m)
        self.assertEqual(rec["calls"], 2)
        self.assertEqual(rec["ok"], 1)
        self.assertEqual(rec["fail"], 1)
        self.assertEqual(m.usage()["calls_today"], 2)
        self.assertEqual(m.usage()["zhitu_today"], 2)
        self.assertEqual(m.usage()["daily_limit"], 200)
        self.assertEqual(m.usage()["remaining"], 198)

    def test_missing_token_counts_as_failure(self):
        m = Market({"data": {"provider": "zhitu", "zhitu_token": ""}})
        with self.assertRaises(util.DataError):
            m._zt_get("/x")
        rec = self._zhitu_rec(m)
        self.assertEqual((rec["calls"], rec["fail"]), (1, 1))
        self.assertIn("Token", rec["last_error"] or "")

    def test_nav_chain_failover_recorded(self):
        """净值链 akshare 失败 → 智兔成功：failover + active 都必须落盘。"""
        m = self._market()
        m.akshare_on = True
        m.warnings = []
        rows = [{"jzrq": "2026-09-10", "dwjz": "1.0"}]
        # 只伪造网络层（http_get_json）：智兔的计数逻辑（_zt_get）真实执行
        with mock.patch.object(Market, "_ak_nav_items",
                               side_effect=util.DataError("AKShare 瞬时失败(1/4)")):
            with mock.patch.object(util, "http_get_json", return_value=rows):
                seq = m.fund_history("000001", force_live=True)
        self.assertEqual(seq[-1], ("2026-09-10", 1.0))
        usage = m.usage()
        self.assertEqual(usage["active"].get("nav"), "zhitu")
        self.assertTrue(usage["degraded"])
        self.assertEqual((usage["failover"][0]["from"],
                          usage["failover"][0]["to"]), ("akshare", "zhitu"))
        self.assertEqual(usage["sources"]["zhitu"]["ok"], 1)
        # 原有 warnings 行为不变（其它模块在用）
        self.assertTrue(any("akshare" in w for w in m.warnings))

    def test_snapshot_present_in_usage_and_market_helper(self):
        """_zt_get 返回空列表也算一次「已尝试」（旧实现只在成功返回后才计数）。"""
        m = self._market()
        with mock.patch.object(util, "http_get_json", return_value=[]):
            self.assertEqual(
                m._zt_get("/hz/history/fsjy/000300.SH/d", purpose="index_kline"),
                [])
        usage = m.usage()
        self.assertEqual(usage["snapshot"]["today"]["calls"], 1)
        self.assertEqual(usage["snapshot"]["today"]["sources"]["zhitu"]["limit"], 200)
        self.assertEqual(usage["snapshot"]["active"]["index_kline"], "zhitu")
        snap = m.usage_snapshot()
        self.assertEqual(snap["today"]["calls"], 1)
        self.assertEqual(snap["today"]["sources"]["zhitu"]["limit"], 200)
        self.assertFalse(snap["degraded"])       # index_kline 首选就是 zhitu

    def test_degraded_when_index_kline_falls_back(self):
        m = self._market()
        # index_kline 链首选 zhitu，实际由 eastmoney 供货 → 必须判为降级
        m._note("eastmoney", ok=True, purpose="index_kline")
        usage = m.usage()
        self.assertTrue(usage["degraded"])
        self.assertIn("指数K线", usage["degraded_reason"])


class EastmoneyPaginationCountingTest(_IsolatedUsageFile):
    """东财分页只算“一次逻辑调用”，失败要记进 fail（不发真实请求）。"""

    def test_multipage_counts_as_single_call(self):
        m = Market({"data": {"provider": "eastmoney", "zhitu_token": ""}})
        pages = [
            {"Data": {"LSJZList": [{"FSRQ": "2026-09-%02d" % (10 + i),
                                    "DWJZ": "1.1"} for i in range(20)]},
             "TotalCount": 40},
            {"Data": {"LSJZList": [{"FSRQ": "2026-09-30", "DWJZ": "1.2"}]},
             "TotalCount": 40},
        ]
        with mock.patch.object(util, "http_get_json", side_effect=pages):
            with mock.patch.object(datasource.time, "sleep", lambda *_a: None):
                got = m._em_range("000001", "2026-09-01", "2026-09-30")
        self.assertEqual(len(got), 21)
        em = m.usage()["sources"]["eastmoney"]
        self.assertEqual(em["calls"], 1, "分页应汇总成一次调用")
        self.assertEqual(em["ok"], 1)
        self.assertEqual(em["fail"], 0)

    def test_pagination_failure_counted(self):
        m = Market({"data": {"provider": "eastmoney", "zhitu_token": ""}})
        with mock.patch.object(util, "http_get_json",
                               side_effect=util.DataError("东财 502")):
            with self.assertRaises(util.DataError):
                m._em_range("000001", "2026-09-01", "2026-09-30")
        em = m.usage()["sources"]["eastmoney"]
        self.assertEqual(em["calls"], 1)
        self.assertEqual(em["fail"], 1)
        self.assertIn("502", em["last_error"])


class QuoteChainCountingTest(_IsolatedUsageFile):
    """指数实时行情链（新浪→腾讯→东财）：每通道计数 + 降级可视（离线）。"""

    CFG = {"data": {"provider": "zhitu", "zhitu_token": "T"},
           "market": {"indices": [{"name": "沪深300", "secid": "1.000300",
                                   "benchmark": True}]}}

    def _fake_http(self, url, headers=None, timeout=12, tries=2):
        if url.startswith("https://hq.sinajs.cn/"):
            raise util.DataError("模拟新浪 403")
        if url.startswith("https://qt.gtimg.cn/"):
            # 腾讯格式：v_s_sh000300="1~名称~代码~价~涨跌~涨幅~量~额";
            body = 'v_s_sh000300="1~沪深300~000300~4500.5~55.5~1.25~123~456";'
            return body.encode("gb18030")
        raise util.DataError("未知URL " + url[:60])

    def test_sina_fail_then_tencent_success(self):
        m = Market(self.CFG)
        with mock.patch.object(util, "http_get", side_effect=self._fake_http):
            out = m.indices_quote(force=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["code"], "000300")
        usage = m.usage()
        self.assertEqual((usage["sources"]["sina"]["calls"],
                          usage["sources"]["sina"]["fail"]), (1, 1))
        self.assertEqual((usage["sources"]["tencent"]["calls"],
                          usage["sources"]["tencent"]["ok"]), (1, 1))
        self.assertIn("模拟新浪 403", usage["sources"]["sina"]["last_error"])
        self.assertEqual(usage["active"]["quote"], "tencent")
        self.assertTrue(usage["degraded"])
        self.assertIn("tencent", usage["degraded_reason"])
        self.assertIn("sina", usage["failover"][0]["from"])
        self.assertEqual(usage["failover"][0]["to"], "tencent")

    def test_eastmoney_success_is_not_degraded(self):
        m = Market(self.CFG)

        def _em(url, headers=None, timeout=12, tries=2):
            # 新浪/腾讯返回无法解析的报文（真实场景里它们可能被墙/改格式）
            if not url.startswith("https://push2.eastmoney.com/"):
                return b"<html>blocked</html>"
            return json.dumps({"data": {"diff": [
                {"f12": "000300", "f14": "沪深300", "f2": 4500.0,
                 "f3": 1.25, "f4": 55.0}]}}).encode("gb18030")

        with mock.patch.object(util, "http_get", side_effect=_em):
            out = m.indices_quote(force=True)
        self.assertEqual(len(out), 1)
        usage = m.usage()
        self.assertEqual(usage["sources"]["eastmoney"]["ok"], 1)
        self.assertEqual(usage["sources"]["sina"]["fail"], 1)
        self.assertEqual(usage["sources"]["tencent"]["fail"], 1)
        self.assertEqual(usage["active"]["quote"], "eastmoney")
        # 链上东财是首选 → 不算降级
        self.assertFalse(usage["degraded"])


class PartialCacheReadTest(_IsolatedUsageFile):
    """真实读缓存路径也不该产生新的调用记录。"""

    def test_partial_cache_read_adds_no_calls(self):
        m = Market({"data": {"provider": "eastmoney", "zhitu_token": ""}})
        m.akshare_on = False                       # 通道链只剩 eastmoney
        code = "000000"                            # 不会被真实使用的代码
        today = util.today_str()
        cache = {"updated": util.now_iso(),
                 "items": {util.add_days(today, -i): 1.0 + i * 0.001
                           for i in range(1, 200)}}
        path = m._nav_cache_path(code)
        util.save_json(path, cache)
        self.addCleanup(lambda: path.exists() and path.unlink())
        before = m.usage()["calls_today"]
        seq = m.fund_history(code, need_from=util.add_days(today, -30))
        self.assertGreater(len(seq), 0)            # 磁盘缓存够用 → 不走在线
        self.assertEqual(m.usage()["calls_today"], before,
                         "读缓存路径不应增加任何调用计数")


class IndexHistoryOfflineTest(_IsolatedUsageFile):
    """index_history(offline=True)：只读缓存、零新增调用、缓存空直接报错。"""

    ZT_CFG = {"data": {"provider": "zhitu", "zhitu_token": "T",
                       "zhitu_daily_limit": 200},
              "market": {"index": {"name": "沪深300", "zhitu_code": "000300.SH",
                                   "eastmoney_secid": "1.000300"}}}
    EM_CFG = {"data": {"provider": "eastmoney", "zhitu_token": ""},
              "market": {"index": {"name": "沪深300", "zhitu_code": "000300.SH",
                                   "eastmoney_secid": "1.000300"}}}

    def setUp(self):
        super().setUp()
        # 关键：把 K线缓存路径整体改到临时目录，**绝不读写 data/cache 里的真实缓存**
        # （否则本用例既可能被真实缓存干扰，也可能删掉生产缓存文件）。
        self.cache_dir = tempfile.mkdtemp(prefix="fundai_kline_")
        self.addCleanup(shutil.rmtree, self.cache_dir, True)

        def _fake_cache_path(dm_or_secid, _dir=self.cache_dir):
            safe = re.sub(r"[^0-9A-Za-z]", "_", str(dm_or_secid))
            return Path(_dir) / "kline_{}.json".format(safe)

        patcher = mock.patch.object(Market, "_kline_cache_path",
                                    staticmethod(_fake_cache_path))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _boom(*_a, **_kw):
        raise AssertionError("离线模式发起了网络请求（util.http_get_json）")

    @staticmethod
    def _boom_text(*_a, **_kw):
        raise AssertionError("离线模式发起了网络请求（util.http_get_text）")

    @staticmethod
    def _boom_raw(*_a, **_kw):
        raise AssertionError("离线模式发起了网络请求（util.http_get）")

    def _write_cache(self, m, key, items, updated=None):
        """把假的历史K线写进**临时**缓存目录（离线测试不联网、不碰真实缓存）。"""
        path = m._kline_cache_path(key)
        util.save_json(path, {"updated": updated or util.now_iso(),
                              "items": items})
        return path

    def test_offline_reads_cache_without_any_request(self):
        m = Market(self.ZT_CFG)
        # 缓存明显落后于今天（历史回测的常态）→ 默认逻辑会联网补，离线必须不补
        items = {util.add_days("2018-01-02", i): [3000.0 + i, 1e8]
                 for i in range(60)}
        self._write_cache(m, "000300.SH", items)
        self.assertEqual(m.usage()["calls_today"], 0)
        with mock.patch.object(util, "http_get_json", self._boom), \
                mock.patch.object(util, "http_get_text", self._boom_text), \
                mock.patch.object(util, "http_get", self._boom_raw):
            seq = m.index_history(need_from="2018-01-02", offline=True)
        self.assertEqual(len(seq), 60)
        self.assertEqual(seq[0], ("2018-01-02", 3000.0, 1e8))
        # 零新增调用（含失败计数）
        usage = m.usage()
        self.assertEqual(usage["calls_today"], 0, "离线回测不应新增任何调用")
        self.assertEqual((usage["sources"]["zhitu"]["calls"],
                          usage["sources"]["eastmoney"]["calls"]), (0, 0))
        self.assertEqual(usage["failover"], [])
        self.assertIsNone(usage["active"].get("index_kline"))

    def test_offline_slices_by_need_from(self):
        m = Market(self.ZT_CFG)
        items = {util.add_days("2019-01-02", i): [3000.0 + i, 1e8]
                 for i in range(90)}
        self._write_cache(m, "000300.SH", items)
        with mock.patch.object(util, "http_get_json", self._boom):
            seq = m.index_history(need_from="2019-02-01", offline=True)
        self.assertEqual(seq[0][0], "2019-02-01")
        self.assertEqual(len(seq), 60)
        self.assertEqual(m.usage()["calls_today"], 0)

    def test_offline_empty_cache_raises(self):
        m = Market(self.ZT_CFG)
        with mock.patch.object(util, "http_get_json", self._boom):
            with self.assertRaises(util.DataError) as ctx:
                m.index_history(offline=True)
        self.assertIn("离线模式", str(ctx.exception))
        self.assertIn("为空", str(ctx.exception))
        self.assertEqual(m.usage()["calls_today"], 0)

    def test_offline_without_zhitu_uses_eastmoney_cache(self):
        m = Market(self.EM_CFG)
        self.assertFalse(m.use_zhitu)
        items = {util.add_days("2017-01-03", i): [2900.0 + i, 9e7]
                 for i in range(40)}
        self._write_cache(m, "1.000300", items)      # key = eastmoney_secid
        with mock.patch.object(util, "http_get_json", self._boom):
            seq = m.index_history(need_from="2017-01-03", offline=True)
        self.assertEqual(len(seq), 40)
        self.assertEqual(m.usage()["calls_today"], 0)

    def test_offline_without_zhitu_empty_cache_raises(self):
        m = Market(self.EM_CFG)
        with mock.patch.object(util, "http_get_json", self._boom):
            with self.assertRaises(util.DataError) as ctx:
                m.index_history(offline=True)
        self.assertIn("离线模式", str(ctx.exception))
        self.assertEqual(m.usage()["calls_today"], 0)

    def test_online_path_still_requests_and_counts(self):
        """对照：offline=False 时该 key 缓存落后 → 仍走在线并计数（默认行为不变）。

        注意：这里只断言“确实发生了在线尝试且被计入”（calls>=1、fail>=1），
        不锁定精确条数 —— 在线路径的“成功探针记录”条数由 datasource 的在线实现决定。
        """
        m = Market(self.EM_CFG)
        items = {util.add_days("2017-01-03", i): [2900.0 + i, 9e7]
                 for i in range(40)}
        self._write_cache(m, "1.000300", items)
        with mock.patch.object(util, "http_get_json",
                               side_effect=util.DataError("模拟联网失败")) as mocked:
            # 缓存不为空 → 东财通道吞掉异常退回本地缓存（与旧行为一致）
            seq = m.index_history(need_from="2017-01-03")
            self.assertGreaterEqual(mocked.call_count, 1,
                                    "离线=False 必须真的发起在线请求")
        self.assertEqual(len(seq), 40)
        em = m.usage()["sources"]["eastmoney"]
        self.assertGreaterEqual(em["calls"], 1, "在线路径必须计数")
        self.assertGreaterEqual(em["fail"], 1, "在线失败必须计入 fail")
        self.assertIn("模拟联网失败", em["last_error"])


class ResetAndPathTest(_IsolatedUsageFile):
    """reset 与路径解析。"""

    def test_reset_clears_file(self):
        au.note("zhitu", ok=True)
        self.assertTrue(os.path.exists(self.path))
        au.reset()
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self._snap()["today"]["calls"], 0)

    def test_default_path_is_data_file(self):
        m = au.ApiUsage()          # 不传路径时按 ENV/默认解析
        self.assertEqual(str(m.path()), self.path)
        self.assertIn("api_usage.json", str(au.store().path()))

    def test_note_never_raises_on_bad_input(self):
        au.note(None)            # 空来源：直接忽略，不计数
        au.note("")
        au.note("zhitu")         # 有效调用
        au.note("zhitu", error=object(), purpose=object())   # 脏对象也不能抛
        au.note_failover(None, None, None)     # 参数不全：忽略
        au.note_failover("nav", "sina", "tencent", object())   # 脏原因也不能抛
        au.note_failover("None", "None", "None")   # 字面量 "None" 归一并忽略
        au.note_active(None, None)
        au.snapshot(cfg="不是字典", provider=None, chain="不是字典")
        snap = self._snap()
        self.assertEqual(snap["today"]["calls"], 3)
        self.assertEqual(len(snap["failover"]), 1)   # 只有合法那条被写入
        self.assertTrue(snap["date"])
        for fo in snap["failover"]:
            self.assertTrue(isinstance(fo["reason"], str))   # 脏对象转成字符串
            self.assertNotEqual(fo["to"], "None")            # 不写字符串 "None"


if __name__ == "__main__":
    unittest.main()

class ActivePurposeTest(unittest.TestCase):
    """`active` 只应记录**真实用途**，不能出现 None 之类的脏键。

    2026-09-11 审计发现：`api_usage.note(purpose=None)` 用 `_safe_str(None)` 得到
    字符串 "None"，于是界面显示 `active: {"None": "zhitu"}`（无用途的探测被当成一种用途）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "usage.json")
        self.store = au.ApiUsage(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_none_purpose_not_recorded(self):
        self.store.note("zhitu", ok=True, purpose=None)
        snap = self.store.snapshot({})
        self.assertEqual(snap["active"], {}, "purpose=None 不应写入 active")

    def test_real_purpose_recorded(self):
        self.store.note("zhitu", ok=True, purpose="nav")
        self.store.note("eastmoney", ok=False, error="x", purpose="quote")
        self.store.note_failover("quote", "eastmoney", "sina", "连接重置")
        snap = self.store.snapshot({})
        self.assertEqual(snap["active"], {"nav": "zhitu", "quote": "sina"})
        self.assertEqual(snap["today"]["calls"], 2)
        self.assertEqual(snap["today"]["fail"], 1)

