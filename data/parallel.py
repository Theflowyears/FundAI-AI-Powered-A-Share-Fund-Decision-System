# -*- coding: utf-8 -*-
"""并行加速底座：把"独立任务"铺满 CPU（本机 24 物理核 / 32 线程）。

为什么单独一个模块
------------------
研究脚本（`regime_study` / `regime_backtest` / `harness`）与审计脚本（`audit*`）
都需要并行，但两类负载性质不同：

* **CPU 密集**（组合扫描、逐日指标、引擎回放）→ 用**进程池**（Python 有 GIL，
  线程池在这种负载上几乎不加速）。本机 24 核，实测组合扫描可提速 ~10 倍。
* **I/O 密集**（HTTP 探测、跑子进程做审计）→ 用**线程池**即可（等 I/O 时释放 GIL）。

并行纪律（避免"越并越慢/结果错乱"）
------------------------------------
1. **只并行纯计算**：会写共享文件的步骤（SQLite 写入、`pool_state.json`、计数器）
   留在主进程串行执行，否则会出现交叉写与计数漂移；
2. **每个 worker 自己读缓存**，不要用大对象当任务参数（避免把几 MB 数据 pickle 很多遍）；
3. **任务粒度别太细**：小于 ~10ms 的任务并行开销大于收益，用 `chunksize` 成批；
4. `FUNDAI_JOBS` 环境变量可覆盖并行度（`--jobs N` 等价），设为 1 即退回串行。

用法
----
    from parallel import pmap, tmap, workers
    out = pmap(worker_fn, tasks, init=_init_worker, initargs=(cfg,))   # 进程池
    out = tmap(lambda t: http_call(t), tasks, workers=16)              # 线程池
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def use_utf8_stdout():
    """把 stdout 切成 UTF-8 —— **已经是 UTF-8 就不再包一层**。

    为什么加这个判断（2026-09-11 踩到的坑）：多个脚本各自
    `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` 时，前一个包装器被垃圾回收
    会**关掉底层 buffer**，后续 print 直接 `ValueError: I/O operation on closed file`。
    这个 bug 只在"一个进程里 import 了两个脚本"时出现（并行 worker、组合脚本最容易撞上）。
    """
    try:
        enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
        if enc == "utf8":
            return sys.stdout
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
    except Exception:
        pass
    return sys.stdout


use_utf8_stdout()


def workers(n=None, for_io=False):
    """并行度：显式 n > 环境变量 FUNDAI_JOBS > CPU 数。

    `for_io=True`（HTTP/子进程）时给到上限的 2 倍——等待网络的时间本来就该靠并发填满；
    CPU 密集时**不超过物理核数**（超配只会互相抢缓存，实测收益为负）。
    """
    if n:
        return max(1, int(n))
    env = os.environ.get("FUNDAI_JOBS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    cpu = os.cpu_count() or 4
    if for_io:
        return max(2, min(64, cpu * 2))
    try:
        import multiprocessing as mp
        phys = mp.cpu_count()          # 逻辑核（Python 不区分物理/逻辑）
        return max(2, phys)
    except Exception:
        return max(2, cpu)


def can_spawn():
    """当前进程能否用**进程池**。

    Windows 用 spawn 起子进程：子进程必须能重新 import 主模块。如果主程序是
    `python -`（stdin）、`python -c` 或交互式解释器，`__main__` 没有文件路径，
    spawn 会直接崩（实测 `BrokenProcessPool: child process terminated abruptly`）。
    这种情况下自动退回串行，并给出提示——绝不"默默算错"。
    """
    import __main__
    return bool(getattr(__main__, "__file__", None))


_PROBE = {}


def probe_process_pool():
    """实测一次"进程池到底能不能建起来"（结果缓存）。

    为什么在 `can_spawn()` 之外还要实测（2026-09-12 踩到）：
      即使主模块有文件路径，**受限环境**（受限令牌/沙箱/某些杀软策略）下
      `ProcessPoolExecutor` 也会在创建管道时抛 `PermissionError: [WinError 5]`。
      只看 `can_spawn()` 会高高兴兴地走并行分支，然后整个命令炸掉——
      而正确的行为是"退回串行并把原因讲清楚"。
    """
    if "ok" in _PROBE:
        return _PROBE["ok"]
    if not can_spawn():
        _PROBE["ok"] = False
        _PROBE["why"] = "主模块无文件路径（stdin/-c 调用）"
        return False
    try:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=1) as ex:
            ok = ex.submit(int, 1).result(timeout=60)
        _PROBE["ok"] = (ok == 1)
        if not _PROBE["ok"]:
            _PROBE["why"] = "子进程返回值异常"
    except Exception as e:                     # PermissionError/OSError/BrokenProcessPool…
        _PROBE["ok"] = False
        _PROBE["why"] = "{}: {}".format(type(e).__name__, str(e)[:80])
    return _PROBE["ok"]


def pmap(fn, tasks, init=None, initargs=(), jobs=None, chunksize=None):
    """进程池 map（保持顺序）。`init` 在每个 worker 启动时执行一次，用于加载共享数据。

    任何一步建不起进程池（受限环境/无主模块路径），都**退回串行**并只提示一次：
    宁可慢一点，也不能让整条命令失败或算错。
    """
    tasks = list(tasks)
    if not tasks:
        return []
    n = min(workers(jobs), len(tasks))
    if n > 1 and not probe_process_pool():
        if not _WARNED.get("spawn"):
            print("[parallel] 进程池不可用（{}）→ 本次退回串行（结果口径不变，只是慢）。"
                  "要并行请在有权限的环境用脚本文件运行。".format(
                      _PROBE.get("why") or "原因未知"))
            _WARNED["spawn"] = True
        n = 1
    if n <= 1:
        if init:
            init(*initargs)
        return [fn(t) for t in tasks]
    from concurrent.futures import ProcessPoolExecutor
    cs = chunksize or max(1, len(tasks) // (n * 4))
    try:
        with ProcessPoolExecutor(max_workers=n, initializer=init,
                                 initargs=initargs) as ex:
            return list(ex.map(fn, tasks, chunksize=cs))
    except Exception as e:
        # 兜底：建池/执行阶段仍失败（环境策略中途收紧等）→ 串行再来一遍
        print("[parallel] 进程池执行失败（{}）→ 退回串行重算。".format(
            type(e).__name__))
        if init:
            init(*initargs)
        return [fn(t) for t in tasks]


_WARNED = {}


def tmap(fn, tasks, jobs=None):
    """线程池 map（保持顺序），适合 HTTP/子进程等 I/O 密集任务。"""
    tasks = list(tasks)
    if not tasks:
        return []
    n = min(workers(jobs, for_io=True), len(tasks))
    if n <= 1:
        return [fn(t) for t in tasks]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(fn, tasks))


def tmap_unordered(fn, tasks, jobs=None):
    """线程池 map（不保证顺序），适合互相independent且耗时差异大的探测任务。"""
    tasks = list(tasks)
    if not tasks:
        return []
    n = min(workers(jobs, for_io=True), len(tasks))
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out = []
    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = {ex.submit(fn, t): t for t in tasks}
        for fu in as_completed(futs):
            try:
                out.append(fu.result())
            except Exception as e:      # 单个任务失败不影响整体
                out.append({"error": "{}: {}".format(type(e).__name__, str(e)[:120]),
                            "task": futs[fu]})
    return out


def describe(jobs=None):
    return "并行度 {}（CPU {}）".format(workers(jobs), os.cpu_count())
