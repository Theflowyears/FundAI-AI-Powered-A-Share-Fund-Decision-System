# -*- coding: utf-8 -*-
"""通用工具：路径、日期、HTTP、JSON、金额格式化。仅标准库。"""
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _project_root():
    """项目根目录（数据/配置/静态资源的落点）。

    优先级（2026-09-11 加入，为打包准备）：
      1. 环境变量 `FUNDAI_HOME`：安装版（pip/wheel）或想换数据目录时用；
      2. 源码树：`fundai/` 的上一级 —— 便携包与开发环境都是这个布局。
    """
    import os
    env = os.environ.get("FUNDAI_HOME")
    if env:
        p = Path(env).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return p
    return Path(__file__).resolve().parent.parent


PROJECT = _project_root()
DATA_DIR = PROJECT / "data"
CACHE_DIR = DATA_DIR / "cache"
DEMO_DIR = DATA_DIR / "demo"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TZ_CN = timezone(timedelta(hours=8))

# 进程内交易日历：由 Market 在成功取得指数K线后注册（真实交易日，含节假日剔除）；
# 未注册时回退为“工作日近似”。list[str] 升序（YYYY-MM-DD）。
_CALENDAR = None
CALENDAR_SOURCE = "weekday"


class DataError(Exception):
    """数据源异常（网络/解析）。"""


# ---------------- 路径 ----------------
def ensure_dirs():
    for d in (DATA_DIR, CACHE_DIR, DEMO_DIR):
        d.mkdir(parents=True, exist_ok=True)


def data_file(name):
    ensure_dirs()
    return DATA_DIR / name


def cache_file(name):
    ensure_dirs()
    return CACHE_DIR / name


def demo_file(name):
    ensure_dirs()
    return DEMO_DIR / name


# ---------------- 时间 ----------------
def now_dt():
    return datetime.now()


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_d(s):
    if isinstance(s, date):
        return s
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def fmt_d(d):
    return d.strftime("%Y-%m-%d") if isinstance(d, date) else str(d)[:10]


def add_days(s, n):
    return fmt_d(parse_d(s) + timedelta(days=n))


def utc_ms_date(ms):
    return datetime.fromtimestamp(ms / 1000.0, TZ_CN).strftime("%Y-%m-%d")


def is_weekday(s):
    return parse_d(s).weekday() < 5


# ---------------- 交易日历（真实K线日期集，可注册） ----------------
def set_calendar(dates):
    """注册真实交易日序列（指数K线日期，升序、YYYY-MM-DD）。"""
    global _CALENDAR, CALENDAR_SOURCE
    clean = sorted({fmt_d(d) for d in (dates or [])})
    if len(clean) >= 5:
        _CALENDAR = clean
        CALENDAR_SOURCE = "kline"


def calendar_dates():
    return _CALENDAR


def _trading_dates_between(start, end, cal):
    """在 [start,end] 内按 cal 或工作日近似生成交易日字符串（升序）。

    注意：K线日历只覆盖到**历史**交易日 —— 若查询窗口越过日历终点（如“今天→未来
    截止日”），日历内取完后，**日历终点之后**用工作日近似补齐（剔周末；法定节假日
    无法预知 → 近似），否则会误返回空列表（仪表盘“距截止日已无交易日”的根因）。
    """
    lo, hi = parse_d(start), parse_d(end)
    if hi < lo:
        return []
    if cal:
        out = []
        for d in cal:
            dd = parse_d(d)
            if dd < lo:
                continue
            if dd > hi:
                break
            out.append(d)
        cur = max(lo, parse_d(cal[-1]) + timedelta(days=1))
        seen = set(out)
        while cur <= hi:
            if cur.weekday() < 5:
                ds = fmt_d(cur)
                if ds not in seen:
                    out.append(ds)
                    seen.add(ds)
            cur += timedelta(days=1)
        return out
    out = []
    d = lo
    while d <= hi:
        if d.weekday() < 5:
            out.append(fmt_d(d))
        d += timedelta(days=1)
    return out


def trading_days_between(start, end):
    """[start,end] 内的交易日（已注册K线日历则用真实日历，否则工作日近似）。"""
    return _trading_dates_between(start, end, _CALENDAR)


def add_trading_days(s, n):
    """返回 s 之后第 n 个交易日；n<=0 时返回 s。

    已注册K线日历则按真实日历；日历终点之前不足 n 个时，超出部分回退为
    工作日近似（不影响“日历终点之后无节假日数据”的常态情况）。
    """
    if n <= 0:
        return fmt_d(parse_d(s))
    span = int(n * 7 + 12)
    end_s = add_days(s, span)
    days = _trading_dates_between(add_days(s, 1), end_s, _CALENDAR)
    if len(days) >= n:
        return days[n - 1]
    if _CALENDAR:
        # 日历不够长：先用日历里的日子，再用工作日补足差额
        rest = _trading_dates_between(
            add_days((days[-1] if days else s), 1), end_s, None)
        days = days + rest
    return days[n - 1] if len(days) >= n else (days[-1] if days else fmt_d(parse_d(s)))


def prev_trading_day(s):
    """返回 s 之前最近的一个交易日（不含 s）；已注册日历则用真实日历。"""
    if _CALENDAR:
        lo, hi = parse_d(s), parse_d(s)
        prev = None
        for d in _CALENDAR:
            dd = parse_d(d)
            if dd >= hi:
                break
            prev = d
        if prev:
            return prev
    # 无日历，或 s 早于日历起点：退回纯工作日近似（避免与日历混用导致空结果）
    days = _trading_dates_between(add_days(s, -10), add_days(s, -1), None)
    return days[-1] if days else fmt_d(parse_d(s))


def nav_value_date(submit_at):
    """场外基金“净值成交日”：当日 15:00 前提交按当日收盘净值，
    15:00 后提交顺延到下一交易日净值。submit_at 形如 'YYYY-MM-DD HH:MM:SS'。
    """
    s = str(submit_at or "")
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s[:10]  # 只有日期 → 当天净值
    if dt.strftime("%H:%M") < "15:00":
        return dt.strftime("%Y-%m-%d")
    return add_trading_days(dt.strftime("%Y-%m-%d"), 1)


# ---------------- 数字 ----------------
def r2(x):
    return round(float(x) + 1e-9, 2)


def r4(x):
    return round(float(x) + 1e-9, 4)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def money(x):
    return "¥{:,}".format(r2(x))


def pct2(x):
    return "{:+.2f}%".format(float(x) * 100)


# ---------------- HTTP ----------------
_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)
_AUTH_RE = re.compile(r"(Authorization:\s*Bearer\s+)[^\s]+", re.IGNORECASE)


def _redact(text):
    """把 URL/报文里的 token=xxx 与 Bearer 凭据打码，防止泄漏到日志/前端/数据库。"""
    s = str(text or "")
    s = _TOKEN_RE.sub(r"\1***", s)
    s = _AUTH_RE.sub(r"\1***", s)
    return s


def _err_tail(url, last):
    return _redact(str(url)[:90]) if last is None else \
        _redact(str(url)[:90]) + "：" + _redact(str(last))


def http_get(url, headers=None, timeout=12, tries=2):
    last = None
    for i in range(max(1, tries)):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "*/*")
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError,
                socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise DataError("请求失败 {}".format(_err_tail(url, last)))


def http_get_text(url, headers=None, timeout=12, tries=2):
    b = http_get(url, headers=headers, timeout=timeout, tries=tries)
    return b.decode("utf-8", errors="replace")


def http_get_json(url, headers=None, timeout=12, tries=2):
    txt = http_get_text(url, headers=headers, timeout=timeout, tries=tries)
    try:
        return json.loads(txt)
    except Exception as e:
        raise DataError("JSON 解析失败 {}: {}".format(
            _redact(str(url)[:90]), _redact(str(e)[:160])))


def http_post_json(url, payload, headers=None, timeout=60, tries=1):
    data = json.dumps(payload).encode("utf-8")
    last = None
    for i in range(max(1, tries)):
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("User-Agent", UA)
            req.add_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body)
        except Exception as e:
            last = e
            time.sleep(1.0)
    raise DataError("POST 失败 {}".format(_err_tail(url, last)))


# ---------------- JSON 文件 ----------------
def write_json_atomic(path, obj, indent=2, tries=4):
    """原子写 JSON：临时文件 + os.replace（旧内容不会出现“写一半”的损坏态）。

    与 save_json 的差别：不抛异常（返回 True/False），供计数这类「写失败也不能
    打断主流程」的场景使用；失败前重试，便于多进程/多线程并发写同一文件。
    返回 True=已落盘。
    """
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=indent),
                       "utf-8")
    except Exception:
        return False
    for i in range(max(1, int(tries))):
        try:
            os.replace(str(tmp), str(p))
            return True
        except OSError:
            time.sleep(0.15 * (i + 1))
        except Exception:
            return False
    return False


def load_json(path, default=None):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            return default
    return default


def save_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    # 多进程/多线程并发写同一缓存时，os.replace 可能被占用；短重试后仍失败再抛
    last = None
    for i in range(4):
        try:
            tmp.replace(p)
            return
        except OSError as e:
            last = e
            time.sleep(0.15 * (i + 1))
    raise last
