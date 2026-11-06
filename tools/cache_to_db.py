# -*- coding: utf-8 -*-
"""把 `data/cache/` 下的成百上千个小 JSON 收进单个 `data/cache.db`（SQLite）。

为什么：`data/cache/` 是按日/按代码切分的缓存（`micro_<date>.json` 约 260 个、
`nav_<code>.json` 几十个、`kline_*.json` 几份……），单个体积小但**文件数到几百上千**。
把这样的目录传到 GitHub / 云盘 / 微信，瓶颈在"文件数"而不是"数据量"：
逐个文件的开销、漏传风险、增量同步成本都远超内容本身。

做法（**不动口径**）：
  * 逐文件读出 JSON → 紧凑序列化（去掉缩进与多余空格）→ 必要时 LZMA 压缩 → 写进
      `cache.db` 的 `entry(key, value, bytes, raw_bytes, mtime)` 表；
  * 打包成功后**删除已入库的 JSON 文件**（用 `--keep-files` 可保留，先看体积对比）；
  * 运行时由 `fundai/cachestore.py` + `util.load_json` 的只读回退按需读出——
    读到的对象与原 `json.load` 完全一致；
  * **磁盘文件永远优先**：本机跑出来的新缓存不会被库里的旧快照盖掉。

用法：
    python tools/cache_to_db.py                # 打包当前目录（默认删除已入库的 JSON）
    python tools/cache_to_db.py --keep-files   # 只打包、保留 JSON（对比体积）
    python tools/cache_to_db.py --dry-run      # 只统计，不写库
    python tools/cache_to_db.py <目录>          # 指定项目根（默认脚本上一级）
"""
import io
import json
import lzma
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 压缩门槛：只有明显收益才压缩（小文件压缩反而更占地方、还慢）
MIN_COMPRESS_BYTES = 2048
MIN_GAIN = 0.90          # 压缩后 < 原文 90% 才采用

SCHEMA = """
CREATE TABLE IF NOT EXISTS entry(
    key TEXT PRIMARY KEY,      -- 文件名（如 micro_2026-09-11.json）
    value BLOB NOT NULL,       -- 紧凑 JSON 文本 或 LZMA 压缩后的字节
    bytes INTEGER,             -- 库内占用字节
    raw_bytes INTEGER,         -- 磁盘原始文件字节
    mtime REAL
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def pack(root, keep_files=False, dry=False):
    data = Path(root) / "data"
    cache = data / "cache"
    db = data / "cache.db"
    if not cache.is_dir():
        print("没有 {} 目录".format(cache))
        return 1
    files = sorted(p for p in cache.rglob("*.json") if p.is_file())
    if not files:
        print("{} 下没有 JSON（已经打包过了？）".format(cache))
        return 0
    raw_total = sum(p.stat().st_size for p in files)
    print("待入库 JSON：{} 个（{:.2f} MB）".format(len(files), raw_total / 1048576))
    if dry:
        return 0
    if db.exists():
        db.unlink()
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA)
    packed = failed = 0
    stored_total = compressed = 0
    groups = {}
    t0 = time.time()
    for p in files:
        try:
            obj = json.loads(p.read_text("utf-8"))
        except Exception as e:
            failed += 1
            print("  [跳过] {}：{}".format(p.name, str(e)[:60]))
            continue
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        blob = text
        if len(text) >= MIN_COMPRESS_BYTES:
            try:
                comp = lzma.compress(text, preset=6)
                if len(comp) < len(text) * MIN_GAIN:
                    blob = comp
                    compressed += 1
            except Exception:
                blob = text
        rel = p.relative_to(cache).as_posix()
        con.execute("INSERT OR REPLACE INTO entry(key,value,bytes,raw_bytes,mtime) "
                    "VALUES(?,?,?,?,?)",
                    (rel, blob, len(blob), p.stat().st_size, p.stat().st_mtime))
        packed += 1
        stored_total += len(blob)
        g = rel.split("_")[0] if "_" in rel else rel
        s = groups.setdefault(g, [0, 0])
        s[0] += 1
        s[1] += len(blob)
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                ("built", time.strftime("%Y-%m-%d %H:%M:%S")))
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                ("source_files", str(packed)))
    con.commit()
    con.execute("VACUUM")
    con.close()
    if not keep_files:
        for p in files:
            try:
                p.unlink()
            except OSError:
                pass
        # 清掉空目录
        for d in sorted((q for q in cache.rglob("*") if q.is_dir()),
                        key=lambda q: -len(q.parts)):
            try:
                d.rmdir()
            except OSError:
                pass
    print("入库 {} 个（失败 {}），压缩 {} 个，耗时 {:.1f}s".format(
        packed, failed, compressed, time.time() - t0))
    print("库大小 {:.2f} MB ｜ 原 JSON 合计 {:.2f} MB ｜ 压缩比 {:.0%}".format(
        db.stat().st_size / 1048576, raw_total / 1048576,
        db.stat().st_size / max(1, raw_total)))
    print("分组：")
    for g, (n, b) in sorted(groups.items(), key=lambda kv: -kv[1][0])[:10]:
        print("   {:<12} {:>4} 个  {:.2f} MB".format(g, n, b / 1048576))
    if keep_files:
        print("（保留了 JSON 文件；去掉 --keep-files 才会删除）")
    else:
        print("已删除入库的 JSON；运行时由 fundai/cachestore.py 按需读出。")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = Path(args[0]).resolve() if args else ROOT
    return pack(root, keep_files="--keep-files" in sys.argv,
                dry="--dry-run" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
