# -*- coding: utf-8 -*-
"""消息语义推断（事件型）——回答“这条消息意味着什么”，而不是数词频。

词典打分只能记住“词语→方向”的表面关联（如 ETF 溢价公告里
“收盘价/临时停牌/参考净值”被反复打成利空，其实与 A 股大盘方向无关）。
本模块把消息先识别成**可解释的事件**，再按事件的金融含义给出
方向/强度/受影响的资产类别，并给出人能看懂的依据（reason）。

事件类型（event / scope）：
- market     大盘方向（宏观宽松/紧缩、地缘冲突、经济数据、资本市场政策）
- sector     板块方向（行业政策/景气）
- single     个股/单一主体（业绩、减持、回购、立案…）
- fund_premium_warning   ETF 场内溢价/临停的产品层面提示 → 无大盘方向，
              不参与消息净分，也不应被学成语录词（避免“收盘价→利空”式误学）
- holdings_disclosure    机构持股比例例行变动（港交所权益披露，常批量刷屏）→
              单一个股筹码趋势、无大盘方向，AI 自动判中性归档、不进人工复核队列

强度说明：与词典一致按 ±6 封顶；事件信号典型 ±2~4，配合置信度显示。
说明（十年训练）：离线环境没有可投喂的十年级事件→次日收益语料，且本程序
用“可解释规则”替代黑箱：规则来自对 A 股常见事件的常识金融传导（货币政策
→流动性→股市；地缘→避险；业绩→个股；溢价提示→产品层面）；每一条自动
“次日大盘方向”会在下一交易日收盘后结算命中率（direction_stats），相当于
用真实后验持续校准/替换规则强度——比无法审计的端到端模型更适合**小额实盘的资金约束**。
"""
import re

from . import lexicon, strategy

# 事件“方向”→ 词典标签
_DIR2LABEL = {1: "bull", -1: "bear", 0: "neutral"}


def _txt(title, text):
    return "{} {}".format(str(title or ""), str(text or ""))


# ---------------- 事件规则（按优先级排列） ----------------
def _premium_warning(t):
    """ETF 场内溢价/临时停牌 产品提示：特征= 停牌/溢价 + 参考净值/交易价格 同现。"""
    return (("临时停牌" in t or "盘中停牌" in t or ("停牌" in t and "溢价" in t))
            and ("二级市场" in t or "场内" in t)
            and ("参考净值" in t or "交易价格" in t or "份额" in t)
            and "ETF" in t.upper())


_GEO_KWS = ("冲突升级", "冲突加剧", "局势升级", "局势恶化", "军事打击", "导弹",
            "发动袭击", "空袭", "开战", "宣战", "战争", "武装冲突", "地缘紧张",
            "地缘风险", "轰炸", "交火", "边境冲突", "战事")


def _cbank_ease(t):
    return any(k in t for k in ("降准", "降息", "下调政策利率", "下调利率",
                                "LPR下调", "MLF净投放", "逆回购净投放", "大额逆回购",
                                "流动性释放", "全面降准", "定向降准", "降准降息"))


def _cbank_tight(t):
    return any(k in t for k in ("加息", "上调政策利率", "上调利率", "LPR上调",
                                "提高存款准备金率", "收紧货币政策", "流动性收紧",
                                "紧缩", "回笼流动性"))


_REFORM_KWS = ("稳增长", "扩大内需", "提振消费", "促消费", "活跃资本市场",
               "中长期资金入市", "支持资本市场", "稳定资本市场", "回购增持贷款",
               "并购六条", "国九条", "市值管理", "分红新规", "平准基金")


_INDUSTRY_KWS = ("产业政策", "专项规划", "试点扩围", "示范城市", "补贴政策",
                 "纳入目录", "国产替代", "专项行动", "产业基金", "大规模设备更新",
                 "以旧换新", "国产化", "自主可控", "首次写入政府工作报告")


_DATAPOS_KWS = ("PMI重回扩张区间", "PMI站上荣枯线", "PMI超预期", "社融超预期",
                "新增贷款超预期", "信贷超预期", "CPI温和回升", "通胀回升", "出口超预期")
_DATANEG_KWS = ("PMI不及预期", "PMI跌破荣枯线", "社融不及预期", "信贷不及预期",
                "出口不及预期", "CPI低于预期", "需求疲软", "通缩压力")


_EARN_POS = ("预增", "扭亏为盈", "大幅增长", "净利润增长", "营收增长", "利润翻倍",
             "业绩超预期", "超预期增长", "同比大增", "扣非增长", "盈利改善", "业绩预喜")
_EARN_NEG = ("预亏", "业绩预减", "净利润下滑", "净利下滑", "亏损扩大", "由盈转亏",
             "业绩不及预期", "同比下降", "大幅亏损", "业绩变脸", "计提减值致亏")
_HOLDER_POS = ("回购", "增持", "举牌", "员工持股计划", "股权激励", "实控人增持",
               "股东承诺不减持", "拟回购")
_HOLDER_NEG = ("减持计划", "拟减持", "股东减持", "大宗减持", "清仓式减持", "解禁",
               "限售股解禁", "补充质押", "质押爆仓", "商誉减值", "立案调查",
               "收到立案告知", "监管处罚", "罚款", "终止上市", "触及退市",
               "披星戴帽", "财务造假", "资金占用被查", "被强制退市")

# 板块偏好：地缘风险下的结构性方向（黄金/军工避险）等
_GEO_BIAS = ("黄金", "军工")


def _holdings_disclosure(t):
    """交易所权益披露类：机构/股东持股比例例行变动（摩根大通 H 股披露等）。

    特征=“持股比例”与升/降至、%变动同现（多为港交所例行权益披露，批量刷屏，
    只反映单一机构在某只股的筹码趋势，不是当日市场方向信息）。
    """
    if "持股比例" in t and re.search(
            r"(升至|降至|上升至|下降至|升破|跌破|从\s*\d{1,3}(\.\d+)?\s*%)", t):
        return True
    if ("权益披露" in t or "香港交易所信息显示" in t) and "持股" in t:
        return True
    return False


def classify_event(title, text):
    """主入口：识别事件 → {event,scope,dir,label,strength,confidence,reason}。

    命中规则即返回可解释结论；未命中返回 event='none'（由词典层兜底）。
    """
    t = _txt(title, text)
    # 1) ETF 产品溢价提示（最高优先级：不得误读为大盘方向）
    if _premium_warning(t):
        return {"event": "fund_premium_warning", "scope": "single",
                "dir": 0, "label": "neutral", "strength": 0,
                "confidence": 0.92,
                "reason": "ETF 场内溢价/临时停牌属于基金产品层面的风险提示"
                          "（二级市场交易价高于份额参考净值），不代表 A 股大盘方向"}
    # 1.5) 权益披露/持股比例例行变动：单一个股的筹码趋势，无大盘方向 → AI 自动归档
    if _holdings_disclosure(t):
        way = "下降（减持趋势）" if re.search(r"(降至|跌破|减少)", t) else "上升（增持趋势）"
        return {"event": "holdings_disclosure", "scope": "single",
                "dir": 0, "label": "neutral", "strength": 0,
                "confidence": 0.9,
                "reason": "交易所权益披露类例行数据（持股比例{}）：仅是单一机构在"
                          "个股上的筹码趋势记录，无当日大盘方向信息，AI 自动判中性"
                          "归档，不占用人工复核额度".format(way)}
    # 2) 地缘冲突升级 → 风险厌恶（大盘利空；黄金/军工等避险主题另计）
    for k in _GEO_KWS:
        if k in t:
            return {"event": "geo_conflict", "scope": "market", "dir": -1,
                    "label": "bear", "strength": 3, "confidence": 0.75,
                    "reason": "地缘冲突升级（{}）→ 全球风险偏好收缩，A 股避险情绪上升；"
                              "黄金/军工等避险资产相对受益".format(k)}
    # 3) 央行宽松/紧缩
    if _cbank_ease(t):
        return {"event": "cbank_ease", "scope": "market", "dir": 1,
                "label": "bull", "strength": 4, "confidence": 0.8,
                "reason": "央行宽松（降准/降息/流动性投放）→ 市场流动性改善、"
                          "无风险利率下行，利好股市与利率敏感成长板块"}
    if _cbank_tight(t):
        return {"event": "cbank_tight", "scope": "market", "dir": -1,
                "label": "bear", "strength": 4, "confidence": 0.8,
                "reason": "货币收紧（加息/上调利率/收紧流动性）→ 估值承压，"
                          "流动性收缩利空股市"}
    # 4) 资本市场/宏观政策
    for k in _REFORM_KWS:
        if k in t:
            return {"event": "market_policy", "scope": "market", "dir": 1,
                    "label": "bull", "strength": 3, "confidence": 0.7,
                    "reason": "资本市场/稳增长政策（{}）→ 政策暖风，提振市场风险偏好".format(k)}
    # 5) 行业政策（命中板块词则 sector 级，否则轻量大盘利多）
    for k in _INDUSTRY_KWS:
        if k in t:
            th = strategy._theme_of(t)
            scope = "sector" if th else "market"
            return {"event": "industry_policy", "scope": scope, "dir": 1,
                    "label": "bull", "strength": 2,
                    "confidence": 0.6 if th else 0.45,
                    "reason": "产业政策/试点（{}）{}→ 利好对应板块景气预期".format(
                        k, ("（{}）".format(th) if th else "→ 大盘情绪偏暖"))}
    # 6) 经济数据
    for k in _DATAPOS_KWS:
        if k in t:
            return {"event": "eco_data", "scope": "market", "dir": 1,
                    "label": "bull", "strength": 2, "confidence": 0.55,
                    "reason": "经济数据（{}）超预期 → 基本面修复预期，利好股市".format(k)}
    for k in _DATANEG_KWS:
        if k in t:
            return {"event": "eco_data", "scope": "market", "dir": -1,
                    "label": "bear", "strength": 2, "confidence": 0.55,
                    "reason": "经济数据（{}）偏弱 → 基本面担忧压制市场".format(k)}
    # 7) 公司业绩（single：对个股/所属主题是方向信号）
    for k in _EARN_POS:
        if k in t:
            return {"event": "earnings", "scope": "single", "dir": 1,
                    "label": "bull", "strength": 2, "confidence": 0.6,
                    "reason": "业绩（{}）改善/超预期 → 对公司及所属主题是基本面利好，"
                              "强化持仓信心；若已提前反应则弹性有限".format(k)}
    for k in _EARN_NEG:
        if k in t:
            return {"event": "earnings", "scope": "single", "dir": -1,
                    "label": "bear", "strength": 2, "confidence": 0.6,
                    "reason": "业绩（{}）恶化 → 对公司及所属主题构成基本面利空".format(k)}
    # 8) 股东行为：增持回购（+）/ 减持解禁爆雷（-）
    for k in _HOLDER_POS:
        if k in t:
            return {"event": "holder_flow", "scope": "single", "dir": 1,
                    "label": "bull", "strength": 2, "confidence": 0.6,
                    "reason": "股东/公司（{}）→ 资金与信心层面的正面信号".format(k)}
    for k in _HOLDER_NEG:
        if k in t:
            heavy = any(h in k or h in t for h in
                        ("立案", "处罚", "退市", "质押爆仓", "财务造假",
                         "资金占用", "披星戴帽", "强制退市"))
            return {"event": "holder_flow", "scope": "single", "dir": -1,
                    "label": "bear",
                    "strength": 3 if heavy else 2,
                    "confidence": 0.7,
                    "reason": "{} → 个股层面利空（若持仓需重新评估基本面）".format(k)}
    return {"event": "none", "scope": "none", "dir": 0, "label": "",
            "strength": 0, "confidence": 0.0, "reason": ""}


def infer_news(title, text, dict_score=None):
    """news 采集用：事件语义优先，词典兜底。

    dict_score：lexicon.score_text 的结果（{label,strength,...}）或 None。
    返回 {label,strength,event,scope,reason,confidence}。
    """
    ev = classify_event(title, text)
    if ev["event"] != "none":
        return {"label": ev["label"], "strength": ev["strength"],
                "event": ev["event"], "scope": ev["scope"],
                "reason": ev["reason"], "confidence": ev["confidence"]}
    ds = dict_score or lexicon.score_text(_txt(title, text))
    # 词典兜底：附一句可解释依据（命中词数量）
    return {"label": ds.get("label", "neutral"),
            "strength": int(ds.get("strength") or 0),
            "event": "none", "scope": "none",
            "reason": "词典语义层未命中事件模式，按情绪词频判定（利好词/利空词差 {}）".format(
                int(ds.get("strength") or 0)),
            "confidence": 0.5}
