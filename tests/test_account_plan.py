# -*- coding: utf-8 -*-
"""投入本金可自定义 + 目标 = 本金 × 倍数（默认 1.3）的测试。

用户要求（2026-09-11）：投入总金额可自定义（默认 1000），目标改为在设定值基础上
**增长 30%**，其余算法不变。因此这里覆盖：
1. `settings.account_plan()` 的推导与回退（旧配置没有 target_multiple 时不漂移）；
2. `Engine.ensure_account()`：目标永远按「账本本金 × 配置倍数」重算（改倍数立即生效，
   不需要重新 init）；
3. `fundai.configedit`：在面板上改本金/倍数时，派生键 `account.target_value` 自动同步；
4. CLI `init --cash N`：目标 = N × 倍数，并写回配置。
全部离线，只操作临时账本/临时配置。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fundai import configedit, settings
from fundai.engine import Engine
from fundai.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _cfg(**acct):
    cfg = json.loads(json.dumps(settings.DEFAULT_CFG))
    cfg["account"].update(acct)
    return cfg


class LlmPromptTest(unittest.TestCase):
    """LLM 提示词里的本金/目标必须**跟着配置走**，而且不能被花括号格式坑到。

    2026-09-11 回归：把 target 写进提示词时给含 `{"score": ...}` 字面花括号的字符串
    接了 `.format()`，直接 `KeyError: '"score"'` —— `run-daily` 全线报错。
    """

    def test_prompt_builds_and_contains_numbers(self):
        from unittest import mock
        from fundai import analysis
        captured = {}

        def fake_post(*a, **kw):
            payload = a[1] if len(a) > 1 else kw.get("payload") or {}
            captured["messages"] = payload.get("messages") or []
            # 注意：llm_analyze 对 <60 字的正文会判为无效并返回 None（防垃圾输出），
            # 所以这里的假响应必须给足长度。
            return {"choices": [{"message": {"content": json.dumps(
                {"score": 10, "title": "试",
                 "text": "测试正文。" * 20})}}],
                "model": "test"}

        cfg = _cfg(initial_cash=2000.0, target_multiple=1.3)
        cfg["llm"] = {"enabled": True, "api_key": "x", "provider": "t",
                      "base_url": "https://example.invalid", "model": "m"}
        with mock.patch.object(analysis.util, "http_post_json",
                               side_effect=fake_post):
            r = analysis.llm_analyze(cfg, {"日期": "2026-09-11"})
        self.assertIsNotNone(r, "提示词构造失败会返回 None")
        joined = " ".join(m["content"] for m in captured["messages"])
        self.assertIn("2000", joined)      # 本金
        self.assertIn("2600", joined)      # 目标 = 2000 × 1.3
        self.assertIn("30%", joined)       # 增长幅度


class AccountPlanTest(unittest.TestCase):
    def test_default_is_1000_and_plus_30pct(self):
        initial, target, mult = settings.account_plan(_cfg(initial_cash=1000.0,
                                                          target_multiple=1.3))
        self.assertEqual(initial, 1000.0)
        self.assertEqual(target, 1300.0)
        self.assertAlmostEqual(mult, 1.3)

    def test_custom_initial_scales_target(self):
        for cash, mult, want in ((2000.0, 1.3, 2600.0), (500.0, 1.3, 650.0),
                                 (1000.0, 1.5, 1500.0), (3000.0, 1.2, 3600.0)):
            initial, target, m = settings.account_plan(
                _cfg(initial_cash=cash, target_multiple=mult))
            self.assertEqual((initial, target), (cash, want),
                             "本金 {} × {} 应为 {}".format(cash, mult, want))

    def test_missing_multiple_falls_back_to_explicit_target(self):
        # 旧配置只有 target_value（没有 target_multiple）→ 不得漂移
        cfg = _cfg(initial_cash=1000.0)
        cfg["account"].pop("target_multiple", None)
        cfg["account"]["target_value"] = 1500.0
        self.assertEqual(settings.account_plan(cfg)[1], 1500.0)
        self.assertAlmostEqual(settings.account_plan(cfg)[2], 1.5)

    def test_invalid_multiple_falls_back_to_default(self):
        initial, target, mult = settings.account_plan(
            _cfg(initial_cash=1000.0, target_multiple=0))
        self.assertEqual(target, 1300.0)
        self.assertAlmostEqual(mult, 1.3)

    def test_target_multiple_overrides_stale_target_value(self):
        # 倍数存在时以倍数重算（避免"两个真值"打架）
        initial, target, _ = settings.account_plan(
            _cfg(initial_cash=1000.0, target_multiple=1.3, target_value=9999.0))
        self.assertEqual(target, 1300.0)


class EnsureAccountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _engine(self, cfg, lg):
        """**不要**用默认 market：`Engine.__init__` 会 new 一个 `Market`，
        而 `Market` 注册的是**全局**交易日历（`util.set_calendar`），
        会污染其它用例（实测把 test_core 的"未注册日历→工作日近似"断言打挂）。
        这里传一个最小 stub，保持测试无副作用。
        """
        class _StubMarket:
            warnings = []
            token = ""
            akshare_on = False
            use_zhitu = False

            def reload(self, cfg):
                pass

        return Engine(cfg, lg, market=_StubMarket())

    def test_target_rederived_from_ledger_initial(self):
        cfg = _cfg(initial_cash=1000.0, target_multiple=1.3)
        lg = Ledger(self.db, initial_cash=1000.0)
        lg.set_account_meta({"initial_cash": 1000.0, "target_value": 1500.0,
                             "start_date": "2026-09-06"})
        meta = self._engine(cfg, lg).ensure_account()
        self.assertEqual(meta["target_value"], 1300.0,
                         "配置倍数 1.3 应把旧目标 1500 覆盖为 1300")

    def test_changing_multiple_takes_effect_without_reinit(self):
        cfg = _cfg(initial_cash=2000.0, target_multiple=1.3)
        lg = Ledger(self.db, initial_cash=2000.0)
        lg.set_account_meta({"initial_cash": 2000.0, "target_value": 3000.0})
        eng = self._engine(cfg, lg)
        self.assertEqual(eng.ensure_account()["target_value"], 2600.0)
        cfg["account"]["target_multiple"] = 1.5
        self.assertEqual(self._engine(cfg, lg).ensure_account()["target_value"],
                         3000.0)


class ConfigEditDerivedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "config.json"
        cfg = json.loads(json.dumps(settings.DEFAULT_CFG))
        cfg["account"].update({"initial_cash": 1000.0, "target_multiple": 1.3,
                               "target_value": 1300.0})
        self.path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        self._bd = configedit.backup_dir
        d = self.tmp / "bk"
        d.mkdir(exist_ok=True)
        configedit.backup_dir = lambda: d

    def tearDown(self):
        configedit.backup_dir = self._bd
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _acct(self):
        return json.loads(self.path.read_text(encoding="utf-8"))["account"]

    def test_initial_cash_updates_target(self):
        r = configedit.apply_changes({"account.initial_cash": 2000.0},
                                     path=self.path)
        self.assertTrue(r["ok"], r)
        a = self._acct()
        self.assertEqual(a["initial_cash"], 2000.0)
        self.assertEqual(a["target_value"], 2600.0, "改本金必须重算目标")

    def test_multiple_updates_target(self):
        r = configedit.apply_changes({"account.target_multiple": 1.5},
                                     path=self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._acct()["target_value"], 1500.0)

    def test_invalid_multiple_rejected(self):
        r = configedit.apply_changes({"account.target_multiple": -1}, path=self.path)
        self.assertFalse(r["ok"])
        self.assertEqual(self._acct()["target_value"], 1300.0)


class InitCliTest(unittest.TestCase):
    """CLI：`init --cash N` 目标 = N × 倍数，并把计划写回配置。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "t.db"
        # 用临时 FUNDAI_HOME 隔离：init 会写配置
        self.home = self.tmp / "home"
        self.home.mkdir()
        shutil.copy2(settings.CONFIG_PATH if settings.CONFIG_PATH.exists()
                     else ROOT / "config.example.json", self.home / "config.json")
        self.env = dict(os.environ, FUNDAI_HOME=str(self.home))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_custom_cash_and_default_multiple(self):
        p = subprocess.run([PY, "app.py", "init", "--db", str(self.db),
                            "--cash", "2500"],
                           cwd=str(ROOT), env=self.env, capture_output=True,
                           timeout=300)
        out = (p.stdout or b"").decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, out[-300:])
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
        self.assertEqual(data["initial_cash"], 2500.0)
        self.assertEqual(data["target_value"], 3250.0)   # 2500 × 1.3
        cfg = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["account"]["initial_cash"], 2500.0)
        self.assertEqual(cfg["account"]["target_value"], 3250.0)

    def test_init_explicit_target_wins(self):
        p = subprocess.run([PY, "app.py", "init", "--db", str(self.db),
                            "--cash", "1000", "--target", "1400"],
                           cwd=str(ROOT), env=self.env, capture_output=True,
                           timeout=300)
        out = (p.stdout or b"").decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, out[-300:])
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
        self.assertEqual(data["target_value"], 1400.0)
        self.assertAlmostEqual(data["target_multiple"], 1.4, places=6)


if __name__ == "__main__":
    unittest.main()
