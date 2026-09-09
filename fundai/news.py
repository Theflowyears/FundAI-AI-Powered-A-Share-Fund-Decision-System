# -*- coding: utf-8 -*-
"""消息面（广域版）：多源拉取 A 股快讯 → 逐条词典情绪打分 → 板块/基金联动标注。

- 数据源链：东财快讯（公开 JSON）→ 新浪财经 7x24（公开 JSON），都失败才报错；
  两个源都可用时合并去重（广域收集），单条记录保留原始正文供筛选。
- 每条消息额外输出：
    auto_label / auto_strength —— 自动情绪（利好/利空/中性/无关 + 强度）；
    sectors / funds —— 命中哪些板块主题、会联动哪几只候选基金（供人工筛选）；
    extra 学习词叠加：读取用户打标学到的新词词典参与自动打分。
- 兼容旧字段：ok/items/bull/bear/net/score/amplitude/date。
"""
import hashlib
import re

from . import lexicon, screening, settings, strategy, util
from .util import DataError

EM_NEWS = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
           "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=80")
EM_HEADERS = {"Referer": "https://kuaixun.eastmoney.com/"}
SINA_FEED = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=100"
             "&zhibo_id=152&tag_id=0&dire=f&dpc=1")

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
    """多源合并抓取；任一源失败自动跳过，全部失败抛 DataError。"""
    got, errs = [], []
    for fn in (_em_news, _sina_news):
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
        from pathlib import Path
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


def fetch_news(amplitude=8, cfg=None, pool=None):
    """返回 {ok, items, feed, bull, bear, net, score, amplitude}。

    feed 为全量已打标消息（供人工筛选界面）；bull/bear 为 Top 摘要（供研判文案）。
    失败抛 DataError。
    """
    items = _collect_all()
    extra = learned_extra()
    pool_list = pool if pool is not None else settings.pool_of(cfg) if cfg else []
    feed = []
    for it in items:
        txt = (it.get("title") or "") + " " + (it.get("text") or "")
        sc = lexicon.score_text(txt, extra=extra)
        themes = _theme_links(txt)
        entry = {
            "id": _item_id(it),
            "source": it.get("source", ""),
            "time": it.get("time", ""),
            "title": (it.get("title") or "").strip(),
            "text": (it.get("text") or "").strip(),
            "url": "",
            "auto_label": sc["label"],
            "auto_strength": sc["strength"],
            "auto_net": sc["net"],
            "sectors": themes,
            "funds": _fund_links(themes, pool_list),
            "user_label": "",
            "user_strength": 0,
        }
        feed.append(entry)
    # Top 摘要（保持旧接口 bull/bear 为 dict 列表：time/title/summary）
    bull = [{"time": e["time"], "title": e["title"][:140],
             "summary": e["text"][:300]}
            for e in feed if e["auto_label"] == "bull"]
    bear = [{"time": e["time"], "title": e["title"][:140],
             "summary": e["text"][:300]}
            for e in feed if e["auto_label"] == "bear"]
    net = sum(e["auto_strength"] for e in feed
              if e["auto_label"] in ("bull", "bear"))
    net = max(-8, min(8, net))
    score = int(max(-100, min(100, net * max(1, int(amplitude)))))
    bull.sort(key=lambda x: x.get("time", ""), reverse=True)
    bear.sort(key=lambda x: x.get("time", ""), reverse=True)
    return {"ok": True, "items": len(feed), "feed": feed,
            "bull": bull[:5], "bear": bear[:5],
            "net": net, "score": score, "amplitude": int(amplitude)}


def news_cache_file(date_s):
    return util.cache_file("news_{}.json".format(date_s))


def load_news(date_s, amplitude=8, force=False, cfg=None):
    """带文件缓存的当日消息面（默认每天只在线抓一次；force=True 强制重抓，
    让新学到的词参与当天自动打分，并覆盖缓存）。"""
    path = news_cache_file(date_s)
    amp = int(amplitude)
    cached = util.load_json(path)
    if not force and cached and cached.get("date") == date_s and \
            cached.get("ok") and cached.get("amplitude") == amp:
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
    util.save_json(path, res)
    return res
