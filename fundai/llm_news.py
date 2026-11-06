# -*- coding: utf-8 -*-
"""用大语言模型替代粗糙的关键词词典：对“词典兜底”快讯做语义定级。

**为什么做**（352 个交易日回测证据，见 README「5.2 消息面口径治理」）：
- 词典层样本 6.2 万条，日级 IC −0.070、方向增量 −1.4%；**去掉词典层后 IC 转正 +0.039**；
- 乌龙类型靠“缩阈值”筛不掉：把“解除 4.03 亿元合同”判成利好(+4)、
  “腕戴设备出货量同比下滑”判成利好(+5)、“连板股晋级率仅20%”判成利空(−8)；
- 精确度上：判利多但含明显负面词 4.0%，判利空但含明显正面词 7.5%，
  且利多:利空 = 44260:17994（系统性偏多）。

**怎么用**（2026-09-16 按用户要求改为「AI 为主」）：
- LLM 判断**直接计入消息分**（`contribute=True` 默认），不再只做降噪一票否决——
  实测 `news_llm_contribute=False` 时 AI 判了什么对评分毫无影响，等于白花钱；
- 覆盖面从"词典兜底桶"扩大到**整条流**，按 `🔴重要 → |词典强度| → 主题命中等`
  排序取前 `news_llm_cap` 条（其余走本地 `context.assess()` 的上下文兜底）；
- **把市场状态喂给模型**：同一条"降准"在"当日已大涨"与"连续下跌"下的方向不同，
  这就是用户说的"利空利多每天都在变"——所以 prompt 里带当日涨跌与状态标签，
  且缓存按 (prompt 版本, 市场状态) 分组，行情变了会重判而不是复用旧结论；
- 磁盘缓存（同一 id + 同一状态永不重复计费），任何失败都自动回退本地兜底，绝不断主流程。

标签 → 强度：big_bull/big_bear = ±6，bull/bear = ±2，neutral/irrelevant = 0。
"""
import json

from . import util

CACHE = "llm_news_labels.json"
# 提示词版本：改了 SYSTEM 或输出口径就 +1，使旧缓存自动失效（避免"口径变了还在用旧判断"）
PROMPT_VERSION = 3
LABEL_STRENGTH = {"big_bull": 6, "bull": 2, "neutral": 0, "bear": -2,
                  "big_bear": -6, "irrelevant": 0}
VALID = set(LABEL_STRENGTH.keys())
MAX_TEXT = 220
MAX_TITLE = 90

SYSTEM = (
    "你是A股快讯标注员。任务：判断每条快讯对**次日沪深300指数**的方向影响，"
    "不是判断它对某只个股是否是好消息。判定规则：\n"
    "1) 宏观/政策宽松（降准降息、财政发力、稳增长、资本市场支持）= 利多；\n"
    "2) 收紧/监管处罚/风险暴露（央行收紧、立案调查、违约爆雷、地缘冲突升级）= 利空；\n"
    "3) 个股或行业事件（业绩、订单、减持增持、合同、产能）只有在**能代表整体市场情绪"
    "或涉及权重板块**时才给方向（bull/bear）；否则 neutral；\n"
    "4) “利好出尽/预期兑现/数据下滑/合同解除/减持/质押/解禁/亏损/下调”等按字面负面处理，"
    "不要因为出现‘增长、中标、项目’等词就判利好；\n"
    "5) 与A股行情无关（海外政治、体育、娱乐、天气、纯科普）= irrelevant；\n"
    "6) 程度：big_* 用于能改变市场级别的重大事件，其余用 bull/bear，不确定一律 neutral。\n"
    "7) **方向取决于上下文，不要凭关键词下结论**：\n"
    "   · 同一个词在不同语境下方向可能相反：『自动驾驶事故/召回』利空，"
    "『自动驾驶法规落地/量产获批』利多；『签约订单』利多，『解除合同』利空。\n"
    "   · 注意否定与转折：『未能/不再/未触及』会把方向反转；"
    "『但/然而/不过』之后的分句才是真实结论。\n"
    "   · 『交易所/公告/披露/申报/成交额/分化』这类词只是新闻措辞，**不构成方向**。\n"
    "   · 结合我给你的【当日市场状态】：若当日已大涨而消息偏多，要警惕「利好出尽」，"
    "可降级为 neutral；若已大跌而消息偏空，同样可降级。\n"
    "只输出 JSON，不要解释。"
)


def _cache_path():
    return util.cache_file(CACHE)


def load_cache():
    store = util.load_json(_cache_path(), {"labels": {}}) or {"labels": {}}
    # 提示词版本变化 → 旧判断作废（口径已变，沿用会污染结论）
    if int(store.get("prompt_version") or 0) != PROMPT_VERSION:
        return {"prompt_version": PROMPT_VERSION, "labels": {}}
    return store


def save_cache(store):
    store = dict(store or {})
    store["prompt_version"] = PROMPT_VERSION
    util.save_json(_cache_path(), store)


def context_key(regime=None):
    """把"当下市场状态"折成一个短键，用于缓存分组。

    为什么要它：方向会随行情阶段变化（用户："利空利多不是每天都在变得吗"）。
    同一条消息在"上涨段"和"下跌段"被判成不同方向是**正确**的，缓存必须区分，
    否则第一次的判断会被永久复用。
    """
    r = regime or {}
    try:
        chg = float(r.get("chg") or 0.0)
    except (TypeError, ValueError):
        chg = 0.0
    band = "up" if chg > 0.8 else ("down" if chg < -0.8 else "flat")
    return "{}_{}".format(PROMPT_VERSION, band)


def regime_brief(regime=None):
    """渲染给模型的【当日市场状态】文字。"""
    r = regime or {}
    if not r:
        return "（未提供）"
    chg = r.get("chg")
    parts = []
    if chg is not None:
        parts.append("沪深300 当日 {:+.2f}%".format(float(chg)))
    for k, label in (("ma_align", "均线多头"), ("vol_rank", "波动分位"),
                     ("micro_pct", "情绪分位")):
        if r.get(k) is not None:
            parts.append("{}={}".format(label, r.get(k)))
    return "；".join(parts) or "（未提供）"


def classify_batch(items, cfg, timeout=120, regime=None):
    """一批（≤30 条）→ {id: {label, strength, confidence, reason}}。失败返回 {}。

    `regime` 为当日市场状态：模型需要它才能判断"利好出尽/利空兑现"这类
    **随行情变化**的方向，否则同一句话在任何行情下都会被判成同一个方向。
    """
    llm = (cfg or {}).get("llm") or {}
    if not llm.get("enabled") or not llm.get("api_key"):
        return {}
    base = (llm.get("base_url") or "").rstrip("/")
    if not base:
        return {}
    payload_items = [{"id": it.get("id"), "title": (it.get("title") or "")[:MAX_TITLE],
                      "text": (it.get("text") or "")[:MAX_TEXT],
                      "time": it.get("time") or ""} for it in items]
    user = ("【当日市场状态】" + regime_brief(regime) + "\n"
            "【快讯列表】\n" + json.dumps(payload_items, ensure_ascii=False) +
            "\n\n输出 JSON：{\"items\":[{\"id\":\"原样回填\",\"label\":\"big_bull|bull|"
            "neutral|bear|big_bear|irrelevant\",\"confidence\":0~1,"
            "\"reason\":\"不超过20字\"}]}，条数与输入一致，只输出 JSON。")
    try:
        js = util.http_post_json(
            base + "/chat/completions",
            {"model": llm.get("model", "qwen-flash"),
             "messages": [{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": user}],
             "temperature": 0.1,
             "max_tokens": 2000,
             "response_format": {"type": "json_object"}},
            headers={"Authorization": "Bearer " + llm.get("api_key", "")},
            timeout=timeout)
        content = (js["choices"][0]["message"]["content"] or "").strip()
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:].lstrip()
        obj = json.loads(content)
        # 模型可能返回顶层数组，也可能包在 items/data 里（实测 qwen 返回裸数组）
        if isinstance(obj, dict):
            rows = obj.get("items") or obj.get("data") or obj.get("results") or []
        elif isinstance(obj, list):
            rows = obj
        else:
            rows = []
        out = {}
        for r in rows:
            iid = str(r.get("id") or "")
            lab = str(r.get("label") or "").strip().lower()
            if not iid or lab not in VALID:
                continue
            try:
                conf = float(r.get("confidence") or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            out[iid] = {"label": lab, "strength": LABEL_STRENGTH[lab],
                        "confidence": round(util.clamp(conf, 0.0, 1.0), 2),
                        "reason": str(r.get("reason") or "")[:40]}
        return out
    except Exception:
        return {}


def label_items(items, cfg, max_items=120, batch=20, use_cache=True,
                progress=None, regime=None, all_items=False):
    """整条消息流 → LLM 标签（缓存 + 限量 + 回退）。

    与旧版的两处关键差别（2026-09-16「AI 为主」改造）：
      1. `all_items=True` 时**不再只看"词典兜底桶"**，而是覆盖整条流，按
         `🔴重要 → |本地强度| → 主题命中数` 排序取前 `cap` 条；
      2. 缓存按 `context_key(regime)` 分组——**行情状态变了就重判**，
         不会把"上涨段"的结论永久复用到"下跌段"。

    返回 {"labels": {id: {...}}, "calls": n, "from_cache": n,
          "skipped": n, "candidates": n, "context": 状态键}
    """
    st = (cfg or {}).get("strategy") or {}
    cap = int(max_items if max_items is not None
              else st.get("news_llm_cap", 120) or 0)
    batch = max(1, min(30, int(batch or st.get("news_llm_batch", 20) or 20)))
    store = load_cache() if use_cache else {"labels": {}}
    cached_all = dict(store.get("labels") or {})
    ckey = context_key(regime)
    # 只复用**同一市场状态**下的旧判断（见 context_key 注释）
    cached = {k: v for k, v in cached_all.items()
              if (v or {}).get("context") == ckey}
    if all_items:
        cand = [e for e in (items or []) if e.get("id") is not None]
    else:
        cand = [e for e in (items or [])
                if e.get("event_type") == "dict"
                and e.get("auto_label") in ("bull", "bear")]
    cand.sort(key=lambda e: (-(1 if e.get("important") else 0),
                             -abs(int(e.get("auto_strength") or 0)),
                             -len(e.get("sectors") or [])))
    if cap > 0:
        cand = cand[:cap]
    todo = [e for e in cand if str(e.get("id")) not in cached]
    calls, fails = 0, 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        got = classify_batch(chunk, cfg, regime=regime)
        calls += 1
        if progress:
            progress(min(i + batch, len(todo)), len(todo))
        if not got:
            fails += 1
            if fails >= 3:                         # 连续失败才放弃（避免烧配额）
                break
            continue
        fails = 0
        for k, v in got.items():
            v = dict(v)
            v["title"] = next((e.get("title") or "" for e in chunk
                               if str(e.get("id")) == k), "")[:60]
            v["context"] = ckey
            cached[k] = v
        if calls % 5 == 0 and use_cache:
            save_cache({"updated": util.now_iso(), "labels": cached})
    if use_cache and calls:
        save_cache({"updated": util.now_iso(), "labels": cached})
    return {"labels": {str(e.get("id")): cached[str(e.get("id"))]
                       for e in cand if str(e.get("id")) in cached},
            "calls": calls, "from_cache": len(cand) - len(todo),
            "skipped": len(todo) - sum(1 for e in todo if str(e.get("id")) in cached),
            "candidates": len(cand), "context": ckey}


def apply_labels(feed, labels, contribute=True):
    """把 LLM 标签写回 feed（保留本地判断在 dict_label 字段里，便于对照）。

    `contribute=True`（2026-09-16 起的默认）：**AI 的方向直接计入消息分**。
    旧默认是 False，只让 LLM 做"降噪一票否决"（判中性/无关的条目不计净情绪），
    结果就是"AI 判了也不影响评分"——用户明确要求改成 AI 为主，故翻转默认值。
    仍保留 `contribute=False` 供回测对照（README §5.2 的历史口径）。
    """
    n = 0
    for e in feed or []:
        lab = (labels or {}).get(str(e.get("id")))
        if not lab:
            continue
        e["dict_label"] = e.get("auto_label")
        e["dict_strength"] = e.get("auto_strength")
        e["auto_label"] = lab["label"]
        e["auto_strength"] = int(lab["strength"])
        e["auto_net"] = int(lab["strength"])
        e["llm_reason"] = lab.get("reason") or ""
        e["llm_conf"] = lab.get("confidence")
        e["llm_label"] = True
        e["llm_contribute"] = bool(contribute)
        e["llm_dropped"] = lab["label"] in ("neutral", "irrelevant")
        e["decided_by"] = "ai" if contribute else "ai_veto"
        e["confidence"] = lab.get("confidence")
        n += 1
    return n
