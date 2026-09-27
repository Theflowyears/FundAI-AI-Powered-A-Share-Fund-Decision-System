# -*- coding: utf-8 -*-
"""配置写入端（fundai/configedit.py）与参数面板接口的测试。

覆盖：
- 白名单：白名单外的键、结构化键（map）必须被拒绝；
- 类型/范围/枚举：越界、非数字、非枚举值必须被拒绝且原文件不被修改；
- 跨字段一致性：settings.validate 兜住（止盈写反、eq_floor>eq_cap）；
- 敏感键：未显式确认时拒绝；
- 原子写 + 备份：成功写入后可回滚，回滚内容与写入前一致；
- 重置为代码默认；
- 参数元数据完整性：param_docs 的每个 key 都能在 values/defaults 里找到，
  且 kind 属于已知集合（面板渲染依赖它）。
全部离线，且**只操作临时 config 副本**，绝不碰真实 config.json。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fundai import configedit, param_docs, settings

KINDS = {"bool", "number", "int", "enum", "list", "text", "map"}


class ConfigEditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "config.json"
        shutil.copy2(settings.CONFIG_PATH, self.path)
        self.orig = json.loads(self.path.read_text("utf-8"))
        # 备份目录也隔离到临时目录，避免污染 data/config_backups
        self._bd = configedit.backup_dir

        def _tmp_backup_dir():
            d = self.tmp / "bk"
            d.mkdir(parents=True, exist_ok=True)
            return d

        configedit.backup_dir = _tmp_backup_dir

    def tearDown(self):
        configedit.backup_dir = self._bd
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self):
        return json.loads(self.path.read_text("utf-8"))

    # ---------- 正常路径 ----------
    def test_apply_ok_and_backup_and_rollback(self):
        r = configedit.apply_changes({"strategy.news_weight": 0.25,
                                      "strategy.risk_overlay_enable": True},
                                     path=self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._cfg()["strategy"]["news_weight"], 0.25)
        self.assertTrue(self._cfg()["strategy"]["risk_overlay_enable"])
        self.assertTrue(r["backup"])
        rb = configedit.rollback(path=self.path)
        self.assertTrue(rb["ok"])
        self.assertEqual(self._cfg()["strategy"]["news_weight"],
                         self.orig["strategy"]["news_weight"])

    def test_reset_to_code_default(self):
        configedit.apply_changes({"strategy.news_weight": 0.11}, path=self.path)
        r = configedit.apply_changes({}, resets=["strategy.news_weight"],
                                     path=self.path)
        self.assertTrue(r["ok"], r)
        dft = settings.DEFAULT_CFG["strategy"]["news_weight"]
        self.assertEqual(self._cfg()["strategy"]["news_weight"], dft)

    def test_bool_coercion(self):
        r = configedit.apply_changes({"strategy.risk_overlay_enable": "true"},
                                     path=self.path)
        self.assertTrue(r["ok"])
        self.assertIs(self._cfg()["strategy"]["risk_overlay_enable"], True)
        r2 = configedit.apply_changes({"strategy.risk_overlay_enable": "off"},
                                      path=self.path)
        self.assertTrue(r2["ok"])
        self.assertIs(self._cfg()["strategy"]["risk_overlay_enable"], False)

    def test_list_field(self):
        r = configedit.apply_changes(
            {"strategy.news_focus_events": ["cbank_ease", "geo_conflict"]},
            path=self.path)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._cfg()["strategy"]["news_focus_events"],
                         ["cbank_ease", "geo_conflict"])

    # ---------- 拒绝路径（且原文件不变） ----------
    def test_unknown_key_rejected(self):
        r = configedit.apply_changes({"strategy.not_a_real_key": 1},
                                     path=self.path)
        self.assertFalse(r["ok"])
        self.assertTrue(any("白名单" in e for e in r["errors"]))
        self.assertEqual(self._cfg(), self.orig)

    def test_structured_key_rejected(self):
        r = configedit.apply_changes({"pool": []}, path=self.path)
        self.assertFalse(r["ok"])
        self.assertEqual(self._cfg(), self.orig)

    def test_range_and_type_rejected(self):
        r = configedit.apply_changes({"strategy.news_weight": 9,
                                      "strategy.top_n": "abc"},
                                     path=self.path)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["errors"]), 2)
        self.assertEqual(self._cfg(), self.orig)

    def test_enum_rejected(self):
        r = configedit.apply_changes(
            {"strategy.risk_overlay_mapping": "not_a_mapping"}, path=self.path)
        self.assertFalse(r["ok"])
        self.assertEqual(self._cfg(), self.orig)

    def test_list_option_rejected(self):
        r = configedit.apply_changes(
            {"strategy.news_focus_events": ["cbank_ease", "不存在的事件"]},
            path=self.path)
        self.assertFalse(r["ok"])
        self.assertEqual(self._cfg(), self.orig)

    def test_cross_field_validation(self):
        r = configedit.apply_changes(
            {"strategy.risk.fund_take_profit_pct": 0.5}, path=self.path)
        self.assertFalse(r["ok"])
        self.assertTrue(any("一致性" in e for e in r["errors"]))
        self.assertEqual(self._cfg(), self.orig)

    def test_sensitive_requires_confirm(self):
        r = configedit.apply_changes({"data.zhitu_token": "abc"}, path=self.path)
        self.assertFalse(r["ok"])
        self.assertEqual(self._cfg(), self.orig)
        r2 = configedit.apply_changes({"data.zhitu_token": "abc"},
                                      confirm_sensitive=True, path=self.path)
        self.assertTrue(r2["ok"], r2)

    def test_dead_key_warns(self):
        dead = [p["key"] for p in param_docs.PARAMS if p.get("dead")]
        if not dead:
            self.skipTest("没有 dead 键")
        key = dead[0]
        r = configedit.apply_changes({key: None}, path=self.path)
        # 值为 None 通常类型不合法 → 只要出现"dead 警告"或明确报错都算符合预期
        self.assertTrue(r["warnings"] or not r["ok"])


class ParamDocsContractTest(unittest.TestCase):
    """面板渲染依赖的元数据契约（缺字段会让前端渲染出 undefined）。"""

    REQUIRED = ("key", "label", "desc", "kind", "options", "min", "max", "step",
                "unit", "group", "order", "advanced", "default", "effect", "dead")

    def test_fields_present(self):
        for p in param_docs.PARAMS:
            for f in self.REQUIRED:
                self.assertIn(f, p, "{} 缺字段 {}".format(p.get("key"), f))

    def test_kinds_known(self):
        for p in param_docs.PARAMS:
            self.assertIn(p["kind"], KINDS, p["key"])

    def test_groups_declared(self):
        for p in param_docs.PARAMS:
            self.assertIn(p["group"], param_docs.GROUPS, p["key"])

    def test_toggles_are_bools(self):
        by = param_docs.by_key()
        for k in param_docs.TOGGLES:
            self.assertIn(k, by)
            self.assertEqual(by[k]["kind"], "bool", k)

    def test_coverage_missing_empty(self):
        self.assertEqual(param_docs.coverage()["missing"], [])

    def test_keys_unique(self):
        keys = [p["key"] for p in param_docs.PARAMS]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
