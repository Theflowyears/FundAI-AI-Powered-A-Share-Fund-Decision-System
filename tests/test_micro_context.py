# -*- coding: utf-8 -*-
"""情绪分布上下文与保留策略的测试（纯函数 + 临时缓存目录）。"""
import glob
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fundai import microstructure as ms, util


class ContextStatsTest(unittest.TestCase):
    def test_basic_stats(self):
        scores = [-60, -20, 0, 10, 20, 40, 60]
        c = ms._context_stats(scores, ["d%d" % i for i in range(len(scores))], 30)
        self.assertEqual(c["days"], 7)
        self.assertEqual(c["latest"], 60)
        self.assertEqual(c["pct_rank"], 1.0)          # 最高分 → 100% 分位
        self.assertEqual(c["min"], -60)
        self.assertEqual(c["max"], 60)
        self.assertEqual(c["hot_days"], 2)            # 40 与 60
        self.assertEqual(c["cold_days"], 1)           # -60
        self.assertEqual(c["label"], "偏热")
        self.assertEqual(c["window"], 30)
        self.assertEqual(c["from"], "d0")

    def test_cold_extreme_label(self):
        c = ms._context_stats([50, 40, 30, 20, 10, 0, -80],
                              ["d%d" % i for i in range(7)])
        self.assertEqual(c["latest"], -80)
        self.assertLessEqual(c["pct_rank"], 0.2)
        self.assertEqual(c["label"], "偏冷")

    def test_trend_sign(self):
        rising = ms._context_stats([0, 0, 0, 0, 0, 10, 20, 30, 40, 50],
                                   ["d%d" % i for i in range(10)])
        self.assertGreater(rising["trend"], 0)
        falling = ms._context_stats([50, 40, 30, 20, 10, 0, 0, 0, 0, 0],
                                    ["d%d" % i for i in range(10)])
        self.assertLess(falling["trend"], 0)

    def test_empty(self):
        self.assertEqual(ms._context_stats([], []), {"days": 0})


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patch = mock.patch.object(
            util, "cache_file", side_effect=lambda n: Path(self.dir) / n)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _mk(self, n):
        for i in range(n):
            name = "micro_2026-{:02d}-{:02d}.json".format(i // 28 + 1, i % 28 + 1)
            (Path(self.dir) / name).write_text("{}", encoding="utf-8")

    def test_no_prune_below_threshold(self):
        self._mk(100)
        r = ms.prune_snaps(keep_days=120)
        self.assertEqual(r["removed"], 0)
        self.assertEqual(len(glob.glob(os.path.join(self.dir, "micro_*.json"))), 100)

    def test_prune_keeps_newest(self):
        self._mk(200)
        r = ms.prune_snaps(keep_days=120)
        left = sorted(glob.glob(os.path.join(self.dir, "micro_*.json")))
        self.assertEqual(len(left), 120)
        self.assertEqual(r["kept"], 120)
        self.assertGreater(r["removed"], 0)


if __name__ == "__main__":
    unittest.main()
