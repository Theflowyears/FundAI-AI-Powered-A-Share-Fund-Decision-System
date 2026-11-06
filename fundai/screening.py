# -*- coding: utf-8 -*-
"""消息人工筛选 + 算法自我学习进化（可视化筛选的后端核心）。

数据流
------
1) 每日“拉取消息” → news.fetch_news() 全量抓取并自动打标（词典分 + 学习词叠加）；
2) ScreeningStore.ingest_feed() 把当天消息入库（保留用户已打标项）；
3) 用户在前端逐条复核/打标（利好/利空/无关 + 强度），rate_item() 落库；
4) learn_all()：按“用户标签与正文”，从中文 n-gram 候选里学出新词
   （词 → 方向/强度），存进 lexicon 表；news 采集自动打分时读取 learned_extra()，
   让算法“越筛越聪明”；
5) 用户还可给当天一个“次日大盘方向”判断（看多/看空/中性 + 信心），
   引擎在下一交易日收盘后自动结算命中率（direction_stats）；
6) 信息缺口（当天消息太少/抓取失败）写入 data/search_queue.json —— DSH 助手用
   本地搜索补全后回填 data/search_results.json，import_search_results() 合并入库。

本模块零第三方依赖，数据存 data/screening.db（独立于主账本，可安全删除重建）。
"""
import hashlib
import json
import re
import sqlite3
import threading
import time

from . import context, lexicon, util

# 用户打标标签 → 文案 / 方向 / 情绪强度（用于人工消息净分）
LABEL_TEXT = {"big_bull": "重大利好", "bull": "利好", "neutral": "中性",
              "bear": "利空", "big_bear": "重大利空", "irrelevant": "与市场无关"}
LABEL_POL = {"big_bull": 1, "bull": 1, "neutral": 0,
             "bear": -1, "big_bear": -1, "irrelevant": 0}
LABEL_STR = {"big_bull": 2, "bull": 1, "neutral": 0,
             "bear": -1, "big_bear": -2, "irrelevant": 0}
LABEL_DIRS = ("big_bull", "bull", "bear", "big_bear")
DIR_TEXT = {"bull": "看多", "bear": "看空", "neutral": "中性"}

SEARCH_QUEUE_FILE = util.data_file("search_queue.json")
SEARCH_RESULTS_FILE = util.data_file("search_results.json")
SEARCH_LAST_FILE = util.data_file("search_last_merge.json")

LABEL_RE = re.compile(r"[\u4e00-\u9fa5]+")

# 打标前的“清洗层”配置
RECENT_TITLE_DAYS = 7   # 该窗口内出现过相同(归一化)标题 → 视为重复滚动/跨日重播，不再重复入库
_NORM_RE = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]+")


def norm_title(title):
    """标题归一化：去标点/空白/大小写，用于跨日/滚动去重。"""
    return _NORM_RE.sub("", str(title or "")).lower()


# 产品/披露/行情播报类“例行噪音”：不占人工复核额度、不参与净情绪、默认归档
AUTO_NEUTRAL_EVENTS = {"fund_premium_warning", "holdings_disclosure",
                       "market_broadcast"}
BROADCAST_EVENT = "market_broadcast"

# ---------------------------------------------------------------------------
# 行情播报识别（2026-09-16 用户反馈）
# ---------------------------------------------------------------------------
# 用户原话：
#   「消息界面重复的消息还是太多了，比如那种财联社报黄金啥的涨跌之类的，还有类似这种的
#     深证成指涨1.01%，现报13422.186点；上证指数涨0.30%，现报3875.776点；
#     创业板指涨1.82%，现报3307.017点。，全部都让AI默认读取就行了，
#     只是单纯的消息面，根本不构成利空利好」
#
# 为什么必须当成一类专门处理，而不是继续靠"相似度去重"：
#   1) **不是新闻，是报价**：句子里没有"事件"，只有价格与涨跌。市场早就实时定价过了，
#      它对"次日大盘方向"不含任何增量信息（判利好利空是错的）；
#   2) **数字每次都在变** → 标题归一化去重失效（`深证成指涨1.01%` 与 `涨0.98%`
#      归一化后仍有差异），于是同一条行情一天能刷几十条；
#   3) 靠相似度归并只能把它们标成"重复"**但照样显示**，用户看到的还是满屏重复。
# 所以：识别为一类 → 整类归档 → 每天每组只留一条代表（带合并条数），原文另存可追溯。
BROADCAST_SUBJECTS = (
    # 指数
    "上证指数", "深证成指", "创业板指", "科创50", "沪深300", "中证500",
    "上证50", "北证50", "中证1000", "恒生指数", "恒生科技", "日经",
    "纳斯达克", "标普", "道琼斯", "富时中国", "A50",
    # 境外指数（财联社会播报亚太/欧洲盘）——实测「泰国综合指数跌幅扩大至1%」漏过
    "泰国综合指数", "韩国综合指数", "富时新加坡", "澳洲标普", "澳证200",
    "德国DAX", "法国CAC", "英国富时", "欧洲斯托克", "台湾加权", "印度孟买",
    # 商品/汇率/债
    "黄金", "白银", "原油", "布伦特", "WTI", "铜", "铝", "螺纹钢", "铁矿石",
    "比特币", "以太坊", "美元指数", "离岸人民币", "在岸人民币", "人民币",
    "十年期国债", "国债期货", "沪金", "沪银", "碳酸锂",
    # 板块指数（也常被播报）
    "半导体指数", "券商指数", "银行指数", "军工指数", "白酒指数", "医药指数",
)
# 报价动词/单位：出现才可能是"播报"，避免误伤"黄金价格上涨带动黄金股"这类真新闻
BROADCAST_MARKERS = ("现报", "报", "涨", "跌", "上涨", "下跌", "涨幅", "跌幅",
                     "点", "美元", "元/盎司", "元/吨", "元/桶", "元/克",
                     "日内", "盘中", "报收", "收盘价")
# 真事件词：出现这些说明是**有内容**的新闻（政策/公司/资金），不是纯播报
EVENT_WORDS = ("政策", "发布", "印发", "批准", "会议", "讲话", "表示", "宣布",
               "签约", "中标", "订购", "订单", "业绩", "预增", "预减", "亏损",
               "减持", "增持", "回购", "立案", "处罚", "退市", "违约", "降准",
               "降息", "加息", "收购", "重组", "投产", "量产", "获批", "出口",
               "进口", "库存", "产量", "需求", "供给", "研报", "评级", "目标价",
               # 板块/个股层面（2026-09-16 修误伤）：
               # 「黄金股集体涨停，山东黄金涨9.98%」是**板块事件**不是商品报价，
               # 出现"股/板块/概念"这类词时一律按真消息处理，绝不归档。
               "黄金股", "板块", "概念", "题材", "个股", "涨停潮", "龙头股",
               # 研判性表述：出现这些说明不是干巴巴的报价播报，而是有人在解读
               # （「上证指数跌破3000点，市场恐慌情绪升温」属于研判，不该归档）。
               "恐慌", "情绪", "升温", "降温", "承压", "走低", "走高", "升温",
               "破位", "失守", "创下", "领涨", "领跌", "分化", "观望",
               # 「历史首次站上900元/桶」「创新高/新低/创纪录」是**事件性表述**，
               # 不是例行报价，保留（实测被误归档过）。
               # 注意：**不把"突破/站上"放进这里** —— 「现货黄金突破4340美元/盎司，
               # 日内涨1.08%」其实还是报价播报（实测漏归档过）；真正的新闻性突破
               # 通常同时带"历史/首次/新高"，那一个词就足以拦住了。
               "历史", "首次", "新高", "新低", "创纪录")
# 人写的"盘面复盘"栏目：带这些词的通常是有人解读过的内容（午评/收盘分析/
# 涨停分析），不是机器刷的裸报价，一律保留。
COMMENTARY_WORDS = ("早盘", "午间", "午评", "早评", "收盘", "复盘", "涨停分析",
                    "跌停分析", "盘面", "异动", "一图看懂", "连板", "走强",
                    "走弱", "全线", "半日", "收评", "龙虎榜", "资金流向")
# 明确的"报价动词"：只有它们才说明这是播报
# （"突破/站上/触及/失守/跌破"也算报价动作，它们本身就是价格描述）
BROADCAST_VERBS = ("现报", "报", "涨", "跌", "上涨", "下跌", "涨幅", "跌幅",
                   "涨超", "跌超", "突破", "站上", "触及", "失守", "跌破")

_PCT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?\s*%")
_NUMDOT_RE = re.compile(r"\d+(?:\.\d+)?\s*点")


def _count_subjects(txt):
    """命中的"行情标的"名字。长名优先（'沪深300' 命中后不再重复算 '300'）。"""
    hit = []
    rest = txt
    for name in sorted(BROADCAST_SUBJECTS, key=len, reverse=True):
        if name in rest:
            hit.append(name)
            rest = rest.replace(name, " ")
    return hit


def detect_broadcast(txt):
    """判断是否"纯行情播报"，返回类型字符串或 None。

    判据（满足其一即算）：
      * **多标的播报**：≥2 个标的 + 百分比/点数 → 典型"三大指数播报"；
      * **单标的报价**：1 个标的 + 报价动作（现报/报/涨跌）+ 百分比或点数；
    同时要求**不含真事件词**（政策/业绩/减持…），避免把
    「央行降准」「黄金股涨停」这类真信号误归档。
    返回 'index_broadcast' / 'commodity_broadcast' / None。
    """
    txt = str(txt or "")
    if not txt or any(w in txt for w in EVENT_WORDS):
        return None
    # 人写的盘面复盘（早盘/午评/收盘/涨停分析…）不是裸报价 → 保留
    if any(w in txt for w in COMMENTARY_WORDS):
        return None
    subs = _count_subjects(txt)
    if not subs:
        return None
    has_pct = bool(_PCT_RE.search(txt))
    has_pt = bool(_NUMDOT_RE.search(txt))
    has_move = any(m in txt for m in BROADCAST_VERBS)
    if not (has_move and (has_pct or has_pt)):
        return None
    # 指数类 vs 商品/汇率类（仅供展示分类，处理方式一致）
    idx = {"上证指数", "深证成指", "创业板指", "科创50", "沪深300", "中证500",
           "上证50", "北证50", "恒生指数", "恒生科技", "日经", "纳斯达克",
           "标普", "道琼斯", "富时中国", "A50"}
    if len(subs) >= 2:
        return "index_broadcast" if (set(subs) & idx) else "commodity_broadcast"
    if set(subs) & idx:
        return "index_broadcast"
    return "commodity_broadcast"


def broadcast_key(title):
    """播报分组键：剥掉所有数字与标点 → 同一句模板的播报归为一组。

    `深证成指涨1.01%，现报13422.186点` 与 `深证成指涨0.98%，现报13380.5点`
    必须归到同一组（这是"数字一直在变导致去重失效"的解药）。
    """
    t = re.sub(r"\d+(?:\.\d+)?", "#", str(title or ""))
    return _NORM_RE.sub("", t)[:40]


def broadcast_summary(rows):
    """把一组播报渲染成一条"代表条"（保留原始条目数，便于用户知道发生了什么）。"""
    if not rows:
        return None
    rep = dict(rows[0])
    n = len(rows)
    names = []
    for r in rows:
        names += _count_subjects("{} {}".format(r.get("title") or "",
                                                r.get("text") or ""))
    uniq = sorted(set(names), key=lambda x: -len(x))[:4]
    rep["title"] = "【行情播报】{}（当日 {} 条同类，已自动归档）".format(
        "、".join(uniq) if uniq else "指数/商品报价", n)
    rep["text"] = "；".join((r.get("title") or "")[:60] for r in rows[:5])
    rep["auto_label"] = "irrelevant"
    rep["auto_strength"] = 0
    rep["auto_net"] = 0
    rep["event_type"] = BROADCAST_EVENT
    rep["auto_reason"] = ("行情播报（纯报价，不含事件，已被市场实时定价）"
                          "→ 自动归档，不参与消息面打分")
    rep["decided_by"] = "auto_archive"
    rep["_broadcast_n"] = n
    return rep



_CJK_ONLY_RE = re.compile(r"[^\u4e00-\u9fff]+")


def _skeleton(text):
    """标题骨架：只留汉字（剥掉数字/英文代码/百分比/标点），让同模板的批量
    披露（如“摩根大通在药明康德H股持股比例由9.90%升至10.01%”与“…中际旭创
    …13.44%升至14.01%”）归并为一条，其余不再占用人工复核额度。"""
    return _CJK_ONLY_RE.sub("", str(text or ""))[:64]


def collapse_same_template(items, ratio=0.82, min_len=12):
    """同模板批量消息归并（items 按时间倒序传入）。

    仅对“AI 判中性且未人工打标”的消息生效：与已保留某条标题骨架相似度 ≥ratio
    的，视为同一模板刷屏 → 归入 dupes（带 _dup_of 指向代表条 id）。方向明确
    (bull/bear/irrelevant)、已打标、或命中 AUTO_NEUTRAL_EVENTS 的原样保留。
    返回 (kept, dupes)。
    """
    import difflib
    kept, dupes, skels = [], [], []
    for r in items:
        lab = r.get("auto_label") or "neutral"
        if (r.get("user_label") or lab in ("bull", "bear", "irrelevant")
                or r.get("event_type") in AUTO_NEUTRAL_EVENTS):
            kept.append(r)
            continue
        key = _skeleton(r.get("title") or "")
        dup_of = None
        if len(key) >= min_len:
            for sk, rep_id in skels:
                if difflib.SequenceMatcher(None, sk, key).ratio() >= ratio:
                    dup_of = rep_id
                    break
        if dup_of:
            d = dict(r)
            d["_dup_of"] = dup_of
            dupes.append(d)
        else:
            if len(key) >= min_len:
                skels.append((key, r.get("item_id") or r.get("id")))
            kept.append(r)
    return kept, dupes


class ScreeningStore:
    def __init__(self, db_path=None):
        self.db_path = str(db_path or util.data_file("screening.db"))
        self.lock = threading.RLock()
        if self.db_path != ":memory:":
            import os
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                    timeout=30)
        self.conn.row_factory = sqlite3.Row
        # 词典学习：改后台线程做（打标不再阻塞），这里放协调用的锁与状态
        self._learn_lock = threading.Lock()
        self._learn_thread = None
        self._learn_pending = False
        self._schema()

    def _schema(self):
        with self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS items(
                date TEXT NOT NULL,
                item_id TEXT NOT NULL,
                source TEXT DEFAULT '',
                time TEXT DEFAULT '',
                title TEXT DEFAULT '',
                text TEXT DEFAULT '',
                url TEXT DEFAULT '',
                sectors TEXT DEFAULT '[]',
                funds TEXT DEFAULT '[]',
                auto_label TEXT DEFAULT '',
                auto_strength REAL DEFAULT 0,
                auto_net REAL DEFAULT 0,
                event_type TEXT DEFAULT '',
                auto_reason TEXT DEFAULT '',
                user_label TEXT DEFAULT '',
                user_strength REAL DEFAULT 0,
                rated_at TEXT,
                PRIMARY KEY(date, item_id)
            );
            CREATE TABLE IF NOT EXISTS direction(
                date TEXT PRIMARY KEY,
                dir TEXT NOT NULL,
                confidence REAL DEFAULT 1,
                created TEXT,
                resolved_date TEXT,
                next_chg REAL,
                hit INTEGER
            );
            CREATE TABLE IF NOT EXISTS intraday(
                date TEXT PRIMARY KEY,
                time TEXT,
                mid_price REAL,
                mid_chg REAL,
                summary TEXT,
                dir_view TEXT,
                confidence REAL,
                features TEXT,
                close REAL,
                pm_chg REAL,
                hit INTEGER,
                created TEXT,
                resolved TEXT
            );
            CREATE TABLE IF NOT EXISTS lexicon(
                word TEXT PRIMARY KEY,
                bull INTEGER DEFAULT 0,
                bear INTEGER DEFAULT 0,
                neutral INTEGER DEFAULT 0,
                docs INTEGER DEFAULT 0,
                updated TEXT
            );
            -- 术语 × **市场实际反应** 的逐日样本（2026-09-16 重做）：
            -- 一行 = "某天某术语出现了，且当天消息整体方向为 <sign>，次日沪深300 实际 <chg>"
            -- 这是"学习判断"的唯一事实来源：用市场反应学，不用词频猜。
            CREATE TABLE IF NOT EXISTS term_stats(
                date TEXT NOT NULL,
                word TEXT NOT NULL,
                sign INTEGER DEFAULT 0,      -- +1 当日消息整体偏多 / -1 偏空 / 0 中性
                chg REAL,                    -- 次日沪深300 涨跌幅（%）
                up INTEGER DEFAULT 0,        -- 次日是否上涨
                regime TEXT DEFAULT '',      -- 当日市场状态（上涨/下跌/震荡）
                src TEXT DEFAULT '',         -- ai / context / user
                updated TEXT,
                PRIMARY KEY(date, word)
            );
            -- 聚合后的条件统计（前端与打分只读这张表，避免每次全表扫）
            -- 关键字段是 hit/n/chg：**方向来自命中的市场反应**，不是词的极性
            CREATE TABLE IF NOT EXISTS term_agg(
                word TEXT PRIMARY KEY,
                n_up INTEGER DEFAULT 0,      -- 每个分组的样本数
                n_down INTEGER DEFAULT 0,
                n_neu INTEGER DEFAULT 0,
                n INTEGER DEFAULT 0,
                hit REAL,                    -- 多头样本中"次日上涨"的比例
                chg REAL,                    -- 平均次日涨跌（%）
                lift REAL,                   -- 相对基准上涨率的提升度
                by_regime TEXT DEFAULT '{}', -- {"上涨":{...},"下跌":{...},"震荡":{...}}
                updated TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_term_stats_word ON term_stats(word);
            -- 自动归档的原文（2026-09-16）：行情播报/同模板刷屏被合并后，
            -- 原始条目存这里而不是直接丢掉，保证"可追溯、可复核"。
            CREATE TABLE IF NOT EXISTS archived_items(
                date TEXT NOT NULL,
                item_id TEXT NOT NULL,
                source TEXT DEFAULT '',
                time TEXT DEFAULT '',
                title TEXT DEFAULT '',
                text TEXT DEFAULT '',
                url TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                archived_at TEXT,
                PRIMARY KEY(date, item_id)
            );
            """)
        # 兼容旧库：为 items 补新增列（event_type/auto_reason/auto_net/important）
        icols = [r["name"] for r in self.conn.execute("PRAGMA table_info(items)")]
        for cname, cdef in (("event_type", "TEXT DEFAULT ''"),
                            ("auto_reason", "TEXT DEFAULT ''"),
                            ("auto_net", "REAL DEFAULT 0"),
                            ("important", "INTEGER DEFAULT 0"),
                            # 2026-09-16：方向来源与把握度（AI 为主 / 本地上下文 /
                            # 人工）。没有这两列就无法回答"这条是谁判的、把握多大"，
                            # 也无法挑出"低把握度"给人工抽检。
                            ("decided_by", "TEXT DEFAULT ''"),
                            ("confidence", "REAL DEFAULT 0"),
                            ("evidence", "TEXT DEFAULT ''"),
                            ("next_chg", "REAL")):
            if cname not in icols:
                self.conn.execute("ALTER TABLE items ADD COLUMN {} {}".format(
                    cname, cdef))

    # ---------------- 消息入库 / 查询 ----------------
    def reapply_auto(self, date_s):
        """用最新语义规则重算某日已入库消息的自动标签/事件/依据（不覆盖人工标签）。

        规则升级后无需重新抓取即可让存量行同步新口径（如 ETF 溢价提示 → 产品层面中性）。
        返回处理的条数。
        """
        from . import semantics
        rows = self.items_for(date_s)
        n = 0
        with self.lock:
            for r in rows:
                ev = semantics.infer_news(r.get("title") or "",
                                          r.get("text") or "")
                evt = ev.get("event") or "dict"
                if evt == "none":
                    evt = "dict"
                with self.conn:
                    self.conn.execute(
                        "UPDATE items SET auto_label=?, auto_strength=?, "
                        "auto_net=?, event_type=?, auto_reason=? WHERE date=? "
                        "AND item_id=?",
                        (ev.get("label") or "neutral",
                         float(ev.get("strength") or 0),
                         float(ev.get("strength") or 0),
                         evt, (ev.get("reason") or "")[:400],
                         date_s, r["item_id"]))
                n += 1
        return n

    def recent_title_keys(self, asof_date_s=None, days=RECENT_TITLE_DAYS):
        """近 days 天（含当日）已入库标题的归一化键集合，用于打标前清洗去重。"""
        asof_date_s = asof_date_s or util.today_str()
        lo = util.add_days(asof_date_s, -(days - 1))
        rows = self.conn.execute(
            "SELECT title FROM items WHERE date>=? AND title!=''",
            (lo,)).fetchall()
        return {norm_title(r["title"]) for r in rows if norm_title(r["title"])}

    @staticmethod
    def _fuzzy_dup(keys, key, ratio=0.93):
        """归一化标题模糊重复判定（仅用于“长标题”的近重复转帖/滚动）。

        不同公司/不同ETF的模板化快讯虽共享大量句式，相似度通常在 0.85 以下；
        同事件换一两个字的长标题相似度在 0.95 上下——取 ≥0.93 & ≥28 字作为安全区。
        """
        if len(key) < 28:
            return False
        import difflib
        for t in keys:
            if not t or t == key or len(t) < 28:
                continue
            if abs(len(t) - len(key)) > 4:
                continue
            if difflib.SequenceMatcher(None, t, key).ratio() >= ratio:
                return True
        return False

    @staticmethod
    def review_split(items, cap=50, low_conf=0.45):
        """分流：**按 AI 把握度**挑出需要人工抽检的（前 cap 条），其余按 AI 采纳/已打标。

        2026-09-16 改造（用户："要我人工介入的每天不超过五十次！是一共五十次！"）：
        旧口径是"auto_label == neutral 的进人工额度"，问题有两个：
          ① 中性不代表"AI 没把握"——很多中性是 AI **很确定**地判成中性（如例行披露），
             把它们推给人工纯属浪费额度；
          ② 真正该看的是**低把握度**（AI 自己不确定），无论它当时判的是中性还是方向。
        现在：按 `confidence` 升序（最没把握的排最前），低于 `low_conf` 的才进人工队列；
        AI 有把握的一律直接采纳，不占人工额度。

        返回 (to_review, extra, ai_accepted, done)。items 保持传入顺序（除 to_review
        内部按把握度升序，便于"先看最不确定的"）。
        """
        to_review, extra, ai, done = [], [], [], []
        cap = int(cap)
        for r in items:
            if r.get("user_label"):
                done.append(r)
            else:
                conf = r.get("confidence")
                try:
                    conf = float(conf) if conf is not None else None
                except (TypeError, ValueError):
                    conf = None
                # conf 为 None：老数据（改造前入库、没有把握度字段）按"待确认"处理，
                # 与旧口径兼容；有把握度的一律按阈值判断。
                low = (conf is None) or (conf < float(low_conf))
                if low:
                    to_review.append(r)
                else:
                    ai.append(r)
        # 最没把握的排在最前（None 视为最不确定），其余保持原顺序
        to_review.sort(key=lambda r: (r.get("confidence") is not None,
                                      r.get("confidence") if r.get("confidence")
                                      is not None else -1.0))
        if cap > 0 and len(to_review) > cap:
            extra, to_review = to_review[cap:], to_review[:cap]
        return to_review, extra, ai, done

    def ingest_feed(self, date_s, feed, keep_user=True, skip_recent=True,
                    learn=True):
        """把当天全量消息写入（自动字段覆盖；用户已打标字段保留）。返回写入条数。

        `learn=True`（默认）：写完之后**后台触发一次术语学习**。为什么放在这里
        （2026-09-16）：AI 判定后不再需要人工打标，而旧实现只在 `rate_item()` 里
        触发学习——AI 全自动之后就再没有任何东西推动学习了，术语统计会永远空着。

        skip_recent=True：先做打标前清洗——
        1) 近 RECENT_TITLE_DAYS 天已入库的相同(归一化)标题直接跳过
           （滚动换时戳 / 跨日重播 / 跨源同文转帖）；
        2) 长标题与近期标题模糊相似(≥0.94)的跨源转帖/滚动重复也跳过；
        两种跳过均不影响“同一 item_id 的 force 刷新”。
        """
        if not feed:
            return 0
        n = 0
        recent_keys = None
        recent_list = []
        if skip_recent:
            recent_keys = self.recent_title_keys(date_s) if date_s else set()
            asof = date_s or util.today_str()
            lo = util.add_days(asof, -(RECENT_TITLE_DAYS - 1))
            rows = self.conn.execute(
                "SELECT title FROM items WHERE date>=? AND title!=''",
                (lo,)).fetchall()
            recent_list = [norm_title(r["title"]) for r in rows]
        with self.lock:
            for it in feed:
                item_id = it.get("id") or ""
                key = norm_title(it.get("title") or "")
                # ---- 行情播报：入库前就归到"播报类"，不占 AI 预算、不进净情绪 ----
                # （2026-09-16）这里不改写 feed 内容，只打上 event_type/标签，
                # 由 news_payload 的 AUTO_NEUTRAL_EVENTS 把它归到"已自动归档"。
                if not it.get("event_type") or it.get("event_type") == "dict":
                    kind = detect_broadcast("{} {}".format(
                        it.get("title") or "", it.get("text") or ""))
                    if kind:
                        it = dict(it)
                        it["event_type"] = BROADCAST_EVENT
                        it["auto_label"] = "irrelevant"
                        it["auto_strength"] = 0
                        it["auto_net"] = 0
                        it["decided_by"] = "auto_archive"
                        it["auto_reason"] = (
                            "行情播报（{}，纯报价不含事件）→ 自动归档，不参与打分".format(kind))
                if skip_recent:
                    same_id = bool(item_id) and self.conn.execute(
                        "SELECT 1 FROM items WHERE date=? AND item_id=?",
                        (date_s, item_id)).fetchone()
                    exact_dup = bool(key) and key in recent_keys
                    if exact_dup and not same_id:
                        continue  # 同标题近期已入库（且不是本条目刷新）→ 重复跳过
                    if not (exact_dup and same_id):
                        # 模糊重复：跨源转帖/滚动重播（同一 item_id 刷新除外）
                        if key and self._fuzzy_dup(recent_list, key):
                            continue
                if keep_user:
                    old = self.conn.execute(
                        "SELECT user_label,user_strength,rated_at FROM items "
                        "WHERE date=? AND item_id=?",
                        (date_s, item_id)).fetchone()
                else:
                    old = None
                user_label = (dict(old).get("user_label") if old else "") or ""
                user_strength = float(dict(old).get("user_strength") if old else 0) or 0.0
                rated_at = (dict(old).get("rated_at") if old else "") or ""
                # 判断来源兜底（2026-09-16）：带方向的消息可能是事件规则直接命中
                # （没走 AI/LLM），也可能被"例行噪音自动归档"提前拦下；
                # 这些路径不会写 decided_by，落库后会留下空值。
                # 这里按事件类型补一个**如实的**来源标签，保证每一行都答得出"谁判的"。
                decided_by = it.get("decided_by") or ""
                if not decided_by:
                    decided_by = ("event" if (it.get("event_type") or "") not in
                                  ("", "dict") else "context")
                with self.conn:
                    self.conn.execute(
                        "INSERT INTO items(date,item_id,source,time,title,text,url,"
                        "sectors,funds,auto_label,auto_strength,auto_net,event_type,"
                        "auto_reason,user_label,user_strength,rated_at,important,"
                        "decided_by,confidence,evidence) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(date,item_id) DO UPDATE SET "
                        "source=excluded.source,time=excluded.time,title=excluded.title,"
                        "text=excluded.text,url=excluded.url,sectors=excluded.sectors,"
                        "funds=excluded.funds,auto_label=excluded.auto_label,"
                        "auto_strength=excluded.auto_strength,auto_net=excluded.auto_net,"
                        "event_type=excluded.event_type,auto_reason=excluded.auto_reason,"
                        "user_label=excluded.user_label,"
                        "user_strength=excluded.user_strength,rated_at=excluded.rated_at,"
                        "important=excluded.important,"
                        # 2026-09-16：**判断来源、把握度、证据必须落库**。
                        # 少了这三列，"谁判的""AI 有多大把握"在库里无从查证，
                        # 人工抽检队列（按把握度排序）会退化成"全表都是 NULL"。
                        "decided_by=excluded.decided_by,"
                        "confidence=excluded.confidence,"
                        "evidence=excluded.evidence",
                        (date_s, item_id, it.get("source") or "",
                         it.get("time") or "", it.get("title") or "",
                         it.get("text") or "", it.get("url") or "",
                         json.dumps(it.get("sectors") or [], ensure_ascii=False),
                         json.dumps(it.get("funds") or [], ensure_ascii=False),
                         it.get("auto_label") or "", float(it.get("auto_strength") or 0),
                         float(it.get("auto_net") or 0),
                         it.get("event_type") or "",
                         (it.get("auto_reason") or "")[:400],
                         user_label, user_strength, rated_at,
                         int(it.get("important") or 0),
                         decided_by,
                         float(it.get("confidence") or 0),
                         (it.get("evidence") or "")[:2000]))
                if recent_keys is not None and key:
                    recent_keys.add(key)
                n += 1
        # 自动学习：AI 为主之后，打标不再由人触发，学习必须自己跟上。
        # 用异步（learn_async）避免拉取请求被学习拖慢；失败也不影响入库结果。
        if learn and n:
            try:
                self.learn_async()
            except Exception:
                pass
        return n

    def items_for(self, date_s):
        rows = self.conn.execute(
            "SELECT * FROM items WHERE date=? ORDER BY time DESC, rowid DESC",
            (date_s,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["sectors"] = json.loads(d.get("sectors") or "[]")
            except Exception:
                d["sectors"] = []
            try:
                d["funds"] = json.loads(d.get("funds") or "[]")
            except Exception:
                d["funds"] = []
            out.append(d)
        return out

    def rate_item(self, date_s, item_id, label, strength=None):
        """用户打标一条消息。label ∈ LABEL_TEXT.keys()。随后重算学习词典。"""
        if label not in LABEL_TEXT:
            raise ValueError("未知标签: {}".format(label))
        w = float(LABEL_STR.get(label, 0)) if strength is None else float(strength)
        with self.lock:
            with self.conn:
                cur = self.conn.execute(
                    "UPDATE items SET user_label=?, user_strength=?, rated_at=? "
                    "WHERE date=? AND item_id=?",
                    (label, w, util.now_iso(), date_s, item_id))
                if cur.rowcount == 0:
                    raise ValueError("消息不存在: {} / {}".format(date_s, item_id))
        # 打标只写库 + 触发**后台**学习：不阻塞请求（旧实现在这里同步重跑全量学习，
        # 消息一多就把界面卡住 —— 2026-09-15 用户反馈“主动标注特别慢”的根因）。
        self.learn_async()
        row = self.conn.execute(
            "SELECT * FROM items WHERE date=? AND item_id=?",
            (date_s, item_id)).fetchone()
        return dict(row) if row else None

    def labels_summary(self, date_s):
        """当天打标情况：{labeled, by_label:{}, net, directional}。"""
        rows = self.items_for(date_s)
        by, net, n_dir = {}, 0.0, 0
        for r in rows:
            lab = r.get("user_label") or ""
            if not lab:
                continue
            by[lab] = by.get(lab, 0) + 1
            # 人工打标强度本就有符号（LABEL_STR：bear=-1/big_bear=-2），
            # 这里统一走 signed_strength 归一化，保证与自动侧口径一致
            net += lexicon.signed_strength(lab, r.get("user_strength"))
            if lab in LABEL_DIRS:
                n_dir += 1
        return {"labeled": sum(by.values()), "by_label": by,
                "net": round(net, 2), "directional": n_dir}

    def human_news_meta(self, date_s, amplitude=8, mode="scaled", scale=10.0):
        """用户已打标 → 人工消息分（方向性标签≥1 才启用）。

        返回 {score, net, net_raw, net_mode, directional, labeled} 或 None。
        净情绪口径与自动消息面一致：`scaled`（默认）按刻度换算抗饱和——
        旧口径 `clamp(Σ强度, ±8)` 在打标条数多时同样天天顶格（实测 51 条标签
        Σ=27 → 顶格 +8 → 分数恒定 64 → 合成限幅后恒 +25 = 零区分度）；
        `mode="sum"` 可复现旧口径。
        """
        s = self.labels_summary(date_s)
        if s["directional"] < 1:
            return None
        net_raw = float(s["net"])
        if mode == "sum" or not scale:
            net = max(-8.0, min(8.0, net_raw))
        else:
            net = max(-8.0, min(8.0, net_raw / float(scale)))
        return {"score": int(max(-100, min(100, net * max(1, int(amplitude))))),
                "net": round(net, 3), "net_raw": round(net_raw, 1),
                "net_mode": mode, "net_scale": float(scale or 0),
                "directional": s["directional"], "labeled": s["labeled"]}

    def news_breakdown(self, date_s, human_mode=False, top=8, mode="scaled",
                       scale=None):
        """消息分明细：{net, counts, directional, items(按|强度|降序 top)}。

        human_mode=True 统计用户方向打标（与 human_news_meta 同口径）；
        False 统计自动词典标签。供 records.calc 评分明细展示“分数怎么来的”。
        """
        counts, contribs, net = {}, [], 0.0
        for r in self.items_for(date_s):
            if human_mode:
                lab, strength = (r.get("user_label") or "",
                                 float(r.get("user_strength") or 0))
            else:
                lab, strength = (r.get("auto_label") or "",
                                 float(r.get("auto_strength") or 0))
            if lab not in LABEL_DIRS:
                continue
            counts[lab] = counts.get(lab, 0) + 1
            # 按标签取符号（自动侧事件型强度是正数、方向在 label 上；人工侧本就有符号，
            # 归一化后两者一致，避免“利空算成利多”，证据见 lexicon.signed_strength）
            strength = lexicon.signed_strength(lab, strength)
            net += strength
            contribs.append({"title": (r.get("title") or "")[:70],
                             "label": lab, "strength": round(strength, 2)})
        contribs.sort(key=lambda x: -abs(x["strength"]))
        net_raw = round(net, 2)
        sc = float(scale or (10.0 if human_mode else 240.0))
        if mode == "sum" or not sc:
            net_scaled = max(-8.0, min(8.0, net_raw))
        else:
            net_scaled = max(-8.0, min(8.0, net_raw / sc))
        return {"net": net_raw, "net_raw": net_raw,
                "net_scaled": round(net_scaled, 3), "net_mode": mode,
                "net_scale": sc, "counts": counts,
                "directional": sum(counts.values()), "items": contribs[:top]}

    # ---------------- 学习：用户标签 → 词典进化 ----------------
    # 设计要点（2026-09-15 按用户要求重做）：
    #   ① **只扫完整金融术语**（lexicon.scan_terms，Aho-Corasick 线性扫描）——
    #      不再枚举 2~6 字滑窗，所以词典里不可能出现“基金二级市场”这种碎片；
    #   ② **学的是“术语 × 方向”的内在联系**，不是词本身：
    #      对每个术语统计它在利好/利空/中性样本里各出现多少次，
    #      用**提升度（lift）**衡量“这个术语与某个方向到底有多强的关联”，
    #      只把 lift 足够高、方向足够稳的术语收进学习词典；
    #   ③ **异步学习**：打标只写库，学习放到后台线程（打标点一下就返回，不再卡）。
    MIN_DOCS = 2          # 至少出现在 2 条已打标消息里（单次出现不足以说明关联）
    MIN_TERM_DOCS = 2
    RELEVANCE_MIN = 1.35  # 方向侧占比 / 中性侧占比 的最小提升度
    MAX_TERMS = 400
    # ---- 新口径（2026-09-16）：学"术语 × 市场实际反应" ----
    MIN_SAMPLES = 5       # 少于这么多**真实反应样本**就不给方向（宁缺勿滥）

    # ------------------------------------------------------------------
    # 主入口：重建"术语 × 市场实际反应"统计
    # ------------------------------------------------------------------
    def learn_terms(self, min_samples=None):
        """重建术语统计：**只记录市场怎么反应，不给词贴永久方向**。

        与旧 `learn_all()` 的根本区别（用户 2026-09-16 指出"利空利多每天都在变"）：
          * 旧：统计"某词在利多消息里出现几次" → 于是「自动驾驶 利多 34 次」，
                方向被当成词的性质永久固化，且"交易所"这类**写作高频词**也被收进去；
          * 新：统计"含某词的消息**次日市场实际怎么走**" → 得到的是
                (样本数 n, 上涨比例 hit, 平均涨跌 chg, 分组 by_regime)，
                打分时再结合**上下文**（结果动词/对象层级/否定转折）决定方向。

        样本单位是"术语×日期"，方向来自次日沪深300 的真实涨跌，
        因此天然会随行情阶段变化（同一词在上涨段与下跌段拿到不同 hit）。
        """
        need = int(min_samples or self.MIN_SAMPLES)
        rows = self.conn.execute(
            "SELECT date, title, text, auto_label, user_label, event_type, "
            "decided_by FROM items").fetchall()
        # 次日涨跌：优先用账本 records（run_daily 每天落库），没有就跳过该日
        chg_by_date = self._next_day_chg(getattr(self, "_ledger_db", None))
        now = util.now_iso()
        samples = {}
        dates = set()
        for r in rows:
            date_s = str(r["date"])[:10]
            nxt = chg_by_date.get(date_s)
            if nxt is None:
                continue                      # 没有市场反应 → 这条不产生样本
            # 产品层面风险提示（ETF 溢价/临停）不是方向信号，其模板句会污染统计
            if r["event_type"] == "fund_premium_warning":
                continue
            lab = r["user_label"] or r["auto_label"] or ""
            if r["user_label"]:
                src = "user"
            elif r["decided_by"]:
                src = str(r["decided_by"])
            else:
                src = "context"
            pol = LABEL_POL.get(lab, 0)
            txt = "{} {}".format(r["title"] or "", r["text"] or "")
            terms = lexicon.scan_terms(txt)
            if not terms:
                continue
            sign = 1 if pol > 0 else (-1 if pol < 0 else 0)
            regime = "上涨" if nxt > 0.3 else ("下跌" if nxt < -0.3 else "震荡")
            dates.add(date_s)
            for w in terms:
                if lexicon.is_base_word(w) or w in context.NOISE_TERMS:
                    continue
                key = (date_s, w)
                cur = samples.get(key)
                if cur is None:
                    samples[key] = [sign, nxt, 1 if nxt > 0 else 0, regime, src]
                else:
                    cur[0] = max(cur[0], sign, key=lambda x: abs(x))
        # 写出逐日样本
        with self.lock:
            with self.conn:
                self.conn.execute("DELETE FROM term_stats")
                self.conn.executemany(
                    "INSERT OR REPLACE INTO term_stats"
                    "(date,word,sign,chg,up,regime,src,updated) VALUES(?,?,?,?,?,?,?,?)",
                    [(d, w, v[0], util.r4(v[1]), v[2], v[3], v[4], now)
                     for (d, w), v in samples.items()])
        # 聚合成条件统计
        agg = self._aggregate_terms()
        return len(agg)

    def _next_day_chg(self, ledger_db=None):
        """{日期: 次日沪深300涨跌幅%}。来自**账本**的每日 records（真实收盘价差）。

        为什么要从账本读：`records` 表（每日 index_close）属于账本库
        `data/fund200.db`，不在消息库里；而"学习"必须用**市场的真实反应**当老师，
        这正是本模块与旧"词频词典"的分水岭。
        `ledger_db` 可注入（测试用临时库），省略时按 `util.data_file` 解析。
        """
        out = {}
        try:
            recs = self._records(ledger_db)
        except Exception:
            return out
        dates = sorted(recs)
        for i, d in enumerate(dates[:-1]):
            a = recs.get(d)
            b = recs.get(dates[i + 1])
            if not a or not b:
                continue
            try:
                ca, cb = float(a), float(b)
            except (TypeError, ValueError):
                continue
            if ca > 0 and cb > 0:
                out[d] = (cb / ca - 1.0) * 100.0
        return out

    def _records(self, ledger_db=None):
        """日期 → 指数收盘（账本 records 表）。读不到就返回空（学习降级为"无样本"）。"""
        path = ledger_db
        if path is None:
            try:
                path = util.data_file("fund200.db")
            except Exception:
                return {}
        try:
            con = sqlite3.connect(str(path))
        except Exception:
            return {}
        try:
            rows = con.execute(
                "SELECT date, index_close FROM records ORDER BY date").fetchall()
        except Exception:
            return {}
        finally:
            try:
                con.close()
            except Exception:
                pass
        out = {}
        for r in rows:
            try:
                v = float(r[1])
            except (TypeError, ValueError, IndexError):
                continue
            if v > 0:
                out[str(r[0])[:10]] = v
        return out

    def _aggregate_terms(self):
        """逐日样本 → 条件统计（含基准率与提升度）。"""
        rows = self.conn.execute(
            "SELECT word,sign,chg,up,regime FROM term_stats").fetchall()
        base_up = base_n = 0
        buckets = {}
        for r in rows:
            base_n += 1
            base_up += int(r["up"] or 0)
            w = r["word"]
            b = buckets.setdefault(w, {"n": 0, "up": 0, "chg": 0.0, "by": {}})
            b["n"] += 1
            b["up"] += int(r["up"] or 0)
            b["chg"] += float(r["chg"] or 0.0)
            reg = str(r["regime"] or "")
            rb = b["by"].setdefault(reg, {"n": 0, "up": 0, "chg": 0.0})
            rb["n"] += 1
            rb["up"] += int(r["up"] or 0)
            rb["chg"] += float(r["chg"] or 0.0)
        base_rate = (base_up / float(base_n)) if base_n else 0.5
        now = util.now_iso()
        out = {}
        for w, b in buckets.items():
            n = b["n"]
            if n < self.MIN_SAMPLES or n == 0:
                continue                # 样本不足 → 不给方向
            up = b["up"] / float(n)
            by = {}
            for reg, rb in b["by"].items():
                if rb["n"] >= max(3, self.MIN_SAMPLES // 2):
                    by[reg] = {"n": rb["n"], "hit": round(rb["up"] / float(rb["n"]), 3),
                               "chg": round(rb["chg"] / rb["n"], 3)}
            lift = round(up / base_rate, 3) if base_rate > 0 else 1.0
            out[w] = {"n": n, "hit": round(up, 3),
                      "chg": round(b["chg"] / n, 3), "lift": lift,
                      "by_regime": by}
        # 方向性排序：|hit-0.5| 大的排前面
        ordered = sorted(out.items(), key=lambda kv: -abs(kv[1]["hit"] - 0.5))[:self.MAX_TERMS]
        with self.lock:
            with self.conn:
                self.conn.execute("DELETE FROM term_agg")
                self.conn.executemany(
                    "INSERT OR REPLACE INTO term_agg"
                    "(word,n_up,n_down,n_neu,n,hit,chg,lift,by_regime,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [(w, 0, 0, 0, v["n"], v["hit"], v["chg"], v["lift"],
                      json.dumps(v["by_regime"], ensure_ascii=False), now)
                     for w, v in ordered])
        return dict(ordered)

    def term_stats(self, limit=None, min_samples=None):
        """{word: {n, hit, chg, lift, by_regime}} —— 供 `context.assess()` 使用。

        只返回**样本充足**的术语；打分时还要与上下文证据合并，绝不单独定方向。
        `min_samples` 省略时用 `self.MIN_SAMPLES`（可被
        `strategy.context_min_samples` 覆盖，见 news._term_stats）。
        """
        need = int(min_samples or self.MIN_SAMPLES)
        try:
            rows = self.conn.execute(
                "SELECT * FROM term_agg ORDER BY n DESC").fetchall()
        except Exception:
            return {}
        out = {}
        for r in rows:
            if int(r["n"] or 0) < need:
                continue
            try:
                by = json.loads(r["by_regime"] or "{}")
            except Exception:
                by = {}
            out[r["word"]] = {"n": r["n"], "hit": r["hit"], "chg": r["chg"],
                              "lift": r["lift"], "by_regime": by}
            if limit and len(out) >= int(limit):
                break
        return out

    def learn_all(self):
        """向后兼容的旧入口：等价于 `learn_terms()`。"""
        return self.learn_terms()

    def learn_async(self, delay=0.3):
        """**异步**学习：打标立刻返回，学习由后台线程做（同一时刻只跑一个）。

        为什么必须异步（2026-09-15 用户反馈“主动标注特别慢”）：旧实现每点一次打标
        都会在请求线程里**同步**重跑 `learn_all()`（全量已打标消息 × 全部候选词），
        消息一多就是几秒到几十秒的卡顿。现在打标只写一行，学习在后台合并执行。
        """
        import threading
        with self._learn_lock:
            if self._learn_thread and self._learn_thread.is_alive():
                self._learn_pending = True
                return False
            self._learn_pending = False

            def _work():
                try:
                    time.sleep(delay)          # 合并连续点击（连点 5 次只学 1 次）
                    while True:
                        self.learn_all()
                        with self._learn_lock:
                            if not self._learn_pending:
                                break
                            self._learn_pending = False
                except Exception:
                    pass

            self._learn_thread = threading.Thread(target=_work, daemon=True)
            self._learn_thread.start()
            return True

    def wait_learn(self, timeout=30.0):
        """等后台学习结束（测试/命令行用）。"""
        t = self._learn_thread
        if t is not None:
            t.join(timeout)
        return not (t is not None and t.is_alive())

    def lexicon_relevance(self, limit=50):
        """{word: lift}：术语的**市场反应**相对基准的提升度（不是"词的方向强度"）。"""
        try:
            rows = self.conn.execute(
                "SELECT word, lift FROM term_agg ORDER BY ABS(lift-1) DESC LIMIT ?",
                (int(limit),)).fetchall()
            return {r["word"]: r["lift"] for r in rows}
        except Exception:
            return {}

    def lexicon_rows(self, limit=200):
        """学习成果展示：**市场反应统计**。

        返回字段与旧版保持同名前缀，但语义已改（2026-09-16）：
          word / n（样本数）/ hit（含该词的消息次日上涨比例）/ chg（平均次日涨跌）
          / lift（相对基准）/ by_regime（按市场状态分组）/ updated
        旧字段 bull/bear 不再代表"词的方向"，置 0 以免前端误读成永久极性。
        """
        try:
            rows = self.conn.execute(
                "SELECT * FROM term_agg ORDER BY n DESC, ABS(lift-1) DESC LIMIT ?",
                (int(limit),)).fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            try:
                by = json.loads(r["by_regime"] or "{}")
            except Exception:
                by = {}
            out.append({"word": r["word"], "bull": 0, "bear": 0,
                        "neutral": r["n_neu"], "docs": r["n"],
                        "hit": r["hit"], "chg": r["chg"], "lift": r["lift"],
                        "by_regime": by, "updated": r["updated"]})
        return out

    def learned_extra(self):
        """兼容旧调用：**不再返回词的方向**。

        旧实现返回 `{word: (+1/-1, weight)}`，只要字面出现就按固定方向加分——正是
        用户指出的"把方向永久钉在词上"（"自动驾驶这些，利空利多不是每天都在变吗"）。
        现在返回空字典：学习成果只以 `term_stats()` 的**条件统计**形式参与判断，
        由 `context.assess()` 结合上下文（结果动词/对象层级/否定转折/市场状态）使用。
        """
        return {}

    # ---------------- 方向判断与命中率 ----------------
    def set_direction(self, date_s, direction, confidence=1.0):
        if direction not in DIR_TEXT:
            raise ValueError("方向必须是 bull/bear/neutral")
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO direction(date,dir,confidence,created) "
                    "VALUES(?,?,?,?) "
                    "ON CONFLICT(date) DO UPDATE SET dir=excluded.dir,"
                    "confidence=excluded.confidence,created=excluded.created",
                    (date_s, direction, util.clamp(float(confidence), 0.1, 1.0),
                     util.now_iso()))
        return self.direction(date_s)

    def direction(self, date_s):
        r = self.conn.execute(
            "SELECT * FROM direction WHERE date=?", (date_s,)).fetchone()
        return dict(r) if r else None

    def direction_pending(self, today_s):
        """跨天显示用：取“已给出、但还没结算、且针对今天（或更晚）”的那条判断。

        判断按“判断日”入库（date=给出判断的那天，针对次日），因此跨天后前端若只查
        `date=today` 会显示空白——这里把它补上（含 made_on/for_date/pending 标记）。

        2026-09-12 修正（用户反馈“周末/周一打开面板看不到 AI 次日方向”）：
        旧实现要求 `add_trading_days(date,1) == today`，于是**非交易日**（周六/周日/
        节假日）必然匹配不到任何行 → 面板一直显示“今日尚未运行研判”，看起来像
        “不再自动记录方向”。现在改为“针对日期 >= 今天且尚未结算”的最新一条判断，
        并把 `date` 归一化成它**针对的交易日**（for_date），前端无需再懂交易日历。
        """
        today_s = str(today_s)[:10]
        rows = self.conn.execute(
            "SELECT * FROM direction WHERE date <= ? ORDER BY date DESC LIMIT 8",
            (today_s,)).fetchall()
        best = None
        for r in rows:
            d = dict(r)
            try:
                nxt = util.add_trading_days(d["date"], 1)
            except Exception:
                continue
            if nxt < today_s:
                continue                      # 判断针对的那天已经过去 → 不再“待结算展示”
            best = d
            best["made_on"] = d["date"]       # 判断日（给出判断的那天）
            best["for_date"] = nxt            # 针对的交易日
            best["date"] = nxt                # 归一化：前端按“判断日”列显示交易日
            best["pending"] = d.get("resolved_date") is None
            break
        return best

    # ---------------- 午间盘中诊断（学习/自检用） ----------------
    def intraday_upsert(self, row):
        """写入/覆盖当日午间诊断记录。"""
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO intraday(date,time,mid_price,mid_chg,summary,"
                    "dir_view,confidence,features,created) VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(date) DO UPDATE SET time=excluded.time,"
                    "mid_price=excluded.mid_price,mid_chg=excluded.mid_chg,"
                    "summary=excluded.summary,dir_view=excluded.dir_view,"
                    "confidence=excluded.confidence,features=excluded.features,"
                    "created=excluded.created",
                    (str(row.get("date"))[:10], str(row.get("time") or ""),
                     float(row.get("mid_price") or 0), float(row.get("mid_chg") or 0),
                     str(row.get("summary") or "")[:2000],
                     str(row.get("dir_view") or "neutral"),
                     float(row.get("confidence") or 0.6),
                     str(row.get("features") or ""), util.now_iso()))
        return self.intraday_get(row.get("date"))

    def intraday_get(self, date_s):
        r = self.conn.execute("SELECT * FROM intraday WHERE date=?",
                              (str(date_s)[:10],)).fetchone()
        return dict(r) if r else None

    def intraday_settle(self, date_s, close, pm_chg, hit):
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "UPDATE intraday SET close=?, pm_chg=?, hit=?, resolved=? "
                    "WHERE date=?", (util.r2(close), util.r4(pm_chg), int(hit),
                                     util.now_iso(), str(date_s)[:10]))
        return self.intraday_get(date_s)

    def intraday_stats(self, limit=60):
        rows = self.conn.execute(
            "SELECT * FROM intraday ORDER BY date DESC LIMIT ?",
            (int(limit),)).fetchall()
        hist = [dict(r) for r in rows]
        done = [x for x in hist if x.get("hit") is not None]
        ok = sum(1 for x in done if x["hit"])
        by_view = {}
        for x in done:
            s = by_view.setdefault(x.get("dir_view") or "neutral", [0, 0])
            s[0] += 1
            s[1] += 1 if x["hit"] else 0
        return {"history": hist, "total": len(done), "hit": ok,
                "hit_rate": (ok / len(done)) if done else None,
                "by_view": by_view, "latest": hist[0] if hist else None}

    def resolve_directions(self, today_s, today_chg):
        """today_s 收盘后结算：凡“判断日已过去至少一个交易日”的未结算记录，
        用 today_s 的当日涨跌结算命中。返回结算条数。"""
        if today_chg is None:
            return 0
        rows = self.conn.execute(
            "SELECT date,dir FROM direction WHERE resolved_date IS NULL").fetchall()
        done = 0
        for r in rows:
            expect = util.add_trading_days(r["date"], 1)
            if today_s < expect:
                continue
            chg = float(today_chg)
            d = r["dir"]
            hit = 1 if (d == "bull" and chg > 0) else (
                1 if (d == "bear" and chg < 0) else (
                    1 if d == "neutral" and abs(chg) <= 0.003 else 0))
            with self.lock:
                with self.conn:
                    self.conn.execute(
                        "UPDATE direction SET resolved_date=?, next_chg=?, hit=? "
                        "WHERE date=?", (today_s, util.r4(chg), hit, r["date"]))
            done += 1
        return done

    def direction_stats(self, limit=400):
        rows = self.conn.execute(
            "SELECT * FROM direction WHERE hit IS NOT NULL "
            "ORDER BY date DESC LIMIT ?", (int(limit),)).fetchall()
        hist = []
        for r in rows:
            d = dict(r)
            d["hit_txt"] = "命中" if d["hit"] else "未中"
            hist.append(d)
        tot = len(hist)
        ok = sum(1 for x in hist if x["hit"])
        by_dir = {}
        for x in hist:
            s = by_dir.setdefault(x["dir"], [0, 0])
            s[0] += 1
            s[1] += (1 if x["hit"] else 0)
        return {"history": hist, "total": tot, "hit": ok,
                "hit_rate": (ok / tot) if tot else None,
                "by_dir": {k: {"n": v[0], "hit": v[1],
                               "rate": (v[1] / v[0]) if v[0] else None}
                           for k, v in by_dir.items()}}

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ================= 搜索补全队列（DSH 本地搜索回填） =================
def _queue_file_state():
    return util.load_json(SEARCH_QUEUE_FILE, {"items": []})


def _save_queue(state):
    state["updated"] = util.now_iso()
    util.save_json(SEARCH_QUEUE_FILE, state)


def open_queue_items():
    state = _queue_file_state()
    return [it for it in state.get("items", []) if it.get("status") == "open"]


def ensure_daily_news_queue(date_s, ok, count, note=""):
    """当天消息太少/抓取失败时，写一条“待 DSH 本地搜索补全”的队列项。"""
    reason = None
    if not ok:
        reason = "在线消息源全部失败"
    elif count < 8:
        reason = "当天仅抓到 {} 条，偏少，建议搜索补全综述".format(count)
    if not reason:
        return open_queue_items()
    query = "{} A股 收盘 市场要闻 利好 利空 政策 汇总".format(date_s)
    qid = hashlib.md5("{}|daily_news|{}".format(date_s, query).encode()).hexdigest()[:12]
    state = _queue_file_state()
    items = state.setdefault("items", [])
    for it in items:
        if it.get("id") == qid:
            return [x for x in items if x.get("status") == "open"]
    items.append({"id": qid, "date": date_s, "kind": "daily_news",
                  "query": query, "reason": reason,
                  "status": "open", "created": util.now_iso(),
                  "note": note or ""})
    _save_queue(state)
    return [x for x in items if x.get("status") == "open"]


def import_search_results(cfg=None):
    """把 DSH 助手回填的 data/search_results.json 合并进当天消息库。

    处理流程：按 queue_id 找到队列项(open) → 把结果里的标题/摘要打标后入库
    （source=本地搜索回填）→ 队列项置为 merged → 写 data/search_last_merge.json。
    幂等：队列项已非 open 的结果会跳过。
    """
    res = util.load_json(SEARCH_RESULTS_FILE)
    results = (res or {}).get("results") or []
    if not results:
        return {"added": 0, "note": "data/search_results.json 无内容"}
    queue = {it["id"]: it for it in _queue_file_state().get("items", [])
             if it.get("status") == "open"}
    store = ScreeningStore()
    add_rows = []
    merged_ids = []
    for item in results:
        qid = item.get("queue_id")
        q = queue.get(qid)
        if not q or not item.get("ok"):
            continue
        date_s = q.get("date") or util.today_str()
        for i, n in enumerate((item.get("items") or [])[:30]):
            title = (n.get("title") or "").strip()[:200]
            snip = (n.get("snippet") or n.get("summary") or "").strip()
            add_rows.append({"date": date_s, "qid": qid, "idx": i,
                             "title": title or snip[:80], "text": snip or title,
                             "url": n.get("url") or "",
                             "time": (n.get("time") or q.get("created") or "")[:16],
                             "note": q.get("note") or ""})
        merged_ids.append(qid)
    added, by_date = 0, {}
    if add_rows:
        stats = store.term_stats()
        regime = None
        try:
            from . import news as _news
            regime = _news.market_regime(cfg)
        except Exception:
            regime = None
        # 与线上同口径打分（上下文条件化），并把结果交给 AI 定级——
        # 回填来的消息和实时抓取的消息必须用**同一套判断**，否则又会出现两套口径。
        feed = []
        for a in add_rows:
            entry = _news.score_item(
                {"id": "SRC-{}-{}".format(a["qid"], a["idx"]),
                 "source": "本地搜索回填", "time": a["time"],
                 "title": a["title"], "text": a["text"], "url": a["url"]},
                pool=None, term_stats=stats, regime=regime)
            if entry:
                entry["_date"] = a["date"]
                feed.append(entry)
        try:
            from . import llm_news
            sel = llm_news.label_items(feed, cfg, regime=regime, all_items=True)
            if sel.get("labels"):
                llm_news.apply_labels(feed, sel["labels"], contribute=True)
        except Exception:
            pass
        rows_by_date = {}
        for e in feed:
            rows_by_date.setdefault(e.pop("_date"), []).append(e)
        for date_s, rows in rows_by_date.items():
            store.ingest_feed(date_s, rows, keep_user=True)
            added += len(rows)
            by_date[date_s] = by_date.get(date_s, 0) + len(rows)
    if merged_ids:
        state = _queue_file_state()
        for it in state.get("items", []):
            if it.get("id") in merged_ids:
                it["status"] = "merged"
                it["merged_at"] = util.now_iso()
        _save_queue(state)
    out = {"added": added, "by_date": by_date,
           "note": ("新增 {} 条搜索补全消息".format(added) if added
                    else "本次无可合并的新结果")}
    util.save_json(SEARCH_LAST_FILE, {"run_at": util.now_iso(), **out})
    store.close()
    return out


def auto_merge_search_results(cfg=None, date_s=None):
    """**自动**合并搜索回填结果（把"点一次按钮"变成"不用点"）。

    为什么：用户要求"每天人工介入一共不超过 50 次"，而旧流程里
    「合并搜索回填结果」是一个必须手点的按钮——即使结果早就躺在
    `data/search_results.json` 里，不点就不生效。这类"纯粹等人点一下"的动作
    应当彻底自动化，所以：
      * 每次研判/拉取消息之前调用一次本函数；
      * 合并成功记 `interventions.auto('search_merge')`（不占人工额度，但可量化）；
      * 有结果却合并不了（queue_id 对不上等）时记 `human('search_merge')` 并说明，
        此时才真正需要人看一眼。
    """
    try:
        res = util.load_json(SEARCH_RESULTS_FILE) or {}
    except Exception:
        res = {}
    results = [r for r in (res.get("results") or []) if isinstance(r, dict)]
    if not results:
        return {"merged": False, "reason": "无待合并结果"}
    try:
        out = import_search_results(cfg)
    except Exception as e:
        try:
            from . import interventions as _iv
            _iv.human("search_merge", detail=[{"why": "自动合并失败：{}".format(
                str(e)[:120])}], date_s=date_s)
        except Exception:
            pass
        return {"merged": False, "reason": str(e)[:160]}
    added = int(out.get("added") or 0)
    try:
        from . import interventions as _iv
        if added:
            _iv.auto("search_merge", n=1, date_s=date_s, detail=[{
                "why": "自动合并 {} 条本地搜索补全消息，无需人工点击".format(added)}])
        else:
            _iv.human("search_merge", detail=[{
                "why": "有搜索结果但未合并成功（queue_id 可能不匹配），需人工确认"}],
                date_s=date_s)
    except Exception:
        pass
    return {"merged": bool(added), "added": added, "note": out.get("note")}
