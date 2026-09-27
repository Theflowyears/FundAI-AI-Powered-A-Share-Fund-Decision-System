# -*- coding: utf-8 -*-
"""测试环境判定：区分「本机调优部署」与「干净环境（发行包 / 新克隆的仓库）」。

为什么要这层：
  发行包与新克隆的仓库**故意不携带** `config.json`、数据库、缓存（含密钥与 GB 级数据）。
  但有一部分测试是在**校验本部署的具体取值**——例如"线上是不是固定 70% 基准仓位"
  "参数元数据是否覆盖了当前 config.json 的**全部**叶子键"。
  这类断言在干净环境里没有意义（那里的 config.json 只是程序自动生成的默认值），
  应当 **skip 并说明原因**，而不是失败、也不该被删掉：在本机调优部署里它们仍然强制执行。

判定口径（2026-09-12 收紧）：
  光看"config.json 存不存在"不够——**任何人在干净仓库里跑一次 `app.py doctor` 都会
  自动生成一个只有默认值的 config.json**，于是这些用例会拿默认值去比对本机调优值而误报失败。
  因此改为按"配置规模"判断：叶子键数量达到本机调优部署的量级，才算"真实部署配置"。

用法：
    from _env import needs_config, needs_data
    @needs_config
    def test_xxx(self): ...
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
# 本机调优部署的 config.json 叶子键 > 110；自动生成的默认配置约 76 个。
# 阈值取 100：既能挡住"默认配置"，也不至于因为少量参数增删而误判。
CONFIG_LEAF_MIN = 100


def _leaf_count(node):
    """递归数叶子键（与 param_docs._leaf_keys 同口径，但不依赖包内模块）。"""
    if isinstance(node, dict):
        n = 0
        for v in node.values():
            n += _leaf_count(v)
        return n
    if isinstance(node, list):
        return 0
    return 1


def _config_profile():
    if not CONFIG_PATH.exists():
        return None, 0, "没有 config.json"
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:                     # 坏文件：交给专门的容错用例去测
        return None, 0, "config.json 无法解析（{}）".format(type(e).__name__)
    return cfg, _leaf_count(cfg), ""


CFG, CFG_LEAVES, CFG_NOTE = _config_profile()
HAS_CONFIG = CFG is not None
IS_TUNED_CONFIG = HAS_CONFIG and CFG_LEAVES >= CONFIG_LEAF_MIN

_WHY = ("需要本机调优部署的 config.json（叶子键 ≥ {}，实测 {}）：{}"
        .format(CONFIG_LEAF_MIN, CFG_LEAVES,
                CFG_NOTE or "干净环境里 config.json 只是程序自动生成的默认值"))

needs_config = unittest.skipUnless(IS_TUNED_CONFIG, _WHY)


def needs_data(name):
    """依赖某个数据产物（如 data/regime_windows.json）的用例。"""
    return unittest.skipUnless(
        (ROOT / "data" / name).exists(),
        "需要数据产物 data/{}（发行包不含）".format(name))
