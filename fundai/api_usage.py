# -*- coding: utf-8 -*-
"""数据源调用计数与降级状态：**持久化**（进程重启不清零）+ 分源 + 可视。

为什么需要它
------------
旧实现把调用次数放在 ``Market`` 的**纯内存** dict 里（``_zt_calls`` / ``_ak_calls``），
于是有三个真实痛点：
1. 每次改代码/重启服务，界面上的「今日已用 N 次」直接归零 —— 无法判断配额消耗；
2. 只有智兔「成功返回后」才 +1，失败、akshare、东财/新浪/腾讯通道**完全没计数**，
   显示的用量显著低于真实请求数；
3. 通道故障切换只往内存 ``self.warnings`` 追一句中文提示，重启即消失，界面无法
   区分「正常」与「已降级」。

本模块把所有「一次网络尝试」落到 ``data/api_usage.json``（``util.data_file``），
按**日期**分桶，并提供 ``note`` / ``note_failover`` / ``note_active`` / ``snapshot``。

数据结构（version=1）
--------------------
::

    {
      "version": 1,
      "updated": "2026-09-11 18:20:01",
      "days": {
        "2026-09-11": {
          "sources": {
            "zhitu":     {"calls": 12, "ok": 12, "fail": 0, "limit": 200,
                          "last_ok": "...", "last_fail": null, "last_error": null},
            "akshare":   {"calls": 9, "ok": 9, "fail": 0, "limit": null, ...},
            "eastmoney": {"calls": 3, "ok": 2, "fail": 1, "limit": null, ...},
            "sina": {}, "tencent": {}, "ths": {}
          }
        }
      },
      "failover": [{"ts": "...", "purpose": "nav", "from": "akshare",
                    "to": "eastmoney", "reason": "AKShare 瞬时失败(1/4)"}],
      "active": {"nav": "eastmoney", "index_kline": "zhitu", "quote": "eastmoney"}
    }

保证
----
* 线程安全：``threading.Lock`` 保护所有读改写；
* 原子写入：临时文件 + ``os.replace``（``util.write_json_atomic``）；
* 容错：JSON 损坏 / 字段缺失 / 类型不对 → 丢弃重建，**绝不抛异常**；
* 所有对外函数都内部 ``try/except``，失败静默跳过（计数不能打断主流程）；
* 只保留最近 ``KEEP_DAYS``(30) 天；``failover`` 只留最近 ``MAX_FAILOVER``(100) 条。

对外 API
--------
``note(source, ok=True, error=None, purpose=None, limit=None, cfg=None)``
    记一次调用尝试（calls+1，ok/fail 分别 +1，失败存截断后的 error；purpose 不为空
    时同时把该 purpose 的「当前供货源」记为 source）。
``note_failover(purpose, frm, to, reason)``
    记一次通道降级（from → to）。
``note_active(purpose, source)``
    记某用途当前实际供货的数据源。
``snapshot(cfg=None, provider=None)``
    给 Web 用的完整视图（见下方字段说明）。
``reset(path=None)``
    清空存储（测试用）。

``snapshot()`` 返回字段
----------------------
``date`` / ``today`` / ``yesterday`` / ``days`` / ``failover`` / ``active`` /
``degraded`` / ``degraded_reason`` / ``provider`` / ``store`` / ``updated`` / ``error``。
其中 ``today`` = ``{"calls", "limit", "remaining", "exhausted", "sources", "fail", "ok"}``；
``days`` 为最近 14 天摘要 ``[{"date", "calls", "fail"}]``（倒序，**首位是今天**，即使
今天还没调用也会出现）；``yesterday`` 同结构（含 ``sources`` 明细）。
``remaining`` 在 ``limit`` 为 None（无上限）时也是 None。
"""
import json
import os
import re
import threading
from pathlib import Path

from . import util

# 计数文件版本号（结构变更时递增，旧结构会被容错丢弃重建）
VERSION = 1

# 计数文件路径覆盖（测试用；不设则走 util.data_file("api_usage.json")）
ENV_PATH = "FUNDAI_USAGE_FILE"

# 只保留最近 30 天（更早的日桶直接删掉，避免文件无限膨胀）
KEEP_DAYS = 30
# failover 列表上限（只留最近 100 条）
MAX_FAILOVER = 100
# 失败原因截断长度
ERROR_MAX = 200
# snapshot 里 days 摘要的天数
DAYS_RECENT = 14

# 已知数据源（顺序即界面展示顺序；未知来源也会被记录，不丢数据）
SOURCES = ("zhitu", "akshare", "eastmoney", "sina", "tencent", "ths")

# 各用途的通道优先级（用于判定「是否已降级」：不在首位即降级）
# nav：akshare → zhitu → eastmoney（与 Market._nav_chain 一致，akshare 不可用时缺席）
# index_kline：zhitu → eastmoney
# quote：eastmoney（名义主源）→ sina → tencent —— 与 indices_quote 的
#        “新浪优先”实现不同，这里以**东财**为主源，新浪/腾讯视为降级备胎：
#        界面需要知道“现在是谁在供货”，而不是“谁先被调用”。
CHAIN_DEFAULT = {
    "nav": ("akshare", "zhitu", "eastmoney"),
    "index_kline": ("zhitu", "eastmoney"),
    "quote": ("eastmoney", "sina", "tencent"),
}

PURPOSE_LABEL = {
    "nav": "基金净值",
    "index_kline": "指数K线",
    "quote": "指数实时行情",
    "index_bars": "风格指数K线",
    "index_profile": "基金概况/名称",
}

# 密钥/长串打码（复用 util 里的规则，避免 token 落盘）
_REDACT_RE = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)


# ---------------- 内部小工具 ----------------
def _safe_str(x, maxlen=None):
    """任意对象 → 安全字符串；失败返回空串。"""
    try:
        s = x if isinstance(x, str) else str(x)
    except Exception:
        return ""
    if maxlen and len(s) > maxlen:
        s = s[:maxlen]
    return s


def _clip(txt, n=ERROR_MAX):
    """截断错误文本并打码（token 不落盘），保留末尾（异常信息常在末尾）。"""
    s = _safe_str(txt)
    s = _REDACT_RE.sub(r"\1***", s)
    s = " ".join(s.split())
    if len(s) > n:
        s = s[: max(1, n - 3)] + "..."
    return s


# 这些“看起来是 None”的字符串必须归一成 None，否则界面/JSON 里会出现字面量 "None"
_NULLISH = ("", "none", "null", "nil", "n/a", "na")


def _clean_name(x, maxlen=24):
    """来源/purpose 名归一：去空白、小写化“None”字样 → 返回字符串或 None。"""
    s = " ".join(_safe_str(x).split())[:maxlen]
    return None if s.strip().lower() in _NULLISH else s


def _positive_int(x):
    try:
        v = int(x)
        return v if v > 0 else None
    except Exception:
        return None


def _as_list(x):
    return list(x) if isinstance(x, list) else []


def _default_file():
    """计数文件路径：优先环境变量（测试隔离），否则 data/api_usage.json。"""
    env = os.environ.get(ENV_PATH, "").strip()
    if env:
        return Path(env)
    try:
        return Path(util.data_file("api_usage.json"))
    except Exception:
        return Path(util.PROJECT) / "data" / "api_usage.json"


def _blank():
    return {"version": VERSION, "updated": None, "days": {}, "failover": [],
            "active": {}}


def _src_rec(raw):
    """把任意脏数据规范成一个源计数桶（缺字段补 0/None，类型不对就丢）。"""
    out = {"calls": 0, "ok": 0, "fail": 0, "limit": None,
           "last_ok": None, "last_fail": None, "last_error": None}
    if not isinstance(raw, dict):
        return out
    for k in ("calls", "ok", "fail"):
        try:
            v = int(raw.get(k) or 0)
        except Exception:
            v = 0
        out[k] = v if v >= 0 else 0
    out["limit"] = _positive_int(raw.get("limit"))
    for k in ("last_ok", "last_fail", "last_error"):
        v = raw.get(k)
        out[k] = (_safe_str(v, ERROR_MAX) or None) if v not in (None, "") else None
    return out


def _normalize(obj):
    """容错加载：任何结构问题都退化为空结构，绝不抛异常。"""
    out = _blank()
    if not isinstance(obj, dict):
        return out
    days_in = obj.get("days")
    days = {}
    if isinstance(days_in, dict):
        for dk, dv in days_in.items():
            ds = _safe_str(dk)[:10]
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", ds):
                continue
            srcs = {}
            raw_srcs = (dv or {}).get("sources") if isinstance(dv, dict) else None
            if isinstance(raw_srcs, dict):
                for sk, sv in raw_srcs.items():
                    name = _safe_str(sk)[:24]
                    if name:
                        srcs[name] = _src_rec(sv)
            if srcs:
                days[ds] = {"sources": srcs}
    out["days"] = days
    fo = []
    for it in _as_list(obj.get("failover")):
        if not isinstance(it, dict):
            continue
        frm, to = _clean_name(it.get("from")), _clean_name(it.get("to"))
        if not (frm or to):
            continue
        fo.append({"ts": _safe_str(it.get("ts"))[:19],
                   "purpose": _safe_str(it.get("purpose"))[:24],
                   "from": frm, "to": to,
                   "reason": _clip(it.get("reason"), 160) or None})
    out["failover"] = fo[-MAX_FAILOVER:]
    act = {}
    raw_act = obj.get("active")
    if isinstance(raw_act, dict):
        for k, v in raw_act.items():
            name = _safe_str(v)[:24]
            if name:
                act[_safe_str(k)[:24]] = name
    out["active"] = act
    out["updated"] = _safe_str(obj.get("updated"), 19) or None
    return out


def _empty_source():
    return {"calls": 0, "ok": 0, "fail": 0, "limit": None,
            "last_ok": None, "last_fail": None, "last_error": None}


# ---------------- 持久化存储 ----------------
class ApiUsage:
    """按日期分桶的数据源调用计数 + 降级记录（线程安全、原子落盘）。

    ``path=None`` 时路径在**每次读写前**动态解析（``FUNDAI_USAGE_FILE`` →
    ``data/api_usage.json``），因此测试设环境变量后不需要重建对象。
    """

    def __init__(self, path=None):
        self._lock = threading.Lock()
        self._path = Path(path) if path else None

    # -------- 路径 / 时间 --------
    def path(self):
        return self._path or _default_file()

    @staticmethod
    def _today():
        try:
            return _safe_str(util.today_str())[:10]
        except Exception:
            return ""

    @staticmethod
    def _now_iso():
        try:
            return _safe_str(util.now_iso())[:19]
        except Exception:
            return ""

    # -------- 读写 --------
    def load(self):
        """读取并容错规范化；文件不存在/损坏一律返回空结构。"""
        try:
            p = self.path()
            if not p.exists():
                return _blank()
            try:
                raw = json.loads(p.read_text("utf-8"))
            except Exception:
                return _blank()      # 坏 JSON：丢弃重建，绝不让主流程崩
            if not isinstance(raw, dict) or int(raw.get("version") or 0) > \
                    VERSION:
                return _blank()
            return _normalize(raw)
        except Exception:
            return _blank()

    def _save(self, data):
        try:
            data["version"] = VERSION
            data["updated"] = self._now_iso()
            util.write_json_atomic(self.path(), data)
            return True
        except Exception:
            return False

    def reset(self, path=None):
        """清空计数文件（测试用）。path 只影响本次调用，不改变对象路径。"""
        try:
            p = Path(path) if path else self.path()
            if p.exists():
                p.unlink()
        except Exception:
            pass
        return None

    # -------- 记录 --------
    def note(self, source, ok=True, error=None, purpose=None, limit=None,
             cfg=None):
        """记一次调用尝试。任何异常都被吞掉（计数不能影响主流程）。

        source : zhitu / akshare / eastmoney / sina / tencent / ths / ...
        ok     : 本次尝试是否成功
        error  : 失败原因（截断到 ~200 字，token 打码）
        purpose: nav / index_kline / quote；非空时同时更新 active[purpose]
        limit  : 配额上限（只有 zhitu 有意义，其余传 None）
        cfg    : 兼容保留（本层不需要，仅签名占位）——传了也不会报错
        """
        try:
            s = _safe_str(source)[:24]
            if not s:
                return None
            day = self._today()
            if not day:
                return None
            with self._lock:
                data = self.load()
                dayd = data["days"].setdefault(day, {"sources": {}})
                rec = dayd["sources"].setdefault(s, _empty_source())
                ts = self._now_iso()
                lim = _positive_int(limit)
                if lim:
                    # 上限是“当天+来源”的常量（智兔来自 data.zhitu_daily_limit）：
                    # 成功、失败都写，否则一次失败就会让界面显示成「无上限」。
                    rec["limit"] = lim
                rec["calls"] = int(rec.get("calls") or 0) + 1
                if ok:
                    rec["ok"] = int(rec.get("ok") or 0) + 1
                    rec["last_ok"] = ts
                else:
                    rec["fail"] = int(rec.get("fail") or 0) + 1
                    rec["last_fail"] = ts
                    rec["last_error"] = _clip(error) or "未提供错误信息"
                # 只有"确实带了用途"的调用才更新 active。
                # 不能用 _safe_str(None) —— 它会得到字符串 "None"，
                # 于是界面出现 `active: {"None": "zhitu"}` 这种脏键（2026-09-11 修为 _clean_name）。
                pur = _clean_name(purpose)
                if pur:
                    data["active"][pur] = s
                self._prune(data, day)
                self._save(data)
        except Exception:
            pass
        return None

    def note_failover(self, purpose, frm, to, reason=None):
        """记一次通道降级（frm 失败 → 换用 to）；同时更新 active[purpose]。

        ``to`` 为空/None（链上已无更下一家）会被规范成 None，不会写成字符串 "None"。
        """
        try:
            pur = _clean_name(purpose)
            a, b = _clean_name(frm), _clean_name(to)
            if not pur or not (a or b):
                return None
            ts = self._now_iso()
            with self._lock:
                data = self.load()
                data["failover"] = (_as_list(data.get("failover")) + [{
                    "ts": ts, "purpose": pur, "from": a, "to": b,
                    "reason": _clip(reason, 160) or None,
                }])[-MAX_FAILOVER:]
                if b:
                    data["active"][pur] = b
                self._prune(data, self._today())
                self._save(data)
        except Exception:
            pass
        return None

    def note_active(self, purpose, source):
        """记某用途当前实际供货的数据源（成功返回后调用）。"""
        try:
            pur = _clean_name(purpose)
            src = _clean_name(source)
            if not pur or not src:
                return None
            with self._lock:
                data = self.load()
                if data["active"].get(pur) != src:
                    data["active"][pur] = src
                    self._prune(data, self._today())
                    self._save(data)
        except Exception:
            pass
        return None

    # -------- 清理 --------
    def _prune(self, data, today=None):
        """只保留最近 KEEP_DAYS 天；failover 截到 MAX_FAILOVER 条。"""
        try:
            days = data.get("days") or {}
            if len(days) > KEEP_DAYS:
                for k in sorted(days)[:-KEEP_DAYS]:
                    days.pop(k, None)
            fo = _as_list(data.get("failover"))
            if len(fo) > MAX_FAILOVER:
                data["failover"] = fo[-MAX_FAILOVER:]
        except Exception:
            pass
        return data

    # -------- 视图 --------
    def _day_calls(self, days, ds, source=None):
        """某天某源（或全部源）的 calls/fail。"""
        recs = ((days.get(ds) or {}).get("sources") or {})
        if source:
            r = recs.get(source) or {}
            return int(r.get("calls") or 0), int(r.get("fail") or 0)
        c = f = 0
        for r in recs.values():
            c += int((r or {}).get("calls") or 0)
            f += int((r or {}).get("fail") or 0)
        return c, f

    def snapshot(self, cfg=None, provider=None, chain=None):
        """给 Web 用的完整视图（字段见模块 docstring）。永不抛异常。

        cfg      : 兼容保留（本层不读配置，仅签名占位）
        provider : 界面展示用的数据源标签（如 "AKShare(基金净值) + 智兔/东财(指数)"）
        chain    : 覆盖默认通道优先级（``{purpose: (源, ...)}``），用于「是否降级」判定
        """
        out = {
            "date": self._today(),
            "provider": provider,
            "today": {"calls": 0, "limit": None, "remaining": None,
                      "exhausted": False, "ok": 0, "fail": 0, "sources": {}},
            "yesterday": None,
            "days": [],
            "failover": [],
            "active": {},
            "degraded": False,
            "degraded_reason": None,
            "store": _safe_str(self.path()),
            "updated": None,
            "error": None,
        }
        try:
            data = self.load()
            days = data.get("days") or {}
            today = out["date"] or (sorted(days)[-1] if days else "")
            out["date"] = today
            out["updated"] = data.get("updated")
            srcs = dict(((days.get(today) or {}).get("sources") or {}))
            # 固定源始终出现（界面稳定），未知源也保留
            ordered = list(SOURCES) + [k for k in srcs if k not in SOURCES]
            view = {}
            tot_calls = tot_ok = tot_fail = 0
            limit = None
            for name in ordered:
                r = _src_rec(srcs.get(name)) if name in srcs else _empty_source()
                view[name] = r
                tot_calls += r["calls"]
                tot_ok += r["ok"]
                tot_fail += r["fail"]
                if name == "zhitu" and r["limit"]:
                    limit = r["limit"]
            # 界面/engine 里的“智兔配额”口径：智兔可用（有 Token）时，
            # 每源 limit 缺省回落 data.zhitu_daily_limit（默认 200）。
            if not limit and _zhitu_enabled(cfg):
                limit = _zhitu_limit(cfg)
            rem = None if not limit else max(0, int(limit) - tot_calls)
            out["today"] = {
                "calls": tot_calls, "limit": limit, "remaining": rem,
                "exhausted": bool(limit and tot_calls >= limit),
                "ok": tot_ok, "fail": tot_fail, "sources": view,
            }
            yday = _prev_date(today)
            if yday:
                c, f = self._day_calls(days, yday)
                out["yesterday"] = {"date": yday, "calls": c, "fail": f,
                                    "sources": dict(
                                        ((days.get(yday) or {}).get("sources")
                                         or {}))}
            # days 摘要必须**包含今天**（即使今天还没调用），否则界面无法显示“今日 0 次”
            out["days"] = [{"date": ds, "calls": self._day_calls(days, ds)[0],
                            "fail": self._day_calls(days, ds)[1]}
                           for ds in _recent_dates(today, DAYS_RECENT)]
            out["failover"] = _as_list(data.get("failover"))[-20:][::-1]
            out["active"] = dict(data.get("active") or {})
            deg, why = self._degraded(out["active"], out["failover"], chain)
            out["degraded"] = deg
            out["degraded_reason"] = why
        except Exception as e:
            out["error"] = _clip(e)
        return out

    def _degraded(self, active, failover=None, chain=None):
        """是否处于降级状态 + 原因（基于「当前供货源 ≠ 首选源」）。"""
        cfg_chain = dict(CHAIN_DEFAULT)
        if isinstance(chain, dict):
            for k, v in chain.items():
                if v:
                    cfg_chain[k] = tuple(v)
        reasons = []
        for pur, src in sorted((active or {}).items()):
            seq = tuple(cfg_chain.get(pur) or ())
            if not seq or src == seq[0]:
                continue                     # 未知用途 或 正是首选源
            if src in seq:
                idx = seq.index(src)
                last = _last_failover(failover, pur, src) or {}
                reason = last.get("reason") or "上游不可用"
                frm = last.get("from")
                if frm and idx > 0 and frm == seq[idx - 1]:
                    # 上游正是链里的前一家：直说“前一家 → 当前家”
                    reasons.append("{} 已降级：{} → {}（{}）".format(
                        PURPOSE_LABEL.get(pur, pur), frm, src, reason))
                elif frm and frm != src:
                    # 例如 quote：先探过新浪才落到腾讯，链上“前一家”其实是东财。
                    # 如实描述“谁在供货 + 哪家失败过”，不编造链路。
                    reasons.append(
                        "{} 已降级：当前由 {} 供货，非首选 {}（{} 失败：{}）"
                        .format(PURPOSE_LABEL.get(pur, pur), src, seq[0],
                                frm, reason))
                else:
                    reasons.append("{} 已降级：当前由 {} 供货，非首选 {}".format(
                        PURPOSE_LABEL.get(pur, pur), src, seq[0]))
            else:
                reasons.append("{} 使用非常规通道 {}".format(
                    PURPOSE_LABEL.get(pur, pur), src))
        return (bool(reasons), "；".join(reasons) if reasons else None)

    def usage_summary(self, provider=None, daily_limit=None, cfg=None):
        """兼容旧 ``Market.usage()`` 的键：provider/calls_today/daily_limit/
        zhitu_today/akshare_today（前端 app.js 已在用，不能删）。"""
        snap = self.snapshot(cfg=cfg, provider=provider)
        today = snap.get("today") or {}
        srcs = today.get("sources") or {}
        calls = int(today.get("calls") or 0)
        lim = _positive_int(daily_limit if daily_limit is not None
                            else today.get("limit"))
        rem = None if not lim else max(0, int(lim) - calls)
        return {
            # ---- 旧字段（必须保持） ----
            "provider": provider,
            "calls_today": calls,
            "daily_limit": lim,
            "zhitu_today": int((srcs.get("zhitu") or {}).get("calls") or 0),
            "akshare_today": int((srcs.get("akshare") or {}).get("calls") or 0),
            # ---- 新增：分源明细 / 昨日 / 剩余 / 降级视图 ----
            "date": snap.get("date"),
            "ok_today": int(today.get("ok") or 0),
            "fail_today": int(today.get("fail") or 0),
            "remaining": rem,
            "exhausted": bool(today.get("exhausted")),
            "sources": srcs,
            "by_source": {k: int((v or {}).get("calls") or 0)
                          for k, v in srcs.items()},
            "sources_detail": srcs,
            "yesterday": snap.get("yesterday"),
            "days": snap.get("days"),
            "failover": snap.get("failover"),
            "active": snap.get("active"),
            "degraded": bool(snap.get("degraded")),
            "degraded_reason": snap.get("degraded_reason"),
            "store": snap.get("store"),
            "updated": snap.get("updated"),
            "snapshot": snap,
        }


# ---------------- 便捷小工具 ----------------
def _last_failover(failover, purpose, to=None):
    """最近一条匹配的 failover 记录（用于生成降级原因）。"""
    for it in reversed(_as_list(failover)):
        if not isinstance(it, dict):
            continue
        if purpose and it.get("purpose") != purpose:
            continue
        if to and it.get("to") not in (to, None):
            continue
        return it
    return None


def _prev_date(ds):
    """ds 的前一天（ds 非法时返回 None）。"""
    try:
        return util.add_days(ds, -1)
    except Exception:
        return None


def _recent_dates(today, n):
    """最近 n 天日期（倒序，首位=今天）；日期非法时退化为“已记录的桶倒序”。"""
    try:
        return [util.add_days(today, -i) for i in range(int(n))]
    except Exception:
        return []


def _zhitu_enabled(cfg):
    """配置里是否启用智兔（有 token 且 provider 在 zhitu/akshare）。"""
    try:
        d = (cfg or {}).get("data") or {}
        tok = _safe_str(d.get("zhitu_token")).strip()
        prov = (_safe_str(d.get("provider")) or "zhitu").strip().lower()
        return bool(tok) and prov in ("zhitu", "akshare")
    except Exception:
        return False


def _zhitu_limit(cfg):
    try:
        d = (cfg or {}).get("data") or {}
        return _positive_int(d.get("zhitu_daily_limit", 200)) or 200
    except Exception:
        return 200


def _provider_of(cfg):
    """根据配置推断数据源标签（与 Market.provider_label 同口径；web 层可覆盖）。"""
    try:
        d = (cfg or {}).get("data") or {}
        tok = _safe_str(d.get("zhitu_token")).strip()
        prov = (_safe_str(d.get("provider")) or "zhitu").strip().lower()
        if prov == "akshare":
            return "AKShare(基金净值) + 智兔/东财(指数)" if tok else "AKShare + 东财"
        if prov == "zhitu" and tok:
            return "智兔数服 + 东财兜底"
        return "东财/天天基金"
    except Exception:
        return None


def source_chain(cfg=None):
    """按配置给出通道优先级（供 Market 生成降级判定用 chain）。"""
    chain = dict(CHAIN_DEFAULT)
    if not _zhitu_enabled(cfg):
        chain["nav"] = tuple(s for s in chain["nav"] if s != "zhitu")
        chain["index_kline"] = ("eastmoney",)
    return chain


# ---------------- 模块级默认实例（进程内单例） ----------------
_STORE = ApiUsage()


def store():
    """返回进程内默认存储对象。"""
    return _STORE


def note(source, ok=True, error=None, purpose=None, limit=None, cfg=None):
    """模块级快捷方式，见 :meth:`ApiUsage.note`。"""
    return _STORE.note(source, ok=ok, error=error, purpose=purpose,
                       limit=limit, cfg=cfg)


def note_failover(purpose, frm, to, reason=None):
    """模块级快捷方式，见 :meth:`ApiUsage.note_failover`。"""
    return _STORE.note_failover(purpose, frm, to, reason)


def note_active(purpose, source):
    """模块级快捷方式，见 :meth:`ApiUsage.note_active`。"""
    return _STORE.note_active(purpose, source)


def snapshot(cfg=None, provider=None, chain=None):
    """模块级快捷方式，见 :meth:`ApiUsage.snapshot`。

    ``provider`` 省略时按 ``cfg`` 推断（等价于 Market.provider_label）。
    """
    if provider is None:
        provider = _provider_of(cfg)
    return _STORE.snapshot(cfg=cfg, provider=provider, chain=chain)


def usage_summary(cfg=None, provider=None, daily_limit=None):
    """模块级快捷方式，见 :meth:`ApiUsage.usage_summary`。"""
    if provider is None:
        provider = _provider_of(cfg)
    return _STORE.usage_summary(provider=provider, daily_limit=daily_limit,
                                cfg=cfg)


def reset(path=None):
    """模块级清空（测试用）。"""
    return _STORE.reset(path)
