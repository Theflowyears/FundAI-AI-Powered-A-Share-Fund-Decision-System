# -*- coding: utf-8 -*-
"""用大语言模型替代粗糙的关键词词典：对“词典兜底”快讯做语义定级。

**为什么做**（352 个交易日回测证据，见 README「5.2 消息面口径治理」）：
- 词典层样本 6.2 万条，日级 IC −0.070、方向增量 −1.4%；**去掉词典层后 IC 转正 +0.039**；
- 乌龙类型靠“缩阈值”筛不掉：把“解除 4.03 亿元合同”判成利好(+4)、
  “腕戴设备出货量同比下滑”判成利好(+5)、“连板股晋级率仅20%”判成利空(−8)；
- 精确度上：判利多但含明显负面词 4.0%，判利空但含明显正面词 7.5%，
  且利多:利空 = 44260:17994（系统性偏多）。

**怎么用**：只对词典兜底那条桶（未命中任何事件规则）的消息调用 LLM，
按天限量（默认 120 条/天，按 |词典强度| 优先——它们主导当日分数），
磁盘缓存（同一 id 永不重复计费），任何失败都自动回退词典标签，绝不断主流程。

标签 → 强度：big_bull/big_bear = ±6，bull/bear = ±2，neutral/irrelevant = 0。
"""
import json

from . import util

CACHE = "llm_news_labels.json"
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
    "只输出 JSON，不要解释。"
)


def _cache_path():
    return util.cache_file(CACHE)


def load_cache():
    return util.load_json(_cache_path(), {"labels": {}}) or {"labels": {}}


def save_cache(store):
    util.save_json(_cache_path(), store)


def classify_batch(items, cfg, timeout=120):
    """一批（≤30 条）→ {id: {label, strength, confidence, reason}}。失败返回 {}。"""
    llm = (cfg or {}).get("llm") or {}
    if not llm.get("enabled") or not llm.get("api_key"):
        return {}
    base = (llm.get("base_url") or "").rstrip("/")
    if not base:
        return {}
    payload_items = [{"id": it.get("id"), "title": (it.get("title") or "")[:MAX_TITLE],
                      "text": (it.get("text") or "")[:MAX_TEXT],
                      "time": it.get("time") or ""} for it in items]
    user = ("【快讯列表】\n" + json.dumps(payload_items, ensure_ascii=False) +
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
                progress=None):
    """词典兜底桶 → LLM 标签（缓存 + 限量 + 回退）。

    返回 {"labels": {id: {...}}, "calls": n, "from_cache": n, "skipped": n}
    """
    st = (cfg or {}).get("strategy") or {}
    cap = int(max_items or st.get("news_llm_cap", 120) or 0)
    batch = max(1, min(30, int(batch or st.get("news_llm_batch", 20) or 20)))
    store = load_cache() if use_cache else {"labels": {}}
    cached = dict(store.get("labels") or {})
    # 优先队列：🔴重要 → |词典强度| 大 → 命中主题多（它们主导当日分数）
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
        got = classify_batch(chunk, cfg)
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
            cached[k] = v
        if calls % 5 == 0 and use_cache:
            save_cache({"updated": util.now_iso(), "labels": cached})
    if use_cache and calls:
        save_cache({"updated": util.now_iso(), "labels": cached})
    return {"labels": {str(e.get("id")): cached[str(e.get("id"))]
                       for e in cand if str(e.get("id")) in cached},
            "calls": calls, "from_cache": len(cand) - len(todo),
            "skipped": len(todo) - sum(1 for e in todo if str(e.get("id")) in cached),
            "candidates": len(cand)}


def apply_labels(feed, labels, contribute=False):
    """把 LLM 标签写回 feed（保留词典判断在 dict_label 字段里，便于对照）。

    contribute=False（默认）：LLM 只作**降噪/一票否决**——它判中性/无关的条目
    不再计入净情绪，其方向判断不额外加分（实测它与词典方向一致率 94%、
    命中率并不更高，见 README「5.2 消息面口径治理」）；contribute=True 才让 LLM 方向计入。
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
        n += 1
    return n
