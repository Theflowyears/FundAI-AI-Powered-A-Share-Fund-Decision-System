# -*- coding: utf-8 -*-
"""基础情绪词典与文本打分（与数据源解耦，供 news 采集与 screening 学习共用）。

- base_sentiment()/score_text()：基于关键词词典给一条新闻算 利好/利空 分；
- learned_extra 支持：调用方可叠加“用户打标学习出的新词”权重；
- token_candidates()：中文 n-gram 候选词提取（供 screening 从用户打标结果里
  挖掘“新学到的词”，纯标准库、无分词依赖）。
"""
import re

# 利好关键词（按强度粗分两层）
BULL_STRONG = ["降准", "降息", "放水", "增持", "回购", "涨停潮", "预增", "扭亏",
               "获批", "中标", "突破", "新高", "牛市", "加速", "政策支持",
               "并购重组", "重组", "收购", "补贴", "减税", "超预期", "翻倍"]
BULL_WEAK = ["利好", "上涨", "大涨", "回暖", "反弹", "修复", "增长", "看好",
             "景气", "加仓", "上调", "提价", "扩产", "放宽", "见底", "抄底",
             "创新高", "转强", "主线", "活跃", "回暖"]
BEAR_STRONG = ["立案", "处罚", "退市", "预亏", "预减", "爆雷", "违约", "崩盘",
               "加息", "收紧", "制裁", "关税", "调查", "计提", "减值", "跌停",
               "新低", "套现", "业绩雷", "冻结", "诉讼", "仲裁", "资金占用", "违规"]
BEAR_WEAK = ["利空", "下跌", "大跌", "减持", "解禁", "风险", "回落", "走弱",
             "低迷", "萎缩", "承压", "流出", "抛售", "看空", "停产", "裁员",
             "下修", "降价", "收缩", "降级", "终止合作", "承压", "分歧"]

# 硬宏观关键词：仅这些可直接判定相关（无需行业名词）
HARD_MACRO = ["降准", "降息", "加息", "证监会", "交易所", "央行", "国务院", "发改委",
              "政策", "关税", "制裁", "IPO", "涨停", "跌停", "退市", "立案", "处罚",
              "回购", "增持", "减持", "解禁", "并购", "重组", "上市", "爆雷", "违约",
              "崩盘", "预亏", "预减", "业绩雷", "北向", "主力资金", "国家队", "国常会"]
# 相关性名词：标题包含其一（或硬宏观词）才参与情绪计分，过滤非行情新闻
MARKET_NOUNS = ["A股", "股市", "股票", "指数", "板块", "证监会", "交易所", "央行",
                "财政部", "国务院", "发改委", "政策", "基金", "北向", "主力",
                "上市公司", "公司", "行业", "产能", "订单", "业绩", "年报", "中报",
                "季报", "获批", "药品", "芯片", "半导体", "汽车", "消费", "地产",
                "银行", "证券", "保险", "原油", "黄金", "美元", "汇率", "利率",
                "国债", "债券", "货币", "涨价", "降价", "IPO", "并购", "重组",
                "涨停", "跌停", "上市", "新股", "中签", "科创", "人工智能", "算力",
                "机器人", "低空", "军工", "通信", "存储", "光伏", "新能源", "白酒"]

# 否定字/否定前缀：用来识别“未触及要约收购”“不会导致”里的利好词是“被否定的”，不应计分
NEG_UNITS = ("未", "不", "无", "没", "非")
# 明确的利空“事件词”：即使与次要利好词同现，也应以利空为主（防止“减持+未触及要约收购”被反转成利好）
EVENT_BEAR = ("减持", "冻结", "立案", "处罚", "退市", "爆雷", "违约", "预亏", "预减",
              "跌停", "套现", "解禁", "诉讼", "仲裁", "业绩雷", "减值", "计提", "终止")

# 中文字符正则（用于切分后做 n-gram）
_HANZI = re.compile(r"[\u4e00-\u9fa5]+")
# 常用虚词/功能字：含这些字的 n-gram 大概率是碎词，学习时直接丢弃
_STOP_CHARS = set("的了是在和有这那与及或个而也都被把让对从向为于其之就并又很还最要将用因"
                  "且所当等据按更已既虽然因为如果如此由于以及一个我们你们他们它们我你他她")
_STOP_WORDS = {"公司", "股份", "有限", "今天", "昨日", "今日", "明日", "市场", "行情",
               "板块", "消息", "新闻", "记者", "报道", "网友", "评论", "相关", "表示",
               "显示", "出现", "目前", "有望", "或将", "可能", "已经", "开始", "持续"}


def denied(kw, txt, i):
    """判断 txt 中位于 i 处的关键词 kw 是否被否定修饰（前文含否定字）。"""
    window = txt[max(0, i - 6):i]
    return any(n in window for n in NEG_UNITS)


def count_kw(txt, kws, strength):
    """统计 txt 中 kws 命中次数（跳过被否定修饰的命中），strength 为单次分值。"""
    total = 0
    for k in kws:
        pos = 0
        while True:
            i = txt.find(k, pos)
            if i < 0:
                break
            if not denied(k, txt, i):
                total += strength
            pos = i + len(k)
    return total


def relevant(txt):
    if any(k in txt for k in HARD_MACRO):
        return True
    return any(k in txt for k in MARKET_NOUNS)


def base_sentiment(txt):
    """纯词典情绪 → (bull, bear)。不计学习词。"""
    bs = count_kw(txt, BULL_STRONG, 3) + count_kw(txt, BULL_WEAK, 1)
    be = count_kw(txt, BEAR_STRONG, 3) + count_kw(txt, BEAR_WEAK, 1)
    # 明确利空“事件主导”校正：若命中了减持/冻结等事件词，即使残留弱利好词也不翻多
    if any(e in txt for e in EVENT_BEAR) and be >= 3:
        if bs < be * 2:
            bs = 0
    return bs, be


def score_text(txt, extra=None):
    """综合打分：词典 + 学习词叠加。

    extra: {word: (dir01, weight)}，dir01 ∈ {-1,1}，weight>0 加到对应方向。
    返回 dict：{bull, bear, net, relevance, label, strength}
    label ∈ bull/bear/neutral/irrelevant；strength 为净情绪分（带方向，截断 ±6）。
    """
    bs, be = base_sentiment(txt)
    if extra:
        for w, (d, wgt) in extra.items():
            if w in txt:
                if d > 0:
                    bs += wgt
                elif d < 0:
                    be += -wgt
    net = bs - be
    rel = relevant(txt)
    if not rel:
        return {"bull": bs, "bear": be, "net": 0, "relevance": False,
                "label": "irrelevant", "strength": 0}
    if net >= 2:
        label = "bull"
    elif net <= -2:
        label = "bear"
    else:
        label = "neutral"
    return {"bull": bs, "bear": be, "net": net, "relevance": True,
            "label": label, "strength": max(-6, min(6, net))}


def token_candidates(txt):
    """从一段中文文本提取候选学习词（2~6 字 n-gram，含停用字/碎词的丢弃）。

    返回去重后的词列表。只在 screening 学习时使用。
    """
    out = []
    for run in _HANZI.findall(txt or ""):
        n = len(run)
        for size in range(2, min(7, n) + 1):
            for i in range(0, n - size + 1):
                w = run[i:i + size]
                if any(c in _STOP_CHARS for c in w):
                    continue
                if w in _STOP_WORDS or w in out:
                    continue
                # 太通用的“一/二/…”数字串也不学
                if w.isdigit():
                    continue
                out.append(w)
    return out


def is_base_word(w):
    """该词是否已在基础词典中（学习词不与基础词典重复叠加）。"""
    if not w:
        return True
    return w in BULL_STRONG or w in BULL_WEAK or \
        w in BEAR_STRONG or w in BEAR_WEAK
