# -*- coding: utf-8 -*-
"""并行加速基准：给"改造前后"一个可复现的数字（不是感觉快）。

跑法：python tools/bench_parallel.py [--jobs 8]
输出：每一步的串行/并行耗时与加速比，落盘 data/bench_parallel.json。

注意：进程池在 Windows 用 spawn，**必须用脚本文件运行**（stdin/-c 调用时
`parallel.pmap` 会自动退回串行并提示）。
"""
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))

import parallel  # noqa: E402

sys.stdout = parallel.use_utf8_stdout()

import harness  # noqa: E402
from fundai import settings, util  # noqa: E402


def bench_base(jobs):
    p = util.cache_file(harness.BASE_CACHE)
    if p.exists():
        p.unlink()
    t0 = time.time()
    days = harness.build_base(use_cache=True, jobs=jobs)
    return len(days), time.time() - t0


def bench_score_series(jobs, days):
    """评分序列（组合扫描的前置步骤）：15 组权重 × 2188 天。"""
    import itertools
    cfg = settings.load_config()
    tasks = list(itertools.product((0.0, 0.10, 0.20, 0.30, 0.45),
                                   (0.0, 0.10, 0.20)))
    t0 = time.time()
    parallel.pmap(_score_one, tasks, init=_init_score, initargs=(days, cfg),
                  jobs=jobs)
    return len(tasks), time.time() - t0


_SCORE = {}


def _init_score(days, cfg):
    _SCORE["days"] = days
    _SCORE["cfg"] = cfg


def _score_one(task):
    w_news, w_micro = task
    return harness.score_series(_SCORE["days"], _SCORE["cfg"],
                                w_news=w_news, w_micro=w_micro)


def main():
    jobs = None
    if "--jobs" in sys.argv:
        jobs = int(sys.argv[sys.argv.index("--jobs") + 1])
    print("=== fundai 并行基准（{}）===".format(parallel.describe(jobs)))
    out = {"cpu": os.cpu_count(), "jobs": jobs, "steps": []}
    for label, fn in (("逐日指标（261 天窗口 × 2188 天）", None),):
        pass
    # 1) 逐日指标
    n1, s1 = bench_base(1)
    n2, s2 = bench_base(jobs)
    print("逐日指标：串行 %.1fs → 并行 %.1fs（%d 天，加速 %.1f×）"
          % (s1, s2, n2, s1 / max(s2, 1e-6)))
    out["steps"].append({"step": "build_base", "serial": round(s1, 2),
                         "parallel": round(s2, 2), "speedup": round(s1 / max(s2, 1e-6), 2)})
    days = harness.load_days()
    # 2) 评分序列：**刻意只测串行** —— 实测 15 组只要 0.1 秒，铺到 32 个进程反而
    #    变成 0.7 秒（进程启动/序列化开销 ≫ 计算量）。这条留在基准里当反例：
    #    "只并行值得并行的部分"。regime_study 的正确做法是让**每个 worker 自己算
    #    它那一组**（1 组/进程，无额外开销），而不是把 15 组当 15 个任务发出去。
    n3, s3 = bench_score_series(1, days)
    print("评分序列：串行 %.2fs（%d 组）—— 太轻，**不并行**（并行实测反而 0.7s）"
          % (s3, n3))
    out["steps"].append({"step": "score_series", "serial": round(s3, 2),
                         "parallel": None,
                         "note": "过轻，并行反而更慢（0.7s）→ 保持串行"})
    util.save_json(str(util.data_file("bench_parallel.json")), out)
    print("已存档 data/bench_parallel.json")


if __name__ == "__main__":
    main()
