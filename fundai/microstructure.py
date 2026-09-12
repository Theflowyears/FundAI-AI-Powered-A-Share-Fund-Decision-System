# -*- coding: utf-8 -*-
"""市场微观结构脉搏（借鉴 Cailianpress-Feishu-Bot 的同花顺量化复盘算法）。

在“均线/动量/消息面”之外补上短线情绪维度：
- 抓取：涨停池 / 炸板池 / 跌停池 / 最强风口 / 市场大局观（同花顺公开接口）；
- 因果反馈：昨日涨停今日晋级率（连板效应 → 赚钱效应定性）；
- 炸板率监控：封板质量（炸板 /(涨停+炸板)）；
- 亏钱效应：跌停家数分档；市场温度计：涨跌家数分布与量能环比；
- 题材持续性：今日 Top5 概念 ∩ 昨日 Top5 → 持续主线 / 主流换血；
- 连板梯队：非ST 各身位名单（高标 = 情绪风向标）；
- 题材热度：涨停概念 + 风口板块映射到本项目 THEME_GROUPS，供选基加“热度因子”；
- 情绪分：广度 + 涨跌停结构 + 晋级率 + 炸板率加权（-100~+100），
  两端“物极必反/亢龙有悔”阻尼（与参考项目“禅师总结”同源理念）。

纪律：本模块失败绝不阻断主流程——全部接口可降级，缺什么维度就重归一化权重；
回测/演示不接本模块（历史逐日抓取会打爆接口且改变“纯量化回放”口径）。
"""
import re

from . import settings, strategy, util
from .util import DataError

THS_HEADERS = {
    "Referer": "https://data.10jqka.com.cn/datacenter/limitup/",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}
# 涨停池字段（沿用参考仓库实测有效的字段集，461252=行业）
_POOL_FIELDS = ("10,19,48,9001,9002,9003,9004,133970,133971,199112,330323,"
                "330324,330325,330329,330333,330334,1968584,3475914,3541450,461252")
POOL_KINDS = {
    "zt": "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool",
    "zb": "https://data.10jqka.com.cn/dataapi/limit_up/open_limit_pool",
    "dt": "https://data.10jqka.com.cn/dataapi/limit_up/lower_limit_pool",
}
BLOCK_TOP = "https://data.10jqka.com.cn/dataapi/limit_up/block_top"
OVERVIEW = ("https://data.10jqka.com.cn/mobileapi/hotspot_focus/"
            "market_state/v1/overview")
SCHEMA = 1  # 快照结构版本


# ---------------- 基础解析（纯函数，可离线单测） ----------------
def parse_high_days(raw, fallback=1):
    """"14天9板"→(14,9)；"首板"→(1,1)；"3"→(1,3)。"""
    s = str(raw or "")
    m = re.search(r"(\d+)\s*天\s*(\d+)\s*板", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    if "首板" in s:
        return 1, 1
    m = re.search(r"(\d+)\s*板", s)
    if m:
        return 1, int(m.group(1))
    try:
        n = int(float(s))
        return 1, max(1, n)
    except (TypeError, ValueError):
        return 1, max(1, int(fallback or 1))


def is_st(name):
    s = str(name or "").upper()
    return "ST" in s or s.startswith("*")


def split_concepts(reason):
    return [c.strip() for c in str(reason or "").split("+") if c.strip()]


# 概念 → 本项目主题组 的映射（先过 strategy.THEME_GROUPS 关键词，再补常见题材后缀）
_CONCEPT_THEME_EXTRA = [
    (["存储", "晶圆", "光刻", "封测", "集成电路"], "半导体芯片"),
    (["算力", "数据中心", "液冷", "光模块", "CPO", "IDC"], "人工智能"),
    (["智能驾驶", "自动驾驶", "车联网"], "电子"),
    (["消费电子", "苹果", "面板", "OLED"], "电子"),
    (["机器人", "减速器", "伺服"], "人工智能"),
    (["信创", "软件", "国产操作系统", "数据库"], "计算机软件"),
    (["数字经济", "数字经济", "数据要素"], "计算机软件"),
    (["券商", "期货", "信托"], "证券"),
    (["保险"], "证券"),
    (["再融资"], None),
]


def theme_of_concept(name):
    """概念/板块名 → 本项目 THEME_GROUPS 主题（匹配不到返回 None）。"""
    s = str(name or "")
    for kws, theme in strategy.THEME_GROUPS:
        if any(k in s for k in kws):
            return theme
    for kws, theme in _CONCEPT_THEME_EXTRA:
        if theme and any(k in s for k in kws):
            return theme
    return None


def _strip_market(code):
    return str(code or "").split(".")[0]


def snapshot_from_raw(date_s, raw, prev_snap=None):
    """原始抓取 → 指标快照（可离线单测）。

    raw: {"zt": rows, "zb": rows, "dt": rows, "blocks": rows, "ov": dict}
         （rows 为同花顺 info 列表；可为 None 表示该源失败）
    prev_snap: 上一交易日快照（含 zt_codes/concepts），用于晋级率与题材持续性。
    """
    zt_rows = raw.get("zt") or []
    zb_raw = raw.get("zb")
    dt_raw = raw.get("dt")
    zb_rows = zb_raw or []
    dt_rows = dt_raw or []
    blocks = raw.get("blocks") or []
    ov = raw.get("ov") or {}
    rf = ov.get("rise_fall") or {}

    zt_nonst = [r for r in zt_rows if not is_st(r.get("name"))]
    zt_codes = sorted({_strip_market(r.get("code")) for r in zt_nonst
                       if r.get("code")})
    zt_count = int(rf.get("limit_up") or 0) or len(zt_rows)
    zb_count = len(zb_rows) if zb_raw is not None else None
    dt_count = int(rf.get("limit_down") or 0) or \
        (len(dt_rows) if dt_raw is not None else 0)
    zb_denom = (zt_count or 0) + (zb_count or 0)
    zb_rate = (zb_count / zb_denom) if (zb_count is not None and zb_denom) \
        else None

    # 因果反馈：昨日涨停 ∩ 今日涨停 → 晋级率
    promotion = promotion_rate = None
    if prev_snap and (prev_snap.get("zt_codes") or []):
        prev_codes = set(prev_snap["zt_codes"])
        if prev_codes:
            promotion = len(prev_codes & set(zt_codes))
            promotion_rate = promotion / len(prev_codes)

    # 题材统计（非ST 涨停原因）+ 持续判定
    from collections import Counter
    concepts = Counter()
    for r in zt_nonst:
        for c in split_concepts(r.get("reason_type")):
            concepts[c] += 1
    top_concepts = concepts.most_common(5)
    prev_concepts = prev_snap.get("concepts") if prev_snap else None
    persistent = []
    new_entries = []
    if prev_concepts:
        prev_top = {c for c, _ in sorted(
            prev_concepts.items(), key=lambda x: -x[1])[:5]}
        for c, n in top_concepts:
            (persistent if c in prev_top else new_entries).append(c)

    # 连板梯队（非ST，≥2板）
    ladder = {}
    for r in zt_nonst:
        _days, boards = parse_high_days(r.get("high_days"),
                                        r.get("limit_up_days") or 1)
        if boards >= 2:
            reason = (split_concepts(r.get("reason_type"))
                      or ["—"])[0]
            ladder.setdefault(boards, []).append(
                "{}({})".format(r.get("name"), reason[:12]))
    max_board = max(ladder) if ladder else 1

    # 题材热度（概念计数 + 最强风口涨停家数 → 映射到主题组，计数求和）
    # 同时保留分项口径：theme_parts.concepts/blocks 供热度算法按“概念 + 0.5×风口”
    # 去重加权（旧字段 theme_counts 仍存“相加”口径，保证兼容）。
    theme_concepts, theme_blocks = Counter(), Counter()
    for c, n in concepts.items():
        th = theme_of_concept(c)
        if th:
            theme_concepts[th] += n
    for b in blocks:
        th = theme_of_concept(b.get("name"))
        if th:
            theme_blocks[th] += int(b.get("limit_up_num") or 0)
    theme_cnt = Counter(theme_concepts)
    for th, n in theme_blocks.items():
        theme_cnt[th] += n
    theme_counts = dict(theme_cnt)

    # 市场广度（涨跌家数，平票算中性折半）
    rise, fall, deuce = (int(rf.get("rise") or 0), int(rf.get("fall") or 0),
                         int(rf.get("deuce") or 0))
    total_ad = rise + fall + deuce
    rise_ratio = ((rise + 0.5 * deuce) / total_ad) if total_ad else None

    snap = {
        "schema": SCHEMA, "date": date_s,
        "zt": zt_count, "zb": zb_count, "dt": dt_count,
        "zt_nonst": len(zt_nonst), "zb_rate": round(zb_rate, 4)
        if zb_rate is not None else None,
        "promotion": promotion, "promotion_rate": (round(promotion_rate, 4)
                                                   if promotion_rate is not None
                                                   else None),
        "prev_date": prev_snap.get("date") if prev_snap else None,
        "concepts": {c: n for c, n in concepts.most_common(30)},
        "top_concepts": [[c, n] for c, n in top_concepts],
        "persistent": persistent, "new_entries": new_entries,
        "ladder": {str(k): v[:8] for k, v in sorted(ladder.items(),
                                                    reverse=True)},
        "max_board": max_board,
        "theme_counts": theme_counts,
        "theme_parts": {"concepts": dict(theme_concepts),
                        "blocks": dict(theme_blocks)},
        "zt_codes": zt_codes,
        "rise": rise, "fall": fall, "deuce": deuce,
        "rise_ratio": round(rise_ratio, 4) if rise_ratio is not None else None,
        "turnover": {"now": (ov.get("turnover") or {}).get("now"),
                     "pre": (ov.get("turnover") or {}).get("pre")},
        "partial": {"zt": raw.get("zt") is None, "zb": raw.get("zb") is None,
                    "dt": raw.get("dt") is None, "ov": not bool(rf),
                    "blocks": raw.get("blocks") is None},
    }
    score, parts, flags = sentiment_score(snap)
    snap["score"] = score
    snap["parts"] = parts
    snap["flags"] = flags
    snap["qualitative"] = qualitative(snap)
    snap["zen"] = zen_quote(rise_ratio, promotion_rate)
    return snap


# ---------------- 情绪分（-100~+100，缺维度自动重归一化） ----------------
def sentiment_score(snap):
    """广度40 + 涨跌停结构25 + 晋级率20 + 炸板率15（可得维度重归一化）。"""
    parts, avail = {}, {}

    rr = snap.get("rise_ratio")
    if rr is not None:
        v = util.clamp((rr - 0.5) * 200.0, -100.0, 100.0)
        parts["breadth"] = int(round(v))
        avail["breadth"] = 40.0

    zt, dt = snap.get("zt") or 0, snap.get("dt") or 0
    if snap.get("zt") is not None and snap.get("dt") is not None:
        v = util.clamp((zt - dt) / 60.0 * 100.0, -100.0, 100.0)
        parts["struct"] = int(round(v))
        avail["struct"] = 25.0

    pr = snap.get("promotion_rate")
    if pr is not None:
        v = util.clamp((pr - 0.30) / 0.30 * 100.0, -100.0, 100.0)
        parts["promotion"] = int(round(v))
        avail["promotion"] = 20.0

    zr = snap.get("zb_rate")
    if zr is not None:
        v = util.clamp((0.35 - zr) / 0.35 * 100.0, -100.0, 60.0)
        parts["zb"] = int(round(v))
        avail["zb"] = 15.0

    if not avail:
        return None, {}, []
    total_w = sum(avail.values())
    s = sum(parts[k] * (avail[k] / total_w) for k in avail)
    flags = []
    # 物极必反 / 亢龙有悔：情绪两端阻尼，避免“沸点追高、冰点割肉”
    if s >= 70:
        s = 70.0 - (s - 70.0) * 0.5
        flags.append("overheat")
    elif s <= -70:
        s = -70.0 + (-s - 70.0) * 0.5
        flags.append("freezing")
    return int(round(util.clamp(s, -100, 100))), parts, flags


def qualitative(snap):
    """按参考仓库的档位给情绪定性。"""
    pr = snap.get("promotion_rate")
    if pr is None:
        eff = "缺少昨日对比"
    elif pr > 0.5:
        eff = "极佳"
    elif pr > 0.3:
        eff = "良好"
    elif pr > 0.15:
        eff = "谨慎"
    else:
        eff = "恶劣"
    dt = snap.get("dt") or 0
    loss = "风险可控" if dt < 5 else ("风险扩散" if dt < 15 else "大面横行")
    s = snap.get("score")
    if s is None:
        mood = "数据不足"
    elif s >= 45:
        mood = "沸点"
    elif s >= 15:
        mood = "偏暖"
    elif s > -15:
        mood = "平淡"
    elif s > -45:
        mood = "偏冷"
    else:
        mood = "冰点"
    return {"promotion_effect": eff, "loss_effect": loss, "mood": mood}


def zen_quote(rise_ratio, promotion_rate):
    """情绪分位 → 禅外题话（参考仓库“禅师总结”的同源理念）。"""
    if rise_ratio is None:
        return "数据不足，观天之道，执天之行。"
    if rise_ratio >= 0.8:
        return "亢龙有悔，切莫盲目追高；物极必反，沸点次日防承接转弱。"
    if rise_ratio >= 0.6:
        return "上善若水，顺势而为，关注主线轮动。"
    if rise_ratio >= 0.4:
        return "持而盈之，不如其已；震荡市宜减冗余，守本心。"
    if promotion_rate is not None and promotion_rate <= 0.15:
        return "众之所恶，故几于道；亏钱效应极致，静待修复，不割在地板上。"
    return "否极泰来；处于情绪低位，跌出性价比，留意修复信号。"


# ---------------- 抓取（在线） ----------------
def _ths_json(url, timeout=18):
    js = util.http_get_json(url, headers=THS_HEADERS, timeout=timeout,
                            tries=2)
    if js.get("status_code") != 0:
        raise DataError("同花顺接口报错：{}".format(
            str(js.get("status_msg"))[:80]))
    return js.get("data") or {}


def _fetch_pool(kind, date_compact):
    """涨停/炸板/跌停池：单页 limit=200 兜底翻页（接口 total 为总条数）。"""
    out, page = [], 1
    while page <= 5:
        url = ("{base}?page={p}&limit=200&field={f}"
               "&filter=HS,GEM2STAR,ST,NEW&order_field=330324&order_type=0"
               "&date={d}").format(base=POOL_KINDS[kind], p=page,
                                   f=_POOL_FIELDS, d=date_compact)
        data = _ths_json(url)
        info = data.get("info") or []
        out.extend(info)
        total = int(((data.get("page") or {}).get("total")) or 0)
        if not info or len(out) >= total:
            break
        page += 1
    return out


def _fetch_blocks(date_compact):
    """最强风口：data 直接是板块列表（含 name/limit_up_num/stock_list）。"""
    url = "{}?filter=HS,GEM2STAR,ST,NEW&date={}".format(BLOCK_TOP,
                                                        date_compact)
    js = util.http_get_json(url, headers=THS_HEADERS, timeout=20, tries=2)
    if js.get("status_code") != 0:
        raise DataError("风口接口报错")
    data = js.get("data")
    return data if isinstance(data, list) else \
        ((data or {}).get("info") or [])


def fetch_raw(date_s):
    """抓取某日原始数据；单源失败记 None 不抛（全失败才 DataError）。"""
    dc = date_s.replace("-", "")
    raw, errs = {}, []
    for kind in ("zt", "zb", "dt"):
        try:
            raw[kind] = _fetch_pool(kind, dc)
        except Exception as e:
            raw[kind] = None
            errs.append("{}池:{}".format({"zt": "涨停", "zb": "炸板",
                                          "dt": "跌停"}[kind], str(e)[:60]))
    try:
        raw["blocks"] = _fetch_blocks(dc)
    except Exception as e:
        raw["blocks"] = None
        errs.append("风口:{}".format(str(e)[:60]))
    try:
        raw["ov"] = _ths_json("{}?date={}".format(OVERVIEW, dc))
    except Exception as e:
        raw["ov"] = {}
        errs.append("大局观:{}".format(str(e)[:60]))
    if raw.get("zt") is None and not raw.get("ov"):
        raise DataError("微观结构全部源不可用：" + "；".join(errs))
    # “全空”判定：盘前/非交易日接口会返回空池（status 0）——若大局观涨跌家数
    # 也接近全零，视为当日无数据，绝不落盘污染晋级率与情绪历史。
    _rf = (raw.get("ov") or {}).get("rise_fall") or {}
    _ad_zero = (int(_rf.get("rise") or 0) + int(_rf.get("fall") or 0)
                + int(_rf.get("deuce") or 0)) == 0
    if (_ad_zero and not raw.get("zt") and not raw.get("zb")
            and not raw.get("dt")):
        raise DataError("微观结构当日无数据（盘前/休市/非交易日）")
    raw["_errs"] = errs
    return raw


# ---------------- 快照存取（本地缓存 = 历史复盘数据库） ----------------
def snap_file(date_s):
    return util.cache_file("micro_{}.json".format(date_s))


def load_snap(date_s):
    js = util.load_json(snap_file(date_s))
    if js and js.get("date") == date_s and js.get("schema") == SCHEMA:
        return js
    return None


def build_day(date_s, force=False, _depth=0):
    """构建/读取某日快照（幂等）。昨日快照缺失时尽力补抓（仅一层递归）。"""
    if not force:
        hit = load_snap(date_s)
        if hit:
            return hit
    prev = util.prev_trading_day(date_s)
    prev_snap = None
    if _depth < 6:
        try:
            prev_snap = build_day(prev, _depth=_depth + 1)
        except DataError:
            prev_snap = None
    raw = fetch_raw(date_s)
    snap = snapshot_from_raw(date_s, raw, prev_snap=prev_snap)
    snap["generated"] = util.now_iso()
    if raw.get("_errs"):
        snap["errors"] = raw["_errs"]
    util.save_json(snap_file(date_s), snap)
    try:                       # 保留策略：缓存里留足够天数（默认 260 个交易日）辅助决策
        prune_snaps(cfg=settings.load_config())
    except Exception:
        pass
    return snap


def load_pulse(date_s, force=False, cfg=None):
    """引擎入口：当日情绪脉搏；失败返回 {"ok": False, ...} 绝不抛。"""
    st = (cfg or {}).get("strategy", {}) or {}
    if not st.get("micro_enable", True):
        return {"ok": False, "message": "strategy.micro_enable=false，已关闭"}
    try:
        snap = build_day(date_s, force=force)
        return {"ok": True, "date": date_s, "snap": snap,
                "score": snap.get("score"),
                "errors": snap.get("errors") or []}
    except DataError as e:
        # 在线失败 → 读旧快照兜底（昨日口径仍比没有强，标注滞后）
        old = load_snap(date_s)
        if old:
            return {"ok": True, "date": date_s, "snap": old,
                    "score": old.get("score"), "stale": True, "errors": []}
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": "微观结构异常：{}".format(str(e)[:120])}


def history_pulses(end_date_s, days=5, cfg=None):
    """最近 N 个交易日快照（只读缓存，不联网；缺失日跳过）。"""
    st = (cfg or {}).get("strategy", {}) or {}
    if not st.get("micro_enable", True):
        return []
    out, d = [], end_date_s
    for _ in range(days * 2 + 6):
        snap = load_snap(d)
        if snap:
            out.append(snap)
            if len(out) >= days:
                break
        d = util.prev_trading_day(d)
    out.reverse()
    return out


# ---------------- 输出：文案 / 题材热度 ----------------
THEME_HEAT_DECAY = (0.5, 0.3, 0.2)   # 最新 → 旧 的时间衰减
THEME_HEAT_BLOCK_W = 0.5             # 风口系数：与概念口径去重（同一批涨停股不再记两次）
THEME_HEAT_PERSIST = 0.12            # 持续性加成：窗口内每多出现 1 天
THEME_HEAT_IGNITE = 1.5              # 点火：今日占比 ≥ 其余日均值 ×1.5
THEME_HEAT_IGNITE_MIN = 0.08         # 点火的最小今日占比门槛


def _day_theme_values(snap):
    """单日主题强度：概念涨停家数 + 0.5×最强风口涨停家数。

    旧快照（无 theme_parts）退回 theme_counts（相加口径），保证历史可比。
    """
    parts = snap.get("theme_parts") or {}
    con = parts.get("concepts") or {}
    blk = parts.get("blocks") or {}
    if con or blk:
        out = {k: float(v) for k, v in con.items()}
        for k, v in blk.items():
            out[k] = out.get(k, 0.0) + THEME_HEAT_BLOCK_W * float(v)
        return out
    return {k: float(v) for k, v in (snap.get("theme_counts") or {}).items()}


def theme_heat_report(pulse_days_snaps, decay=THEME_HEAT_DECAY):
    """题材热度报告（逐日可复算、可审计）。

    相对旧版的四点优化：
      1) 先算“当日占比”＝主题强度／当日涨停家数：避免涨停家数多的一天仅因基数大
         就压过后来的新热点（旧版直接加权家数，09-07 的 142 家一路压到现在）；
      2) 单日强度＝概念家数 + 0.5×风口家数：旧版两者相加，同一批涨停股被重复计数；
      3) 衰减加权后乘“持续性加成”（窗口内出现 k 天 → ×(1+0.12(k−1))），
         奖励连续主线、抑制一日游；
      4) 点火标记：今日占比 ≥ 其余日均值×1.5 且 ≥0.08 → 新热点（界面 🔥）。
    另给 market_heat（加权涨停家数/60，可看每日升降）与 trend。
    输入顺序无关：内部按 date 升序自排（旧版按调用方顺序取权重，web 层多反转一次
    导致“最新一天只拿 0.2 权重”，是热度看着不更新的根因）。
    """
    seq = [s for s in (pulse_days_snaps or []) if s and s.get("date")]
    seq.sort(key=lambda s: str(s.get("date")))
    seq = seq[-len(decay):]
    if not seq:
        return {"map": {}, "rows": [], "days": [], "decay": [], "market_heat": None}
    n = len(seq)
    wts = list(decay[:n])[::-1]          # 旧 → 新
    shares, values, zts = [], [], []
    for s in seq:
        vals = _day_theme_values(s)
        zt = float(s.get("zt") or 0)
        base = zt or sum(vals.values()) or 1.0
        zts.append(zt)
        values.append(vals)
        shares.append({k: v / base for k, v in vals.items() if v > 0})
    agg = {}
    for i in range(n):
        for k, sh in shares[i].items():
            agg[k] = agg.get(k, 0.0) + wts[i] * sh
    rows = []
    for k, v in agg.items():
        seen = sum(1 for i in range(n) if shares[i].get(k, 0.0) > 0)
        today_sh = shares[-1].get(k, 0.0)
        others = [shares[i].get(k, 0.0) for i in range(n - 1)]
        avg_other = (sum(others) / len(others)) if others else 0.0
        ignite = bool(today_sh >= THEME_HEAT_IGNITE_MIN and
                      today_sh >= THEME_HEAT_IGNITE * avg_other)
        rows.append({
            "theme": k, "heat": v * (1.0 + THEME_HEAT_PERSIST * (seen - 1)),
            "today": round(today_sh, 4), "days_seen": seen, "ignition": ignite,
            "shares": [round(shares[i].get(k, 0.0), 4) for i in range(n)],
            "values": [int(values[i].get(k, 0) or 0) for i in range(n)],
        })
    mx = max([r["heat"] for r in rows] or [0.0]) or 1.0
    for r in rows:
        r["heat"] = round(r["heat"] / mx, 4)
    rows.sort(key=lambda r: (-r["heat"], r["theme"]))
    mh = None
    trend = None
    if any(zts):
        mh = round(sum(wts[i] * zts[i] for i in range(n)) / 60.0, 3)
        if n >= 2:
            d = zts[-1] - zts[-2]
            trend = "up" if d > 0 else ("down" if d < 0 else "flat")
    return {"map": {r["theme"]: r["heat"] for r in rows}, "rows": rows,
            "days": [str(s.get("date")) for s in seq], "decay": wts,
            "date": str(seq[-1].get("date")), "zt_latest": int(zts[-1]),
            "zt_prev": (int(zts[-2]) if n >= 2 else None),
            "market_heat": mh, "trend": trend,
            "ignitions": [r["theme"] for r in rows if r["ignition"]][:5]}


def theme_heat_map(pulse_days_snaps):
    """{主题: 0~1 热度}（theme_heat_report 的兼容薄封装，选基因子用）。"""
    return theme_heat_report(pulse_days_snaps)["map"]


def _context_stats(scores, dates, days=30):
    """纯统计：近 N 日情绪分的“位置与状态”（供决策参考，不参与打分）。"""
    if not scores:
        return {"days": 0}
    cur = scores[-1]
    arr = sorted(scores)
    rank = sum(1 for x in arr if x <= cur) / float(len(arr))
    mean = sum(arr) / len(arr)
    std = (sum((x - mean) ** 2 for x in arr) / len(arr)) ** 0.5
    recent, prev = scores[-5:], scores[-10:-5]
    trend = (sum(recent) / len(recent) - sum(prev) / len(prev)) if prev else None
    return {"days": len(scores), "latest": cur, "pct_rank": round(rank, 3),
            "mean": round(mean, 1), "std": round(std, 1),
            "min": min(arr), "max": max(arr),
            "hot_days": sum(1 for x in scores if x >= 40),
            "cold_days": sum(1 for x in scores if x <= -40),
            "trend": (None if trend is None else round(trend, 1)),
            "label": ("偏热" if rank >= 0.8 else ("偏冷" if rank <= 0.2 else "中性")),
            "from": dates[0] if dates else None,
            "to": dates[-1] if dates else None,
            "window": days}


def sentiment_context(end_date_s, days=30, cfg=None):
    """近 N 个交易日情绪分布上下文：分位、均值/波动、冷热天数、5 日趋势。

    缓存里保留的交易日远多于画图用的 7 天（默认保留 120 天，见 prune_snaps），
    因此可以把“当前情绪处在什么位置”作为决策参考：例如分位 ≥0.8（偏热）时
    降低追高冲动、≤0.2（偏冷）时留意修复。
    """
    st = (cfg or {}).get("strategy", {}) or {}
    days = int(days or st.get("micro_context_days", 30) or 30)
    snaps = history_pulses(end_date_s, days=days, cfg=cfg)
    scores = [s.get("score") for s in snaps if s.get("score") is not None]
    return _context_stats(scores, [s.get("date") for s in snaps], days)


def prune_snaps(keep_days=None, cfg=None, dry=False):
    """情绪快照保留策略：只保留最近 keep_days 个交易日的快照文件。

    缓存里留足够天数（默认 120 个交易日 ≈ 半年）辅助决策；超过 keep_days+30 才清理，
    避免频繁删文件、也不让目录无限膨胀。返回 {"kept":, "removed":}。
    """
    import glob
    import os
    st = (cfg or {}).get("strategy", {}) or {}
    keep_days = max(30, int(keep_days or st.get("micro_cache_days", 120) or 120))
    files = sorted(glob.glob(str(util.cache_file("micro_*.json"))))
    if len(files) <= keep_days + 30:
        return {"kept": len(files), "removed": 0}
    drop = files[:len(files) - keep_days]
    removed = 0
    for f in drop:
        if dry:
            removed += 1
            continue
        try:
            os.remove(f)
            removed += 1
        except OSError:
            pass
    return {"kept": len(files) - removed, "removed": removed,
            "note": "保留最近 {} 个交易日（strategy.micro_cache_days）".format(keep_days)}


def review_lines(pulse, snaps=None):
    """把快照整理成研判文案段落（结构对齐参考仓库的复盘研报）。"""
    snap = (pulse or {}).get("snap") or {}
    if not snap:
        return []
    q = snap.get("qualitative") or {}
    lines = ["【短线情绪·因果反馈】"]
    if snap.get("promotion_rate") is not None:
        lines.append("昨日({})涨停今日晋级 {} 只（{:.1f}%），赚钱效应：{}。".format(
            snap.get("prev_date") or "前一日", snap.get("promotion") or 0,
            snap["promotion_rate"] * 100, q.get("promotion_effect", "")))
    else:
        lines.append("缺少昨日涨停池对比（首日接入后逐日累积即可用）。")
    zr = snap.get("zb_rate")
    lines.append("炸板率 {}（涨停 {} / 炸板 {}）；{}。".format(
        "{:.1f}%".format(zr * 100) if zr is not None else "—",
        snap.get("zt"), snap.get("zb"),
        "封板质量好" if (zr is not None and zr < 0.3) else
        ("封板质量差，谨慎追高" if zr is not None else "炸板数据缺失")))
    dt = snap.get("dt") or 0
    lines.append("亏钱效应：跌停 {} 家（{}）。".format(dt, q.get("loss_effect")))
    rr = snap.get("rise_ratio")
    if rr is not None:
        n_bar = int(util.clamp(rr * 10, 0, 10))
        lines.append("市场温度计：涨 {} : 跌 {}（胜率 {:.1f}%）{}{}，量能 {}（前值 {}）。".format(
            snap.get("rise"), snap.get("fall"), rr * 100,
            "🔴" * n_bar, "🟢" * (10 - n_bar),
            (snap.get("turnover") or {}).get("now") or "—",
            (snap.get("turnover") or {}).get("pre") or "—"))
    top = snap.get("top_concepts") or []
    if top:
        lines.append("主流题材：" + "、".join(
            "{}({}家)".format(c, n) for c, n in top[:5]) + "。")
    if snap.get("persistent"):
        lines.append("题材持续性：持续主线 {}；新题材 {}。".format(
            "、".join(snap["persistent"][:4]),
            "、".join((snap.get("new_entries") or [])[:3]) or "无（无新面孔）"))
    lad = snap.get("ladder") or {}
    if lad:
        parts = ["{}板：{}".format(k, "、".join(v[:4]))
                 for k, v in sorted(lad.items(), key=lambda x: -int(x[0]))][:4]
        lines.append("连板梯队（非ST）：" + "；".join(parts))
    else:
        lines.append("连板梯队：无 ≥2 板梯队（情绪断档，短线冰点特征）。")
    if snap.get("score") is not None:
        lines.append("情绪定性：{}，微观情绪分 {:+d}（物极必反两端已阻尼；{}）。".format(
            q.get("mood"), snap["score"],
            "过热" if "overheat" in (snap.get("flags") or []) else
            ("冰点" if "freezing" in (snap.get("flags") or []) else "常规区")))
    lines.append("禅外题话：“{}”".format(snap.get("zen")))
    return lines


if __name__ == "__main__":  # 快速自检：python -m fundai.microstructure
    import json
    d = util.today_str()
    p = load_pulse(d, force=True)
    print(json.dumps(p.get("snap") or p, ensure_ascii=False, indent=1)[:3000])
    for ln in review_lines(p):
        print(ln)
