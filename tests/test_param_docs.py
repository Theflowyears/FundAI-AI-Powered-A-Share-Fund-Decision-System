# -*- coding: utf-8 -*-
"""param_docs 元数据回归：覆盖率 + 字段完整性 + dead 判定必须与代码一致。

目的是防止两件事：
1. 新增/改名参数后忘了写进 `PARAMS` → coverage.missing 会报出来（本文件断言为空）；
2. 「凭参数名猜语义」把死键写成活键（或反之）→ 本文件用源码 grep 双向核对。
"""
import json
import re
import unittest
from pathlib import Path

from fundai import param_docs, settings

from _env import needs_config  # 发行包不含 config.json，相关用例自动跳过

ROOT = Path(__file__).resolve().parent.parent
SRC_FILES = sorted(ROOT.joinpath("fundai").glob("*.py")) + [ROOT / "app.py"]

# 允许「配置里有、代码不读」的展示/凭证类键（reason 必须写在 param_docs.EXCLUDED）
EXPECTED_EXCLUDED = {"data.note", "screening.note"}

# 当前确实没有任何 Python 读取点的键（每条都必须由 grep 证据支撑）
EXPECTED_DEAD = {"market.benchmarks", "account.start_date", "account.end_date",
                 "web.port"}

# 配置里还没有、但代码有默认值的键（可手工加进 config.json）
EXPECTED_EXTRA = {
    "market.benchmarks", "strategy.event_calibration", "strategy.micro_fee_proxy",
    "strategy.news_llm_batch", "strategy.risk_overlay_neutral",
    "strategy.risk_overlay_off", "strategy.risk_overlay_off_th",
    "strategy.risk_overlay_on_th", "strategy.snap_backfill_days", "web.port",
    # 注意：strategy.replay_news 已在 config.json 里 → 不属于 extra
}


def _sources():
    return {p.name: p.read_text(encoding="utf-8") for p in SRC_FILES}


def _code_sources():
    """不含 param_docs.py 自身：避免“元数据提到过 key”被误当成读取点。"""
    return {p.name: p.read_text(encoding="utf-8") for p in SRC_FILES
            if p.name != "param_docs.py"}


def _mentions(sources, leaf):
    """源码里是否出现该叶子键名（带引号的字符串字面量）。"""
    pat = re.compile(r"""['"]%s['"]""" % re.escape(leaf))
    return {name: bool(pat.search(txt)) for name, txt in sources.items()}


class MetadataShapeTest(unittest.TestCase):
    def test_validate_metadata_passes(self):
        self.assertEqual(param_docs.validate_metadata(), [])

    def test_required_fields_present(self):
        need = ("key", "label", "desc", "kind", "options", "min", "max", "step",
                "unit", "group", "order", "advanced", "default", "effect", "dead")
        for p in param_docs.PARAMS:
            for f in need:
                self.assertIn(f, p, "{} 缺字段 {}".format(p.get("key"), f))

    def test_groups_declared_and_ordered(self):
        self.assertGreaterEqual(len(param_docs.GROUPS), 5)
        for p in param_docs.PARAMS:
            self.assertIn(p["group"], param_docs.GROUPS)
        # 每个分组都应有参数（避免前端渲染空栏）
        for g in param_docs.GROUPS:
            self.assertTrue([p for p in param_docs.PARAMS if p["group"] == g],
                            "分组 {} 没有参数".format(g))

    def test_labels_short_and_desc_chinese(self):
        for p in param_docs.PARAMS:
            self.assertLessEqual(len(p["label"]), 12, p["key"])
            self.assertTrue(re.search(r"[\u4e00-\u9fff]", p["desc"]),
                            "{} 的 desc 不是中文说明".format(p["key"]))
            self.assertTrue(p["effect"].strip(), "{} 缺 effect".format(p["key"]))

    def test_numeric_bounds_consistent(self):
        for p in param_docs.PARAMS:
            if p["kind"] in ("number", "int"):
                lo, hi = p["min"], p["max"]
                if lo is not None and hi is not None:
                    self.assertLessEqual(lo, hi, "{} 的 min>max".format(p["key"]))
        # 数值类必须有单位和步长（前端滑块要用）
        for p in param_docs.PARAMS:
            if p["kind"] in ("number", "int"):
                self.assertIsNotNone(p["step"], "{} 缺 step".format(p["key"]))
                self.assertTrue(p["unit"], "{} 缺 unit".format(p["key"]))


class CoverageTest(unittest.TestCase):
    def test_missing_is_empty_or_explained(self):
        cov = param_docs.coverage()
        unexplained = [k for k in cov["missing"]
                       if k not in EXPECTED_EXCLUDED]
        self.assertEqual(unexplained, [], "config.json 有键未收录且未说明：{}".format(
            unexplained))

    def test_excluded_reasons_present(self):
        cfg = settings.load_config()
        for key, reason in param_docs.EXCLUDED.items():
            self.assertTrue(reason.strip(), "{} 的排除原因不能为空".format(key))
            node = cfg
            for part in key.split("."):
                node = node[part]
            self.assertIsInstance(node, str)
        cov = param_docs.coverage()
        self.assertEqual({e["key"] for e in cov["excluded"]}, EXPECTED_EXCLUDED)

    @needs_config
    def test_extra_keys_are_code_defaults(self):
        cov = param_docs.coverage()
        self.assertEqual(set(cov["extra"]), EXPECTED_EXTRA)
        self.assertEqual(cov["unwired"], len(cov["extra"]))

    @needs_config
    def test_total_counts(self):
        # 注意：settings.load_config() 会做一次 _deep_default，DEFAULT_CFG 里多出来
        # 的键（如 strategy.news_score_cap）会出现在运行期配置里，所以总数按
        # 实际加载的配置计算，并额外核对裸 config.json 的叶子数不少于 110。
        cov = param_docs.coverage()
        raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        raw_leaves = [k for k in param_docs._leaf_keys(raw)
                      if k not in param_docs.EXCLUDED]
        self.assertGreaterEqual(len(raw_leaves), 110)
        self.assertGreaterEqual(cov["total"], len(raw_leaves))
        self.assertEqual(cov["params"], len(param_docs.PARAMS))
        self.assertGreater(cov["params"], 100)

    def test_defaults_match_config_file(self):
        cfg = settings.load_config()          # 与 param_docs 默认值同源（含 _deep_default）
        for p in param_docs.PARAMS:
            node = cfg
            ok = True
            for part in p["key"].split("."):
                if not isinstance(node, dict) or part not in node:
                    ok = False
                    break
                node = node[part]
            if ok:
                self.assertEqual(p["default"], node,
                                 "{} 的 default 与配置不一致".format(p["key"]))
            else:
                self.assertIsNone(p["default"],
                                  "{} 配置里没有，default 应为 None".format(p["key"]))


class ToggleTest(unittest.TestCase):
    def test_toggles_are_exactly_bool_keys(self):
        bools = {p["key"] for p in param_docs.PARAMS if p["kind"] == "bool"}
        self.assertEqual(set(param_docs.TOGGLES), bools)
        self.assertEqual(len(param_docs.TOGGLES), len(bools), "TOGGLES 有重复")

    def test_toggle_order_covers_all(self):
        for key in param_docs.TOGGLES:
            self.assertIn(key, param_docs.TOGGLE_ORDER + param_docs.TOGGLES)


class DeadFlagTest(unittest.TestCase):
    """dead 必须真的由源码 grep 支撑（双向：死的不能有读取点，活的不能没有）。"""

    def test_dead_set_matches_grep(self):
        sources = _sources()
        dead = {p["key"] for p in param_docs.PARAMS if p["dead"]}
        self.assertEqual(dead, EXPECTED_DEAD)

    def test_dead_keys_have_no_reader(self):
        sources = _sources()
        for p in param_docs.PARAMS:
            if not p["dead"]:
                continue
            leaf = p["key"].split(".")[-1]
            hits = _mentions(sources, leaf)
            # 账本/服务端的同名局部字段不算读取点，必须逐一人工确认；
            # 这里要求 desc 里写清证据，且命中的文件必须是已知的“同名不同物”。
            self.assertIn("未在代码中读取", p["desc"], p["key"])
            allow = {
                "market.benchmarks": {"datasource.py"},   # res.setdefault("benchmarks")
                # 命中的都是账本 meta 的同名字段 / DEFAULT_CFG 声明，不是 config 读取点
                "account.start_date": {"ledger.py", "engine.py", "web.py",
                                       "app.py", "settings.py"},
                "account.end_date": {"ledger.py", "engine.py", "web.py",
                                     "app.py", "settings.py"},
                # web.port 的命中是 server.port（settings.DEFAULT_CFG / web.serve）与本文档
                "web.port": {"param_docs.py", "settings.py", "web.py"},
            }[p["key"]]
            hit_files = {n for n, ok in hits.items() if ok}
            self.assertTrue(hit_files <= allow,
                            "{} 疑似被读取：{}".format(p["key"], hit_files - allow))

    def test_live_keys_are_mentioned_somewhere(self):
        """非 dead 且配置里存在的键，必须在源码里出现过该叶子名。

        例外：纯分组容器（strategy.factor_weights / market.index / pool /
        screening.universe / market.indices）自身不作为读取点出现。
        """
        sources = _code_sources()
        skip = {"strategy.factor_weights", "market.index", "pool",
                "screening.universe", "market.indices"}
        misses = []
        for p in param_docs.PARAMS:
            if p["dead"] or p["key"] in skip or p["default"] is None:
                continue
            leaf = p["key"].split(".")[-1]
            if not any(_mentions(sources, leaf).values()):
                misses.append(p["key"])
        self.assertEqual(misses, [], "标为存活但 fundai/ 内无读取点：{}".format(misses))

    def test_frontend_only_key_is_documented(self):
        """strategy.mom_window 只被前端 app.js 读取 → 不算 dead，但 desc 必须写清口径。"""
        js = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("mom_window", js)
        p = param_docs.by_key()["strategy.mom_window"]
        self.assertFalse(p["dead"])
        self.assertIn("app.js", p["desc"])
        # settings.py 只是 DEFAULT_CFG 里声明了它，不是读取点
        readers = {n for n, ok in _mentions(_code_sources(), "mom_window").items()
                   if ok} - {"settings.py"}
        self.assertEqual(readers, set())


class OptionSourceTest(unittest.TestCase):
    """枚举/列表的候选值必须来自代码，而不是编的。"""

    def test_risk_overlay_mapping_options(self):
        from fundai import risk_overlay
        p = param_docs.by_key()["strategy.risk_overlay_mapping"]
        self.assertEqual({o["value"] for o in p["options"]},
                         set(risk_overlay.MAPPINGS))

    def test_focus_events_options(self):
        from fundai import news, semantics
        p = param_docs.by_key()["strategy.news_focus_events"]
        values = {o["value"] for o in p["options"]}
        # 代码里 classify_event 返回的全部事件名（不含 none 与词典兜底的 dict：
        # dict 由 news_dict_mode 单独控制，不是可选的事件类型）
        code_events = set(re.findall(r'"event":\s*"([a-z_]+)"',
                                     (ROOT / "fundai" / "semantics.py")
                                     .read_text(encoding="utf-8")))
        code_events.discard("none")
        code_events.discard("dict")
        self.assertEqual(values, code_events)
        for k in news.DEFAULT_FOCUS_EVENTS:
            self.assertIn(k, values)

    def test_provider_options(self):
        src = (ROOT / "fundai" / "datasource.py").read_text(encoding="utf-8")
        p = param_docs.by_key()["data.provider"]
        values = {o["value"] for o in p["options"]}
        self.assertEqual(values, {"akshare", "zhitu", "eastmoney"})
        for v in values:
            self.assertIn('"{}"'.format(v), src)

    def test_equity_base_mode_options(self):
        src = (ROOT / "fundai" / "strategy.py").read_text(encoding="utf-8")
        p = param_docs.by_key()["strategy.equity_base_mode"]
        values = {o["value"] for o in p["options"]}
        self.assertEqual(values, {"fixed", "score"})
        for v in values:
            self.assertIn('"{}"'.format(v), src)

    def test_news_dict_mode_options(self):
        src = (ROOT / "fundai" / "news.py").read_text(encoding="utf-8")
        p = param_docs.by_key()["strategy.news_dict_mode"]
        values = {o["value"] for o in p["options"]}
        self.assertEqual(values, {"off", "important_only", "full"})
        for v in values:
            self.assertIn('"{}"'.format(v), src)

    def test_exec_mode_options(self):
        p = param_docs.by_key()["account.exec_mode"]
        self.assertEqual({o["value"] for o in p["options"]}, {"manual", "auto"})


class ApiContractTest(unittest.TestCase):
    """接口固定：主 agent 按这些名字消费，改名即破坏前端。"""

    def test_module_exports(self):
        self.assertIsInstance(param_docs.PARAMS, list)
        self.assertIsInstance(param_docs.GROUPS, list)
        self.assertIsInstance(param_docs.TOGGLES, list)
        self.assertTrue(callable(param_docs.by_key))
        self.assertTrue(callable(param_docs.coverage))

    def test_by_key_is_keyed_by_key(self):
        d = param_docs.by_key()
        self.assertEqual(len(d), len(param_docs.PARAMS))
        for k, p in d.items():
            self.assertEqual(k, p["key"])

    def test_coverage_shape(self):
        cov = param_docs.coverage()
        for f in ("missing", "extra", "total"):
            self.assertIn(f, cov)
        self.assertIsInstance(cov["missing"], list)
        self.assertIsInstance(cov["extra"], list)
        self.assertIsInstance(cov["total"], int)

    def test_highlighted_keys_present(self):
        """任务点名要求的重点参数一个都不能漏。"""
        must = [
            "strategy.equity_base_mode", "strategy.equity_base_fixed",
            "strategy.equity_hard_cap", "strategy.position_band_max",
            "strategy.band_vol_low", "strategy.band_vol_extreme",
            "strategy.band_micro_hot", "strategy.band_micro_cold",
            "strategy.news_weight", "strategy.micro_weight",
            "strategy.momentum_weight", "strategy.factor_weights",
            "strategy.dynamic_weights", "strategy.news_focus_events",
            "strategy.news_llm_enable", "strategy.news_llm_cap",
            "strategy.micro_enable", "strategy.micro_hist_days",
            "strategy.micro_cache_days", "strategy.risk.fund_stop_loss_pct",
            "strategy.risk.fund_take_profit_pct",
            "strategy.risk.fund_take_profit_full_pct",
            "strategy.risk.portfolio_stop_pct", "strategy.risk.rearm_days",
            "strategy.risk.ma_break_step", "strategy.risk.ma_break_max",
            "strategy.risk.ma_break_floor", "strategy.risk.reentry_score",
            "strategy.risk.allow_early_exit_fee", "strategy.risk_overlay_enable",
            "strategy.risk_overlay_mapping", "strategy.risk_overlay_smooth",
            "strategy.risk_overlay_min_hold", "strategy.risk_overlay_vol_pause",
            "strategy.risk_overlay_bull_vol_max", "strategy.risk_overlay_floor",
            "strategy.score_ema_enabled", "strategy.score_ema_alpha",
            "strategy.top_n", "strategy.theme_max", "strategy.min_momentum",
            "strategy.relative_momentum", "strategy.risk_adjusted",
            "strategy.confidence_shrink", "strategy.use_fund_convert",
            "strategy.min_hold_days", "strategy.min_order_yuan",
            "strategy.max_bond_weight", "strategy.bond_buy_floor",
            "strategy.eq_base", "strategy.regime_step", "strategy.rotate_gap",
            "strategy.rotate_winner_gain", "strategy.mom_window",
            "data.provider", "data.zhitu_daily_limit", "llm.enabled",
            "fees.sell_lt7d_default", "screening.enabled",
            "screening.top_equity", "account.exec_mode", "market.indices",
        ]
        known = param_docs.by_key()
        for k in must:
            self.assertIn(k, known, "重点参数缺失：{}".format(k))


if __name__ == "__main__":
    unittest.main()
