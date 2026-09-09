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
import time
from datetime import datetime
from urllib.parse import quote

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
EA_KLINE_FIELDS = ("fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,"
                   "f56,f57,f58,f59,f60,f61&klt=101&fqt=0")
EA_QUOTE_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EA_QUOTE_FIELDS = "f2,f3,f4,f12,f14"
SINA_QUOTE_URL = "https://hq.sinajs.cn/list={codes}"
SINA_QUOTE_REFERER = "https://finance.sina.com.cn/"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
TENCENT_QUOTE_REFERER = "https://stock.qq.com/"

ZT_FRESH_TTL = 900  # 秒
QUOTE_FRESH_TTL = 45  # 指数实时行情缓存秒数（界面 60s 轮询只读缓存）


class Market:
    """统一行情入口：按配置选择 akshare/智兔/东财，均带缓存与每日调用统计。

    - data.provider = "akshare"：场外基金净值用 AKShare（免费、无配额，内部走东财公开源），
      指数仍走智兔（有 Token 时）或东财直连；akshare 不可用时自动降级。
    - data.provider = "zhitu"（默认备选）：净值/指数都以智兔为主。
    - data.provider = "eastmoney"：全部走东财/天天基金公开接口。
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.data_cfg = self.cfg.get("data", {}) or {}
        self.token = (self.data_cfg.get("zhitu_token") or "").strip()
        self.provider = (self.data_cfg.get("provider") or "zhitu").strip().lower()
        self.akshare_on = self.provider == "akshare"
        # 是否使用智兔接口（指数等）：zhitu 或 akshare 模式且有 Token 时启用
        self.use_zhitu = bool(self.token) and self.provider in ("zhitu", "akshare")
        # 兼容旧调用：provider == zhitu 且带 Token
        self.zhitu_on = bool(self.token) and self.provider == "zhitu"
        self.warnings = []
        self._probe = None
        self._fresh_ts = {}
        self._zt_calls = {}   # date -> 智兔调用次数
        self._ak_calls = {}   # date -> akshare 调用次数
        self._ak_ok = None
        self._zt_limit = int(self.data_cfg.get("zhitu_daily_limit", 200) or 200)
        self._idx_quote = None      # 指数实时行情缓存
        self._idx_quote_ts = 0.0

    # ---------------- 通道优先级 ----------------
    def provider_label(self):
        if self.akshare_on:
            return "AKShare(基金净值) + 智兔/东财(指数)" if self.token else "AKShare + 东财"
        if self.zhitu_on:
            return "智兔数服 + 东财兜底"
        return "东财/天天基金"

    def _nav_chain(self):
        """净值获取通道顺序（成功即停）。"""
        chain = []
        if self.akshare_on:
            chain.append("akshare")
        if self.token:
            chain.append("zhitu")
        chain.append("eastmoney")
        return chain

    def _count(self, kind):
        today = util.today_str()
        if kind == "zhitu":
            self._zt_calls[today] = self._zt_calls.get(today, 0) + 1
        elif kind == "akshare":
            self._ak_calls[today] = self._ak_calls.get(today, 0) + 1

    def usage(self):
        today = util.today_str()
        zt = self._zt_calls.get(today, 0)
        ak = self._ak_calls.get(today, 0)
        return {
            "provider": self.provider_label(),
            "calls_today": zt + ak,
            "daily_limit": self._zt_limit if self.token else None,
            "zhitu_today": zt,
            "akshare_today": ak,
        }

    # ---------------- 智兔工具 ----------------
    def _zt_get(self, path, timeout=25):
        """调用智兔接口并计数（不过量硬截断，只记录提示）。"""
        if not self.token:
            raise DataError("未配置智兔 Token")
        url = ZT_BASE + path + ("&" if "?" in path else "?") + "token=" + self.token
        js = util.http_get_json(url, timeout=timeout, tries=1)
        today = util.today_str()
        self._zt_calls[today] = self._zt_calls.get(today, 0) + 1
        return js
    def _ak_nav_items(self, code):
        """AKShare 场外基金全量净值 -> {date: nav}。"""
        try:
            import akshare as ak
        except Exception as e:
            raise DataError("AKShare 未安装或导入失败：{}".format(e))
        if self._ak_ok is False:
            raise DataError("AKShare 模块异常，已停用")
        try:
            df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        except Exception as e:
            self._ak_ok = False
            raise DataError("AKShare 基金净值失败({})：{}".format(code, str(e)[:150]))
        self._ak_ok = True
        self._count("akshare")
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

    def _em_range(self, code, start, end):
        """东财 lsjz 区间分页（每页上限 20），返回 {date: nav}。"""
        if start > end:
            return {}
        out, page, total = {}, 1, None
        while True:
            url = ("{}?fundCode={}&pageIndex={}&pageSize=20&startDate={}"
                   "&endDate={}".format(EA_NAV_URL, code, page, start, end))
            js = util.http_get_json(url, headers=EA_NAV_HEADERS, timeout=15)
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
        txt = util.http_get_text(EA_PINGZHONG.format(code=code), timeout=20)
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

    def fund_history(self, code, need_from=None):
        """升序 [(date, nav)]。按 data.provider 选择通道链，全部失败才用缓存。"""
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

        ttl_ok = self._fresh_ts.get(code, 0) + ZT_FRESH_TTL > time.time()
        # 内存 TTL 未到期 或 磁盘缓存已覆盖最近交易日 → 直接复用，避免服务重启后重复在线拉取
        if cache_ok and (ttl_ok or self._cache_is_fresh(cache)):
            return _serve()
        last_err = None
        for prov in self._nav_chain():
            try:
                if prov == "akshare":
                    got = self._ak_nav_items(code)
                elif prov == "zhitu":
                    got = {d: n for d, n in self._zt_fund_navs(code)}
                else:  # eastmoney（增量 + pingzhong 兜底）
                    got = {d: n for d, n in self._em_fund_history(code,
                                                                  need_from=need)}
                if got:
                    items.update(got)
                    util.save_json(self._nav_cache_path(code),
                                   {"updated": util.now_iso(), "items": items})
                    self._fresh_ts[code] = time.time()
                    return _serve()
            except DataError as e:
                last_err = e
                self.warnings.append("净值通道 {} 失败({})：{}".format(prov, code, e))
        if cache_ok:
            self.warnings.append("{} 使用本地缓存（在线通道均失败）".format(code))
            return _serve()
        raise DataError("基金 {} 净值获取失败且无缓存：{}".format(code, last_err or "所有通道失败"))

    def fund_latest(self, code):
        seq = self.fund_history(code)
        return seq[-1] if seq else (None, None)

    def fund_name(self, code):
        meta = util.load_json(util.cache_file("fund_meta.json"), {})
        if code in meta and meta[code].get("name"):
            return meta[code]["name"]
        name = code
        try:
            if self.use_zhitu:
                js = self._zt_get(ZT_PROFILE.format(code=code), timeout=15)
                name = js.get("jc") or js.get("qc") or code
            else:
                txt = util.http_get_text(EA_PINGZHONG.format(code=code), timeout=15)
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

    def _parse_eastmoney_quote(self, items):
        secids = ",".join(it["secid"] for it in items)
        url = ("{}?fltt=2&invt=2&fields={}&secids={}".format(
            EA_QUOTE_URL, EA_QUOTE_FIELDS, secids))
        try:
            raw = util.http_get(url, headers={"Referer": "https://quote.eastmoney.com/"},
                                timeout=8, tries=1)
            js = json.loads(raw.decode("gb18030", errors="replace"))
            diff = (((js.get("data") or {}).get("diff")) or [])
        except Exception:
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

    def indices_quote(self, force=False):
        """拉取一组大盘指数实时行情（新浪 → 腾讯 → 东财，带进程内短缓存）。

        返回 [{code, name, price, chg_pct, chg_amt, benchmark}]；失败返回 []。
        chg_pct 为小数（-0.003 表示 -0.30%），chg_amt 为涨跌点/额。
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
        try:
            b = util.http_get(SINA_QUOTE_URL.format(codes=codes),
                              headers={"Referer": SINA_QUOTE_REFERER},
                              timeout=8, tries=1)
            parsed = self._parse_sina(b, items)
        except DataError:
            parsed = {}
        if not parsed:
            try:
                b = util.http_get(TENCENT_QUOTE_URL.format(codes=codes),
                                  headers={"Referer": TENCENT_QUOTE_REFERER},
                                  timeout=8, tries=1)
                parsed = self._parse_tencent(b, items)
            except DataError:
                parsed = {}
        if not parsed:
            parsed = self._parse_eastmoney_quote(items)
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

    def _zt_index_history(self, start, end):
        dm = self._index_key()
        js = self._zt_get("{}?st={}&et={}".format(
            ZT_INDEX_HIST.format(dm=dm),
            start.replace("-", ""), end.replace("-", "")))
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

    def _em_index_history(self, secid, need_from):
        cache = util.load_json(self._kline_cache_path(secid), {"items": {}})
        items = dict(cache.get("items", {}))
        try:
            beg_d = util.add_days(need_from, -15)
            end = util.add_days(util.today_str(), 3)
            url = ("{}?secid={}&{}&beg={}&end={}".format(
                EA_KLINE_URL, secid, EA_KLINE_FIELDS, beg_d, end))
            js = util.http_get_json(url, timeout=15)
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
            util.save_json(self._kline_cache_path(secid),
                           {"updated": util.now_iso(), "items": items})
        except DataError:
            if not items:
                raise
            self.warnings.append("指数K线使用本地缓存: {}".format(secid))
        seq = sorted((d, it[0], it[1]) for d, it in items.items())
        if need_from:
            seq = [x for x in seq if x[0] >= need_from]
        return seq

    def index_history(self, need_from=None):
        """[(date, close, vol)] 升序。"""
        key = self._index_key()
        need = need_from or util.add_days(util.today_str(), -220)
        if self.use_zhitu:
            cache = util.load_json(self._kline_cache_path(key), {"items": {}})
            items = dict(cache.get("items", {}))
            cache_max = max(items) if items else None
            # 智兔按区间取：需要从 need 到“今天”；若缓存最新已含今天则直接读缓存
            try:
                if cache_max is None or cache_max < util.today_str():
                    rows = self._zt_index_history(need, util.add_days(util.today_str(), 1))
                    for d, c, v in rows:
                        items[d] = [c, v]
                    util.save_json(self._kline_cache_path(key),
                                   {"updated": util.now_iso(), "items": items})
            except DataError as e:
                self.warnings.append("智兔指数K线失败，尝试东财：{}".format(e))
                try:
                    rows = self._em_index_history(self.cfg.get("market", {}).get("index", {})
                                                  .get("eastmoney_secid"), need)
                    items = {d: [c, v] for d, c, v in rows}
                    key2 = self.cfg.get("market", {}).get("index", {}) \
                        .get("eastmoney_secid")
                    util.save_json(self._kline_cache_path(key2),
                                   {"updated": util.now_iso(), "items": items})
                except DataError:
                    if not items:
                        raise DataError("指数K线获取失败且无缓存")
                    self.warnings.append("指数K线使用本地缓存")
        else:
            return self._em_index_history(key, need)
        seq = sorted((d, it[0], it[1]) for d, it in items.items())
        if need_from:
            seq = [x for x in seq if x[0] >= need_from]
        return seq

    def index_latest(self):
        seq = self.index_history()
        return seq[-1] if seq else (None, None, None)

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
