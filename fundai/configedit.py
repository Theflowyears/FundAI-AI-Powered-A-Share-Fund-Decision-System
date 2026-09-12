# -*- coding: utf-8 -*-
"""配置写入端：把「参数开关面板」的改动**校验后安全落盘**到 config.json。

设计要点
--------
1. **白名单**：只接受 `param_docs.PARAMS` 里登记过的键。键不存在、拼错、或是
   结构化字段（pool / market.indices / screening.universe 等 kind=="map"/复杂 list）
   一律拒绝，并给出可读原因——避免面板把人写坏配置（settings.validate 只能查
   语义，查不出"键名拼错被静默忽略"这类问题）。
2. **类型与范围**：按 metadata 的 kind/min/max/step/options 做强校验，
   布尔只认 true/false（含 0/1、"true"/"false" 字符串），数值拒绝 NaN/Inf。
3. **跨字段一致性**：先写入一份候选配置，再跑 `settings.validate()`；
   例如 eq_floor > eq_cap、止盈线写反、pool 缺 bond，都会在这里被拦下并原样说明。
4. **原子写 + 备份**：写前把原文件备份到 `data/config_backups/config_<时间>.json`
   （保留最近 KEEP_BACKUPS 份），写用 `util.write_json_atomic`（tmp+os.replace），
   绝不留半个文件。
5. **敏感键守卫**：`data.zhitu_token` / `llm.api_key` 必须显式带
   `confirm_sensitive=True` 才允许改（面板会给二次确认），并且备份里会保留旧值，
   误改可一键回滚。
6. **需要重启的键**：`server.port` / `server.host` 改动后面板要提示"重启服务生效"。

对外 API
--------
    apply_changes(changes=None, resets=(), confirm_sensitive=False, path=None)
        -> {"ok": bool, "applied": {key: {old, new}}, "errors": [...],
            "restart_required": [...], "backup": str|None, "warnings": [...]}
    rollback(backup_name=None)   # 用最近（或指定）备份覆盖回 config.json
    list_backups()               # [(name, size, mtime_str)]
"""
import json
import math
import shutil
from pathlib import Path

from . import param_docs, settings, util

KEEP_BACKUPS = 20
SENSITIVE = ("data.zhitu_token", "llm.api_key")
RESTART_KEYS = ("server.port", "server.host", "web.port")


def backup_dir():
    d = util.data_file("config_backups")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "on", "是", "开"):
        return True
    if s in ("false", "0", "no", "off", "否", "关"):
        return False
    raise ValueError("无法当作布尔值：{!r}".format(v))


def _num(v, is_int, meta):
    if isinstance(v, bool):
        raise ValueError("布尔值不能当数字")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError("不是数字：{!r}".format(v))
    if math.isnan(f) or math.isinf(f):
        raise ValueError("不是有效数字：{!r}".format(v))
    lo, hi = meta.get("min"), meta.get("max")
    if lo is not None and f < float(lo) - 1e-12:
        raise ValueError("低于允许下限 {}（请求 {}）".format(lo, f))
    if hi is not None and f > float(hi) + 1e-12:
        raise ValueError("高于允许上限 {}（请求 {}）".format(hi, f))
    return int(round(f)) if is_int else f


def validate_value(meta, value):
    """按 metadata 校验并**规范化**一个值；不合法直接抛 ValueError（中文原因）。"""
    kind = meta.get("kind")
    if kind == "bool":
        return _truthy(value)
    if kind == "int":
        return _num(value, True, meta)
    if kind == "number":
        return _num(value, False, meta)
    if kind == "enum":
        opts = [o["value"] for o in (meta.get("options") or [])]
        v = str(value)
        if opts and v not in opts:
            raise ValueError("取值必须是 {} 之一（请求 {}）".format(
                "/".join(opts), v))
        return v
    if kind == "list":
        if not isinstance(value, (list, tuple)):
            raise ValueError("需要列表（请求 {!r}）".format(value))
        opts = [o["value"] for o in (meta.get("options") or [])]
        out = []
        for x in value:
            v = str(x)
            if opts and v not in opts:
                raise ValueError("列表元素 {} 不在允许集合 {}".format(
                    v, "/".join(opts[:8]) + ("…" if len(opts) > 8 else "")))
            if v not in out:
                out.append(v)
        return out
    if kind == "text":
        s = str(value)
        if len(s) > 2000:
            raise ValueError("文本过长（>2000 字符）")
        return s
    raise ValueError("结构化参数（kind={}）不支持在面板里改，请直接编辑 config.json"
                     .format(kind))


def _get(cfg, dotted):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


# 目标金额是**派生值**：改了本金或倍数就必须自动跟着重算，
# 否则"面板上改了本金，目标却还停在旧值"是必然会发生的事故。
DERIVED_FROM_PLAN = {"account.target_value"}


def _sync_derived(candidate):
    """把派生键（当前只有 account.target_value）同步成 本金 × 倍数。"""
    acct = candidate.setdefault("account", {})
    try:
        initial = float(acct.get("initial_cash") or 0)
        mult = acct.get("target_multiple")
        if initial > 0 and mult is not None:
            acct["target_value"] = round(initial * float(mult), 2)
    except (TypeError, ValueError):
        pass
    return candidate


def _set(cfg, dotted, value):
    parts = dotted.split(".")
    cur = cfg
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def list_backups():
    import datetime as _dt
    out = []
    for p in sorted(backup_dir().glob("config_*.json")):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size,
                    "mtime": _dt.datetime.fromtimestamp(
                        st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return sorted(out, key=lambda x: x["name"], reverse=True)


def _make_backup(path):
    import datetime as _dt
    src = Path(path)
    if not src.exists():
        return None
    dst = backup_dir() / "config_{}.json".format(
        _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3])
    try:
        shutil.copy2(str(src), str(dst))
    except Exception:
        return None
    olds = sorted(backup_dir().glob("config_*.json"))
    for old in olds[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except Exception:
            pass
    return str(dst)


def apply_changes(changes=None, resets=(), confirm_sensitive=False, path=None):
    """校验并写入配置改动。见模块 docstring 的返回结构。"""
    path = str(path or settings.CONFIG_PATH)
    by_key = param_docs.by_key()
    applied, errors, warnings, restart = {}, [], [], []
    changes = dict(changes or {})
    if not changes and not resets:
        return {"ok": False, "errors": ["没有需要写入的改动"], "applied": {},
                "restart_required": [], "warnings": [], "backup": None}
    # 1) 逐项校验（先全部校验，避免"改一半"）
    planned = []
    for key, raw in changes.items():
        meta = by_key.get(key)
        if not meta:
            errors.append("{}：不是面板可调参数（白名单外，未写入）".format(key))
            continue
        if meta.get("dead"):
            warnings.append("{}：该键在代码中没有读取点（dead），改了不会生效"
                            .format(key))
        if key in SENSITIVE and not confirm_sensitive:
            errors.append("{}：敏感键需要显式确认（confirm_sensitive=true）"
                          .format(key))
            continue
        try:
            val = validate_value(meta, raw)
        except ValueError as e:
            errors.append("{}：{}".format(key, e))
            continue
        planned.append((key, val))
    for key in (resets or []):
        meta = by_key.get(key)
        if not meta:
            errors.append("{}：不是面板可调参数，无法重置".format(key))
            continue
        dft = _get(settings.DEFAULT_CFG, key)
        if dft is None:
            errors.append("{}：代码默认值里没有这个键，无法重置".format(key))
            continue
        try:
            val = validate_value(meta, dft)
        except ValueError as e:
            errors.append("{}：默认值 {} 未通过校验（{}）".format(key, dft, e))
            continue
        planned.append((key, val))
    if errors:
        return {"ok": False, "errors": errors, "applied": {},
                "restart_required": [], "warnings": warnings, "backup": None}
    # 2) 组装候选配置并做跨字段校验
    try:
        raw_cfg = json.loads(Path(path).read_text("utf-8"))
    except Exception as e:
        return {"ok": False, "errors": ["读取 config.json 失败：{}".format(e)],
                "applied": {}, "restart_required": [], "warnings": warnings,
                "backup": None}
    candidate = json.loads(json.dumps(raw_cfg))
    for key, val in planned:
        _set(candidate, key, val)
    _sync_derived(candidate)          # 本金/倍数一变，目标金额立刻重算
    try:
        settings.validate(json.loads(json.dumps(candidate)))
    except ValueError as e:
        return {"ok": False, "errors": ["配置一致性校验未通过：{}".format(e)],
                "applied": {}, "restart_required": [], "warnings": warnings,
                "backup": None}
    # 3) 备份 + 原子写
    backup = _make_backup(path)
    try:
        util.write_json_atomic(path, candidate)
    except Exception as e:
        return {"ok": False, "errors": ["写入失败：{}".format(e)], "applied": {},
                "restart_required": [], "warnings": warnings,
                "backup": backup}
    for key, val in planned:
        applied[key] = {"old": _get(raw_cfg, key), "new": val}
        if key in RESTART_KEYS:
            restart.append(key)
    # 4) 立刻生效（丢弃 mtime 缓存）
    try:
        settings.reload_config(path)
    except Exception as e:
        warnings.append("已写入，但重新加载配置时报错：{}".format(e))
    return {"ok": True, "applied": applied, "errors": [],
            "restart_required": restart, "warnings": warnings,
            "backup": backup}


def rollback(backup_name=None, path=None):
    """用最近（或指定）的备份覆盖回 config.json。"""
    path = str(path or settings.CONFIG_PATH)
    files = sorted(backup_dir().glob("config_*.json"))
    if not files:
        return {"ok": False, "errors": ["没有可用备份"]}
    src = backup_dir() / backup_name if backup_name else files[-1]
    if not src.exists():
        return {"ok": False, "errors": ["备份不存在：{}".format(src.name)]}
    try:
        data = json.loads(src.read_text("utf-8"))
        settings.validate(json.loads(json.dumps(data)))
    except Exception as e:
        return {"ok": False, "errors": ["备份内容不可用：{}".format(e)]}
    _make_backup(path)
    util.write_json_atomic(path, data)
    settings.reload_config(path)
    return {"ok": True, "restored_from": src.name}


if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    print("可调参数：", len(param_docs.PARAMS), "| 开关：", len(param_docs.TOGGLES))
    print("敏感键：", SENSITIVE, "| 需重启：", RESTART_KEYS)
    print("备份目录：", backup_dir())
    print("现有备份：", [b["name"] for b in list_backups()][:5])
