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
import time
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
# quote：sina → tencent → eastmoney —— **必须与实际实现（Market.indices_quote 的
#        "新浪→腾讯→东财" 顺序）一致**。2026-09-15 修复：这张表原先把 eastmoney 写成
#        首选，于是新浪每次正常供货都被判成"已降级"，界面那条降级提示永远不消失。
#        判定表的语义是"首选可用即算正常"：表与实现不一致 = 永久误报。
CHAIN_DEFAULT = {
    "nav": ("akshare", "zhitu", "eastmoney"),
    "index_kline": ("zhitu", "eastmoney"),
    "quote": ("sina", "tencent", "eastmoney"),
}

# 「最近仍在失败」的时间窗（秒）：界面顶部告警用它判定是否**当下**有问题。
# 为什么需要它（2026-09-15 用户反馈"降级提示就没消失过"）：旧逻辑是
# `degraded 或 今日失败>0` 就弹告警 —— 当天只要有一次网络抖动（或早上一次 10060 超时），
# 这条提示就挂到次日 0 点，哪怕后面几十次调用全部成功、供货通道早已恢复。
# 现在按"最近 RECENT_FAIL_WINDOW 秒内是否有失败/切换"判定，恢复后自动消失。
RECENT_FAIL_WINDOW = 3600
# 某源"最近一次失败"晚于"最近一次成功"→ 视为该源当下有问题（持久字段，跨重启有效）
SOURCE_STALE_OK = True
# 「当下可疑」的失败新鲜度与次数阈值（2026-09-16 二次修复，用户："这个就没成功过"）：
# 旧实现只判"最后一次是失败"，这个条件对**失败后再没被调用**的源永真 —— 实测
# eastmoney 当天只被调用 1 次即失败，之后净值走缓存不再调它，告警便整日挂着。
# 现在要求"失败足够新 + 当日失败次数达到阈值"，并配一条真实探活（见 probe_source）
# 让安静的源能自己"证明已恢复"，从根上消除"永真"。
STALE_FAIL_TTL = 1800      # 超过 30 分钟的旧失败不再计入告警（视为已自愈/已不被使用）
# 判"这个源有问题"需要**连续失败**几次。为什么不用"当日失败总数"：
# 实测东财在短时间内重复请求会 `Remote end closed connection`（单次抖动/限流），
# 当日累计 2 次失败就把整条链路标成"降级"太敏感；而"连续失败次数"能区分
# "抖一下就好了"和"真的挂了"——每次成功都会把计数清零。
STALE_MIN_FAILS = 3
# 通道切换（failover）是**瞬时事件**：切换发生后上游通常几十秒就恢复，
# 顶替通道于是长期驻留。实测 11:35 的切换让告警挂了近 1 小时（用户："这个就没成功过"）。
# 因此告警只看最近 15 分钟内的切换；更早的切换仍保留在明细列表里供排查。
FAILOVER_FRESH_WINDOW = 900

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


def _now_ts():
    """当前时间戳（秒）。"""
    try:
        return time.time()
    except Exception:
        return 0.0


def _parse_ts(txt):
    """'2026-09-15 13:17:51' → 时间戳（秒）；解析不了返回 None。"""
    s = _safe_str(txt).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(s[:19], fmt))
        except Exception:
            continue
    return None


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
           "last_ok": None, "last_fail": None, "last_error": None,
           # 连续失败次数：每次成功清零。用于区分"抖动"与"真挂了"
           # （东财短时间内重复请求会 Remote end closed，单次失败不该定性为故障）。
           "fail_streak": 0}
    if not isinstance(raw, dict):
        return out
    for k in ("calls", "ok", "fail", "fail_streak"):
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
            # `_clean_name` 把 "None"/""/"null" 之类的键归一成 None 后丢弃 ——
            # 历史文件里可能残留 `active: {"None": "zhitu"}`（旧版本写进去的），
            # 这里在**读取时**一并清掉，界面不会再出现无用途的脏键。
            pur = _clean_name(k)
            name = _clean_name(v)
            if pur and name:
                act[pur] = name
    out["active"] = act
    out["updated"] = _safe_str(obj.get("updated"), 19) or None
    return out


def _empty_source():
    return {"calls": 0, "ok": 0, "fail": 0, "limit": None,
            "last_ok": None, "last_fail": None, "last_error": None,
            "fail_streak": 0}


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
                    rec["fail_streak"] = 0        # 成功即清零（见 STALE_MIN_FAILS 注释）
                else:
                    rec["fail"] = int(rec.get("fail") or 0) + 1
                    rec["fail_streak"] = int(rec.get("fail_streak") or 0) + 1
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
            deg, why = self._degraded(out["active"], out["failover"], chain,
                                      sources=view)
            out["degraded"] = deg
            out["degraded_reason"] = why
            # ---- 「当下是否真有问题」：给界面顶部告警用 ----
            # 全天的 fail 计数只用于展示（数据源面板），不能拿它决定是否弹告警，
            # 否则一次瞬时抖动会把提示挂一整天（见 RECENT_FAIL_WINDOW 注释）。
            now = _parse_ts(_safe_str(data.get("updated"))) or _now_ts()
            recent_cut = now - RECENT_FAIL_WINDOW
            recent_fails = 0
            stale_sources = []
            stale_detail = {}
            for name, rec in (view or {}).items():
                lf = _parse_ts(rec.get("last_fail"))
                lo = _parse_ts(rec.get("last_ok"))
                if lf is not None and lf >= recent_cut:
                    recent_fails += 1
                # 「当下可疑」的三个**同时**成立条件（2026-09-16 修）：
                #   ① 最后一次事件是失败（lf > lo）
                #   ② 该失败在 STALE_FAIL_TTL 之内（否则早已自愈/该源只是暂时不被调用）
                #   ③ 当日累计失败 ≥ STALE_MIN_FAILS（单次抖动不足以挂告警）
                # 旧实现只判 ①，而 ① 对"失败后再也没被调用"的源**永真**：
                # 实测 eastmoney 当天只被调用 1 次（11:35 失败），之后净值走缓存不再调它，
                # 于是"降级提示"整日不消失——正是用户反馈的"这个就没成功过"。
                if lf is not None and (lo is None or lf > lo):
                    within = lf >= (now - STALE_FAIL_TTL)
                    streak = int(rec.get("fail_streak") or 0)
                    # 兼容旧数据（没有 fail_streak 字段）：回落到当日失败数判断
                    if not streak:
                        streak = int(rec.get("fail") or 0)
                    if within and streak >= STALE_MIN_FAILS:
                        stale_sources.append(name)
                        stale_detail[name] = {
                            "last_fail": rec.get("last_fail"),
                            "fail_streak": streak,
                            "fails_today": int(rec.get("fail") or 0),
                            "age_seconds": int(now - lf),
                            "error": (rec.get("last_error") or "")[:80]}
                    else:
                        # 只是"失败后再没被调用"或"连续失败不足以定性"→ 不告警，
                        # 但在明细里说明原因，避免用户看到有失败记录却不知为何没告警。
                        stale_detail.setdefault("_cleared", {})[name] = (
                            "失败已超时自愈（{} 秒前）".format(int(now - lf))
                            if not within
                            else "连续失败 {} 次，未达阈值 {}（多为限流/抖动）".format(
                                streak, STALE_MIN_FAILS))
            recent_failover = [f for f in _as_list(out.get("failover"))
                               if (_parse_ts((f or {}).get("ts")) or 0) >= recent_cut]
            # 告警只看**新鲜**切换（15 分钟），避免一次切换挂一小时
            fresh_failover = [f for f in _as_list(out.get("failover"))
                              if (_parse_ts((f or {}).get("ts")) or 0)
                              >= (now - FAILOVER_FRESH_WINDOW)]
            out["recent_fails"] = recent_fails
            out["recent_failover"] = len(recent_failover)
            out["recent_sources"] = stale_sources
            out["stale_sources"] = stale_sources
            out["stale_detail"] = stale_detail
            out["window_seconds"] = RECENT_FAIL_WINDOW
            out["stale_ttl_seconds"] = STALE_FAIL_TTL
            out["stale_min_fails"] = STALE_MIN_FAILS
            out["failover_fresh_seconds"] = FAILOVER_FRESH_WINDOW
            out["fresh_failover"] = len(fresh_failover)
            # 告警只在**确有当下问题**时亮，两个来源（互补、都要求"当前仍未恢复"）：
            #   · degraded      —— 首选源确实失败过，当前由备用源供货；
            #   · stale_sources —— 有源**连续失败 ≥STALE_MIN_FAILS 次**且最近一次失败在
            #                      STALE_FAIL_TTL 之内（够了才定性为"这个源有问题"）。
            # 明确**不**把这两项计入告警：
            #   · recent_fails  —— 窗口内失败次数：一次瞬时抖动不该弹提示（19.2 定的原则）；
            #   · fresh_failover —— 刚发生的通道切换：既然已用连续失败阈值定性"是不是真坏"，
            #     就不该让"任何一次失败"（它必然产生一条切换记录）绕过阈值直接弹告警。
            #     切换明细仍在 failover 列表里可见，供排查。
            trouble = []
            # 降级告警要求"降级是**最近**发生的"：
            # 早上 11:35 跌到备用源、之后一直稳定，就没必要把提示挂到收盘
            # ——这正是用户反复反馈的"这个就没成功过"。降级事实仍留在
            # `degraded`/`degraded_reason` 里（面板可见、可排查），只是不再触发顶部告警。
            deg_age = None
            if deg:
                fo_ts = [_parse_ts((f or {}).get("ts")) or 0
                         for f in _as_list(out.get("failover"))]
                fo_ts = [t for t in fo_ts if t]
                if fo_ts:
                    deg_age = int(now - max(fo_ts))
            out["degraded_age_seconds"] = deg_age
            if deg and (deg_age is None or deg_age <= RECENT_FAIL_WINDOW):
                trouble.append("degraded")
            if stale_sources:
                trouble.append("stale:" + ",".join(stale_sources))
            out["trouble_reason"] = "；".join(trouble) or None
            out["has_recent_trouble"] = bool(trouble)
        except Exception as e:
            out["error"] = _clip(e)
        return out

    def _degraded(self, active, failover=None, chain=None, sources=None):
        """是否处于降级状态 + 原因。

        判定原则（2026-09-16 补第二部分）：降级 = **首选源确实在今天失败过**，
        现在退到了备用源。若某个用途从没调用过首选源（当天 0 次），
        那不是"降级"，只是"走了另一条链"——旧实现会把它写成
        「已降级：当前由 eastmoney 供货，非首选 akshare」，而 akshare 当天调用数 = 0，
        属于**空判**，正是用户看到的"这个就没成功过"的第二个来源。
        """
        cfg_chain = dict(CHAIN_DEFAULT)
        if isinstance(chain, dict):
            for k, v in chain.items():
                if v:
                    cfg_chain[k] = tuple(v)
        srcs = sources or {}
        # 故障切换记录是"首选源确实失败过"的**直接证据**，而且比计数更可靠：
        # 有些源失败了却不会留下成功/失败计数（例如 akshare 根本不在当前有效链里，
        # 只在"深链"里被尝试过一次），此时 `srcs['akshare'].fail` 会是 0。
        by_purpose = {}
        for f in _as_list(failover):
            if not isinstance(f, dict):
                continue
            pur = f.get("purpose")
            if not pur:
                continue
            by_purpose.setdefault(pur, []).append(f)
        reasons = []
        for pur, src in sorted((active or {}).items()):
            # 探查性用途不参与降级判定：`probe_source()` 会把探活结果记进 active，
            # 而 "probe" 只是**健康检查**、不是任何数据链路的供货源。若不排除，
            # 每次探活都会造出一条"probe 使用非常规通道X"的假降级（实测踩到）。
            if pur == PROBE_PURPOSE:
                continue
            seq = tuple(cfg_chain.get(pur) or ())
            if seq and src == seq[0]:
                continue                     # 正是首选源供货 → 没有降级
            pref = seq[0] if seq else None
            prec = srcs.get(pref) or {}
            pref_failed = int(prec.get("fail") or 0)
            pref_calls = int(prec.get("calls") or 0)
            fos = by_purpose.get(pur) or []
            # "首选源失败过"的两种证据：计数，或故障切换记录（见上方注释）
            ev = next((f for f in fos if f.get("from") == pref), None)
            pref_evidence = pref_failed > 0 or ev is not None
            # **降级 = 首选源今天确实失败过，因此改用了别的通道**（一次也算，它是"事实描述"）。
            # 注意区分两层语义（2026-09-16 收口）：
            #   · degraded        —— 状态判定：说明"为什么现在是备用源在供货"，供排查；
            #   · has_recent_trouble —— 界面告警：另有"连续失败 ≥N 次且失败够新"等更严的闸门，
            #     所以一次抖动会如实记成"已降级"，但**不会**弹告警。
            if not pref_evidence:
                if pref is None:
                    reasons.append("{} 使用非常规通道 {}".format(
                        PURPOSE_LABEL.get(pur, pur), src))
                elif pref_calls == 0:
                    reasons.append(
                        "{} 走的是备用通道 {}（首选 {} 当天未被调用、也无失败记录，"
                        "非故障降级）".format(PURPOSE_LABEL.get(pur, pur), src, pref))
                else:
                    reasons.append(
                        "{} 当前由 {} 供货（首选 {} 当天 {}/{} 次成功，未判定为故障）"
                        .format(PURPOSE_LABEL.get(pur, pur), src, pref,
                                int(prec.get("ok") or 0), pref_calls))
                continue
            if src in seq:
                idx = seq.index(src)
                last = _last_failover(failover, pur, src) or {}
                if not last and ev:
                    last = ev
                reason = last.get("reason") or "上游不可用"
                frm = last.get("from")
                if frm and idx > 0 and frm == seq[idx - 1]:
                    # 上游正是链里的前一家：直说“前一家 → 当前家”
                    reasons.append("{} 已降级：{} → {}（{}）".format(
                        PURPOSE_LABEL.get(pur, pur), frm, src, reason))
                elif frm and frm != src:
                    reasons.append(
                        "{} 已降级：当前由 {} 供货，非首选 {}（{} 失败：{}）"
                        .format(PURPOSE_LABEL.get(pur, pur), src, seq[0],
                                frm, reason))
                else:
                    reasons.append("{} 已降级：当前由 {} 供货，非首选 {}"
                                   "（首选当天失败 {} 次）".format(
                                       PURPOSE_LABEL.get(pur, pur), src, pref,
                                       pref_failed or 1))
            else:
                reasons.append("{} 使用非常规通道 {}".format(
                    PURPOSE_LABEL.get(pur, pur), src))
        # 只有"首选源确实失败过"才算真降级；纯走备用链的只做说明，不亮告警
        hard = [r for r in reasons if "非故障降级" not in r and "未判定为故障" not in r]
        return (bool(hard), "；".join(reasons) if reasons else None)

    def usage_summary(self, provider=None, daily_limit=None, cfg=None, chain=None):
        """兼容旧 ``Market.usage()`` 的键：provider/calls_today/daily_limit/
        zhitu_today/akshare_today（前端 app.js 已在用，不能删）。

        ``chain`` 必须由调用方按**实际实现**传入（见 ``Market._usage_chain``）：
        否则降级判定会用 CHAIN_DEFAULT，与实际通道顺序不一致时会误报降级。
        """
        snap = self.snapshot(cfg=cfg, provider=provider, chain=chain)
        today = snap.get("today") or {}
        srcs = today.get("sources") or {}
        calls = int(today.get("calls") or 0)
        lim = _positive_int(daily_limit if daily_limit is not None
                            else today.get("limit"))
        # ---- 配额口径（2026-09-15 修正）----
        # **只有智兔有每日上限**（data.zhitu_daily_limit，默认 200）；
        # 东财/新浪/腾讯/akshare 都是免费公开通道，**只要不出故障就是不限量的**。
        # 旧实现把"全通道调用合计"拿去减智兔上限，于是别的通道的调用会吃掉
        # 智兔的"剩余额度"，界面看起来像"每天只有 200 次可用"——这是错的。
        # 现在：remaining/zhitu_remaining 只按智兔算；其余通道单独给出"不限量"计数。
        zhitu_calls = int((srcs.get("zhitu") or {}).get("calls") or 0)
        # 智兔上限：优先取当天记录的 limit，其次取调用方传入的 daily_limit
        zt_rec_limit = (srcs.get("zhitu") or {}).get("limit")
        zt_limit = _positive_int(zt_rec_limit if zt_rec_limit is not None else lim)
        zt_rem = None if not zt_limit else max(0, int(zt_limit) - zhitu_calls)
        other_calls = max(0, calls - zhitu_calls)
        # 分源配额表：以"今天出现过的源"为准（固定源 + 未知源都列出）
        quota = []
        for name in (list(SOURCES) + [k for k in srcs if k not in SOURCES]):
            r = srcs.get(name) or {}
            c = int(r.get("calls") or 0)
            src_limit = _positive_int(r.get("limit"))
            if name == "zhitu" and not src_limit:
                src_limit = zt_limit
            quota.append({
                "source": name,
                "calls": c,
                "limit": src_limit,                       # None = 不限量
                "remaining": (None if not src_limit
                              else max(0, int(src_limit) - c)),
                "exhausted": bool(src_limit and c >= int(src_limit)),
                "unlimited": not src_limit,
            })
        unlimited = [x["source"] for x in quota if x["unlimited"]]
        return {
            # ---- 旧字段（必须保持） ----
            "provider": provider,
            "calls_today": calls,
            "daily_limit": zt_limit,
            # 旧键含义澄清：**这是智兔的今日调用数**（不是全通道）
            "zhitu_today": zhitu_calls,
            "akshare_today": int((srcs.get("akshare") or {}).get("calls") or 0),
            # ---- 新增：分源配额与"不限量"标记 ----
            "quota": quota,
            "zhitu_limit": zt_limit,
            "zhitu_remaining": zt_rem,
            "zhitu_exhausted": bool(zt_limit and zhitu_calls >= int(zt_limit)),
            "other_calls_today": other_calls,
            "unlimited_sources": unlimited,
            "unlimited_calls_today": other_calls,
            # ---- 新增：分源明细 / 昨日 / 剩余 / 降级视图 ----
            "date": snap.get("date"),
            "ok_today": int(today.get("ok") or 0),
            "fail_today": int(today.get("fail") or 0),
            # remaining/exhausted 一律指**智兔**（唯一有配额的通道），
            # 不再用"全通道合计"去比智兔上限
            "remaining": zt_rem,
            "exhausted": bool(zt_limit and zhitu_calls >= int(zt_limit)),
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
            # 「当下是否真有问题」——界面顶部告警只认这几个（全天的 fail 计数只用于展示）
            "has_recent_trouble": bool(snap.get("has_recent_trouble")),
            "trouble_reason": snap.get("trouble_reason"),
            "fresh_failover": snap.get("fresh_failover"),
            "stale_detail": snap.get("stale_detail"),
            # 判定口径也要透出：界面要能告诉用户"为什么这次没报警"，
            # 否则用户只会看到"有失败记录却没提示"，反而更困惑。
            "stale_min_fails": snap.get("stale_min_fails"),
            "stale_ttl_seconds": snap.get("stale_ttl_seconds"),
            "failover_fresh_seconds": snap.get("failover_fresh_seconds"),
            "degraded_age_seconds": snap.get("degraded_age_seconds"),
            "recent_fails": snap.get("recent_fails"),
            "recent_failover": snap.get("recent_failover"),
            "recent_sources": snap.get("recent_sources"),
            "stale_sources": snap.get("stale_sources"),
            "window_seconds": snap.get("window_seconds"),
            "store": snap.get("store"),
            "updated": snap.get("updated"),
            "snapshot": snap,
        }


# ---------------- 真实探活：让"安静的源"能证明自己已恢复 ----------------
# 为什么必须有它（2026-09-16 用户："这个就没成功过"）：告警要能自己灭掉，
# 前提是**有东西能证明该源已恢复**。但项目里很多源是"用到才调"
# （净值走缓存、非交易时段不刷行情），失败后就再没机会写成功记录，
# 状态永远停在"最后一次是失败"。探活给了它一次主动证明的机会。
PROBE_URLS = {
    "sina": "https://hq.sinajs.cn/list=s_sh000300",
    "tencent": "https://qt.gtimg.cn/q=s_sh000300",
    "eastmoney": ("https://push2.eastmoney.com/api/qt/stock/get"
                  "?secid=1.000300&fields=f43,f170"),
}
PROBE_PURPOSE = "probe"


def probe_source(name, purpose=PROBE_PURPOSE, cfg=None, timeout=6):
    """真实探活一个数据源，并把结果**计入用量账（失败也记）**。

    返回 {"ok": bool, "source": name, "detail": str}；异常一律吞掉并记为失败，
    绝不让探活本身打断主流程。
    """
    import urllib.request
    url = PROBE_URLS.get(name)
    if not url:
        return {"ok": False, "source": name, "detail": "无探活地址，跳过"}
    ok, err = False, ""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(2048)
            ok = bool(body) and int(getattr(r, "status", 200)) == 200
            if not ok:
                err = "HTTP {}".format(getattr(r, "status", "?"))
    except Exception as e:
        err = str(e)[:120]
    try:
        note(name, ok=ok, error=err or None, purpose=purpose, cfg=cfg)
    except Exception:
        pass
    return {"ok": ok, "source": name, "detail": "ok" if ok else (err or "无响应")}


def probe_sources(names=None, purpose=PROBE_PURPOSE, cfg=None):
    """批量探活（界面按钮 / 定时任务用）。返回 {name: result}。"""
    names = list(names or ("sina", "tencent", "eastmoney"))
    return {n: probe_source(n, purpose=purpose, cfg=cfg) for n in names}


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


def usage_summary(cfg=None, provider=None, daily_limit=None, chain=None):
    """模块级快捷方式，见 :meth:`ApiUsage.usage_summary`。

    **注意 chain 必须透传**（2026-09-15 修复"降级提示永不消失"的第二个坑）：
    `Market.usage()` 调的是这个**模块级函数**（与同名方法重名，模块级定义在后、
    把类属性盖住了），而它原先**没接 chain**、也没往下传 → 降级判定只能回落到
    `CHAIN_DEFAULT`。加上 `CHAIN_DEFAULT.quote` 又把东财当首选（实现是新浪优先），
    于是"新浪正常供货"被永久判成已降级：界面那条提示怎么都不会消失。
    现在 chain 一路透传到 `snapshot`，判定表与实现一致；缺省时仍回落 CHAIN_DEFAULT。
    """
    if provider is None:
        provider = _provider_of(cfg)
    return _STORE.usage_summary(provider=provider, daily_limit=daily_limit,
                                cfg=cfg, chain=chain)


def reset(path=None):
    """模块级清空（测试用）。"""
    return _STORE.reset(path)
