# -*- coding: utf-8 -*-
"""消息面（广域版）：多源拉取 A 股快讯 → 逐条词典情绪打分 → 板块/基金联动标注。

- 数据源链：东财快讯（公开 JSON）→ 新浪财经 7x24（公开 JSON）→ 财联社电报
  （动态签名 md5(sha1(排序参数串))，算法借鉴 Cailianpress-Feishu-Bot），
  可用源全部抓取后合并去重（广域收集），单条记录保留原始正文供筛选；
  三源皆失败才报错。
- 每条消息额外输出：
    auto_label / auto_strength —— 自动情绪（利好/利空/中性/无关 + 强度）；
    important —— 🔴重要电报（命中“重要/突发/涨停”等关键词；方向明确时强度×1.25）；
    sectors / funds —— 命中哪些板块主题、会联动哪几只候选基金（供人工筛选）；
    extra 学习词叠加：读取用户打标学到的新词词典参与自动打分。
- 兼容旧字段：ok/items/bull/bear/net/score/amplitude/date。
"""
import hashlib
import math
import re
import time
import urllib.parse
from datetime import datetime

from . import lexicon, screening, semantics, settings, strategy, util
from .util import DataError

EM_NEWS = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
           "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=80")
EM_HEADERS = {"Referer": "https://kuaixun.eastmoney.com/"}
SINA_FEED = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=100"
             "&zhibo_id=152&tag_id=0&dire=f&dpc=1")
# 财联社电报（借鉴 Cailianpress-Feishu-Bot：动态签名 = md5(sha1(按key排序的参数串))，
# 签名对“除 sign 外的全部 query 参数”计算，实测 /v1/roll/get_roll_list 通道可用）
CLS_ROLL = "https://www.cls.cn/v1/roll/get_roll_list"
CLS_HEADERS = {"Referer": "https://www.cls.cn/telegraph"}
# 重要电报关键词（同源参考：命中即标注 🔴，参与方向时情绪加权 ×1.25）
IMPORTANT_KWS = ["利好", "利空", "重要", "突发", "紧急", "涨停", "跌停",
                 "大跌", "暴涨", "突破", "降准", "降息", "国常会"]

_CACHE = {}  # 进程内 learned extra 缓存：{(mtime_str, size): extra}


def _em_news():
    js = util.http_get_json(EM_NEWS, headers=EM_HEADERS, timeout=18)
    lst = (((js.get("data") or {}).get("fastNewsList")) or [])
    if not lst:
        raise DataError("东财快讯为空")
    out = []
    for n in lst:
        title = (n.get("title") or "").strip()
        if not title:
            continue
        txt = (n.get("summary") or "").strip() or title
        out.append({"source": "东财快讯",
                    "time": (n.get("showTime") or "")[:16],
                    "title": title[:160], "text": txt})
    return out


def _sina_news():
    js = util.http_get_json(SINA_FEED, timeout=18)
    feed = (((js.get("result") or {}).get("data") or {}).get("feed") or {})
    lst = (feed.get("list")) or []
    if not lst:
        raise DataError("新浪7x24为空")
    out = []
    for n in lst:
        text = re.sub(r"<[^>]+>", "", n.get("rich_text") or "").strip()
        if not text:
            continue
        title = text[:120]
        out.append({"source": "新浪7x24",
                    "time": (n.get("create_time") or "")[:16],
                    "title": title, "text": text})
    return out


def _cls_sign(params):
    """财联社动态签名：md5(sha1(按 key 排序的参数串))（参考仓库同源算法）。"""
    s = "&".join("{}={}".format(k, params[k]) for k in sorted(params))
    sha1 = hashlib.sha1(s.encode("utf-8")).hexdigest()
    return hashlib.md5(sha1.encode("utf-8")).hexdigest()


def _cls_news(rn=50):
    rows, _ = cls_roll_page(None, rn=rn)   # 最新一页（历史回溯见 cls_history）
    if not rows:
        raise DataError("财联社电报为空")
    out = []
    for it in rows:
        t_str = ""
        if it.get("ctime"):
            try:
                t_str = datetime.fromtimestamp(
                    int(it["ctime"]), util.TZ_CN).strftime("%H:%M")
            except (TypeError, ValueError, OSError):
                t_str = ""
        out.append({"source": "财联社电报", "time": t_str,
                    "title": it.get("title") or "",
                    "text": it.get("text") or "", "url": it.get("url") or ""})
    return out


def _dedupe(items):
    seen, out = set(), []
    for it in items:
        key = re.sub(r"\s+", "", (it.get("title") or "")[:60])
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _collect_all():
    """多源合并抓取（东财→新浪→财联社电报）；任一源失败自动跳过，全部失败抛 DataError。"""
    got, errs = [], []
    for fn in (_em_news, _sina_news, _cls_news):
        try:
            got.extend(fn())
        except DataError as e:
            errs.append(str(e))
    items = _dedupe(got)
    if not items:
        raise DataError("全部消息源不可用：" + "；".join(errs) or "空")
    return items


def _item_id(it):
    key = "{}|{}|{}".format(it.get("source", ""), it.get("time", ""),
                            (it.get("title") or "")[:80])
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def _theme_links(txt):
    """标题/正文 → 命中的主题（复用 strategy 的统一主题分组）。"""
    found = set()
    for kws, theme in strategy.THEME_GROUPS:
        if any(k in txt for k in kws):
            found.add(theme)
    return sorted(found)


def _fund_links(themes, pool):
    """主题 → 备选池里可关联的基金代码（债基/宽基不作为攻击联动对象列出）。"""
    codes = []
    for f in pool:
        if f.get("kind") != "equity":
            continue
        th = strategy._theme_of(f.get("name") or "")
        if th and th in themes:
            codes.append(f["code"])
    return codes


def learned_extra():
    """读取“用户打标学习出的新词”→ {word:(dir,weight)}，带进程内缓存。"""
    try:
        p = util.data_file("screening.db")
        key = None
        if p.exists():
            st = p.stat()
            key = (st.st_mtime_ns, st.st_size)
        if key and _CACHE.get("key") == key and _CACHE.get("extra") is not None:
            return _CACHE["extra"]
        extra = screening.ScreeningStore().learned_extra()
        _CACHE.clear()
        _CACHE["key"] = key
        _CACHE["extra"] = extra
        return extra
    except Exception:
        return {}


DEFAULT_FOCUS_EVENTS = ["cbank_ease", "cbank_tight", "holder_flow", "geo_conflict"]


def net_scope(e, focus, dict_mode="off", include_other=False):
    """该条目是否计入当日净情绪（口径证据见 README「5.2 消息面口径治理」）。

    352 个交易日实测：词典兜底层 6.2 万条样本、日级 IC −0.070、方向增量 −1.4%，
    去掉后 IC 转正 +0.039；因此默认只让**有实证边际的事件类型**与 **LLM 逐条定级**
    参与净情绪，词典兜底默认不计入（`news_dict_mode=important_only/full` 可放宽）。
    """
    ev = e.get("event_type")
    if e.get("llm_label") and e.get("llm_contribute"):
        return True                       # 只有显式开启才让 LLM 方向进入净情绪
    if e.get("llm_dropped"):
        return False                      # LLM 判为中性/无关 → 一票否决
    if ev in focus:
        return True
    if ev == "dict":
        if dict_mode == "full":
            return True
        if dict_mode == "important_only" and e.get("important"):
            return True
        return False
    return bool(include_other)


def signed_strength(label, strength):
    """把（标签, 强度）折算成**带符号**贡献（实现与证据见 `lexicon.signed_strength`）。

    2026-09-11 修 BUG：本项目历史上并存两种口径——`semantics.infer_news()` 给
    `label=方向 + strength=正强度`，`lexicon.score_text()` 给带符号强度。旧
    `net_score` 直接求和，导致事件型利空（央行收紧/地缘冲突）被算成利多，
    净情绪十年从未为负。十年实测：修正后负值天数 0→1233、Spearman −0.008→+0.048、
    净情绪最高 20% 的次日上涨率 50.5%→55.9%（`data/news_direction_study.py`）。
    """
    return lexicon.signed_strength(label, strength)


def net_score(feed, mode="scaled", scale=240.0, focus=None, dict_mode="off",
              include_other=False):
    """逐条强度 → 当日净情绪 net ∈ [-8, 8]，返回 (net, raw, n_directional)。

    两个已修的结构性问题：
    1) **饱和**：旧口径 `clamp(Σ强度, ±8)` 实测天天顶格（15/15 天）→ 消息分恒定 →
       日级零区分度；现按刻度换算（mode="scaled"），中位日 ≈2 个 net 单位；
    2) **词典稀释**：关键词词典把“解除合同/出货量下滑”判成利好，6.2 万条样本
       日级 IC −0.070；现按 `focus` + `dict_mode` 只保留有实证边际的来源。
    3) **方向丢失**（2026-09-11 修）：改为 `signed_strength()` 按 label 取符号，
       修复“利空被算成利多”的系统性偏多（见 signed_strength 的 docstring 证据）。
    mode="sum" 复现旧口径；focus=None 时不做来源过滤（回测/演示口径不变）。
    """
    raw, n_dir = 0.0, 0
    for e in feed or []:
        lab = e.get("auto_label")
        if lab not in ("bull", "bear"):
            continue
        if focus is not None and not net_scope(e, focus, dict_mode, include_other):
            continue
        n_dir += 1
        raw += signed_strength(lab, e.get("auto_strength"))
    if mode == "sum" or not scale:
        return max(-8.0, min(8.0, raw)), raw, n_dir
    return max(-8.0, min(8.0, raw / float(scale))), raw, n_dir


def feed_from_db_rows(rows):
    """历史消息库行（`clsdb.scores`：label/strength/event）→ `net_score` 需要的 feed 键。

    键名口径修正：`clsdb.scored_items()` 返回 `label/strength/event`，而
    `net_score`/`net_scope` 读 `auto_label/auto_strength/event_type`。
    把 DB 行直接喂进去会静默得到**恒 0** 的净情绪（2026-09-11 定位到上一轮
    5760 组扫描的“消息权重”维度因此全程喂 0，见 data/news_direction_study.py 口径 A）。
    """
    out = []
    for r in rows or []:
        lab = r.get("label") or r.get("auto_label") or ""
        if not lab:
            continue
        out.append({"auto_label": lab,
                    "auto_strength": int(r.get("strength")
                                          if r.get("strength") is not None
                                          else (r.get("auto_strength") or 0)),
                    "event_type": r.get("event") or r.get("event_type") or "",
                    "important": int(r.get("important") or 0),
                    "title": r.get("title") or ""})
    return out


def daily_score_map(cfg=None, use_cache=True, since=None):
    """十年消息库 → {date: {"net","score","raw","n"}}（与线上同日**同口径**）。

    用途：回测/研究（引擎回放、组合试错）需要“当天消息分”，但线上 `load_news()`
    是抓“今天”的。本函数从 SQLite 消息库按日聚合，口径 = `fetch_news` 里的
    net_mode / net_scale / focus / dict_mode / include_other + amplitude。

    结果按「DB 文件 mtime+size + 口径」签名缓存到 data/cache/news_daily_scores.json，
    重复研究不再扫描 113 万行（首次约 30 秒）。
    """
    st = (cfg or {}).get("strategy") or {}
    amp = max(1, int(st.get("news_amp") or 8))
    mode = str(st.get("news_net_mode") or "scaled")
    scale = float(st.get("news_net_scale") or 240.0)
    focus = list(st.get("news_focus_events") or DEFAULT_FOCUS_EVENTS)
    dict_mode = str(st.get("news_dict_mode") or "off")
    include_other = bool(st.get("news_include_other_events", False))
    from . import clsdb
    dbp = clsdb.db_path()
    sig = None
    if dbp.exists():
        s = dbp.stat()
        sig = "{}-{}-{}".format(int(s.st_mtime), s.st_size, since or "")
    stamp = {"mode": mode, "scale": scale, "focus": sorted(focus),
             "dict": dict_mode, "other": include_other, "amp": amp}
    path = util.cache_file("news_daily_scores.json")
    if use_cache and sig:
        cached = util.load_json(path, {}) or {}
        if cached.get("db_sig") == sig and cached.get("stamp") == stamp:
            return cached.get("days") or {}
    rows = clsdb.scored_items(since=since)
    per_day = {}
    for r in rows:
        ct = r.get("ctime")
        if not ct:
            continue
        d = datetime.fromtimestamp(int(ct), util.TZ_CN).strftime("%Y-%m-%d")
        per_day.setdefault(d, []).append(r)
    days = {}
    for d, rs in per_day.items():
        feed = feed_from_db_rows(rs)
        net, raw, n = net_score(feed, mode=mode, scale=scale, focus=focus,
                                dict_mode=dict_mode, include_other=include_other)
        days[d] = {"net": round(net, 3), "raw": round(raw, 1), "n": n,
                   "score": int(max(-100, min(100, net * amp)))}
    if use_cache and sig:
        util.save_json(path, {"db_sig": sig, "stamp": stamp, "updated":
                              util.now_iso(), "days": days})
    return days



def score_item(it, extra=None, pool=None):
    """单条快讯 → 已打标条目（与 fetch_news 完全同口径，供历史校准复用）。

    返回 None 表示被“相关性预筛”淘汰（与金融行情明显无关的噪音）。
    """
    txt = (it.get("title") or "") + " " + (it.get("text") or "")
    themes = _theme_links(txt)
    sc = lexicon.score_text(txt, extra=extra or {})
    # 事件语义推断优先（回答“这条消息意味着什么”），词典作兜底
    sem = semantics.infer_news(it.get("title") or "", it.get("text") or "",
                               dict_score=sc)
    ev = sem.get("event") or "none"
    if ev == "none":
        if not sc.get("relevance") and not themes and sc.get("strength") == 0:
            return None
        label, strength = sc["label"], sc["strength"]
        reason = sem.get("reason") or ""
    else:
        label, strength = sem["label"], sem["strength"]
        reason = sem.get("reason") or ""
    # 重要电报分级（参考仓库 RED_KEYWORDS 思路）：命中关键词标 🔴，
    # 方向明确者情绪加权 ×1.25（封顶 ±8）
    important = 1 if any(k in txt for k in IMPORTANT_KWS) else 0
    if important and label in ("bull", "bear"):
        strength = int(util.clamp(round(strength * 1.25), -8, 8))
    return {
        "id": _item_id(it),
        "source": it.get("source", ""),
        "time": it.get("time", ""),
        "title": (it.get("title") or "").strip(),
        "text": (it.get("text") or "").strip(),
        "url": it.get("url", ""),
        "auto_label": label,
        "auto_strength": strength,
        "auto_net": int(strength) if ev != "none" else sc.get("net", 0),
        "event_type": ev if ev != "none" else "dict",
        "auto_reason": reason,
        "important": important,
        "sectors": themes,
        "funds": _fund_links(themes, pool or []),
        "user_label": "",
        "user_strength": 0,
    }


def cls_roll_page(last_time=None, rn=50, timeout=18):
    """财联社电报单页 → (items, oldest_ctime)。

    last_time 为**时间游标**：带 refresh_type=1 时返回早于该时刻的电报，
    因此可逐页往回翻历史（实测 1 / 30 / 90 / 365 天前均可取，见 README「四、数据底座」）。
    注意：`/nodeapi/telegraphList` 该通道已 404 下线；rn 上限 50（更大返回空）。
    """
    tz = getattr(util, "TZ_CN", None)
    params = {"app": "CailianpressWeb", "os": "web", "rn": str(int(rn)),
              "sv": "7.7.5"}
    if last_time:
        params["refresh_type"] = "1"
        params["last_time"] = str(int(last_time))
    params["sign"] = _cls_sign(params)
    url = CLS_ROLL + "?" + urllib.parse.urlencode(params)
    js = util.http_get_json(url, headers=CLS_HEADERS, timeout=timeout)
    if js.get("errno") not in (0, None):
        raise DataError("财联社 errno={}".format(js.get("errno")))
    roll = ((js.get("data") or {}).get("roll_data")) or []
    out, ctimes = [], []
    for n in roll:
        if n.get("is_ad"):
            continue
        content = re.sub(r"\s+", " ",
                         str(n.get("content") or n.get("brief") or "").strip())
        if not content:
            continue
        title = str(n.get("title") or "").strip()
        if not title:
            m = re.match(r"^\s*【(.+?)】", content)
            title = m.group(1) if m else content[:60]
        ctime = None
        try:
            ctime = int(n.get("ctime"))
        except (TypeError, ValueError):
            ctime = None
        t_str = ""
        if ctime:
            ctimes.append(ctime)
            try:
                t_str = datetime.fromtimestamp(ctime, util.TZ_CN) \
                    .strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError, OSError):
                t_str = ""
        item_id = str(n.get("id") or "")
        out.append({"id": item_id, "ctime": ctime, "source": "财联社电报",
                    "time": t_str, "title": title[:160], "text": content,
                    "url": ("https://www.cls.cn/detail/{}".format(item_id)
                            if item_id else "")})
    return out, (min(ctimes) if ctimes else None)


def cls_history(days=10, max_pages=600, delay=0.35, progress=None,
                use_cache=True, force=False, until=None, cache_name=None):
    """财联社电报历史（逐页回溯 + 磁盘缓存可断点续跑）。

    until：从该时刻（YYYY-MM-DD 或时间戳）开始往回走，用于**分段并行抓取**；
    cache_name：自定义缓存文件名（并行 worker 各写各的，最后 merge_history_caches）。
    缓存按电报 id 去重，保留 60 天（分段并行时按窗口保留）。
    """
    path = util.cache_file(cache_name or "cls_history.json")
    store = util.load_json(path, {"items": {}}) if use_cache else {"items": {}}
    items = dict(store.get("items") or {})

    def _ts(x):
        if isinstance(x, (int, float)):
            return int(x)
        s = str(x)[:19]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(datetime.strptime(s, fmt).replace(
                    tzinfo=util.TZ_CN).timestamp())
            except ValueError:
                continue
        raise DataError("时间格式无法解析: {}".format(x))

    end_ts = _ts(until) if until else int(util.now_dt().timestamp())
    t_cut = end_ts - max(1, int(days)) * 86400
    t_keep = t_cut                      # 缓存只保留本 worker 的窗口，便于分段并行
    covered = any((it.get("ctime") or 0) and it["ctime"] <= t_cut
                  for it in items.values())
    if covered and not force:
        out = [it for it in items.values()
               if (it.get("ctime") or 0) >= t_cut]
        out.sort(key=lambda x: x.get("ctime") or 0)
        return {"items": out, "pages": 0, "new": 0,
                "cache_total": len(items), "from_cache": True}
    cursor = end_ts
    # 续跑优化：缓存里已有更早的电报时，从最早那条继续往回走，
    # 不重复抓已覆盖的近期页面（否则每次续跑都白跑几十页）
    _ct = [v.get("ctime") or 0 for v in items.values()]
    _ct = [c for c in _ct if c]
    if _ct and min(_ct) > t_cut:
        cursor = min(_ct)
    pages, added, errors = 0, 0, []
    while pages < int(max_pages):
        try:
            roll, oldest = cls_roll_page(cursor)
        except (DataError, OSError) as e:
            errors.append(str(e))
            break
        if not roll:
            break
        for it in roll:
            key = it.get("id") or _item_id(it)
            if key not in items:
                added += 1
            items[key] = it
        pages += 1
        if progress:
            progress(pages, oldest)
        if pages % 10 == 0:
            util.save_json(path, {"updated": util.now_iso(), "items": items})
        if not oldest or oldest <= t_cut or oldest >= cursor:
            break
        cursor = oldest
        time.sleep(max(0.0, float(delay)))
    items = {k: v for k, v in items.items() if (v.get("ctime") or 0) >= t_keep}
    util.save_json(path, {"updated": util.now_iso(), "items": items})
    out = [it for it in items.values() if (it.get("ctime") or 0) >= t_cut]
    out.sort(key=lambda x: x.get("ctime") or 0)
    return {"items": out, "pages": pages, "new": added,
            "cache_total": len(items), "errors": errors, "from_cache": False}


def merge_history_caches(names, into="cls_history.json"):
    """把多个分段并行 worker 的缓存合并进主缓存（按 id 去重）。返回合并后条数。"""
    target = util.load_json(util.cache_file(into), {"items": {}})
    items = dict(target.get("items") or {})
    before = len(items)
    for name in names:
        try:
            part = util.load_json(util.cache_file(name), {"items": {}})
        except Exception:
            continue
        for k, v in (part.get("items") or {}).items():
            cur = items.get(k)
            if cur is None or (v.get("ctime") or 0):
                items[k] = v
    util.save_json(util.cache_file(into),
                   {"updated": util.now_iso(), "items": items})
    return {"before": before, "after": len(items), "added": len(items) - before}


def cls_fetch_to_db(days=1460, until=None, max_pages=20000, delay=0.3,
                    progress=None, batch_pages=20, jobs=None):
    """按时间游标回溯，把电报写入 SQLite（**可多段并行**，WAL 保证并发写安全）。

    until：从该时刻（YYYY-MM-DD 或时间戳）开始往回走（= 本段命名的起点）；
    每 batch_pages 页提交一次，进程/线程被中断也不丢已抓内容。
    返回 {"pages","added","total","from","to"}

    ``jobs``（2026-09-12 新增）：并行度，默认取 ``data/parallel.py`` 的口径
    （CPU 核数，``FUNDAI_JOBS`` 可覆盖，1=串行）。回填是**纯 I/O 等待**（翻页 HTTP），
    因此把 [end_ts−days, end_ts] 均分成 jobs 段、每段一个线程各自走游标；
    写库用各自连接，靠 WAL 串行提交，结果与串行版**同口径去重后完全相同**
    （telegrams 以 id 为主键、INSERT OR IGNORE，重复抓取不会重复计条）。
    """
    from . import clsdb

    def _ts(x):
        if isinstance(x, (int, float)):
            return int(x)
        s = str(x)[:19]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(datetime.strptime(s, fmt).replace(
                    tzinfo=util.TZ_CN).timestamp())
            except ValueError:
                continue
        raise DataError("时间格式无法解析: {}".format(x))

    end_ts = _ts(until) if until else int(util.now_dt().timestamp())
    span = max(1, int(days)) * 86400
    t_cut = end_ts - span
    if jobs is None:
        try:
            jobs = _parallel_workers()
        except Exception:
            jobs = 1
    try:
        jobs = max(1, int(jobs))
    except (TypeError, ValueError):
        jobs = 1
    n_seg = max(1, min(jobs, max(1, int(days))))     # 每段至少一天
    if n_seg > 1:
        seg = int(math.ceil(span / float(n_seg)))
        tasks = []
        for i in range(n_seg):
            seg_end = end_ts - i * seg
            seg_days = min(int(days), int(math.ceil(seg / 86400.0)))
            if seg_end <= t_cut:
                break
            tasks.append({"end": seg_end, "days": max(1, seg_days),
                          "max_pages": max(1, int(math.ceil(
                              int(max_pages) / float(n_seg))) + 20),
                          "delay": delay, "tag": "第{}/{}段".format(i + 1, n_seg)})
        if len(tasks) > 1:
            return _cls_fetch_parallel(tasks, progress, batch_pages, t_cut)
    return _cls_fetch_segment(end_ts, t_cut, max_pages, delay, progress,
                              batch_pages, tag=None)


def _cls_fetch_parallel(tasks, progress, batch_pages, t_cut):
    """多段并行抓取：每段一个线程（I/O 等待为主），写库交给 WAL 串行提交。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    results = [None] * len(tasks)

    def _run(idx, t):
        return idx, _cls_fetch_segment(t["end"], t_cut, t["max_pages"],
                                       t["delay"], progress, batch_pages,
                                       tag=t["tag"])

    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        for idx, res in ex.map(lambda pair: _run(*pair), list(enumerate(tasks))):
            results[idx] = res
    from . import clsdb
    conn = clsdb.connect()
    try:
        total = conn.execute("SELECT count(*) FROM telegrams").fetchone()[0]
    finally:
        conn.close()
    pages = sum(r["pages"] for r in results if r)
    added = sum(r["added"] for r in results if r)
    frm = min((r["from"] for r in results if r), default=None)
    to = max((r["to"] for r in results if r), default=None)
    return {"pages": pages, "added": added, "total": total,
            "from": frm, "to": to, "jobs": len(results)}


def _cls_fetch_segment(end_ts, t_cut, max_pages, delay, progress, batch_pages,
                       tag=None):
    """单段游标回溯（串行版就是它；并行版每段一个线程）。"""
    from . import clsdb
    conn = clsdb.connect()
    cursor = end_ts
    pages, added, buf = 0, 0, []
    from_ts, to_ts = end_ts, end_ts
    try:
        while pages < int(max_pages):
            try:
                roll, oldest = cls_roll_page(cursor)
            except (DataError, OSError):
                break
            if not roll:
                break
            for it in roll:
                ct = int(it.get("ctime") or 0)
                if ct:
                    from_ts = min(from_ts, ct)
                    to_ts = max(to_ts, ct)
                buf.append({"id": it.get("id"), "ctime": ct,
                            "date": datetime.fromtimestamp(ct, util.TZ_CN)
                            .strftime("%Y-%m-%d") if ct else "",
                            "title": it.get("title") or "",
                            "text": it.get("text") or "", "url": it.get("url") or ""})
            pages += 1
            if pages % int(batch_pages) == 0:
                added += clsdb.insert_telegrams(buf, conn=conn)
                buf = []
                if progress:
                    _progress_call(progress, pages, oldest, tag)
            if not oldest or oldest <= t_cut or oldest >= cursor:
                break
            cursor = oldest
            time.sleep(max(0.0, float(delay)))
        added += clsdb.insert_telegrams(buf, conn=conn)
    finally:
        total = conn.execute("SELECT count(*) FROM telegrams").fetchone()[0]
        conn.close()
    return {"pages": pages, "added": added, "total": total,
            "from": datetime.fromtimestamp(from_ts, util.TZ_CN).strftime("%Y-%m-%d"),
            "to": datetime.fromtimestamp(to_ts, util.TZ_CN).strftime("%Y-%m-%d")}


def _progress_call(progress, pages, oldest, tag):
    """进度回调兼容：老的两参签名照旧，能收 tag 的新签名多拿一个分段标签。"""
    try:
        return progress(pages, oldest, tag)
    except TypeError:
        return progress(pages, oldest)


def score_db(limit=None, progress=None, batch=2000, jobs=None):
    """把 DB 里未打分的电报按**线上同口径**打分并写入 scores 表（增量）。

    ``jobs``：并行度（默认取 ``data/parallel.py`` 的口径：CPU 核数，``FUNDAI_JOBS``
    环境变量可覆盖，1 = 串行）。评分是纯计算（正则+词典+事件语义推断），
    **结果与串行逐位一致**，只是把 worker 铺满 CPU：

    * 读任务与**写库留在主进程串行**（SQLite 单写者，多进程并发写会锁表/计数漂移）；
    * 每个 worker 进程自己加载一次 learned_extra（不把大对象当任务参数传）；
    * 进程池不可用时（`python -c`/stdin 调用）自动退回串行并提示，绝不"默默算错"。

    ``jobs<=1`` 时走原来的纯串行路径，行为与历史版本完全一致。
    """
    from . import clsdb
    todo = clsdb.unscored(limit=limit)
    total = len(todo)
    if jobs is None:
        try:
            jobs = _parallel_workers()
        except Exception:
            jobs = 1
    try:
        jobs = max(1, int(jobs))
    except (TypeError, ValueError):
        jobs = 1
    use_parallel = jobs > 1 and total >= 200 and _can_spawn()
    conn = clsdb.connect()
    done, buf = 0, []
    try:
        if use_parallel:
            results = _pmap_score(todo, jobs)
        else:
            extra = learned_extra()
            results = (_score_one_item(it, extra) for it in todo)
        for row in results:
            buf.append(row)
            done += 1
            if len(buf) >= int(batch):
                clsdb.insert_scores(buf, conn=conn)
                buf = []
                if progress:
                    progress(done, total)
        if buf:
            clsdb.insert_scores(buf, conn=conn)
    finally:
        conn.close()
    return {"scored": done, "pending": max(0, total - done),
            "jobs": (jobs if use_parallel else 1), "total": total}


def _load_parallel():
    """加载并行底座 `data/parallel.py`（研究脚本共用；不在包内，需按路径补 sys.path）。

    并行纪律见该模块 docstring：写库留在主进程、worker 自己读缓存、过轻的任务别并行。
    """
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for p in (root, os.path.join(root, "data")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import parallel
    return parallel


def _parallel_workers():
    """复用 data/parallel.py 的并行度口径（CPU 核数 / FUNDAI_JOBS）。"""
    return _load_parallel().workers()


def _can_spawn():
    """能否用进程池：主模块有路径 **且** 实测能真的建起来（受限环境会拒绝建管道）。"""
    try:
        return _load_parallel().probe_process_pool()
    except Exception:
        return False


def _score_row(it, extra):
    """单条电报 → scores 行（与 score_db 串行路径同一口径）。"""
    e = score_item(it, extra=extra, pool=[])
    if e is None:
        return {"id": it["id"], "ctime": it["ctime"], "event": "none",
                "label": "irrelevant", "strength": 0, "important": 0,
                "scope": "none", "title": (it.get("title") or "")[:80]}
    return {"id": it["id"], "ctime": it["ctime"], "event": e["event_type"],
            "label": e["auto_label"], "strength": int(e["auto_strength"] or 0),
            "important": e["important"],
            "scope": "market" if e["event_type"] in (
                "cbank_ease", "cbank_tight", "market_policy",
                "geo_conflict", "eco_data") else "single",
            "title": (e["title"] or "")[:80]}


_EXTRA_CACHE = {}


def _score_worker_init():
    """进程池 worker 只在启动时加载一次词典/学习词（避免每条消息重读磁盘）。"""
    _EXTRA_CACHE["extra"] = learned_extra()


def _score_one_item(it, extra=None):
    return _score_row(it, extra if extra is not None
                      else _EXTRA_CACHE.get("extra") or learned_extra())


def _pmap_score(todo, jobs):
    """进程池评分：保留顺序，单条失败降级为中性行（绝不因一条脏数据中断整批）。

    大列表按块下发（chunksize），避免 28 万条任务逐条 pickle 的通信开销。
    """
    import os
    import sys
    parallel = _load_parallel()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    chunk = max(200, min(2000, len(todo) // (max(1, jobs) * 8) or 200))
    chunks = [todo[i:i + chunk] for i in range(0, len(todo), chunk)]

    def _one(part):
        return [_score_one_item(it) for it in part]

    parts = parallel.pmap(_one, chunks, init=_score_worker_init, jobs=jobs,
                          chunksize=1)
    out = []
    for part in parts:
        out.extend(part or [])
    return out


def fetch_news(amplitude=8, cfg=None, pool=None):
    """返回 {ok, items, feed, bull, bear, net, score, amplitude}。

    feed 为全量已打标消息（供人工筛选界面）；bull/bear 为 Top 摘要（供研判文案）。
    打标前清洗：相关性预筛（与金融行情明显无关的快讯不进入待打标队列，
    避免用户给“战事/外交/生活”噪音打标）；最终去重由入库层的近 7 天归一化标题
    过滤完成（screening.ingest_feed skip_recent）。
    失败抛 DataError。
    """
    items = _collect_all()
    extra = learned_extra()
    pool_list = pool if pool is not None else settings.pool_of(cfg) if cfg else []
    raw_n = len(items)
    feed = []
    screened_out = 0
    for it in items:
        entry = score_item(it, extra=extra, pool=pool_list)
        if entry is None:
            screened_out += 1
            continue
        feed.append(entry)
    # 事件命中率校准（可选，默认关闭）：按历史实测命中率微调各事件类型强度
    # （建议倍数经 n/(n+20) 收缩，来自 calib_history.calibrate；关闭时口径与旧版逐位一致）
    if (cfg or {}).get("strategy", {}).get("event_calibration"):
        try:
            from . import calib_history as _cal
            _c = _cal.load_calibration()
            if _c.get("suggest_multipliers"):
                for e in feed:
                    if e["auto_label"] not in ("bull", "bear"):
                        continue
                    m = _cal.multiplier_for(_c, e["event_type"], e["auto_label"])
                    if m != 1.0 and e["auto_strength"]:
                        e["auto_strength"] = int(util.clamp(
                            round(e["auto_strength"] * m), -8, 8))
                        e["calib_mult"] = m
        except Exception:
            pass
    st_cfg = (cfg or {}).get("strategy", {}) or {}
    # LLM 逐条定级（替代粗糙关键词）：只处理“词典兜底”桶、按天限量、带磁盘缓存、
    # 失败自动回退词典标签（证据见 README「5.2 消息面口径治理」）
    llm_stat = {}
    if st_cfg.get("news_llm_enable", True):
        try:
            from . import llm_news
            cap = int(st_cfg.get("news_llm_cap", 120) or 0)
            sel = llm_news.label_items(feed, cfg, max_items=cap)
            if sel.get("labels"):
                done = llm_news.apply_labels(
                    feed, sel["labels"],
                    contribute=bool(st_cfg.get("news_llm_contribute", False)))
                llm_stat = {"labeled": done, "calls": sel.get("calls", 0),
                            "from_cache": sel.get("from_cache", 0),
                            "dropped": sum(1 for e in feed if e.get("llm_dropped")),
                            "contribute": bool(st_cfg.get("news_llm_contribute",
                                                          False))}
        except Exception as exc:
            llm_stat = {"error": str(exc)[:120]}
    # Top 摘要（保持旧接口 bull/bear 为 dict 列表：time/title/summary/important）
    BULLS = ("bull", "big_bull")
    BEARS = ("bear", "big_bear")
    bull = [{"time": e["time"], "title": e["title"][:140],
             "summary": e["text"][:300], "important": e["important"]}
            for e in feed if e["auto_label"] in BULLS]
    bear = [{"time": e["time"], "title": e["title"][:140],
             "summary": e["text"][:300], "important": e["important"]}
            for e in feed if e["auto_label"] in BEARS]
    st_cfg = (cfg or {}).get("strategy", {}) or {}
    net_mode = str(st_cfg.get("news_net_mode") or "scaled")
    net_scale = float(st_cfg.get("news_net_scale") or 240.0)
    focus = list(st_cfg.get("news_focus_events") or DEFAULT_FOCUS_EVENTS)
    dict_mode = str(st_cfg.get("news_dict_mode") or "off")
    include_other = bool(st_cfg.get("news_include_other_events", False))
    net, net_raw, n_dir = net_score(feed, mode=net_mode, scale=net_scale,
                                    focus=focus, dict_mode=dict_mode,
                                    include_other=include_other)
    score = int(max(-100, min(100, net * max(1, int(amplitude)))))
    bull.sort(key=lambda x: (x["important"], x.get("time", "")), reverse=True)
    bear.sort(key=lambda x: (x["important"], x.get("time", "")), reverse=True)
    return {"ok": True, "items": len(feed), "raw": raw_n,
            "screened_out": screened_out, "feed": feed,
            "bull": bull[:5], "bear": bear[:5],
            "net": round(net, 3), "net_raw": round(net_raw, 1),
            "net_mode": net_mode, "net_scale": net_scale,
            "n_directional": n_dir,
            "scope": {"focus_events": focus, "dict_mode": dict_mode,
                      "include_other": include_other,
                      "n_in_scope": n_dir, "llm": llm_stat},
            "score": score, "amplitude": int(amplitude)}


def news_cache_file(date_s):
    return util.cache_file("news_{}.json".format(date_s))


NEWS_SCHEMA = 3  # 缓存结构版本：>=2 带全量 feed；3 起接入财联社电报+重要度字段


def load_news(date_s, amplitude=8, force=False, cfg=None):
    """带文件缓存的当日消息面（默认每天只在线抓一次；force=True 强制重抓，
    让新学到的词参与当天自动打分，并覆盖缓存）。

    旧版本缓存（无 feed 字段 / schema<2）视为无效：在线重抓后写新结构；
    若在线失败则回退旧缓存（保留 bull/bear 供研判文案，但无 feed 无法打标入库）。
    """
    path = news_cache_file(date_s)
    amp = int(amplitude)
    cached = util.load_json(path)
    feed_ok = isinstance((cached or {}).get("feed"), list)
    if not force and cached and cached.get("date") == date_s and \
            cached.get("ok") and cached.get("amplitude") == amp and \
            cached.get("schema", 1) >= NEWS_SCHEMA and feed_ok:
        return cached
    try:
        res = fetch_news(amplitude=amp, cfg=cfg)
    except DataError:
        if cached and cached.get("date") == date_s:
            return cached
        return {"ok": False, "message": "消息面获取失败（多源均不可用）",
                "items": 0, "feed": [], "bull": [], "bear": [],
                "net": 0, "score": 0, "amplitude": amp}
    res["date"] = date_s
    res["schema"] = NEWS_SCHEMA
    util.save_json(path, res)
    return res
