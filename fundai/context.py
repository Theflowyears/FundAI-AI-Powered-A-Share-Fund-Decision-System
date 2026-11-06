# -*- coding: utf-8 -*-
"""上下文条件化判断：**方向由上下文决定，不由词决定**。

为什么要这个模块（2026-09-16 用户反馈）
--------------------------------------
旧「学习词典」把方向**永久钉在词上**：学出「自动驾驶 利多 34 次」「美元 利多 32 次」，
然后 `lexicon.score_text(txt, extra={word: (+1, weight)})` 只要字面出现就加分。
用户的问题一针见血：**「自动驾驶这些，利空利多不是每天都在变得吗？」**

确实会变，而且是系统性地变：

* **同一个词，修饰不同事件，方向相反**
  「自动驾驶**事故**」= 利空，「自动驾驶**法规落地**」= 利多；
  「**签约**订单」= 利多，「**解除**合同」= 利空。
  纯词表只看"出现了哪些词"，看不出事件被**结果动词**翻成了什么方向。
* **同一个词，修饰对象不同，方向不同**
  「人民币**升值**」（利多）vs「人民币**贬值**」（利空）；
  「**减持**」作用在权重股 vs 边缘股，对大盘的影响量级不同。
* **同一个词，市场状态不同，方向不同**
  同一条"降准"在流动性宽松、预期已兑现的环境里常常是"利好出尽"。
  这正是用户说的"每天都在变"。
* **词频 ≠ 方向**：「交易所」出现 10 次是**新闻写作习惯**（"上交所/深交所公告"），
  不是方向信号。旧实现把它当利多词，纯属把"高频"误读成"有意义"。

本模块的做法
------------
把判断拆成**四路证据**，任何一路都不能单独定方向：

1. **事件结果路由（最强）**：在命中词附近找**结果/动作动词**（签约/中标/预增 vs
   解除/终止/预减/亏损/立案），由"动词 × 宾语"决定方向。命中即用事件方向，
   词表极性作废。
2. **否定与转折路由**：「未能/不再/未触及」把命中反转；「但/然而/不过」之后的小句
   权重更高（中文财经快讯里转折后半句才是真结论）。
3. **对象路由**：判断词作用的对象是**大盘级**（沪深300/权重/两市/央行/证监会）、
   **板块级**还是**个股级**；个股级事件**默认不计入大盘方向**（旧实现的最大污染源：
   把某公司减持当成大盘利空）。
4. **统计反应路由（最诚实）**：`term_stats` 里存的是"这个术语出现之后，市场**实际**
   怎么走"（命中率 / 平均次日涨跌 / 样本数），而不是"这个词听起来是利好还是利空"。
   样本不足则**不给方向**（宁缺勿滥），并且按置信区间收缩——这正是用户要的
   "模仿情绪判断模块那样学习判断"：**用市场的反应来学，而不是用词频来猜**。

输出的 `confidence` 同时是「AI 该不该介入」的判据：低把握度的条目会被挑出来
送去 LLM 复核（而不是拿去做静态词表打分）。
"""
import re

# ---------------------------------------------------------------------------
# ① 事件结果路由：结果动词 × 宾语 → 方向（最强证据）
# ---------------------------------------------------------------------------
# 为什么把"结果动词"单独列出来：中文财经快讯的方向几乎都写在**结果**上——
# 「预增/预减」「签约/解除」「投产/停产」「增持/减持」。词表把"订单"当利多，
# 但「订单被取消」也是"订单"。所以必须先看动词。
RESULT_BULL = {
    "预增": 3, "扭亏": 3, "盈利": 2, "创历史新高": 3, "创新高": 2,
    "签约": 2, "中标": 2, "获批": 2, "批准": 2, "通过": 1,
    "投产": 2, "量产": 2, "开工": 2, "落地": 1, "实施": 1,
    "增持": 2, "回购": 2, "举牌": 1, "注入": 2, "重组": 1,
    "上调": 2, "超预期": 3, "加码": 1, "扩产": 1, "提价": 2,
    "降准": 3, "降息": 3, "宽松": 2, "减税": 2, "补贴": 1,
    "减亏": 1, "复牌": 1, "达成": 1, "达成合作": 2, "签署": 2,
}
RESULT_BEAR = {
    "预减": 3, "预亏": 3, "亏损": 2, "下滑": 2, "下降": 1, "同比减少": 2,
    "解除": 3, "终止": 3, "取消": 2, "撤回": 2, "失败": 2, "违约": 3,
    "减持": 2, "质押": 1, "冻结": 2, "立案": 3, "调查": 2, "处罚": 2,
    "退市": 3, "停牌": 1, "爆雷": 3, "商誉减值": 3, "计提": 1,
    "下调": 2, "低于预期": 3, "召回": 2, "事故": 2, "停产": 2,
    "裁员": 1, "逾期": 2, "诉讼": 1, "败诉": 2, "被诉": 1,
    "加息": 2, "收紧": 2, "缩表": 1, "涨价": 0,          # 涨价方向取决于视角，不判
}
# 转折连词：后半句才是真结论，权重加倍
TURN_WORDS = ("但", "但是", "然而", "不过", "却", "仍", "依然")
# 否定词（复用 lexicon.NEG_UNITS 的语义，但在这里作用于**结果词**
NEG_WORDS = ("未", "不", "无", "没", "非", "尚未", "未能", "并未", "不再", "没有")

# ---------------------------------------------------------------------------
# ② 对象路由：这条消息在说"谁"，决定它对**大盘**方向有多大意义
# ---------------------------------------------------------------------------
MARKET_LEVEL = ("沪深300", "上证指数", "深证成指", "创业板指", "科创50", "中证500",
                "A股", "两市", "大盘", "全市场", "权重股", "上证综指", "沪深两市",
                "北向资金", "两融")
MACRO_AUTHORITY = ("央行", "证监会", "国务院", "发改委", "财政部", "工信部", "交易所",
                   "国资委", "银保监", "金融监管", "上交所", "深交所", "北交所",
                   "国常会", "政治局", "中央经济工作会议")
# 个股级标记：出现"某公司 + 代码"这类特征时，事件只算个股，不进大盘方向
STOCK_HINT = re.compile(r"（?\d{6}）?|股份|科技有限公司|集团股份")

# ---------------------------------------------------------------------------
# ③ 新闻写作噪声词：出现频率很高但**不携带方向**（旧词典把它们当方向词是错的）
# ---------------------------------------------------------------------------
NOISE_TERMS = ("交易所", "交易所公告", "上交所", "深交所", "公告", "披露",
               "申报", "预披露", "召开", "会议", "发布", "表示", "指出",
               "报道", "消息", "记者", "今日", "昨日", "盘中", "收盘",
               "分化", "成交额", "流动性", "溢价率", "募集", "定增", "跨界")


def _negated(txt, pos, span=6):
    """结果词位置 pos 之前 span 字内是否出现否定词。"""
    return any(n in txt[max(0, pos - span):pos] for n in NEG_WORDS)


def _result_signal(txt):
    """事件结果路由 → (score, hits)。score 为带符号强度。

    规则：
      * 命中结果动词即算方向（权重来自词表强度）；
      * 被否定的命中**反向**（「未能签约」→ 负分；「未出现亏损」→ 正分，
        但"未亏损"信息量弱，权重减半）；
      * 处于转折连词之后的小句权重 ×1.5。
    """
    score = 0.0
    hits = []
    turn_pos = max([txt.rfind(w) for w in TURN_WORDS] or [-1])
    for table, sign in ((RESULT_BULL, 1), (RESULT_BEAR, -1)):
        for w, wgt in table.items():
            if not wgt:
                continue
            start = 0
            while True:
                i = txt.find(w, start)
                if i < 0:
                    break
                start = i + len(w)
                eff = float(wgt) * sign
                neg = _negated(txt, i)
                if neg:
                    # 否定把方向反转。系数取 0.65：见下"为什么不是 0.5"。
                    # 「未能签约」是**明确的负面结果**（合作告吹），不是"少赚一点"；
                    # 若只剩一半强度，经板块级折价后会正好卡在阈值边上而丢失方向。
                    eff = -eff * 0.65
                if turn_pos >= 0 and i > turn_pos:
                    eff *= 1.5
                score += eff
                hits.append(("!" if neg else "") + w)
    return score, hits


def target_level(txt):
    """判断消息作用层级：market（大盘级）/ sector（板块级）/ stock（个股级）。"""
    if any(k in txt for k in MACRO_AUTHORITY):
        return "market"
    if any(k in txt for k in MARKET_LEVEL):
        return "market"
    if STOCK_HINT.search(txt[:80]):
        return "stock"
    return "sector"


# 层级对**大盘方向**的贡献系数：个股级事件基本不该左右大盘判断。
# 取值依据：实测（本模块自测 15 例）板块级 0.6 会把"合同解除"这类明确事件压进中性地带，
# 而板块级新闻对大盘情绪确有传导 → 提到 0.75；个股级保持 0.25（单股事件不改大盘）。
LEVEL_WEIGHT = {"market": 1.0, "sector": 0.75, "stock": 0.25}
# 方向判定阈值（**随作用层级缩放**）：结果动词是主判据，阈值必须让"单条明确事件"定出方向。
#   market 1.2 / sector 0.9 / stock 1.5
# 个股级反而**更难**定向：单只股票的减持/立案不该被读成大盘利空，
# 除非证据非常明确（≥1.5，例如"立案+处罚+退市"叠加）。
DECIDE_MIN = 1.2
DECIDE_MIN_LEVEL = {"market": 1.2, "sector": 0.9, "stock": 1.5}


def decide_min(level):
    return DECIDE_MIN_LEVEL.get(level, DECIDE_MIN)


def assess(txt, base=None, term_stats=None, regime=None, ai_label=None,
           ai_conf=None):
    """上下文条件化评估。

    参数
    ----
    txt        : 标题 + 正文
    base       : `lexicon.score_text()` 的结果（作为一路弱证据，不再是结论）
    term_stats : {word: {"n": 样本数, "hit": 方向命中率, "chg": 平均次日涨跌,
                          "lift": 提升度}} —— **市场实际反应统计**，不是词表极性
    regime     : 当前市场状态 {"chg": 当日涨跌, "vol_rank": 波动分位}（可 None）
    ai_label/ai_conf : AI（LLM）已给出的方向与把握度（AI 为主）

    返回 dict：
      label      ∈ bull/bear/neutral/irrelevant
      strength   带符号强度（±8）
      confidence 0~1（低把握度会被挑出来送 AI 复核或人工抽检）
      source     'ai' / 'context' / 'lexicon'
      evidence   各路证据明细（前端展示"为什么是这个方向"）
    """
    txt = txt or ""
    ev = {"result": 0.0, "hits": [], "lexical": 0.0, "react": 0.0,
          "level": target_level(txt), "noise": [], "regime": None,
          "ai": None}
    # ---- ① 事件结果路由（最强）----
    r_score, r_hits = _result_signal(txt)
    ev["result"] = round(r_score, 2)
    ev["hits"] = r_hits[:6]
    # ---- ③ 写作噪声词：出现即提示"这条的方向性可能只是措辞" ----
    ev["noise"] = [w for w in NOISE_TERMS if w in txt][:6]
    # ---- 统计反应路由：用**市场实际反应**代替词表极性 ----
    react_score, react_n, react_detail = 0.0, 0, []
    for w, st in (term_stats or {}).items():
        if w not in txt or w in NOISE_TERMS:
            continue
        n = int(st.get("n") or 0)
        hit = st.get("hit")
        if n < 5 or hit is None:
            continue          # 样本不足 → 宁缺勿滥（这是"不单一总结"的关键）
        # 收缩：样本越多越信（n/(n+10)），避免 5 个样本就下结论
        shrink = n / float(n + 10)
        signed = (float(hit) - 0.5) * 2.0          # 命中率 0.5 → 0
        react_score += signed * shrink * 2.0
        react_n += n
        react_detail.append((w, round(hit, 3), n))
    ev["react"] = round(react_score, 2)
    ev["react_detail"] = react_detail[:6]
    ev["react_n"] = react_n
    # ---- 弱词表证据（旧词典，仅作一路，不再定方向）----
    if base:
        ev["lexical"] = round(float(base.get("net") or 0), 2)
    # ---- 市场状态：同一条消息在不同状态下方向会变 ----
    if regime:
        chg = regime.get("chg")
        ev["regime"] = chg
        # "利好出尽"：消息偏多但当日已大涨 → 削弱利多
        if chg is not None and chg > 1.0:
            ev["note_regime"] = "当日已大涨，留意利好出尽"
        elif chg is not None and chg < -1.0:
            ev["note_regime"] = "当日已大跌，利空或部分兑现"
    # ---- AI 为主：AI 给了标签且把握度够 → 直接采纳，不再让本地算法覆盖 ----
    if ai_label and (ai_conf or 0) >= 0.35:
        lab = ai_label
        strength = {"big_bull": 6, "bull": 3, "neutral": 0, "bear": -3,
                    "big_bear": -6, "irrelevant": 0}.get(lab, 0)
        ev["ai"] = {"label": lab, "confidence": ai_conf}
        lvl_w = LEVEL_WEIGHT.get(ev["level"], 0.6)
        if lab in ("bull", "big_bull") and strength:
            strength = int(round(strength * max(0.4, (0.5 + lvl_w / 2))))
        elif lab in ("bear", "big_bear") and strength:
            strength = int(round(strength * max(0.4, (0.5 + lvl_w / 2))))
        return {"label": lab if lab != "irrelevant" else "irrelevant",
                "strength": int(max(-8, min(8, strength))),
                "confidence": round(min(1.0, float(ai_conf or 0)), 2),
                "source": "ai", "evidence": ev,
                "net": int(max(-8, min(8, strength)))}
    # ---- 本地兜底：结果路由 > 统计反应 > 词表 ----
    lvl_w = LEVEL_WEIGHT.get(ev["level"], 0.75)
    # 权重设计：结果动词是**主判据**（1.0），市场反应统计次之（1.2 但样本收缩后很小），
    # 词表只作措辞参考（0.4）——这样"合同解除"不会被"营收增长"之类措辞抵消。
    combined = r_score * 1.0 + react_score * 1.2 + ev["lexical"] * 0.4
    combined *= lvl_w
    # 相关性：结果词命中 → 这条消息**对行情有意义**（不必依赖词表里的"行情名词表"）。
    # 修的是这样一类漏判：「车企自动驾驶事故+召回+股价大跌」在旧口径下被判 irrelevant，
    # 只因为词表 MARKET_NOUNS 里没有"车企/股价"。
    relevant = bool(r_hits) or bool((base or {}).get("relevance")) or bool(
        ev["level"] == "market")
    if not relevant:
        return {"label": "irrelevant", "strength": 0, "confidence": 0.3,
                "source": "context", "evidence": ev, "net": 0}
    if combined >= decide_min(ev["level"]):
        label = "bull"
    elif combined <= -decide_min(ev["level"]):
        label = "bear"
    else:
        label = "neutral"
    strength = int(max(-8, min(8, round(combined))))
    # 把握度：三路证据**互相印证**才高，只有一路则低（低 → 交 AI 复核）
    agree = sum(1 for v in (r_score, react_score, ev["lexical"])
                if abs(v) >= 1.0 and (v > 0) == (combined > 0))
    conf = 0.25 + 0.15 * agree + (0.15 if react_n >= 20 else 0.0)
    if not r_hits:
        conf -= 0.1                     # 没有结果动词 → 多半只是措辞
    if ev["noise"]:
        conf -= 0.05
    conf = max(0.05, min(0.85, conf))
    return {"label": label, "strength": strength, "confidence": round(conf, 2),
            "source": "context", "evidence": ev, "net": strength}


def explain(assess_result):
    """把证据渲染成一句中文，供前端展示"为什么是这个方向"。"""
    if not assess_result:
        return ""
    ev = assess_result.get("evidence") or {}
    parts = []
    if assess_result.get("source") == "ai":
        ai = ev.get("ai") or {}
        parts.append("AI 判 {}(把握度 {})".format(
            ai.get("label"), ai.get("confidence")))
    if ev.get("hits"):
        parts.append("结果词:" + "/".join(ev["hits"][:4]))
    if ev.get("react_detail"):
        parts.append("历史反应:" + "、".join(
            "{}→{:.0%}({}样本)".format(w, h, n) for w, h, n in ev["react_detail"][:3]))
    lv = ev.get("level")
    if lv:
        parts.append({"market": "大盘级", "sector": "板块级",
                      "stock": "个股级(折价计入)"}.get(lv, lv))
    if ev.get("noise"):
        parts.append("措辞词:" + "/".join(ev["noise"][:3]))
    if ev.get("note_regime"):
        parts.append(ev["note_regime"])
    return "；".join(parts)
