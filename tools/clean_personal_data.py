# -*- coding: utf-8 -*-
"""清空**发布快照**里的个人操作数据（只作用于指定目录，绝不碰本机运行目录）。

为什么单独一个脚本
------------------
发布到 GitHub 的仓库要能"开箱开跑"，但**不能带上发布者自己的操作痕迹**：
真实持仓、成交、资产曲线、每日研判记录、方向判断与命中率、午间诊断、
参数改动历史、备选池重建记录、搜索补全队列……

做法是**只删数据、保留结构**：
  * 账本 `fund200.db`：清空 lots/orders/executed/records/snapshots，并把 meta 复位成
    "起始资金 1000 元、尚未初始化账户"的干净状态（表结构原样保留 → 程序照常跑）；
  * 消息库 `screening.db`：清空 items（个人抓取/打标的消息与打标动作）、direction（方向
    判断与命中率）、intraday（午间诊断），**保留 lexicon**（那是由线上消息聚合而来的
    术语统计，不含任何一条具体消息或链接，属于研究产物）;
  * 运行期状态与日志：直接删除（`live.db` / `pool_state.json` / `risk_signal.json` /
    `api_usage.json` / `search_*.json` / `*.log`）；
  * **研究产物一律保留**：`data/*.json` 的回测与研究报告、`data/cache/` 的行情净值缓存、
    `data/demo.db` 合成演示库、`data/*.py` 研究脚本。

用法：
    python tools/clean_personal_data.py <发布目录> [--dry-run]
"""
import json
import sqlite3
import sys
from pathlib import Path

# 账本：清空的表（保留表结构）
LEDGER_TABLES = ("lots", "orders", "executed", "records", "snapshots")
# 账本 meta：只留"干净起步"所需的键
LEDGER_META_KEEP = {}
LEDGER_META_RESET = {"cash": "1000.0", "seed_cash": "1000.0", "fees": "0"}
# 消息库：清空的表（lexicon 保留：术语统计，非个人数据）
SCREEN_TABLES = ("items", "direction", "intraday")
# 直接删除的运行期文件/目录
DELETE_FILES = ("live.db", "pool_state.json", "risk_signal.json",
                "api_usage.json", "search_queue.json", "search_results.json",
                "server_autostart.log", "signals_update.log")
DELETE_DIRS = ("config_backups",)


def _tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]


def _clean_ledger(path, dry):
    if not path.exists():
        return ["（账本不存在，跳过）"]
    con = sqlite3.connect(str(path))
    notes = []
    try:
        tabs = _tables(con)
        for t in LEDGER_TABLES:
            if t in tabs:
                n = con.execute('SELECT COUNT(*) FROM "{}"'.format(t)).fetchone()[0]
                if not dry:
                    con.execute('DELETE FROM "{}"'.format(t))
                notes.append("{}: 清空 {} 行".format(t, n))
        if "meta" in tabs:
            cols = [r[1] for r in con.execute("PRAGMA table_info(meta)")]
            kcol = "k" if "k" in cols else cols[0]
            vcol = "v" if "v" in cols else cols[1]
            before = {r[0]: r[1] for r in con.execute(
                'SELECT "{}","{}" FROM meta'.format(kcol, vcol))}
            if not dry:
                con.execute("DELETE FROM meta")
                for k, v in LEDGER_META_RESET.items():
                    con.execute('INSERT OR REPLACE INTO meta("{}","{}") '
                                "VALUES(?,?)".format(kcol, vcol), (k, v))
            notes.append("meta: {} → {}".format(
                json.dumps(before, ensure_ascii=False),
                json.dumps(LEDGER_META_RESET, ensure_ascii=False)))
        if "sqlite_sequence" in tabs and not dry:
            con.execute("DELETE FROM sqlite_sequence")
        if not dry:
            con.commit()
            con.execute("VACUUM")
    finally:
        con.close()
    return notes


def _clean_screen(path, dry):
    if not path.exists():
        return ["（消息库不存在，跳过）"]
    con = sqlite3.connect(str(path))
    notes = []
    try:
        tabs = _tables(con)
        for t in SCREEN_TABLES:
            if t in tabs:
                n = con.execute('SELECT COUNT(*) FROM "{}"'.format(t)).fetchone()[0]
                if not dry:
                    con.execute('DELETE FROM "{}"'.format(t))
                notes.append("{}: 清空 {} 行".format(t, n))
        if "lexicon" in tabs:
            n = con.execute("SELECT COUNT(*) FROM lexicon").fetchone()[0]
            notes.append("lexicon: 保留 {} 词（术语统计，非个人数据）".format(n))
        if not dry:
            con.commit()
            con.execute("VACUUM")
    finally:
        con.close()
    return notes


def _clean_demo_series(path, dry):
    """演示库的合成序列里带"某日快照"这类运行痕迹 → 复位为空，让 demo 命令重建。"""
    if not path.exists():
        return None
    if dry:
        return "demo/series.json: 将复位（demo 命令可重建）"
    path.unlink()
    return "demo/series.json: 已删除（python app.py demo 可重建）"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1]).resolve()
    dry = "--dry-run" in sys.argv
    data = root / "data"
    if not data.is_dir():
        print("目录不像发布包（缺 data/）：{}".format(root))
        return 1
    print("=== 清理发布快照的个人操作数据：{}（{}）".format(
        root, "预演，不写盘" if dry else "实际写入"))
    print("[账本 fund200.db]")
    for line in _clean_ledger(data / "fund200.db", dry):
        print("   ", line)
    print("[消息库 screening.db]")
    for line in _clean_screen(data / "screening.db", dry):
        print("   ", line)
    print("[运行期状态与日志]")
    for name in DELETE_FILES:
        p = data / name
        if p.exists():
            if not dry:
                p.unlink()
            print("    删除", name)
    for name in DELETE_DIRS:
        p = data / name
        if p.is_dir():
            if not dry:
                import shutil
                shutil.rmtree(str(p), ignore_errors=True)
            print("    删除目录", name + "/")
    line = _clean_demo_series(data / "demo" / "series.json", dry)
    if line:
        print("   ", line)
    print("[保留]")
    print("    data/*.json 研究报告、data/cache/ 行情净值缓存、data/demo.db 合成库、"
          "data/*.py 研究脚本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
