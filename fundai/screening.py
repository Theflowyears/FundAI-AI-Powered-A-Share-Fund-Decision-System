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

from . import lexicon, util

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
            CREATE TABLE IF NOT EXISTS lexicon(
                word TEXT PRIMARY KEY,
                bull INTEGER DEFAULT 0,
                bear INTEGER DEFAULT 0,
                neutral INTEGER DEFAULT 0,
                docs INTEGER DEFAULT 0,
                updated TEXT
            );
            """)

    # ---------------- 消息入库 / 查询 ----------------
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
    def review_split(items, cap=50):
        """打标前分流：AI 没把握(中性)的进人工额度（前 cap 条），其余按 AI 采纳/已打标。

        返回 (to_review, extra_neutral, ai_accepted, done)。items 保持传入顺序。
        """
        to_review, extra, ai, done = [], [], [], []
        for r in items:
            if r.get("user_label"):
                done.append(r)
            elif (r.get("auto_label") or "neutral") == "neutral":
                (to_review if len(to_review) < int(cap) else extra).append(r)
            else:
                ai.append(r)
        return to_review, extra, ai, done

    def ingest_feed(self, date_s, feed, keep_user=True, skip_recent=True):
        """把当天全量消息写入（自动字段覆盖；用户已打标字段保留）。返回写入条数。

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
                with self.conn:
                    self.conn.execute(
                        "INSERT INTO items(date,item_id,source,time,title,text,url,"
                        "sectors,funds,auto_label,auto_strength,user_label,user_strength,rated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(date,item_id) DO UPDATE SET "
                        "source=excluded.source,time=excluded.time,title=excluded.title,"
                        "text=excluded.text,url=excluded.url,sectors=excluded.sectors,"
                        "funds=excluded.funds,auto_label=excluded.auto_label,"
                        "auto_strength=excluded.auto_strength,user_label=excluded.user_label,"
                        "user_strength=excluded.user_strength,rated_at=excluded.rated_at",
                        (date_s, item_id, it.get("source") or "",
                         it.get("time") or "", it.get("title") or "",
                         it.get("text") or "", it.get("url") or "",
                         json.dumps(it.get("sectors") or [], ensure_ascii=False),
                         json.dumps(it.get("funds") or [], ensure_ascii=False),
                         it.get("auto_label") or "", float(it.get("auto_strength") or 0),
                         user_label, user_strength, rated_at))
                if recent_keys is not None and key:
                    recent_keys.add(key)
                n += 1
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
        self.learn_all()
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
            net += float(r.get("user_strength") or 0)
            if lab in LABEL_DIRS:
                n_dir += 1
        return {"labeled": sum(by.values()), "by_label": by,
                "net": round(net, 2), "directional": n_dir}

    def human_news_meta(self, date_s, amplitude=8):
        """用户已打标 → 人工消息分（方向性标签≥1 才启用）。

        返回 {score, net, directional, labeled} 或 None。
        """
        s = self.labels_summary(date_s)
        if s["directional"] < 1:
            return None
        net = max(-8, min(8, s["net"]))
        return {"score": int(max(-100, min(100, net * max(1, int(amplitude))))),
                "net": net, "directional": s["directional"], "labeled": s["labeled"]}

    # ---------------- 学习：用户标签 → 词典进化 ----------------
    def learn_all(self):
        """基于全部“用户已打标”消息重建学习词表。

        词 = 正文的中文 2~6 字 n-gram（去掉虚词/基础词典词）；只有足够一致、
        样本足够时才进入词表，并同时让“中性/混杂”样本压制明显噪音。
        """
        rows = self.conn.execute(
            "SELECT title,text,user_label FROM items WHERE user_label!='' "
            "AND user_label!='irrelevant'").fetchall()
        counts = {}  # word -> [bull, bear, neutral, docs]
        for r in rows:
            lab = r["user_label"]
            txt = "{} {}".format(r["title"] or "", r["text"] or "")
            words = set(lexicon.token_candidates(txt))
            for w in words:
                if lexicon.is_base_word(w):
                    continue
                if not lexicon.termness(w):
                    # 术语化学习：与常见金融术语无关的 n-gram 碎片不学，
                    # 避免把“断词奇怪”的非行情词当成词典进化
                    continue
                c = counts.setdefault(w, [0, 0, 0, 0])
                if c[3] == 0:  # docs 计数按该词首次出现
                    pass
                # 每篇文档每个词只记一次方向贡献
                if lab == "neutral":
                    c[2] += 1
                else:
                    pol = LABEL_POL.get(lab, 0)
                    if pol > 0:
                        c[0] += 1
                    elif pol < 0:
                        c[1] += 1
                    else:
                        c[2] += 1
                c[3] += 1
        fresh = []
        now = util.now_iso()
        for w, (b, br, nu, docs) in counts.items():
            side = max(b, br)
            if docs < 2 or side < 2:
                continue
            if nu > side * 2:  # 中性出现过多 → 噪音词，不学
                continue
            if b > 0 and br > 0 and min(b, br) * 2 >= side:
                continue  # 多空打架，方向不稳定
            fresh.append((w, b, br, nu, docs, now))
        # 剪枝：同向的包含子串只保留最长词（如“固态电池/固态电/电池”→“固态电池”），
        # 避免同一段文本被重叠词重复计分；方向相反的包含词不互相压制。
        maximal = []
        for cand in sorted(fresh, key=lambda x: (len(x[0]), max(x[1], x[2])),
                           reverse=True):
            sign = 1 if cand[1] > cand[2] else -1
            if any(kw[0] != cand[0] and cand[0] in kw[0] and
                   (1 if kw[1] > kw[2] else -1) == sign
                   for kw in maximal):
                continue
            maximal.append(cand)
        fresh = maximal[:400]
        with self.lock:
            with self.conn:
                self.conn.execute("DELETE FROM lexicon")
                self.conn.executemany(
                    "INSERT INTO lexicon(word,bull,bear,neutral,docs,updated) "
                    "VALUES(?,?,?,?,?,?)", fresh)
        return len(fresh)

    def lexicon_rows(self, limit=200):
        rows = self.conn.execute(
            "SELECT * FROM lexicon ORDER BY (bull+bear) DESC, docs DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def learned_extra(self):
        """{word: (dir, weight)}：news 采集自动打分时叠加学习词。"""
        rows = self.conn.execute(
            "SELECT * FROM lexicon WHERE bull+bear>=2").fetchall()
        out = {}
        for r in rows:
            b, br = r["bull"], r["bear"]
            if b == br:
                continue
            dom = abs(b - br) / float(b + br)
            weight = round(1.2 * dom, 3)
            if weight < 0.35:
                continue
            out[r["word"]] = (1 if b > br else -1, weight)
        return out

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
    extra = store.learned_extra()
    added, by_date = 0, {}
    merged_ids = []
    for item in results:
        qid = item.get("queue_id")
        q = queue.get(qid)
        if not q or not item.get("ok"):
            continue
        date_s = q.get("date") or util.today_str()
        rows = []
        for i, n in enumerate((item.get("items") or [])[:30]):
            title = (n.get("title") or "").strip()[:200]
            snip = (n.get("snippet") or n.get("summary") or "").strip()
            txt = title + " " + snip
            sc = lexicon.score_text(txt, extra=extra)
            rows.append({
                "id": "SRC-{}-{}".format(qid, i),
                "source": "本地搜索回填",
                "time": (n.get("time") or q.get("created") or "")[:16],
                "title": title or snip[:80],
                "text": (snip or title),
                "url": n.get("url") or "",
                "sectors": lexicon.relevant(txt) and
                           [s for s in (q.get("note") or "").split(",") if s] or [],
                "funds": [],
                "auto_label": sc["label"],
                "auto_strength": sc["strength"],
                "user_label": "", "user_strength": 0,
            })
        if rows:
            store.ingest_feed(date_s, rows, keep_user=True)
            added += len(rows)
            by_date[date_s] = by_date.get(date_s, 0) + len(rows)
        merged_ids.append(qid)
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
