# -*- coding: utf-8 -*-
"""项目完整性回归：未定义名扫描 + 策略决策路径冒烟（覆盖本次回测报错的代码分支）。"""
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

from fundai import settings, strategy

from _env import needs_config  # 发行包不含 config.json，相关用例自动跳过

ROOT = Path(__file__).resolve().parent.parent


def _load_namecheck():
    spec = importlib.util.spec_from_file_location(
        "_namecheck", ROOT / "data" / "namecheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class UndefinedNameTest(unittest.TestCase):
    """专治“删代码留下悬空引用”这类 bug（例如回测报 departure 未定义）。"""

    def test_no_undefined_names_in_project(self):
        nc = _load_namecheck()
        issues = []
        for top in ("fundai", "tests", "data"):
            for f in sorted((ROOT / top).rglob("*.py")):
                if "__pycache__" in str(f):
                    continue
                issues += nc.check(str(f))
        issues += nc.check(str(ROOT / "app.py"))
        self.assertEqual(issues, [], "发现未定义名：{}".format(issues))


class CliWiringTest(unittest.TestCase):
    def test_every_subcommand_has_handler(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        cmds = re.findall(r'add_parser\(\s*"([a-z0-9\-]+)"', src)
        self.assertGreaterEqual(len(cmds), 20)
        for c in cmds:
            var = re.search(
                r'(\w+)\s*=\s*sub\.add_parser\(\s*"%s"' % re.escape(c), src)
            if var:
                name = var.group(1)
                self.assertIn("{}.".format(name), src,
                              "子命令 {} 未绑定处理函数".format(c))
        self.assertIn("set_defaults(fn=", src)


def _base_ctx(**over):
    ctx = {
        "score": 30, "cash": 300.0, "committed": 0.0, "eq_mv": 700.0,
        "bond_mv": 0.0, "total": 1000.0, "initial": 1000.0,
        "peak_total": 1000.0, "bond_code": "000217",
        "bond_sellable_mv": 0.0, "bond_locked_mv": 0.0,
        "eq_hold_mv": {"270049": 700.0},
        "sell_eq_by_code": {"270049": 700.0},
        "locked_eq_by_code": {"270049": 700.0},      # 锁定期内（用于覆盖放行分支）
        "fund_pnl": {"270049": {"value": 700.0, "cost": 630.0,
                                "pnl_pct": 0.11, "sellable_mv": 700.0,
                                "locked_mv": 700.0}},
        "funds_mom": {"270049": 0.02}, "funds_mom5": {"270049": 0.01},
        "funds_vol": {"270049": 0.01},
        "equity_codes": ["270049", "002199", "006697"],
        "names": {"270049": "天弘中证银行", "002199": "前海开源军工",
                  "006697": "华宝中证医疗", "000217": "华安黄金"},
        "confidence": 0.6, "defensive": False, "theme_heat": {},
        "risk_overlay": {"action": "off", "scale": 1.0, "reason": "已关闭"},
        "funds_factors": {}, "pending_sell_codes": set(),
        "pending_buy_codes": set(),
    }
    ctx.update(over)
    return ctx


class PlanPathTest(unittest.TestCase):
    """覆盖 decide 路径的关键分支（本次回测报错的正是其中的锁定期放行分支）。"""

    def setUp(self):
        self.cfg = settings.load_config()
        # 固定池：**不要**依赖线上配置/动态池里的具体代码（它们会变，曾导致本用例
        # 顺序相关地偶发失败）。这里用两只一定存在的权益代码，且 deepcopy 避免污染缓存。
        import copy
        self.cfg = copy.deepcopy(self.cfg)
        pool_codes = [f["code"] for f in (self.cfg.get("pool") or [])
                      if f.get("kind") == "equity"]
        self.pool = (pool_codes[:4] if len(pool_codes) >= 2 else ["011609", "008888"])

    def _rotation_ctx(self, **over):
        """构造“持仓明显落后 + 锁定期内且已盈利”的场景，触发放行分支。

        注意：组合试错后生产默认是“固定 70% 基准 + 调整带关闭”，此场景需要
        略微不同的参数才能在 plan() 里走到“轮动换基”分支（见用例内的 cfg 覆盖）。

        **必须确定性**（2026-09-11 修间歇性失败）：旧写法用
        `equity_codes=list(set(...))`，Python 的 set 迭代顺序**每个进程不同**
        （字符串哈希随机化），动量并列时选出的“获胜基金”会随之跳动，
        于是同一个用例约 1/6 的概率走进不同分支。现在改为 sorted + 动量严格递减。
        """
        hold = self.pool[0] if self.pool else "011609"
        wins = sorted(self.pool[1:4]) or ["008888"]
        mom = {}
        for i, c in enumerate(wins):
            mom[c] = 0.12 - 0.02 * i          # 严格递减，避免并列
        mom[hold] = -0.05                     # 明显落后组合内最弱（差距 > rotate_gap）
        codes = sorted(set(list(mom) + [hold]))
        ctx = _base_ctx(
            score=20, cash=300.0, eq_mv=700.0,
            eq_hold_mv={hold: 700.0},
            sell_eq_by_code={hold: 0.0},        # 锁定期内 → 不可卖
            locked_eq_by_code={hold: 700.0},
            fund_pnl={hold: {"value": 700.0, "cost": 660.0, "pnl_pct": 0.06,
                             "sellable_mv": 0.0, "locked_mv": 700.0}},
            funds_mom=mom, funds_mom5={c: 0.05 for c in codes},
            funds_vol={c: 0.01 for c in codes},
            equity_codes=codes,
            funds_factors={}, ma_break={"cut": 0.0},
        )
        ctx.update(over)
        return ctx

    def test_bull_regime_allows_locked_winner_rotation(self):
        # 用“基准 60% + 调整带开启”的场景覆盖“放行锁定期转换”分支。
        # 注意：指数级风控（均线减仓/回撤冷却）会先触发减仓、抢在轮动分支前返回，
        # 因此本用例**显式关掉它们**，只测"温和上行放行轮动"这一条路径，
        # 不再随线上开关（2026-09-11 起默认开启 0.10/0.10）漂移。
        import copy
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("strategy", {})["position_band_max"] = 0.10
        cfg["strategy"]["equity_base_fixed"] = 0.60
        cfg["strategy"].setdefault("risk", {})["ma_break_step"] = 0.0
        cfg["strategy"]["risk"]["peak_trailing_pct"] = 1.0
        ctx = self._rotation_ctx(ma_align=1.0, vol_rank=0.30, micro_pct=0.80)
        ctx["ma_break"] = {"cut": 0.0}
        plan = strategy.plan(cfg, ctx)
        joined = " ".join(plan.get("msgs") or [])
        # 温和上行（调整带 >0）→ 放行锁定期内转换（此前这里因 departure 未定义而报错）
        # 断言用**结构字段 + 文案**双保险：结构字段不随文案调整而失效，
        # 文案断言保证"确实是温和上行放行"而不是被别的分支碰巧触发。
        self.assertTrue(plan.get("rotation") or "温和上行" in joined,
                        "应放行锁定期内转换；实际 msgs=%s" % (plan.get("msgs") or []))
        self.assertTrue(plan.get("orders"), "放行后应生成转换/买卖指令")

    def test_ma_break_cuts_target_with_floor(self):
        ctx = _base_ctx(ma_align=0.0, vol_rank=0.7,
                        ma_break={"cut": 0.4, "floor": 0.4,
                                  "note": "收盘跌破 MA60、MA120 → 减仓 40%"})
        plan = strategy.plan(self.cfg, ctx)
        joined = " ".join(plan.get("msgs") or [])
        self.assertIn("【反应型风控】", joined)
        self.assertTrue(any("底仓" in m for m in plan.get("msgs") or []))

    def test_hard_cap_never_full(self):
        ctx = _base_ctx(score=100, ma_align=1.0, vol_rank=0.1, micro_pct=0.95,
                        ma_break={"cut": 0.0})
        plan = strategy.plan(self.cfg, ctx)
        cap = float((self.cfg["strategy"]).get("equity_hard_cap", 0.80))
        self.assertLessEqual(plan["w_target"], cap + 1e-6)

    def test_defensive_caps_at_quarter(self):
        # 评分低（<rearm_bull_score）且未触发单只止损/止盈 → 防御冷却保持有效 → ≤25%
        neutral = {"value": 700.0, "cost": 690.0, "pnl_pct": 0.02,
                   "sellable_mv": 700.0, "locked_mv": 0.0}
        ctx = _base_ctx(score=5, defensive=True, ma_align=0.0, vol_rank=0.5,
                        ma_break={"cut": 0.0},
                        fund_pnl={"270049": neutral},
                        locked_eq_by_code={})
        plan = strategy.plan(self.cfg, ctx)
        self.assertLessEqual(plan["w_target"], 0.25 + 1e-6)

    def test_plan_runs_with_minimal_ctx(self):
        """plan() 只用 ctx 里存在的键（缺失也不该抛异常）。"""
        plan = strategy.plan(self.cfg, _base_ctx())
        self.assertIn("orders", plan)
        self.assertIn("w_target", plan)


class ConfigConsistencyTest(unittest.TestCase):
    @needs_config
    def test_critical_keys_present(self):
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        st = cfg["strategy"]
        for k in ("news_focus_events", "news_dict_mode", "news_net_scale",
                  "micro_weight", "micro_hist_days", "micro_context_days",
                  "micro_cache_days", "position_band_max", "equity_hard_cap",
                  "risk_overlay_enable", "dynamic_weights", "rotate_winner_gain"):
            self.assertIn(k, st, "config.strategy 缺少 {}".format(k))
        for k in ("fund_stop_loss_pct", "peak_trailing_pct", "rearm_days",
                  "ma_break_step", "ma_break_max", "ma_break_floor"):
            self.assertIn(k, st["risk"], "config.strategy.risk 缺少 {}".format(k))

    def test_removed_keys_are_not_referenced(self):
        """已删除的旧参数（departure_*）不应再被代码引用。"""
        for name in ("departure_start", "departure_span", "departure_max"):
            for f in (ROOT / "fundai").rglob("*.py"):
                txt = f.read_text(encoding="utf-8")
                self.assertNotIn('"{}"'.format(name), txt,
                                 "{} 仍引用已删除参数 {}".format(f.name, name))


if __name__ == "__main__":
    unittest.main()
