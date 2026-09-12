# -*- coding: utf-8 -*-
"""回补近一年（约 260 个交易日）的情绪微观快照，用于情绪维度校准。

THS 接口支持 date= 参数（实测 2025-12 仍可取到），因此可以补齐历史涨停池/炸板/跌停/
风口/大局观 → 重建每日情绪分与题材热度，供"情绪维度该怎么用"的量化研究。
"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
parallel.use_utf8_stdout()  # 已是 UTF-8 就不重包（避免包装器被 GC 关掉底层 buffer）

from fundai import calib, microstructure as ms, util  # noqa: E402

import parallel  # noqa: E402

closes, dates = calib.load_closes(refresh=False)
days = [d for d in dates if d >= util.add_days(util.today_str(), -430)][-260:]
days = list(reversed(days))          # 从最近往回补：先满足近 7/30 日轨迹，再补更早
ok = skip = fail = 0
for i, d in enumerate(days):
    if ms.load_snap(d):
        skip += 1
        continue
    try:
        p = ms.build_day(d, force=True)
        if p and (p.get("snap") or {}).get("score") is not None:
            ok += 1
        else:
            fail += 1
    except Exception:
        fail += 1
    if (i + 1) % 20 == 0:
        print("进度 %d/%d | 新增 %d 跳过 %d 失败 %d" % (
            i + 1, len(days), ok, skip, fail), flush=True)
    time.sleep(0.15)
print("完成：新增 %d，跳过 %d，失败 %d" % (ok, skip, fail))
