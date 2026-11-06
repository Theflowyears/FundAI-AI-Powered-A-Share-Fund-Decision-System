# -*- coding: utf-8 -*-
"""人工介入预算：把"要我点几次"变成可计量、可封顶、可追溯的东西。

用户的要求（2026-09-16）
------------------------
> 「尽可能让 AI 进行判断，要我人工介入的每天不超过五十次！**是一共五十次！**」

这句话有两个要点，缺一不可：

1. **是一共 50 次**——不是"每个模块 50 次"。旧界面写的是"在『待确认』里挑 AI 没把握的
   最多 50 条复核打标"，而打标之外还有：合并搜索回填（每轮 1 次）、成交回填（每单 1 次）、
   备选池重建（1 次）、参数改动（每次 1 次）……这些**全都没有计数**，
   用户实际每天要点的次数早就远超 50，而且无从知道自己用掉了多少。
2. **尽可能让 AI 判断**——所以判据不是"怎么把 50 次分配得更均匀"，而是
   **怎么把需要人工的动作变成不需要人工**。

本模块做三件事
--------------
* `human(kind, ...)`：记一次**真的需要人**的动作（计入 50 次预算）；
* `auto(kind, ...)`：记一次**系统自己做完**的动作（不计入预算，但留痕，
  这样"AI 到底替我做了多少"可以量化展示，而不是一句空话）；
* `status()`：给出当日用量、剩余额度、以及"还欠哪些需要人做的事"。

落库在消息库 `screening.db` 的 `interventions` 表（和消息同级，天然按天聚合）。
"""
import json
import sqlite3
import threading

from . import util

# 每日人工介入总预算（用户明确要求：一共 50 次）
DAILY_BUDGET = 50

# 动作类型 → (中文名, 是否**必须**人工, 说明)
# 说明里的"为什么不必需"就是自动化改造的依据：能自动的都自动，剩下的才占预算。
KINDS = {
    "rate": ("消息复核打标", True, "AI 把握度不足时的抽检；把握度够的由 AI 直接采纳"),
    "order": ("成交回填/确认单", True, "T+1 净值确认后自动入账；金额不一致才需要人核对"),
    "search_merge": ("合并搜索回填", False, "已在每次研判前自动合并，无需人工点击"),
    "pool": ("重建备选池", False, "过期/不足时按 refresh_days 自动重建"),
    "param": ("改动参数开关", False, "属主动调参，不是日常必做"),
    "run": ("手动跑研判/复盘", False, "已有计划任务按点自动跑"),
    "other": ("其他人工操作", True, "兜底分类"),
}


def _connect(db_path=None):
    p = str(db_path or util.data_file("screening.db"))
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS interventions(
        date TEXT NOT NULL,
        kind TEXT NOT NULL,
        mode TEXT NOT NULL,          -- human / auto
        n INTEGER DEFAULT 0,
        detail TEXT DEFAULT '',      -- [{id,title,why}] 或一句话说明
        updated TEXT,
        PRIMARY KEY(date, kind, mode))""")
    con.commit()
    return con


class Budget(object):
    """当日人工介入预算。线程安全（Web 是多线程的）。"""

    def __init__(self, db_path=None, budget=None):
        self.db_path = db_path
        self.budget = int(budget or DAILY_BUDGET)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def log(self, kind, mode, n=1, detail=None, date_s=None):
        """记一次动作。mode='human' 才占预算；'auto' 只留痕。"""
        if kind not in KINDS:
            kind = "other"
        if mode not in ("human", "auto"):
            raise ValueError("mode 必须是 human 或 auto")
        day = str(date_s or util.today_str())[:10]
        payload = json.dumps(detail, ensure_ascii=False) if detail else ""
        with self._lock:
            con = _connect(self.db_path)
            try:
                with con:
                    con.execute(
                        "INSERT INTO interventions(date,kind,mode,n,detail,updated) "
                        "VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(date,kind,mode) DO UPDATE SET "
                        "n=n+excluded.n, updated=excluded.updated, "
                        "detail=CASE WHEN excluded.detail!='' THEN excluded.detail "
                        "ELSE detail END",
                        (day, kind, mode, int(n), payload[:4000], util.now_iso()))
            finally:
                con.close()
        return self.status(date_s=day)

    def human(self, kind, n=1, detail=None, date_s=None):
        return self.log(kind, "human", n=n, detail=detail, date_s=date_s)

    def auto(self, kind, n=1, detail=None, date_s=None):
        return self.log(kind, "auto", n=n, detail=detail, date_s=date_s)

    # ------------------------------------------------------------------
    def status(self, date_s=None, recent=7):
        """当日用量 + 明细 + 近 N 天趋势。"""
        day = str(date_s or util.today_str())[:10]
        con = _connect(self.db_path)
        try:
            today = con.execute(
                "SELECT kind,mode,n,detail,updated FROM interventions WHERE date=?",
                (day,)).fetchall()
            hist = con.execute(
                "SELECT date,SUM(CASE WHEN mode='human' THEN n ELSE 0 END) AS human_n,"
                "SUM(CASE WHEN mode='auto' THEN n ELSE 0 END) AS auto_n "
                "FROM interventions WHERE date<=? GROUP BY date "
                "ORDER BY date DESC LIMIT ?", (day, int(recent))).fetchall()
        finally:
            con.close()
        used = auto_n = 0
        by_kind = {}
        pending = []
        for r in today:
            label = KINDS.get(r["kind"], ("其他", True, ""))[0]
            item = {"kind": r["kind"], "label": label, "mode": r["mode"],
                    "n": r["n"], "updated": r["updated"],
                    "why": KINDS.get(r["kind"], ("", "", ""))[2]}
            if r["mode"] == "human":
                used += int(r["n"] or 0)
                by_kind[r["kind"]] = int(r["n"] or 0)
            else:
                auto_n += int(r["n"] or 0)
            if r["detail"]:
                try:
                    item["detail"] = json.loads(r["detail"])
                except Exception:
                    item["detail"] = r["detail"]
            pending.append(item)
        left = max(0, self.budget - used)
        return {"date": day, "budget": self.budget, "used": used, "left": left,
                "auto": auto_n, "over": used > self.budget,
                "by_kind": by_kind, "items": pending,
                "history": [{"date": r["date"], "human": int(r["human_n"] or 0),
                             "auto": int(r["auto_n"] or 0)} for r in hist],
                "kinds": {k: {"label": v[0], "needs_human": v[1], "why": v[2]}
                          for k, v in KINDS.items()}}

    # ------------------------------------------------------------------
    def low_confidence(self, limit=50, threshold=0.45, date_s=None):
        """挑出"AI 没把握"的消息——人工额度只花在这里（用户选定）。

        判据：AI 把握度 < threshold。把握度足够的条目 AI 已直接采纳，
        **不占人工额度**；这条查询就是"50 次额度该花在哪"的答案。
        """
        day = str(date_s or util.today_str())[:10]
        con = _connect(self.db_path)
        try:
            try:
                rows = con.execute(
                    "SELECT item_id,title,auto_label,auto_strength,confidence,"
                    "decided_by,auto_reason FROM items WHERE date=? "
                    "AND user_label='' AND (confidence IS NULL OR confidence<?) "
                    "ORDER BY (confidence IS NULL) DESC, confidence ASC LIMIT ?",
                    (day, float(threshold), int(limit))).fetchall()
            except sqlite3.OperationalError:
                rows = []
        finally:
            con.close()
        return [{"id": r["item_id"], "title": (r["title"] or "")[:120],
                 "auto_label": r["auto_label"], "strength": r["auto_strength"],
                 "confidence": r["confidence"], "decided_by": r["decided_by"],
                 "reason": (r["auto_reason"] or "")[:160]} for r in rows]


# ---------------- 模块级入口 ----------------
# 注意：**不**缓存模块级 Budget 实例。旧写法 `_BUDGET = Budget()` 会把 DB 路径
# 在导入时固化下来，于是改了 `FUNDAI_HOME` / 配置目录的调用方（测试、便携包）
# 会静默写到别处的库里去。这里每次调用现场构造，路径由 `util.data_file` 每次解析。
def budget():
    return Budget()


def human(kind, n=1, detail=None, date_s=None):
    return Budget().human(kind, n=n, detail=detail, date_s=date_s)


def auto(kind, n=1, detail=None, date_s=None):
    return Budget().auto(kind, n=n, detail=detail, date_s=date_s)


def status(date_s=None, recent=7):
    return Budget().status(date_s=date_s, recent=recent)


def low_confidence(limit=50, threshold=0.45, date_s=None):
    return Budget().low_confidence(limit=limit, threshold=threshold,
                                   date_s=date_s)
