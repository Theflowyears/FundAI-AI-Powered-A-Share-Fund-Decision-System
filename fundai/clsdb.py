# -*- coding: utf-8 -*-
"""财联社历史电报的 SQLite 存储（4 年量级：百万行级、增量写入、随机读取）。

为什么不用 JSON：4 年 ≈ 1000 个交易日 × ~700 条 ≈ 70 万条，单文件 JSON
每次落盘都要重新序列化几百 MB；SQLite（WAL）可以增量插入、按日期索引查询、
多进程并发写安全，是长时间大批量回填的正确载体。

表结构：
  telegrams(id PK, ctime, date, title, text, url)     原始电报
  scores(id PK, event, label, strength, important, scope, title)  线上同口径打分缓存
"""
import json
import sqlite3

from . import util

DB_FILE = "cls_history.db"


def db_path():
    return util.data_file(DB_FILE)


def connect(timeout=60):
    c = sqlite3.connect(str(db_path()), timeout=timeout)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    c.executescript("""
    CREATE TABLE IF NOT EXISTS telegrams(
        id TEXT PRIMARY KEY, ctime INTEGER, date TEXT,
        title TEXT, text TEXT, url TEXT);
    CREATE INDEX IF NOT EXISTS idx_tg_date ON telegrams(date);
    CREATE INDEX IF NOT EXISTS idx_tg_ctime ON telegrams(ctime);
    CREATE TABLE IF NOT EXISTS scores(
        id TEXT PRIMARY KEY, ctime INTEGER, event TEXT, label TEXT,
        strength INTEGER, important INTEGER, scope TEXT, title TEXT);
    CREATE INDEX IF NOT EXISTS idx_sc_ctime ON scores(ctime);
    """)
    return c


def insert_telegrams(rows, conn=None):
    """rows: [{id, ctime, title, text, url}] → 返回新增条数。"""
    c = conn or connect()
    if not rows:
        return 0
    before = c.execute("SELECT count(*) FROM telegrams").fetchone()[0]
    c.executemany(
        "INSERT OR IGNORE INTO telegrams(id,ctime,date,title,text,url) "
        "VALUES(?,?,?,?,?,?)",
        [(str(r.get("id")), int(r.get("ctime") or 0), str(r.get("date") or ""),
          (r.get("title") or "")[:300], (r.get("text") or "")[:1200],
          (r.get("url") or "")[:200]) for r in rows])
    c.commit()
    after = c.execute("SELECT count(*) FROM telegrams").fetchone()[0]
    if conn is None:
        c.close()
    return after - before


def insert_scores(rows, conn=None):
    c = conn or connect()
    if not rows:
        return 0
    c.executemany(
        "INSERT OR REPLACE INTO scores(id,ctime,event,label,strength,important,"
        "scope,title) VALUES(?,?,?,?,?,?,?,?)",
        [(str(r.get("id")), int(r.get("ctime") or 0), r.get("event") or "",
          r.get("label") or "", int(r.get("strength") or 0),
          1 if r.get("important") else 0, r.get("scope") or "",
          (r.get("title") or "")[:80]) for r in rows])
    c.commit()
    if conn is None:
        c.close()
    return len(rows)


def stats():
    c = connect()
    try:
        n = c.execute("SELECT count(*) FROM telegrams").fetchone()[0]
        ns = c.execute("SELECT count(*) FROM scores").fetchone()[0]
        rng = c.execute("SELECT min(date), max(date) FROM telegrams "
                        "WHERE date!=''").fetchone()
        days = c.execute("SELECT count(DISTINCT date) FROM telegrams "
                         "WHERE date!=''").fetchone()[0]
        return {"telegrams": n, "scored": ns, "days": days,
                "from": rng[0], "to": rng[1], "size_mb": round(
                    db_path().stat().st_size / 1048576, 1) if db_path().exists() else 0}
    finally:
        c.close()


def telegrams(limit=None, since=None, order="asc"):
    c = connect()
    try:
        sql = "SELECT id,ctime,date,title,text,url FROM telegrams"
        args = []
        if since:
            sql += " WHERE ctime>=?"
            args.append(int(since))
        sql += " ORDER BY ctime " + ("ASC" if order == "asc" else "DESC")
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def unscored(limit=None):
    c = connect()
    try:
        sql = ("SELECT t.id,t.ctime,t.date,t.title,t.text,t.url FROM telegrams t "
               "LEFT JOIN scores s ON s.id=t.id WHERE s.id IS NULL "
               "ORDER BY t.ctime")
        if limit:
            sql += " LIMIT ?"
            return [dict(r) for r in c.execute(sql, (int(limit),)).fetchall()]
        return [dict(r) for r in c.execute(sql).fetchall()]
    finally:
        c.close()


def scored_items(since=None):
    """[{id, ctime, event, label, strength, important, scope, title}]"""
    c = connect()
    try:
        sql = ("SELECT id,ctime,event,label,strength,important,scope,title "
               "FROM scores")
        args = []
        if since:
            sql += " WHERE ctime>=?"
            args.append(int(since))
        sql += " ORDER BY ctime"
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def import_json(name):
    """把旧的 JSON 缓存导入 DB（一次性迁移），返回新增条数。"""
    from datetime import datetime
    from . import util as _u
    path = _u.cache_file(name)
    if not path.exists():
        return 0
    data = _u.load_json(path, {"items": {}})
    rows = []
    for k, v in (data.get("items") or {}).items():
        ct = v.get("ctime")
        if not ct:
            continue
        try:
            ds = datetime.fromtimestamp(int(ct), _u.TZ_CN).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            ds = ""
        rows.append({"id": k, "ctime": int(ct), "date": ds,
                     "title": v.get("title") or "", "text": v.get("text") or "",
                     "url": v.get("url") or ""})
    return insert_telegrams(rows)


def backfill_dates():
    """补齐 date 列为空的电报（老数据迁移用）。"""
    from datetime import datetime
    from . import util as _u
    c = connect()
    try:
        rows = c.execute("SELECT id, ctime FROM telegrams "
                         "WHERE date IS NULL OR date=''").fetchall()
        if not rows:
            return 0
        upd = []
        for r in rows:
            try:
                upd.append((datetime.fromtimestamp(
                    int(r["ctime"]), _u.TZ_CN).strftime("%Y-%m-%d"), r["id"]))
            except (TypeError, ValueError, OSError):
                continue
        c.executemany("UPDATE telegrams SET date=? WHERE id=?", upd)
        c.commit()
        return len(upd)
    finally:
        c.close()
