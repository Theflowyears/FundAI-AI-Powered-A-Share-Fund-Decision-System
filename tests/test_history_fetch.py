# -*- coding: utf-8 -*-
"""历史抓取的窗口/续跑/合并逻辑测试（用假分页器，不联网）。"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fundai import news, util


def _ts(s):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).replace(
                tzinfo=util.TZ_CN).timestamp())
        except ValueError:
            continue
    raise ValueError(s)


class _FakeFeed:
    """假分页器：按 50 条/页、每页覆盖 1 小时，从 end 往回翻。"""

    def __init__(self, end_ts, span_hours):
        self.end = end_ts
        self.n = int(span_hours)

    def __call__(self, last_time=None, rn=50, timeout=18):
        cur = int(last_time or self.end)
        rows, oldest = [], None
        for i in range(1, 51):
            ts = cur - i * 72          # 每 72 秒一条
            if ts < self.end - self.n * 3600:
                break
            rows.append({"id": "id%d" % ts, "ctime": ts, "source": "财联社电报",
                         "time": "", "title": "t%d" % ts, "text": "x", "url": ""})
            oldest = ts
        return rows, oldest


class HistoryWindowTest(unittest.TestCase):
    def setUp(self):
        import uuid
        patcher = mock.patch.object(news, "cls_roll_page",
                                    _FakeFeed(_ts("2026-05-01 12:00"), 120))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tag = "cls_t{}.json".format(uuid.uuid4().hex[:8])
        self._made = []

    def tearDown(self):
        for name in self._made:
            try:
                p = util.cache_file(name)
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    def _name(self, suffix=""):
        n = self.tag.replace(".json", "{}.json".format(suffix))
        self._made.append(n)
        return n

    def _cache(self, name):
        return util.cache_file(name)

    def test_until_window_and_resume(self):
        n1 = self._name("_a")
        # 从 05-01 往回抓 3 天
        h = news.cls_history(days=3, max_pages=200, delay=0.0,
                             until="2026-05-01", cache_name=n1)
        self.assertFalse(h["from_cache"])
        self.assertGreater(len(h["items"]), 50)
        got1 = {i["ctime"] for i in h["items"]}
        # 再跑一次：窗口已覆盖 → 直接命中缓存，不翻页
        h2 = news.cls_history(days=3, max_pages=200, delay=0.0,
                              until="2026-05-01", cache_name=n1)
        self.assertTrue(h2["from_cache"])
        self.assertEqual(h2["pages"], 0)
        # force 续跑：应从缓存里最早一条继续（不重复抓近期页）
        h3 = news.cls_history(days=30, max_pages=5, delay=0.0, force=True,
                              until="2026-05-01", cache_name=n1)
        self.assertLessEqual(h3["pages"], 5)
        self.assertLess(min(i["ctime"] for i in h3["items"]), min(got1))

    def test_window_excludes_older_than_cutoff(self):
        n2 = self._name("_b")
        news.cls_history(days=2, max_pages=200, delay=0.0,
                         until="2026-05-01", cache_name=n2)
        store = util.load_json(self._cache(n2), {"items": {}})
        cut = _ts("2026-05-01") - 2 * 86400
        self.assertTrue(all((v.get("ctime") or 0) >= cut
                            for v in (store.get("items") or {}).values()))

    def test_merge_caches(self):
        m1, m2, mm = self._name("_m1"), self._name("_m2"), self._name("_mm")
        news.cls_history(days=2, max_pages=200, delay=0.0, until="2026-05-01",
                         cache_name=m1)
        news.cls_history(days=2, max_pages=200, delay=0.0, until="2026-04-01",
                         cache_name=m2)
        a = len((util.load_json(self._cache(m1), {"items": {}}).get("items") or {}))
        b = len((util.load_json(self._cache(m2), {"items": {}}).get("items") or {}))
        res = news.merge_history_caches([m1, m2], into=mm)
        self.assertEqual(res["after"], a + b)          # 两段不同日期，无重叠
        merged = util.load_json(self._cache(mm), {"items": {}})
        self.assertEqual(len(merged.get("items") or {}), a + b)


if __name__ == "__main__":
    unittest.main()
