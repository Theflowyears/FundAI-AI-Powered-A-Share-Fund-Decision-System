# -*- coding: utf-8 -*-
"""一次性补数据脚本（2026-09-12）：把 9/11 的 AI 次日方向口径补进账本记录。

背景（只改数据、不改算法）：
  9/11 的判断行本来就在库里（neutral，信心 0.50），但当天写记录时 `calc` JSON 里
  **没有** `direction` 键——那时"方向"只在 `screening.db` 落一行，口径参数不落账本。
  这导致两个后果：
    1. 事后无法证明这条判断是用哪一版评分算出来的（不可复现）；
    2. `direction-check` 补跑时只能退回 `market_score`（LLM 分，与本项目
       "方向由平滑后合成评分推导"的口径不是同一个数）。

  修复后的 `run_daily` 会把口径写进 `calc.direction`（dir/confidence/score/note），
  离线重跑同一天也能逐位一致。本脚本按**同一口径**给 9/11 那条记录补上这一个键，
  不动评分、不动观点、不动持仓/订单/快照。

导出顺序：先备份 fund200.db，再改；改完打印前后对比。
"""
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # tools/ 的上一级 = 项目根
DB = ROOT / "data" / "fund200.db"
DATE = "2026-09-11"
# 9/11 的平滑后合成评分（records.calc.smooth.value / market_score 均为 -25），
# 经 analysis.view_of 推导 = 中性，信心 0.5 —— 与库内 direction 行的 (neutral, 0.5) 一致。
SMOOTH_SCORE = -25


def main():
    apply = "--apply" in sys.argv
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM records WHERE date=?", (DATE,)).fetchone()
    if not row:
        print("找不到 {} 的研判记录".format(DATE))
        return 1
    calc = json.loads(row["calc"]) if row["calc"] else {}
    before = json.dumps(calc.get("direction"), ensure_ascii=False)
    smooth = (calc.get("smooth") or {}).get("value")
    print("记录 {}：market_score={} calc.smooth.value={} 现有 calc.direction={}".format(
        DATE, row["market_score"], smooth, before))
    if "direction" in calc:
        print("已有 calc.direction，无需补齐。")
        return 0
    if smooth != SMOOTH_SCORE:
        print("平滑分与预期不一致（{} != {}），为安全起见不自动写入".format(
            smooth, SMOOTH_SCORE))
        return 1
    sys.path.insert(0, str(ROOT))
    from fundai.engine import Engine
    ai_dir, ai_conf = Engine.ai_direction(SMOOTH_SCORE)
    print("按现有口径推导：dir={} confidence={:.4f}".format(ai_dir, ai_conf))
    calc["direction"] = {
        "dir": ai_dir, "confidence": round(float(ai_conf), 4),
        "score": int(SMOOTH_SCORE),
        "note": "次日方向由平滑后合成评分推导（看多≥+25 / 看空≤−25 / 其余中性），"
                "下一交易日收盘后结算命中；本键于 2026-09-12 按同一口径补录",
    }
    if not apply:
        print("预览（未写入）：", json.dumps(calc["direction"], ensure_ascii=False))
        print("加 --apply 才真正写入")
        return 0
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = DB.with_name("fund200.db.bak_direction_calc_{}".format(stamp))
    shutil.copy2(str(DB), str(bak))
    with con:
        con.execute("UPDATE records SET calc=? WHERE date=?",
                    (json.dumps(calc, ensure_ascii=False), DATE))
    print("已写入 {}（备份：{}）".format(DATE, bak.name))
    chk = json.loads(con.execute("SELECT calc FROM records WHERE date=?",
                                 (DATE,)).fetchone()[0])
    print("复核 calc.direction =", json.dumps(chk["direction"], ensure_ascii=False))
    print("复核其它字段未被改动：market_score={} view_title={} source={}".format(
        row["market_score"], row["view_title"], row["source"]))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
