# -*- coding: utf-8 -*-
"""数据源：智兔数服（主） + 东财/天天基金（兜底）+ 本地缓存 + 离线演示数据。

数据通道
--------
- 智兔数服：https://api.zhituapi.com  （config.data.zhitu_token）
    * 场外基金历史净值（一次全量）  /jh/hb/lsjz/{code}
    * 基金概况（名称/类型/费率提示） /jh/base/jjgk/{code}
    * 指数日K（区间）              /hz/history/fsjy/{dm}/d?st&et
    * 指数实时                    /hz/real/ssjy/{dm}
  限额：每日 200 次、频率 300 次/分钟。程序节流策略：
    * 进程内 15 分钟刷新冷却 + 磁盘缓存，界面轮询不消耗配额；
    * 每只基金每天只在线拉 1 次全量净值，之后读缓存；
    * 调用次数在界面上可见（/api/state -> api_usage）。
- 东财/天天基金接口作为智兔失败时的兜底（同样带缓存）。
- 演示数据为合成假行情，仅用于离线体验与流程演示。

所有基金均为场外普通基金（股票/指数/债券型），本程序不交易股票。
"""
import json
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import quote

from . import api_usage
from . import util
from .util import DataError

# ---------------- 智兔数服 ----------------
ZT_BASE = "https://api.zhituapi.com"
ZT_NAV = "/jh/hb/lsjz/{code}"
ZT_PROFILE = "/jh/base/jjgk/{code}"
ZT_INDEX_HIST = "/hz/history/fsjy/{dm}/d"
ZT_INDEX_REAL = "/hz/real/ssjy/{dm}"

# ---------------- 东财/天天基金（兜底） ----------------
EA_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
EA_NAV_HEADERS = {"Referer": "https://fundf10.eastmoney.com/"}
EA_PINGZHONG = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
EA_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 东财K线需要普通浏览器请求头：无头直连常被"远端直接断开"（实测重试即可成功）
EA_KLINE_HEADERS = {"Referer": "https://quote.eastmoney.com/",
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/120.0 Safari/537.36")}
EA_KLINE_FIELDS = ("fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,"
                   "f56,f57,f58,f59,f60,f61&klt=101&fqt=0")
EA_QUOTE_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EA_QUOTE_FIELDS = "f2,f3,f4,f12,f14"
SINA_QUOTE_URL = "https://hq.sinajs.cn/list={codes}"
SINA_QUOTE_REFERER = "https://finance.sina.com.cn/"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
TENCENT_QUOTE_REFERER = "https://stock.qq.com/"

ZT_FRESH_TTL = 900  # 秒
QUOTE_FRESH_TTL = 120  # 指数实时行情缓存秒数（>前端 60s 轮询，界面轮询不消耗配额）

# 收盘价确认时刻：当天 15:05 之后抓到的指数K线才算“已收盘完整bar”
CLOSE_TIME = "15:05"

# ---- AKShare 安全探测（避免把不稳定的 V8/py_mini_racer 依赖 import 进长驻服务） ----
_AK_STATE = {"checked": False, "ok": True}
_AK_LOCK = threading.Lock()

# 计数上下文（线程局部）：一次逻辑调用里的多个 HTTP 请求只汇总成一条记录，
# 避免“分页 20 页”被记成 20 次调用，同时保留真实请求次数（attempts）。
_CALL_CTX = threading.local()


def probe_akshare_safe():
    """用子进程探测 akshare 能否安全 import（子进程崩了不伤宿主服务）。

    FUNDAI_NO_AKSHARE=1 可直接禁用（走智兔/东财）。
    返回 True=可用。探测结果影响 Market._nav_chain。
    """
    import os
    with _AK_LOCK:
        if _AK_STATE["checked"]:
            return _AK_STATE["ok"]
        _AK_STATE["checked"] = True
        if os.environ.get("FUNDAI_NO_AKSHARE", "") in ("1", "true", "yes"):
            _AK_STATE["ok"] = False
            return False
        import subprocess
        import sys
        code = ("import akshare\nprint('AK_OK')\n")
        try:
            r = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, timeout=60)
            ok = bool(r and r.returncode == 0 and b"AK_OK" in (r.stdout or b""))
        except Exception:
            ok = False
        _AK_STATE["ok"] = ok
        return ok


class Market:
    """统一行情入口：按配置选择 akshare/智兔/东财，均带缓存与每日调用统计。

    - data.provider = "akshare"：场外基金净值用 AKShare（免费、无配额，内部走东财公开源），
      指数仍走智兔（有 Token 时）或东财直连；akshare 不可用时自动降级。
    - data.provider = "zhitu"（默认备选）：净值/指数都以智兔为主。
    - data.provider = "eastmoney"：全部走东财/天天基金公开接口。
    """

    def reload(self, cfg=None):
        """装载/热更新配置（__init__ 与 config.json 热重载共用，字段口径唯一）。"""
        self.cfg = cfg or {}
        self.data_cfg = self.cfg.get("data", {}) or {}
        self.token = (self.data_cfg.get("zhitu_token") or "").strip()
        self.provider = (self.data_cfg.get("provider") or "zhitu").strip().lower()
        self.akshare_on = self.provider == "akshare"
        # 是否使用智兔接口（指数等）：zhitu 或 akshare 模式且有 Token 时启用
        self.use_zhitu = bool(self.token) and self.provider in ("zhitu", "akshare")
        # 兼容旧调用：provider == zhitu 且带 Token
        self.zhitu_on = bool(self.token) and self.provider == "zhitu"

    def __init__(self, cfg=None):
        self.reload(cfg)
        self.warnings = []
        self._probe = None
        self._fresh_ts = {}
        self._ak_ok = None
        self._ak_fail_streak = 0   # akshare 连续瞬时失败计数（>=4 才永久停用）
        self._zt_limit = int(self.data_cfg.get("zhitu_daily_limit", 200) or 200)
        self._idx_quote = None      # 指数实时行情缓存
        self._idx_quote_ts = 0.0
        self._bootstrap_calendar()  # 用本地K线缓存注册交易日历（离线可用）
        # 注意：调用计数**不再放内存**（重启就清零的老 BUG）——统一由
        # fundai.api_usage 落盘到 data/api_usage.json，按日期分桶。

    # ---------------- 交易日历 ----------------
    def _register_calendar(self, items):
        try:
            util.set_calendar(sorted(items or {}))
        except Exception:
            pass

    def _bootstrap_calendar(self):
        """从本地指数K线缓存注册真实交易日（无网络开销）。"""
        try:
            key = self._index_key()
            cache = util.load_json(self._kline_cache_path(key), {})
            self._register_calendar(cache.get("items") or {})
        except Exception:
            pass

    # ---------------- 通道优先级 ----------------
    def provider_label(self):
        if self.akshare_on:
            return "AKShare(基金净值) + 智兔/东财(指数)" if self.token else "AKShare + 东财"
        if self.zhitu_on:
            return "智兔数服 + 东财兜底"
        return "东财/天天基金"

    def _nav_chain(self):
        """净值获取通道顺序（成功即停）。AKShare 仅当子进程探测可用时启用。"""
        chain = []
        ak_ok = not _AK_STATE["checked"] or _AK_STATE["ok"]
        if self.akshare_on and ak_ok:
            chain.append("akshare")
        if self.token:
            chain.append("zhitu")
        chain.append("eastmoney")
        return chain

    # ---------------- 调用计数 / 降级状态（持久化，见 fundai/api_usage.py） ----------------
    def _usage_chain(self):
        """各用途的通道优先级（供 api_usage 判定「是否已降级」）。"""
        try:
            return {
                "nav": tuple(self._nav_chain()),
                "index_kline": (("zhitu", "eastmoney") if self.use_zhitu
                                else ("eastmoney",)),
                # 实时行情实现是“新浪→腾讯→东财”，但语义上东财才是主源：
                # 新浪/腾讯可用即算降级（界面上要看得出“现在是谁在供货”）。
                "quote": ("eastmoney", "sina", "tencent"),
            }
        except Exception:
            return None

    def _note(self, source, ok=True, error=None, purpose=None, limit=None):
        """记一次网络尝试（成功/失败都算），失败原因交给 api_usage 截断打码。

        计数失败绝不影响主流程（api_usage 内部已吞异常，这里再加一层保险）。
        """
        try:
            api_usage.note(source, ok=bool(ok), error=error, purpose=purpose,
                           limit=limit)
        except Exception:
            pass

    def _note_limit(self, source):
        """该来源的每日上限：只有智兔有配额（其余为 None=不限）。"""
        return self._zt_limit if source == "zhitu" else None

    def usage(self):
        """数据源用量视图（**持久化**，进程重启不清零）。

        返回字段
        --------
        旧字段（前端 app.js 已在用，保留语义不变）：
          ``provider``      数据源标签（provider_label()）
          ``calls_today``   今日全部通道的调用尝试次数（成功与失败都算）
          ``daily_limit``   智兔每日上限；无 Token/无上限时为 None
          ``zhitu_today``   今日智兔调用次数
          ``akshare_today`` 今日 akshare 调用次数
        新增字段：
          ``date`` / ``ok_today`` / ``fail_today`` / ``remaining`` / ``exhausted``
          ``sources``（每源明细：calls/ok/fail/limit/last_ok/last_fail/last_error）
          ``by_source``（每源次数）/ ``sources_detail``（= sources）
          ``yesterday`` / ``days``（最近 14 天）/ ``failover``（最近降级记录）
          ``active``（各用途当前供货源）/ ``degraded`` / ``degraded_reason``
          ``store`` / ``updated`` / ``snapshot``（api_usage.snapshot() 完整视图）

        注意：``daily_limit`` 为 None 表示无上限（engine.py 用
        ``daily_limit - calls_today`` 判断配额，None 时不做限制）。另外
        ``calls_today`` 现在统计**全部通道**（含免费无配额的 akshare/东财/新浪/腾讯，
        且失败也算），因此配额裕度判断会偏保守 —— 若需更精确的智兔口径，请用
        ``zhitu_today``；``remaining`` 为 ``max(0, daily_limit - calls_today)``。
        """
        try:
            return api_usage.usage_summary(
                cfg=self.cfg, provider=self.provider_label(),
                daily_limit=(self._zt_limit if self.token else None))
        except Exception as e:
            # 极端兜底：计数存储彻底不可用时也不能让 state 接口崩
            self.warnings.append("用量统计不可用：{}".format(str(e)[:120]))
            return {
                "provider": self.provider_label(), "calls_today": 0,
                "daily_limit": (self._zt_limit if self.token else None),
                "zhitu_today": 0, "akshare_today": 0,
                "degraded": False, "active": {}, "failover": [],
            }

    def usage_snapshot(self):
        """api_usage.snapshot() 的 Market 版本（Web 层可视用，含降级原因）。

        补 ``chain``：按当前配置给出各用途通道优先级，snapshot 据此判定 degraded。
        内部异常不会外抛 —— api_usage.snapshot() 自身容错，并把问题写进返回值的
        ``error`` 字段（这里只兜底极端情况）。
        """
        try:
            return api_usage.snapshot(cfg=self.cfg,
                                      provider=self.provider_label(),
                                      chain=self._usage_chain())
        except Exception as e:
            snap = api_usage.snapshot(cfg=self.cfg)
            snap["error"] = str(e)[:160]
            return snap

    # ---------------- 智兔工具 ----------------
    def _zt_get(self, path, timeout=25, purpose=None):
        """调用智兔接口：**每次尝试都计数**（成功与失败都算），并带配额上限。

        旧实现只在成功返回后 +1，导致“失败请求”完全不计入用量（计数不准的根因之一）；
        现在失败也会写入 api_usage（fail+1 并保存截断后的错误原因）。
        limit 只有智兔有意义（= data.zhitu_daily_limit，默认 200）。
        purpose: nav / index_kline / index_profile（非空时同时记为 active 供货源）。
        """
        if not self.token:
            self._note("zhitu", ok=False, error="未配置智兔 Token",
                       purpose=purpose, limit=self._note_limit("zhitu"))
            raise DataError("未配置智兔 Token")
        url = ZT_BASE + path + ("&" if "?" in path else "?") + "token=" + self.token
        try:
            js = util.http_get_json(url, timeout=timeout, tries=1)
        except Exception as e:
            self._note("zhitu", ok=False, error=e, purpose=purpose,
                       limit=self._note_limit("zhitu"))
            raise
        self._note("zhitu", ok=True, purpose=purpose,
                   limit=self._note_limit("zhitu"))
        return js

    def _ak_nav_items(self, code, purpose="nav"):
        """AKShare 场外基金全量净值 -> {date: nav}。

        区分“模块级故障”（未安装/接口字段消失 → 当天停用并直连兜底）与
        “单次网络抖动”（允许本次失败并尝试通道链里的下一家；连续失败才停用）。
        每次尝试都计入 api_usage（akshare 无配额，limit=None）。
        """
        try:
            import akshare as ak
        except Exception as e:
            self._ak_ok = False
            self._note("akshare", ok=False, purpose=purpose,
                       error="AKShare 导入失败：{}".format(str(e)[:150]))
            raise DataError("AKShare 未安装或导入失败（已停用）：{}".format(
                str(e)[:150]))
        if self._ak_ok is False:
            self._note("akshare", ok=False, purpose=purpose,
                       error="AKShare 模块异常/连续失败，已停用")
            raise DataError("AKShare 模块异常/连续失败，已停用（走智兔/东财兜底）")
        try:
            df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        except ImportError as e:
            self._ak_ok = False
            self._note("akshare", ok=False, purpose=purpose,
                       error="AKShare 接口实现缺失：{}".format(str(e)[:150]))
            raise DataError("AKShare 接口实现缺失（已停用）：{}".format(code))
        except Exception as e:
            self._ak_fail_streak += 1
            if self._ak_fail_streak >= 4:
                self._ak_ok = False
                self._note("akshare", ok=False, purpose=purpose,
                           error="AKShare 连续 {} 次失败：{}".format(
                               self._ak_fail_streak, str(e)[:150]))
                raise DataError("AKShare 连续 {} 次失败，已停用（走智兔/东财兜底）：{}".format(
                    self._ak_fail_streak, str(e)[:150]))
            self._note("akshare", ok=False, purpose=purpose,
                       error="AKShare 瞬时失败({}/4)：{}".format(
                           self._ak_fail_streak, str(e)[:150]))
            raise DataError("AKShare 基金净值瞬时失败({}/4，自动重试兜底通道)：{}：{}".format(
                self._ak_fail_streak, code, str(e)[:150]))
        self._ak_ok = True
        self._ak_fail_streak = 0
        self._note("akshare", ok=True, purpose=purpose)
        date_col = next((c for c in ("净值日期", "date") if c in df.columns), None)
        nav_col = next((c for c in ("单位净值", "unit_nav") if c in df.columns), None)
        if date_col is None or nav_col is None:
            raise DataError("AKShare 净值列异常: {}".format(list(df.columns)))
        out = {}
        for _, row in df.iterrows():
            d = str(row[date_col])[:10]
            v = row[nav_col]
            if d and v == v and v is not None:  # NaN 检查
                out[d] = float(v)
        if not out:
            raise DataError("AKShare 净值解析为空: {}".format(code))
        return out

    # ================= 场外基金净值 =================
    @staticmethod
    def _nav_cache_path(code):
        return util.cache_file("nav_{}.json".format(code))

    @staticmethod
    def _cache_is_fresh(cache):
        """判断磁盘净值缓存是否“新鲜”，从而跨进程复用、避免服务重启后重复在线拉取。

        两个条件满足其一即视为新鲜：
        1) 缓存已覆盖最近一个应已公布净值的交易日；
        2) 距上次在线尝试不足 ZT_FRESH_TTL(15 分钟)——避免净值尚未公布时反复拉旧数据，
           同时保证 15 分钟后仍会重试，以拿到 20:00~24:00 陆续公布的新净值。
        """
        items = dict(cache.get("items", {}) or {})
        if not items:
            return False
        cache_max = max(items)
        now = util.now_dt()
        today = util.today_str()
        # 最近一个应已公布净值的交易日（净值约 20:00 后公布；忽略法定节假日，按工作日近似）
        if util.is_weekday(today) and now.strftime("%H:%M") >= "20:00":
            latest = today
        else:
            latest = util.prev_trading_day(today)
        if cache_max >= latest:
            return True
        updated = str(cache.get("updated") or "")
        try:
            upd = datetime.strptime(updated[:19], "%Y-%m-%d %H:%M:%S")
            age = (now - upd).total_seconds()
            return 0 <= age < ZT_FRESH_TTL
        except Exception:
            return False

    def _zt_fund_navs(self, code):
        """智兔全量净值（降序返回，转升序 [(date,nav)]）。"""
        js = self._zt_get(ZT_NAV.format(code=code))
        if not isinstance(js, list) or not js:
            raise DataError("智兔基金净值为空: {}".format(code))
        out = {}
        for r in js:
            d = (r.get("jzrq") or r.get("t") or "")[:10]
            v = r.get("dwjz")
            if d and v not in (None, ""):
                try:
                    out[d] = float(v)
                except (TypeError, ValueError):
                    pass
        if not out:
            raise DataError("智兔基金净值解析失败: {}".format(code))
        return sorted(out.items())

    # ---------------- 一次调用的计数辅助（含嵌套调用的去重） ----------------
    def _ping(self, source, ok=True, error=None, purpose=None):
        """记“一次真实 HTTP 尝试”；本线程有计数上下文时只累加，不落盘。

        上下文由 ``_source_call``（线程局部）建立 —— 这样“外层函数记一次汇总结果、
        内层每页/每跳只累加”，既不会把一次逻辑调用记成几十次，也不会漏掉重试次数。
        """
        ctx = getattr(_CALL_CTX, "ctx", None)
        if ctx is not None and ctx.get("source") == source:
            ctx["attempts"] = int(ctx.get("attempts") or 0) + 1
            if not ok:
                err = str(error) if error not in (None, "") else "未知错误"
                if not ctx.get("error"):
                    ctx["error"] = err
            return
        self._note(source, ok=ok, error=error, purpose=purpose,
                   limit=self._note_limit(source))

    def _source_call(self, source, purpose=None):
        """建立“一次逻辑调用”的计数上下文。

        嵌套（如 ``_em_fund_history`` 内部调 ``_em_range``）时只返回字典，由外层
        ``_source_call_end`` 统一写入一条记录（attempts 汇总 + 失败原因）。
        """
        ctx = getattr(_CALL_CTX, "ctx", None)
        if ctx is not None and ctx.get("source") == source:
            return ctx
        return {"source": source, "purpose": purpose, "attempts": 0,
                "error": None, "nested": False}

    def _source_call_end(self, ctx, ok, error=None):
        """结束一次逻辑调用：写一条汇总记录（attempts 次尝试 + ok/fail）。"""
        if not isinstance(ctx, dict) or ctx.get("nested"):
            return
        err = str(error) if error not in (None, "") else ctx.get("error")
        if not ok and not err:
            err = "未知错误"
        self._note(ctx.get("source"), ok=bool(ok), error=err,
                   purpose=ctx.get("purpose"),
                   limit=self._note_limit(ctx.get("source")))

    @contextmanager
    def _call_ctx(self, source, purpose=None):
        """``with m._call_ctx("eastmoney", "nav"):`` —— 退出时按成败写一条记录。

        用于**嵌套调用**（如 ``_em_fund_history`` 内部依次调 ``_em_range`` 与
        ``_em_deep_pingzhong``）：把多次 HTTP 尝试汇总成一条记录，避免一次逻辑调用
        被记成十几次（分页）。
        """
        ctx = self._source_call(source, purpose)
        prev = getattr(_CALL_CTX, "ctx", None)
        _CALL_CTX.ctx = ctx
        try:
            yield ctx
        except Exception as e:
            _CALL_CTX.ctx = prev
            self._source_call_end(ctx, False, e)
            raise
        else:
            _CALL_CTX.ctx = prev
            self._source_call_end(ctx, True)

    def _try_provider(self, purpose, prov, fn):
        """按通道链试一家：成功返回 ``(结果, None)``，失败返回 ``(None, 错误)``。

        失败时记 ``note_failover``（from=prov，to=链里的下一家）——**降级可视**；
        成功时记 ``note_active``（这次是谁供货的）。
        """
        try:
            res = fn()
        except Exception as e:          # 网络/解析异常都按“该通道失败”处理并计数
            try:
                chain = list((self._usage_chain() or {}).get(purpose) or [])
                nxt = chain[chain.index(prov) + 1] if prov in chain and \
                    chain.index(prov) + 1 < len(chain) else None
                api_usage.note_failover(purpose, prov, nxt, str(e)[:180])
            except Exception:
                pass
            return None, e
        try:
            api_usage.note_active(purpose, prov)
        except Exception:
            pass
        return res, None

    def _em_range(self, code, start, end):
        """东财 lsjz 区间分页（每页上限 20），返回 {date: nav}。

        计数：整段分页算“一次逻辑调用”，真实请求次数（含重试）走 _ping 累加；
        任何失败（某页网络错误、整体为空）都记为该次调用失败。
        """
        ctx = self._source_call("eastmoney", "nav")
        prev = getattr(_CALL_CTX, "ctx", None)
        _CALL_CTX.ctx = ctx
        try:
            out = self._em_range_raw(code, start, end)
        except Exception as e:
            _CALL_CTX.ctx = prev
            ctx["attempts"] = max(1, int(ctx.get("attempts") or 0))
            self._source_call_end(ctx, False, e)
            raise
        _CALL_CTX.ctx = prev
        ctx["attempts"] = max(1, int(ctx.get("attempts") or 0))
        self._source_call_end(ctx, True)
        return out

    def _em_range_raw(self, code, start, end):
        """[内部] 东财 lsjz 分页实现，每页请求前后各 _ping 一次（成功/失败都计数）。"""
        if start > end:
            return {}
        out, page, total = {}, 1, None
        while True:
            url = ("{}?fundCode={}&pageIndex={}&pageSize=20&startDate={}"
                   "&endDate={}".format(EA_NAV_URL, code, page, start, end))
            self._ping("eastmoney")
            try:
                js = util.http_get_json(url, headers=EA_NAV_HEADERS, timeout=15)
            except Exception as e:
                self._ping("eastmoney", ok=False, error=e)
                raise
            rows = ((js.get("Data") or {}).get("LSJZList")) or []
            if not rows:
                break
            for r in rows:
                d = (r.get("FSRQ") or "")[:10]
                if d and r.get("DWJZ") not in (None, ""):
                    try:
                        out[d] = float(r["DWJZ"])
                    except (TypeError, ValueError):
                        pass
            if total is None:
                total = int(js.get("TotalCount", 0) or 0)
            if len(out) >= total or len(rows) < 20:
                break
            page += 1
            time.sleep(0.12)
        if not out:
            raise DataError("东财基金净值接口无数据: {}".format(code))
        return out

    def _em_deep_pingzhong(self, code):
        """东财 pingzhongdata 深度补齐：记一次 eastmoney 调用（失败带原因）。"""
        ctx = self._source_call("eastmoney", "nav")
        prev = getattr(_CALL_CTX, "ctx", None)
        _CALL_CTX.ctx = ctx
        try:
            out = self._em_deep_pingzhong_raw(code)
        except Exception as e:
            _CALL_CTX.ctx = prev
            ctx["attempts"] = max(1, int(ctx.get("attempts") or 0))
            self._source_call_end(ctx, False, e)
            raise
        _CALL_CTX.ctx = prev
        ctx["attempts"] = max(1, int(ctx.get("attempts") or 0))
        self._source_call_end(ctx, True)
        return out

    def _em_deep_pingzhong_raw(self, code):
        """[内部] pingzhongdata 抓取与解析（一次 HTTP 尝试，成败都计数）。"""
        self._ping("eastmoney")
        try:
            txt = util.http_get_text(EA_PINGZHONG.format(code=code), timeout=20)
        except Exception as e:
            self._ping("eastmoney", ok=False, error=e)
            raise
        m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", txt)
        if not m:
            raise DataError("pingzhongdata 解析失败: fund={}".format(code))
        arr = json.loads(m.group(1))
        out = {}
        for it in arr:
            d = util.utc_ms_date(it.get("x", 0))
            y = it.get("y")
            if d and y not in (None, ""):
                out[d] = float(y)
        if not out:
            raise DataError("pingzhongdata 无净值: fund={}".format(code))
        return out

    def _em_fund_history(self, code, need_from=None):
        """东财通道：增量新鲜 + 深度补齐（带缓存）。"""
        cache = util.load_json(self._nav_cache_path(code), {"items": {}})
        items = dict(cache.get("items", {}))
        today = util.today_str()
        need = need_from or util.add_days(today, -45)
        cache_min = min(items) if items else None
        ttl_ok = self._fresh_ts.get(code, 0) + ZT_FRESH_TTL > time.time()
        need_refresh = cache_min is None or cache_min > need or not ttl_ok
        online = True
        deep_ok = (cache_min is not None and cache_min <= need)
        if need_refresh:
            # 一次逻辑调用（含“近端区间 + 深度补齐”两次东财请求）：汇总成一条计数
            with self._call_ctx("eastmoney", "nav"):
                try:
                    fresh = self._em_range(code, util.add_days(today, -9), today)
                    items.update(fresh)
                    self._fresh_ts[code] = time.time()
                    if not deep_ok:
                        try:
                            items.update(self._em_deep_pingzhong(code))
                            deep_ok = True
                        except DataError:
                            old = self._em_range(code, need, today)
                            items.update(old)
                            deep_ok = bool(old) and min(old) <= need
                except DataError:
                    online = False
        if online and need_refresh:
            util.save_json(self._nav_cache_path(code),
                           {"updated": util.now_iso(), "items": items})
        elif not online and need_refresh:
            cur_min = min(items) if items else None
            if cur_min is None or cur_min > need:
                raise DataError("基金 {} 净值获取失败且无足够缓存".format(code))
            self.warnings.append("{} 使用本地缓存（网络不可用）".format(code))
        if not deep_ok:
            raise DataError("基金 {} 历史净值不足".format(code))
        seq = sorted((d, nav) for d, nav in items.items() if nav and nav > 0)
        if need_from:
            seq = [x for x in seq if x[0] >= need_from]
        return seq

    def fund_history(self, code, need_from=None, allow_online=True,
                     force_live=False):
        """升序 [(date, nav)]。按 data.provider 选择通道链，全部失败才用缓存。

        allow_online=False：只读磁盘缓存（回测/回放用），绝不触发在线请求，
        历史窗口够用即返回，避免回放烧配额（audit P1-9/P0-3）。
        force_live=True：显式“立即在线刷新”（用于持仓盈亏手动刷新）——忽略内存
        TTL 与磁盘新鲜判断，直接走在线通道；在线失败才回退本地缓存。
        """
        if not re.match(r"^\d{6}$", str(code)):
            raise DataError("基金代码格式错误: {}".format(code))
        today = util.today_str()
        need = need_from or util.add_days(today, -45)
        cache = util.load_json(self._nav_cache_path(code), {"items": {}})
        items = dict(cache.get("items", {}))
        cache_min = min(items) if items else None
        cache_ok = cache_min is not None and cache_min <= need

        def _serve():
            seq = sorted((d, nav) for d, nav in items.items() if nav and nav > 0)
            if need_from:
                seq = [x for x in seq if x[0] >= need_from]
            return seq

        if force_live:
            last_err = None
            for prov in self._nav_chain():
                got, err = self._try_provider("nav", prov, lambda p=prov: (
                    self._ak_nav_items(code) if p == "akshare" else
                    ({d: n for d, n in self._zt_fund_navs(code)} if p == "zhitu"
                     else {d: n for d, n in self._em_fund_history(
                         code, need_from=need)})))
                if err is not None:
                    last_err = err
                    self.warnings.append("净值通道 {} 失败({})：{}".format(
                        prov, code, err))
                    continue
                if got:
                    items.update(got)
                    util.save_json(self._nav_cache_path(code),
                                    {"updated": util.now_iso(),
                                     "items": items})
                    self._fresh_ts[code] = time.time()
                    return _serve()
            if cache_ok:
                self.warnings.append("{} 在线刷新失败，使用本地缓存".format(code))
                return _serve()
            raise DataError("基金 {} 在线刷新失败且无缓存：{}".format(
                code, last_err or "所有通道失败"))
        if not allow_online:
            if cache_ok:
                return _serve()
            raise DataError("基金 {} 本地缓存不足（离线模式，缺 {} 之前数据）".format(
                code, cache_min or need))
        ttl_ok = self._fresh_ts.get(code, 0) + ZT_FRESH_TTL > time.time()
        # 内存 TTL 未到期 或 磁盘缓存已覆盖最近交易日 → 直接复用，避免服务重启后重复在线拉取
        if cache_ok and (ttl_ok or self._cache_is_fresh(cache)):
            return _serve()
        last_err = None
        for prov in self._nav_chain():
            got, err = self._try_provider("nav", prov, lambda p=prov: (
                self._ak_nav_items(code) if p == "akshare" else
                ({d: n for d, n in self._zt_fund_navs(code)} if p == "zhitu"
                 else {d: n for d, n in self._em_fund_history(code,
                                                              need_from=need)})))
            if err is not None:
                last_err = err
                self.warnings.append("净值通道 {} 失败({})：{}".format(prov, code, err))
                continue
            if got:
                items.update(got)
                util.save_json(self._nav_cache_path(code),
                               {"updated": util.now_iso(), "items": items})
                self._fresh_ts[code] = time.time()
                return _serve()
        if cache_ok:
            self.warnings.append("{} 使用本地缓存（在线通道均失败）".format(code))
            return _serve()
        raise DataError("基金 {} 净值获取失败且无缓存：{}".format(code, last_err or "所有通道失败"))

    def fund_latest(self, code, force_live=False):
        seq = self.fund_history(code, force_live=force_live)
        return seq[-1] if seq else (None, None)

    def fund_name(self, code):
        """基金名称（缓存优先）。在线兜底通道也计数：zhitu(index_profile)/eastmoney。"""
        meta = util.load_json(util.cache_file("fund_meta.json"), {})
        if code in meta and meta[code].get("name"):
            return meta[code]["name"]
        name = code
        try:
            if self.use_zhitu:
                js = self._zt_get(ZT_PROFILE.format(code=code), timeout=15,
                                  purpose="index_profile")
                name = js.get("jc") or js.get("qc") or code
            else:
                with self._call_ctx("eastmoney", "index_profile"):
                    self._ping("eastmoney")
                    try:
                        txt = util.http_get_text(EA_PINGZHONG.format(code=code),
                                                 timeout=15)
                    except Exception as e:
                        self._ping("eastmoney", ok=False, error=e)
                        raise
                    m = re.search(r'fS_name\s*=\s*"([^"]+)"', txt)
                    if m:
                        name = m.group(1)
        except DataError:
            pass
        meta[code] = {"name": name, "updated": util.now_iso()}
        util.save_json(util.cache_file("fund_meta.json"), meta)
        return name

    # ================= 指数 =================
    @staticmethod
    def _kline_cache_path(dm_or_secid):
        safe = re.sub(r"[^0-9A-Za-z]", "_", str(dm_or_secid))
        return util.cache_file("kline_{}.json".format(safe))

    def _index_cache_items(self):
        """主基准指数的本地K线：合并**两个可能存在的缓存键**后返回 {date: [close, vol]}。

        为什么合并（2026-09-11 数据事故的加固）：主指数在不同代码路径下会落到
        两个缓存文件——`kline_<智兔代码>.json`（`_index_key()`，use_zhitu 时）
        与 `kline_<东财 secid>.json`（兜底/跨市场通道）。事故中其中一份被
        兜底结果覆盖、另一份一度缺失，导致 `calib.load_closes()` 直接归零、
        所有基于指数历史的校准与回测一起失效。现在读取时取两边的**并集**
        （同一日期取先出现者），任一份存活即可继续工作。
        """
        keys = [self._index_key()]
        secid = (self.cfg.get("market", {}).get("index", {}) or {}).get(
            "eastmoney_secid")
        if secid and str(secid) not in keys:
            keys.append(str(secid))
        merged = {}
        for k in keys:
            try:
                items = ((util.load_json(self._kline_cache_path(k), {}) or {})
                         .get("items") or {})
            except Exception:
                continue
            for d, v in items.items():
                merged.setdefault(d, v)
        return merged

    def _kline_save(self, path, items, updated=None):
        """把K线缓存写回磁盘：与已有数据**取并集**，并拒绝"越写越少"。

        2026-09-11 事故（本项目最严重的一次数据损坏）：智兔失败走东财兜底时，
        兜底接口只返回一小段区间，旧代码直接 `save_json(path, {"items": items})`
        **覆盖**了原缓存 —— 沪深300 的长历史从 2673 根被截成 ~40 根
        （文件 143KB → 2KB），`calib.load_closes()` 随之归零，所有基于
        指数历史的校准/回测一起失效。现在：① 先合并磁盘上已有 bar；
        ② 若并集反而比原文件少（正常不可能），直接拒绝写入并告警。
        """
        old = dict(((util.load_json(path, {}) or {}).get("items")) or {})
        if old:
            for k, v in old.items():
                items.setdefault(k, v)
        if old and len(items) < len(old) * 0.5:
            try:
                api_usage.note("eastmoney", ok=False,
                               error="拒绝写入：缓存并集 {} < 已有 {} 的一半"
                                     .format(len(items), len(old)))
            except Exception:
                pass
            return False
        util.save_json(path, {"updated": updated or util.now_iso(),
                              "items": items})
        return True

    @staticmethod
    def _bar_is_final(updated):
        """updated(ISO串) 是否在收盘(15:05)后抓取 —— 当天bar只有收盘后才可信。"""
        try:
            upd = datetime.strptime(str(updated or "")[:19], "%Y-%m-%d %H:%M:%S")
            return upd.strftime("%H:%M") >= CLOSE_TIME
        except Exception:
            return False

    @classmethod
    def _drop_partial_today(cls, items, updated):
        """把“当天盘中(15:05前)抓到的日内快照bar”从序列剔除 → 用上一交易日收盘。"""
        items = dict(items or {})
        if not items:
            return items
        today = util.today_str()
        mx = max(items)
        if mx != today:
            return items
        if util.now_dt().strftime("%H:%M") < CLOSE_TIME:
            items.pop(mx, None)
        elif not cls._bar_is_final(updated):
            items.pop(mx, None)
        return items

    @staticmethod
    def _trading_today():
        """今天是否(近似)交易日。

        日历尚未覆盖到今天（K线缓存滞后于当日）时，必须退回工作日近似：
        否则形成死锁——“今天要不要抓”依赖日历，而日历只能靠抓取更新，
        常驻服务自缓存那天起就永远拉不到新交易日（2026-09-10 研判卡死在
        09-09 的根因）。日历覆盖到当日时精确查日历；节假日误判只会多一次
        空请求（接口没有当日 bar → 缓存不变），无副作用。
        """
        cal = util.calendar_dates()
        today = util.today_str()
        if cal and min(cal) <= today <= max(cal):
            return today in cal
        return util.is_weekday(today)

    @staticmethod
    def _kline_seq(items, need_from):
        seq = sorted((d, it[0], it[1]) for d, it in items.items())
        if need_from:
            seq = [x for x in seq if x[0] >= need_from]
        return seq

    def _index_key(self):
        idx = self.cfg.get("market", {}).get("index", {})
        if self.use_zhitu:
            return str(idx.get("zhitu_code") or idx.get("eastmoney_secid"))
        return str(idx.get("eastmoney_secid") or idx.get("zhitu_code"))

    def _indices_cfg(self):
        """市场指数快照配置（含基准指数）；缺省用常见大盘指数兜底。"""
        idx = self.cfg.get("market", {}).get("indices") or []
        out = []
        for it in idx:
            secid = str(it.get("secid") or "").strip()
            if secid:
                out.append({"secid": secid, "name": it.get("name") or secid,
                            "benchmark": bool(it.get("benchmark"))})
        return out

    def _quote_codes(self, items):
        """secid("1.000001") → 新浪/腾讯行情代码("s_sh000001")。"""
        codes = []
        for it in items:
            parts = str(it["secid"]).split(".")
            mkt = parts[0] if len(parts) > 1 else "1"
            code6 = parts[-1] if len(parts) > 1 else str(it["secid"])
            prefix = "sh" if mkt == "1" else "sz"
            codes.append("s_" + prefix + code6)
        return codes

    def _parse_sina(self, raw_bytes, items):
        text = raw_bytes.decode("gb18030", errors="replace")
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if '="' not in line:
                continue
            code = line.split("=")[0].replace("var hq_str_", "").strip()
            body = line.split('="', 1)[1].rstrip('";')
            parts = body.split(",")
            if len(parts) < 4 or not code.startswith("s_"):
                continue
            code6 = code[4:] if len(code) > 4 else code
            try:
                out[code6] = {"name": parts[0], "price": float(parts[1]),
                              "chg_amt": float(parts[2]),
                              "chg_pct": float(parts[3]) / 100.0}
            except (TypeError, ValueError):
                continue
        return out

    def _parse_tencent(self, raw_bytes, items):
        text = raw_bytes.decode("gb18030", errors="replace")
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if '="' not in line:
                continue
            code = line.split("=")[0].replace("v_", "").strip()
            body = line.split('="', 1)[1].rstrip('";')
            parts = body.split("~")
            if len(parts) < 6:
                continue
            code6 = parts[2] if len(parts) > 2 else (code[4:] if len(code) > 4 else code)
            try:
                out[code6] = {"name": parts[1], "price": float(parts[3]),
                              "chg_amt": float(parts[4]),
                              "chg_pct": float(parts[5]) / 100.0}
            except (TypeError, ValueError):
                continue
        return out

    def _parse_eastmoney_quote(self, items, errors=None):
        """东财行情：成功返回 {code: {...}}；失败返回 {} 并把原因写入 errors。

        计数由调用方 ``indices_quote`` 统一负责（每次调用一笔），这里不重复计数。
        """
        secids = ",".join(it["secid"] for it in items)
        url = ("{}?fltt=2&invt=2&fields={}&secids={}".format(
            EA_QUOTE_URL, EA_QUOTE_FIELDS, secids))
        try:
            raw = util.http_get(url, headers={"Referer": "https://quote.eastmoney.com/"},
                                timeout=8, tries=1)
            js = json.loads(raw.decode("gb18030", errors="replace"))
            diff = (((js.get("data") or {}).get("diff")) or [])
        except Exception as e:
            if errors is not None:
                errors["error"] = str(e)
            return {}
        out = {}
        for d in diff:
            code6 = str(d.get("f12") or "")
            if d.get("f2") is None:
                continue
            try:
                out[code6] = {
                    "name": d.get("f14") or code6, "price": float(d["f2"]),
                    "chg_amt": (float(d["f4"]) if d.get("f4") is not None else None),
                    "chg_pct": (float(d["f3"]) / 100.0 if d.get("f3") is not None else None)}
            except (TypeError, ValueError):
                continue
        return out

    def _quote_provider(self, prov, codes, items):
        """实时行情单通道：成功返回 (parsed, None)，失败返回 ({}, 错误)。

        计数由调用方 ``indices_quote`` 统一负责（每个被尝试的通道一笔），
        这里只做请求与解析。
        """
        err = None
        try:
            if prov == "sina":
                b = util.http_get(SINA_QUOTE_URL.format(codes=codes),
                                  headers={"Referer": SINA_QUOTE_REFERER},
                                  timeout=8, tries=1)
                parsed = self._parse_sina(b, items)
            elif prov == "tencent":
                b = util.http_get(TENCENT_QUOTE_URL.format(codes=codes),
                                  headers={"Referer": TENCENT_QUOTE_REFERER},
                                  timeout=8, tries=1)
                parsed = self._parse_tencent(b, items)
            else:  # eastmoney
                box = {}
                parsed = self._parse_eastmoney_quote(items, box)
                err = box.get("error")
            if not parsed and not err:
                err = "{} 行情返回空/解析为空".format(prov)
        except Exception as e:
            return {}, str(e)
        return parsed, err

    def indices_quote(self, force=False):
        """拉取一组大盘指数实时行情（新浪 → 腾讯 → 东财，带进程内短缓存）。

        返回 [{code, name, price, chg_pct, chg_amt, benchmark}]；失败返回 []。
        chg_pct 为小数（-0.003 表示 -0.30%），chg_amt 为涨跌点/额。
        计数：每个被尝试的通道各记一笔（失败带原因）；发生通道切换时记
        ``note_failover(purpose="quote")``，供货成功的通道记 ``note_active``。
        """
        items = self._indices_cfg()
        if not items:
            return []
        now = time.time()
        if not force and self._idx_quote is not None and \
                now - self._idx_quote_ts < QUOTE_FRESH_TTL:
            return self._idx_quote
        codes = ",".join(self._quote_codes(items))
        parsed = {}
        for prov in ("sina", "tencent", "eastmoney"):
            got, err = self._quote_provider(prov, codes, items)
            self._note(prov, ok=err is None, error=err, purpose="quote",
                       limit=self._note_limit(prov))
            if err is None:
                parsed = got
                try:
                    api_usage.note_active("quote", prov)
                except Exception:
                    pass
                break
            self._warn_failover("quote", prov, err)
        if not parsed:
            return self._idx_quote if self._idx_quote is not None else []
        out = []
        for it in items:
            code6 = str(it["secid"]).split(".")[-1]
            p = parsed.get(code6)
            if not p:
                continue
            out.append({
                "code": code6,
                "name": it.get("name") or p.get("name") or code6,
                "price": p["price"],
                "chg_pct": p["chg_pct"],
                "chg_amt": p["chg_amt"],
                "benchmark": bool(it.get("benchmark")),
            })
        self._idx_quote = out
        self._idx_quote_ts = now
        return out

    def _warn_failover(self, purpose, frm, err, to=None):
        """记一次降级：写 api_usage（可视）并保留原有 warnings 行为。"""
        try:
            chain = list((self._usage_chain() or {}).get(purpose) or [])
            if to is None and frm in chain and chain.index(frm) + 1 < len(chain):
                to = chain[chain.index(frm) + 1]
        except Exception:
            to = None
        try:
            api_usage.note_failover(purpose, frm, to, str(err)[:180])
        except Exception:
            pass
        return to

    def _zt_index_history(self, start, end, purpose="index_kline"):
        dm = self._index_key()
        js = self._zt_get("{}?st={}&et={}".format(
            ZT_INDEX_HIST.format(dm=dm),
            start.replace("-", ""), end.replace("-", "")), purpose=purpose)
        if not isinstance(js, list):
            raise DataError("智兔指数K线为空: {}".format(dm))
        out = []
        for r in js:
            t = (r.get("t") or "")[:10]
            c = r.get("c")
            v = r.get("v")
            if t and c not in (None, ""):
                out.append((t, float(c), float(v or 0)))
        if not out:
            raise DataError("智兔指数K线解析失败: {}".format(dm))
        return sorted(out)

    def _em_index_history(self, secid, need_from, purpose="index_kline"):
        """东财指数K线（带本地缓存）。每次在线尝试都计数（eastmoney，无配额）。

        修 BUG（2026-09-11）：请求必须带 **beg=0 与 lmt=<很大>**。
        旧写法只传 `beg=need_from&end=today`，东财在缺省 `lmt` 下**只返回 40 根K线**
        （实测 2012 起的请求只回 2017-01-03→2017-02-11 共 40 根）。这段残缺数据
        曾被"兜底覆盖"写回缓存，把沪深300 的 2673 根长历史截成 ~40 根，
        连带 `calib.load_closes()` 归零（见 `_kline_save` 的事故说明）。
        """
        self._ping("eastmoney", purpose=purpose)
        cache = util.load_json(self._kline_cache_path(secid), {"items": {}})
        items = dict(cache.get("items", {}))
        updated = str(cache.get("updated") or "")
        try:
            beg_d = util.add_days(need_from, -15)
            end = util.add_days(util.today_str(), 3)
            # 东财的 beg/end 必须是 **YYYYMMDD**（带横线的 ISO 会被当成非法区间，
            # 实测会静默退化成"只回 40 根"）；lmt 也必须显式给足（缺省同样只回 40 根）。
            # 带上 Referer/UA 更稳（无头直连常被"远端直接断开"，重试即可成功）。
            url = ("{}?secid={}&{}&beg={}&end={}&lmt=10000".format(
                EA_KLINE_URL, secid, EA_KLINE_FIELDS,
                str(beg_d).replace("-", ""), str(end).replace("-", "")))
            js = util.http_get_json(url, timeout=20, headers=EA_KLINE_HEADERS)
            klines = ((js.get("data") or {}).get("klines")) or []
            fresh = []
            for line in klines:
                p = line.split(",")
                if len(p) >= 7:
                    fresh.append((p[0], float(p[2]), float(p[5])))
            if not fresh:
                raise DataError("东财指数K线为空: {}".format(secid))
            for d, c, v in fresh:
                items[d] = [c, v]
            updated = util.now_iso()
            # 并集写回：东财偶尔仍只回短区间，绝不能覆盖更长的本地历史
            self._kline_save(self._kline_cache_path(secid), items,
                             updated=updated)
        except DataError as e:
            self._ping("eastmoney", ok=False, error=e, purpose=purpose)
            if not items:
                raise
            self.warnings.append("指数K线使用本地缓存: {}".format(secid))
        self._register_calendar(items)
        items = self._drop_partial_today(items, updated)
        return self._kline_seq(items, need_from)

    def index_history(self, need_from=None, offline=False):
        """[(date, close, vol)] 升序（收盘K线；当天盘中快照bar会被剔除）。

        ``offline=True``：**只读本地缓存，绝不发起任何在线请求**（不消耗智兔配额）。
        用途：历史区间（牛市/熊市/震荡市）独立回测 —— 回放过去某一段时，缓存的
        最新日期通常远早于“今天”，默认逻辑会去联网补数据，既烧配额又让研究不可复现；
        离线模式直接返回缓存切片（并注册交易日历），缓存为空则**直接报错**
        （``DataError("离线模式：本地指数K线缓存为空")``），而不是偷偷联网。
        """
        key = self._index_key()
        need = need_from or util.add_days(util.today_str(), -220)
        if not offline and not self.use_zhitu:
            return self._em_index_history(key, need)
        cache = util.load_json(self._kline_cache_path(key), {"items": {}})
        # 合并两个可能的缓存键（详见 _index_cache_items 的事故说明）
        items = self._index_cache_items()
        if not items:
            items = dict(cache.get("items", {}))
        if offline:
            # 离线：不判断“缓存是否落后于今天”，也不落盘、不计数
            if not items:
                raise DataError("离线模式：本地指数K线缓存为空")
            self._register_calendar(items)
            return self._kline_seq(items, need_from)
        if self.use_zhitu:
            updated = str(cache.get("updated") or "")
            cache_max = max(items) if items else None
            today = util.today_str()
            now_hhmm = util.now_dt().strftime("%H:%M")
            # 当天bar尚为盘中快照（15:05前抓取）且当前已过收盘 → 需要在线重取收盘bar；
            # 缓存缺失/落后于今天 且 今天是交易日 → 需要在线拉取。
            partial_day = (cache_max == today and not self._bar_is_final(updated))
            need_online = (cache_max is None or cache_max < today) or \
                (partial_day and now_hhmm >= CLOSE_TIME)
            if need_online and (self._trading_today() or cache_max is None):
                try:
                    rows = self._zt_index_history(need, util.add_days(today, 1))
                    for d, c, v in rows:
                        items[d] = [c, v]
                    updated = util.now_iso()
                    # **必须**走并集写回：请求是按 need_from 定范围的，若直接覆盖，
                    # 比请求范围更长的本地历史会被悄悄截断（实测：一次 2018 起的
                    # 增量请求把 2449 根缓存写成 1945 根，`calib.load_closes()` 随之下滑）
                    self._kline_save(self._kline_cache_path(key), items,
                                     updated=updated)
                    try:
                        api_usage.note_active("index_kline", "zhitu")
                    except Exception:
                        pass
                except DataError as e:
                    self.warnings.append("智兔指数K线失败，尝试东财：{}".format(e))
                    self._warn_failover("index_kline", "zhitu", e, to="eastmoney")
                    try:
                        secid = self.cfg.get("market", {}).get("index", {}) \
                            .get("eastmoney_secid")
                        got = {d: [c, v] for d, c, v in
                               self._em_index_history(secid, need)}
                        key2 = secid
                        updated = util.now_iso()
                        # 与已有缓存取并集（兜底接口常只返回一小段，直接覆盖会毁数据）
                        self._kline_save(self._kline_cache_path(key2), got,
                                         updated=updated)
                        self._register_calendar(got)
                        try:
                            api_usage.note_active("index_kline", "eastmoney")
                        except Exception:
                            pass
                    except DataError:
                        if not items:
                            raise DataError("指数K线获取失败且无缓存")
                        self.warnings.append("指数K线使用本地缓存")
            self._register_calendar(items)
            items = self._drop_partial_today(items, updated)
            return self._kline_seq(items, need_from)
        return self._em_index_history(key, need)

    def index_bars(self, secid, years=5, allow_online=True):
        """任意指数的日线 [(date, close, vol)] 升序（读本地缓存；缺失时在线补）。

        供跨市场特征用（创业板/中证500 等风格指数与沪深300 的相对强弱）。
        """
        key = str(secid)
        path = self._kline_cache_path(key)
        cache = util.load_json(path, {"items": {}})
        items = dict(cache.get("items") or {})
        need = util.add_days(util.today_str(), -int(years * 365))
        oldest = min(items) if items else None
        if allow_online and (oldest is None or oldest > need):
            rows = []
            if self.use_zhitu and "." in key:
                code, mk = key.split(".")[1], key.split(".")[0]
                zt_code = "{}.{}".format(code, "SH" if mk == "1" else "SZ")
                try:
                    rows = self._zt_index_history(
                        need, util.add_days(util.today_str(), 1),
                        purpose="index_bars")
                    try:
                        api_usage.note_active("index_bars", "zhitu")
                    except Exception:
                        pass
                except DataError as e:
                    self.warnings.append("{} 智兔K线失败：{}".format(zt_code, e))
                    self._warn_failover("index_bars", "zhitu", e, to="eastmoney")
            if not rows:
                try:
                    rows = self._em_index_history(key, need, purpose="index_bars")
                    try:
                        api_usage.note_active("index_bars", "eastmoney")
                    except Exception:
                        pass
                except DataError as e:
                    self.warnings.append("{} 指数K线失败：{}".format(key, e))
            for d, c, v in rows:
                items[d] = [c, v]
            if rows:
                self._kline_save(path, items)
                self._register_calendar(items)
        return self._kline_seq(items, need)

    def index_latest(self):
        seq = self.index_history()
        return seq[-1] if seq else (None, None, None)

    def extend_index_history(self, years=8, with_benchmarks=False):
        """把主基准指数的历史K线缓存一次性向前延伸到 years 年（≈1 次智兔全区间请求）。

        与 BaoStock 免费历史行情的等价实现：本工程无强依赖，直接复用
        智兔/东财的“按区间全量取日K”接口写入本地缓存，供长周期回测/统计语境使用。
        返回 {source, bars, start, end}。
        """
        start = util.add_days(util.today_str(), -(int(years) * 365 + 10))
        key = self._index_key()
        path = self._kline_cache_path(key)
        cache = util.load_json(path, {"items": {}})
        items = dict(cache.get("items", {}))
        src = "cache"
        try:
            rows = self._zt_index_history(start, util.add_days(util.today_str(), 1))
            src = "zhitu"
            try:
                api_usage.note_active("index_kline", "zhitu")
            except Exception:
                pass
        except DataError as e:
            self.warnings.append("智兔长历史失败，尝试东财：{}".format(e))
            self._warn_failover("index_kline", "zhitu", e, to="eastmoney")
            secid = (self.cfg.get("market", {}).get("index", {})
                     or {}).get("eastmoney_secid")
            seq = self._em_index_history(secid, start)
            # 与已有缓存取并集，绝不因兜底区间更短而丢历史（见 _kline_save 的事故说明）
            merged = dict(items)
            for d, c, v in seq:
                merged[d] = [c, v]
            items = merged
            src = "eastmoney"
            try:
                api_usage.note_active("index_kline", "eastmoney")
            except Exception:
                pass
        else:
            for d, c, v in rows:
                items[d] = [c, v]
        # 写回（_kline_save 会与磁盘已有数据取并集，避免兜底区间更短时丢历史）
        self._kline_save(path, items)
        self._register_calendar(items)
        ks = sorted(items)
        res = {"source": src, "bars": len(items),
               "start": ks[0] if ks else None, "end": ks[-1] if ks else None}

        if with_benchmarks:
            em_secids = []
            for it in self._indices_cfg():
                s = str(it.get("secid") or "")
                if s and s not in em_secids:
                    em_secids.append(s)
            for s in em_secids:
                try:
                    seq = self._em_index_history(s, start)
                    if seq:
                        res.setdefault("benchmarks", {})[s] = {
                            "bars": len(seq), "start": seq[0][0],
                            "end": seq[-1][0]}
                except DataError:
                    continue
        return res

    def probe_online(self):
        if self._probe is not None:
            return self._probe
        try:
            self.index_history()
            self._probe = True
        except DataError:
            self._probe = False
        return self._probe

    # ================= 离线演示数据 =================
    @staticmethod
    def _demo_series_path():
        return util.demo_file("series.json")

    def demo_available(self):
        return self._demo_series_path().exists()

    def demo_series(self):
        return util.load_json(self._demo_series_path())

    def ensure_demo_series(self, force=False):
        """合成半年演示行情（非真实），确定性随机。"""
        path = self._demo_series_path()
        if path.exists() and not force:
            return util.load_json(path)
        import random
        rng = random.Random(20260906)
        n = 126
        end = "2026-09-04"
        dates = util.trading_days_between(
            util.add_days(end, -(n * 7 // 5 + 14)), end)[-n:]
        closes = []
        level = 4660.44
        vol_regime = 0.010
        regime_left = 0
        daily_drift = 0.0
        for i in range(n):
            if regime_left <= 0:
                regime_left = rng.randint(8, 18)
                daily_drift = rng.choice([0.0012, 0.0, -0.0018, -0.0006])
                vol_regime = rng.uniform(0.008, 0.016)
            regime_left -= 1
            shock = 0.0
            if i == n - 60:
                shock = -0.022
            if i == n - 45:
                shock = -0.016
            if i == n - 30:
                shock = 0.014
            level *= (1 + daily_drift + rng.gauss(0, vol_regime) + shock)
            closes.append(level)
        vols = [int(rng.uniform(3.2e8, 5.4e8)) for _ in range(n)]
        # 基金净值：按 config pool 全量生成（权益=主题随机漫步，债基=缓慢爬升）
        funds = {}
        pool = (self.cfg.get("pool") or [])
        eq_codes = [f["code"] for f in pool if f.get("kind") == "equity"]
        bond_codes = [f["code"] for f in pool if f.get("kind") == "bond"]
        for i, code in enumerate(eq_codes):
            seed = round(rng.uniform(0.8, 2.2), 2)
            lv = seed
            drift_k = rng.uniform(0.5, 1.8)
            vol_k = rng.uniform(1.0, 2.2)
            navs = []
            for _ in range(n):
                lv *= (1 + daily_drift * drift_k +
                       rng.gauss(0, vol_regime * vol_k))
                navs.append(round(lv, 4))
            funds[code] = navs
        for code in bond_codes:
            seed = round(rng.uniform(1.0, 1.3), 2)
            navs = []
            v = seed
            for _ in range(n):
                v *= (1 + 0.00015 + rng.gauss(0, 0.00045))
                navs.append(round(v, 4))
            funds[code] = navs
        series = {
            "note": "【演示合成数据】非真实行情，仅用于离线体验与流程演示",
            "generated": util.now_iso(),
            "start": dates[0], "end": dates[-1],
            "index_name": (self.cfg.get("market", {}).get("index", {})
                           or {}).get("name", "沪深300"),
            "dates": dates,
            "index_close": [round(c, 2) for c in closes],
            "index_vol": vols,
            "funds": funds,
        }
        util.save_json(path, series)
        return series
