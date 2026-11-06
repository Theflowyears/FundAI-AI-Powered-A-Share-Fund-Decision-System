# -*- coding: utf-8 -*-
"""缓存库（SQLite）：把 `data/cache/` 下成百上千个小 JSON 收进一个文件按需读取。

为什么要有这一层
----------------
`data/cache/` 里全是**按日/按代码**切分的缓存：涨停情绪快照 `micro_<date>.json`
（约 260 个交易日一个文件）、基金净值 `nav_<code>.json`、指数K线 `kline_*.json` 等。
它们单个体积很小（几 KB），但**文件数会到几百上千**：

* 传到 GitHub / 云盘 / 微信时，几百个小文件本身就是负担（且容易漏传）；
* 压缩包、`git clone`、增量同步在小文件上的开销远大于它们的数据量本身。

所以发布快照把这些 JSON **收进单个 `data/cache.db`**（同内容、同口径，只是换了存放方式），
运行时按需读出——**不影响任何算法口径**：读出来的对象与原来 `json.load` 的完全一致。

读取优先级（关键设计）
----------------------
1. **磁盘上的同名 JSON 优先**：本机跑出来的缓存永远比库里的快照新，
   所以"文件 > 库"的优先级保证了**本机行为与加这层之前逐位一致**；
2. 文件不存在时才查库（发布快照解压后没带 JSON，直接命中库）；
3. 两处都没有 → 返回调用方给的默认值（与原来 `load_json` 找不到文件的行为一致）。

写入不受影响：`util.save_json` 照旧写磁盘 JSON（本机继续按原样累积缓存）。
环境变量 `FUNDAI_CACHE_DB=0` 可完全关闭这层（调试/对比用）。
"""
import json
import os
import sqlite3
import threading
from pathlib import Path

# 压缩魔数（xz/LZMA）：库里的值可能是压缩的，读出后自动识别
_XZ_MAGIC = b"\xfd7zXZ\x00"

_LOCK = threading.RLock()
_LOCAL = threading.local()


def db_path():
    """缓存库路径：`<项目数据目录>/cache.db`（随 FUNDAI_HOME 走）。"""
    from . import util
    return Path(util.DATA_DIR) / "cache.db"


def enabled():
    """是否启用缓存库读取（环境变量 FUNDAI_CACHE_DB=0 可关闭）。"""
    return os.environ.get("FUNDAI_CACHE_DB", "1") not in ("0", "false", "False")


def _conn():
    """每线程一条只读连接（SQLite 连接不能跨线程共享）。"""
    p = db_path()
    if not p.exists():
        return None
    key = str(p)
    c = getattr(_LOCAL, "conn", None)
    if c is not None and getattr(_LOCAL, "path", None) == key:
        return c
    try:
        c = sqlite3.connect("file:{}?mode=ro".format(
            str(p).replace("\\", "/")), uri=True, check_same_thread=False)
        c.execute("SELECT 1 FROM entry LIMIT 1")     # 表不存在会抛 → 视为不可用
    except Exception:
        try:
            c.close()
        except Exception:
            pass
        return None
    _LOCAL.conn, _LOCAL.path = c, key
    return c


def _decode(raw):
    if isinstance(raw, str):
        return json.loads(raw)
    if raw[:6] == _XZ_MAGIC:
        import lzma
        return json.loads(lzma.decompress(raw).decode("utf-8"))
    return json.loads(raw.decode("utf-8"))


def _key(name):
    """文件名（或相对路径）→ 库内键。只接受 basename，避免路径穿越。"""
    return Path(str(name)).name


def _in_configured_cache_dir(path):
    """该路径是否落在**本项目配置的** `data/cache/` 下。

    为什么必须判断：离线回测/审计用例会把指数K线缓存路径**临时重定向到别处**
    （模拟"缓存为空"）。那种情况下如果照样回退到本项目的 `cache.db`，
    用例的前提就不成立了（读出来的是项目里的长历史）。所以只有"读的确实是
    本项目缓存目录里的文件且它不存在"时才回退查库——**缓存路径一旦被重定向，
    库这层自动让路**，语义保持"文件系统说了算"。
    """
    if not enabled():
        return False
    try:
        from . import util
        base = Path(str(util.CACHE_DIR)).resolve()
        p = Path(str(path))
        try:
            rp = p.resolve()
        except OSError:
            rp = Path(os.path.abspath(str(p)))
        return rp.parent == base
    except Exception:
        return False


def fallback_for(path, default, allow=True):
    """只读回退：库里有 → 返回它；否则返回 default（`util.load_json` 内部用）。"""
    if allow and _in_configured_cache_dir(path):
        v = value(Path(str(path)).name)
        if v is not None:
            return v
    return default


def has(name):
    """库里是否有这条缓存（只查 key，不解析内容）。"""
    if not enabled():
        return False
    with _LOCK:
        c = _conn()
        if c is None:
            return False
        try:
            return c.execute("SELECT 1 FROM entry WHERE key=?",
                             (_key(name),)).fetchone() is not None
        except Exception:
            return False


def value(name):
    """按文件名取值；库里没有/解不开返回 None（调用方负责默认值）。"""
    if not enabled():
        return None
    with _LOCK:
        c = _conn()
        if c is None:
            return None
        try:
            row = c.execute("SELECT value FROM entry WHERE key=?",
                            (_key(name),)).fetchone()
        except Exception:
            return None
    if not row:
        return None
    try:
        return _decode(row[0])
    except Exception:
        return None


def cached_json(path, default=None):
    """`util.load_json` 的回退实现：path 是 `data/cache/xxx.json` 这类路径。

    只对缓存目录下的文件名生效（配置文件、账本等不在此列）。
    """
    p = Path(path)
    if p.suffix.lower() != ".json":
        return default
    v = value(p.name)
    return default if v is None else v


def keys(prefix="", suffix=".json"):
    """列出库里的键（可按前缀/后缀过滤）——供"没有磁盘文件时"的目录式枚举。"""
    if not enabled():
        return []
    with _LOCK:
        c = _conn()
        if c is None:
            return []
        try:
            rows = c.execute(
                "SELECT key FROM entry WHERE key LIKE ? ORDER BY key",
                ("{}%{}".format(prefix, suffix),)).fetchall()
        except Exception:
            return []
    return [r[0] for r in rows]


def codes(prefix="nav_", suffix=".json"):
    """列举缓存里出现过的代码（`nav_000217.json` → `000217`）。"""
    out = []
    for k in keys(prefix, suffix):
        stem = k[len(prefix):-len(suffix)] if k.endswith(suffix) else k
        if stem:
            out.append(stem)
    return out


def dates(prefix="micro_", suffix=".json"):
    """列举缓存里出现过的日期（`micro_2026-09-11.json` → `2026-09-11`）。"""
    out = []
    for k in keys(prefix, suffix):
        stem = k[len(prefix):-len(suffix)] if k.endswith(suffix) else k
        if len(stem) == 10 and stem[4] == "-":
            out.append(stem)
    return sorted(out)


def stats():
    """调试/自检用：库的规模与键分布。"""
    p = db_path()
    if not p.exists():
        return {"exists": False, "path": str(p)}
    with _LOCK:
        c = _conn()
        if c is None:
            return {"exists": True, "path": str(p), "readable": False}
        n = c.execute("SELECT COUNT(*) FROM entry").fetchone()[0]
        size = c.execute("SELECT COALESCE(SUM(LENGTH(value)),0) "
                         "FROM entry").fetchone()[0]
        groups = {}
        for (k,) in c.execute("SELECT key FROM entry"):
            g = k.split("_")[0] if "_" in k else k
            groups[g] = groups.get(g, 0) + 1
    return {"exists": True, "path": str(p), "entries": n,
            "payload_mb": round((size or 0) / 1048576.0, 2),
            "file_mb": round(p.stat().st_size / 1048576.0, 2),
            "groups": dict(sorted(groups.items(), key=lambda kv: -kv[1])[:12])}
