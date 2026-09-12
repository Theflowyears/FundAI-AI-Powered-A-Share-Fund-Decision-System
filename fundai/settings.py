# -*- coding: utf-8 -*-
"""配置加载与校验。"""
import json
from pathlib import Path

from . import starter, util

CONFIG_PATH = util.PROJECT / "config.json"

DEFAULT_CFG = {
    "account": {
        "name": "FundAI 量化基金决策",
        # 起始资金：**可自定义**，默认 1000；改它只需要动这一个数
        "initial_cash": 1000.0,
        # 目标线 = 起始资金 × target_multiple（默认 1.3 → +30%）
        # 这是**唯一**的目标来源；target_value 只是它的镜像（写进配置便于人读）
        "target_multiple": 1.3,
        "target_value": 1300.0,
        "start_date": None,
        "end_date": None,
        "exec_mode": "manual"
    },
    "market": {
        "index": {"name": "沪深300", "zhitu_code": "000300.SH",
                  "eastmoney_secid": "1.000300"},
        "indices": [
            {"name": "上证指数", "secid": "1.000001"},
            {"name": "深证成指", "secid": "0.399001"},
            {"name": "创业板指", "secid": "0.399006"},
            {"name": "沪深300", "secid": "1.000300", "benchmark": True},
            {"name": "科创50", "secid": "1.000688"},
            {"name": "中证500", "secid": "1.000905"}
        ]
    },
    "data": {"provider": "zhitu", "zhitu_token": "",
             "zhitu_daily_limit": 200,
             "note": "智兔数服：每日200次 / 频率300次每分钟"},
    # 起始基金池：**不能为空**，否则全新环境（没有 config.json）连服务都起不来
    # （settings.validate 要求至少一只权益 + 一只债券）。取自 fundai/starter.py，
    # 用户自己的 config.json 永远优先（见 tools/gen_starter.py 的说明）。
    "pool": list(starter.POOL),
    "screening": {
        "enabled": True, "refresh_days": 7, "min_total": 15,
        "top_equity": 14, "min_history_days": 120,
        "note": "动态重建备选池：保证>=min_total只，保留当前持仓与债基",
        "universe": list(starter.UNIVERSE)
    },
    "strategy": {
        "eq_base": 0.6, "eq_slope": 0.005, "eq_floor": 0.1,
        "eq_cap": 0.95, "regime_step": 0.1, "bond_buy_floor": 0.12,
        "min_hold_days": 7, "max_bond_weight": 0.35,
        "mom_window": 20, "rotate_gap": 0.05, "min_order_yuan": 10.0,
        "news_weight": 0.35, "news_amp": 8,
        "news_score_cap": 25,
        # 回放是否接入十年消息库的当日消息分（true 时消息面/情绪子系统也进回测）
        "replay_news": True,
        "micro_enable": True, "micro_weight": 0.15, "micro_score_cap": 30,
        "score_ema_enabled": False, "score_ema_alpha": 0.4,
        "top_n": 3, "theme_max": 1, "momentum_weight": 0.8,
        "min_momentum": 0.0, "relative_momentum": False,
        "risk_adjusted": False, "confidence_shrink": 1.0,
        "use_fund_convert": True,
        "factor_weights": {"mom": 0.65, "mom5": 0.15, "vol": 0.20, "bias": 0.0,
                           "heat": 0.0, "theme_heat": 0.10},
        "risk": {
            "fund_stop_loss_pct": -0.08,
            "fund_take_profit_pct": 0.10,
            "fund_take_profit_full_pct": 0.25,
            "portfolio_stop_pct": -0.15,
            "peak_trailing_pct": 0.10,
            "reentry_score": 25,
            "rearm_days": 7,
            "allow_rearm_trade": True,
            "rearm_bull_score": 50,
            "allow_early_exit_fee": False
        }
    },
    "fees": {"buy_rate_default": 0.0, "sell_lt7d_default": 0.015,
             "sell_ge7d_default": 0.0},
    "llm": {"enabled": False, "provider": "deepseek", "api_key": "",
            "base_url": "https://api.deepseek.com", "model": "deepseek-chat",
            "temperature": 0.4},
    "server": {"host": "127.0.0.1", "port": 8787},
}

_cache = {"path": None, "mtime": 0, "cfg": None}


def _read(path):
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(DEFAULT_CFG, ensure_ascii=False, indent=2), "utf-8")
        return json.loads(json.dumps(DEFAULT_CFG))
    return json.loads(p.read_text("utf-8"))


def load_config(path=None):
    path = str(path or CONFIG_PATH)
    try:
        mt = Path(path).stat().st_mtime
    except OSError:
        mt = -1
    c = _cache
    if c["cfg"] is not None and c["path"] == path and c["mtime"] == mt:
        return c["cfg"]
    cfg = _read(path)
    validate(cfg)
    _deep_default(cfg, json.loads(json.dumps(DEFAULT_CFG)))
    c.update(path=path, mtime=mt, cfg=cfg)
    return cfg


def reload_config(path=None):
    """强制丢弃 mtime 缓存并重新读盘（供「参数开关面板」写入后立即生效）。

    为什么要单独一个函数：`load_config()` 按文件 mtime 缓存，面板保存与读取若发生在
    同一秒内（Windows 的 mtime 精度足够粗），缓存命中会**看不到刚写入的值**，
    表现成"点了开关但没生效"。这里先清缓存再读，保证写入后立刻生效。
    """
    _cache.update(path=None, mtime=None, cfg=None)
    return load_config(path)


def _deep_default(cfg, dft):
    for k, v in dft.items():
        if isinstance(v, dict):
            sub = cfg.setdefault(k, {})
            _deep_default(sub, v)
        else:
            cfg.setdefault(k, v)


def validate(cfg):
    errs = []
    if not isinstance(cfg, dict):
        raise ValueError("config.json 格式错误：顶层应为对象")
    acct = cfg.get("account") or {}
    if float(acct.get("initial_cash", 0)) <= 0:
        errs.append("account.initial_cash 必须 > 0")
    tm = acct.get("target_multiple")
    if tm is not None and float(tm) <= 0:
        errs.append("account.target_multiple 必须 > 0（目标 = 本金 × 倍数）")
    if float(acct.get("target_value") or 0) <= 0:
        errs.append("account.target_value 必须 > 0（可由 本金 × target_multiple 自动得出）")
    pool = cfg.get("pool") or []
    if not pool:
        errs.append("pool 为空：请至少配置一只权益基金与一只债券基金")
    codes = [f.get("code") for f in pool if f.get("code")]
    kinds = [f.get("kind") for f in pool if f.get("code")]
    if "equity" not in kinds:
        errs.append("pool 中缺少 kind=equity 的基金")
    if "bond" not in kinds:
        errs.append("pool 中缺少 kind=bond 的基金（作为防御底仓）")
    for f in pool:
        if f.get("kind") not in ("equity", "bond"):
            errs.append("pool 基金 %s 的 kind 必须为 equity 或 bond" % f.get("code"))
    if len(codes) != len(set(codes)):
        errs.append("pool 中存在重复基金代码")
    # 策略参数方向性校验（配置写反了会静默产生反直觉行为，早期报错）
    st = cfg.get("strategy") or {}
    if float(st.get("eq_floor", 0)) > float(st.get("eq_cap", 1)):
        errs.append("strategy.eq_floor({}) 不能大于 eq_cap({})".format(
            st.get("eq_floor"), st.get("eq_cap")))
    if float(st.get("eq_slope", 0)) < 0:
        errs.append("strategy.eq_slope 不能为负")
    rk = st.get("risk") or {}
    if float(rk.get("fund_take_profit_pct", 0.1)) > \
            float(rk.get("fund_take_profit_full_pct", 0.25)):
        errs.append("strategy.risk.fund_take_profit_pct 不能大于 "
                    "fund_take_profit_full_pct")
    if float(rk.get("fund_stop_loss_pct", -0.08)) > 0:
        errs.append("strategy.risk.fund_stop_loss_pct 应为负值（浮亏比例）")
    if float(st.get("max_bond_weight", 0.45)) <= 0 or \
            float(st.get("max_bond_weight", 0.45)) > 1:
        errs.append("strategy.max_bond_weight 应在 (0,1] 区间")
    if float(st.get("bond_buy_floor", 0.12)) < 0 or \
            float(st.get("bond_buy_floor", 0.12)) > \
            float(st.get("max_bond_weight", 0.45)):
        errs.append("strategy.bond_buy_floor 应介于 [0, max_bond_weight]")
    if errs:
        raise ValueError("；".join(errs))


def pool_of(cfg):
    return cfg.get("pool") or []


def account_plan(cfg=None):
    """返回 (起始资金, 目标线, 目标倍数) —— **目标线的唯一来源**。

    规则（2026-09-11 用户要求）：
      * 本金 `account.initial_cash` 可自定义，默认 1000；
      * 目标 = 本金 × `account.target_multiple`（默认 1.3，即**增长 30%**）；
      * `account.target_value` 只是镜像值（写进配置便于人读），若它与倍数不一致，
        **以倍数重新计算**为准 —— 避免"两个真值"互相打架；
      * 只有显式没有 `target_multiple` 的旧配置才回退到 `target_value`
        （老部署不会因为升级而目标漂移）。

    用法：所有需要"目标/本金"的地方都调它，不要再各自读 `target_value`。
    """
    acct = ((cfg or {}).get("account") or {}) if cfg else {}
    initial = float(acct.get("initial_cash") or DEFAULT_CFG["account"]["initial_cash"])
    mult = acct.get("target_multiple")
    if mult is None:
        tv = acct.get("target_value")
        if tv is not None and initial > 0:
            return initial, float(tv), round(float(tv) / initial, 6)
        mult = DEFAULT_CFG["account"]["target_multiple"]
    mult = float(mult)
    if mult <= 0:
        mult = float(DEFAULT_CFG["account"]["target_multiple"])
    return initial, round(initial * mult, 2), mult


def target_value_of(cfg=None):
    """目标金额（= 本金 × 倍数）。"""
    return account_plan(cfg)[1]


def fund_item(cfg, code):
    for f in pool_of(cfg):
        if f.get("code") == code:
            return f
    return None


def kind_of(cfg, code):
    it = fund_item(cfg, code)
    return it.get("kind") if it else "equity"


def equity_candidates(cfg):
    """进攻基金候选（AI 按动量选基的对象）。"""
    return [f for f in pool_of(cfg) if f.get("kind") == "equity"]


def bond_item(cfg):
    """防御债基：优先 kind=bond 的 primary，否则第一个债基。"""
    for f in pool_of(cfg):
        if f.get("kind") == "bond":
            return f
    return None


def primary_index(cfg):
    return cfg.get("market", {}).get("index", {})


def screening_of(cfg):
    return cfg.get("screening", {}) or {}


def universe_of(cfg):
    return (cfg.get("screening", {}) or {}).get("universe") or []


def universe_item(cfg, code):
    """在动态候选池(universe)里按代码取基金元信息（名称解析用）。

    用途：持仓基金被动态池淘汰后，仍要能显示名称与类型（否则界面只剩一串代码）。
    """
    for it in universe_of(cfg):
        if it.get("code") == code:
            return it
    return None


def public_cfg(cfg):
    import copy
    out = copy.deepcopy(cfg)
    llm = out.setdefault("llm", {})
    llm["api_key"] = ("***" if llm.get("api_key") else "")
    d = out.setdefault("data", {})
    d["zhitu_token"] = ("***" if d.get("zhitu_token") else "")
    return out
