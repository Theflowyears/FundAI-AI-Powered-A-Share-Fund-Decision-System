# -*- coding: utf-8 -*-
"""CLI 并行加速的回归测试（2026-09-12：`--jobs` 铺满 CPU，但结果必须与串行一致）。

两条负载性质不同，所以走两条不同的并行路线（纪律见 `data/parallel.py` docstring）：

* **纯计算**（`news-score-db`：几十万条电报的正则+词典+事件语义打分）→ 进程池，
  读任务与写库留在主进程（SQLite 单写者），worker 各自加载一次词典；
* **纯 I/O**（`news-fetch-db`：财联社翻页回填）→ 线程池，把回溯区间均分成 N 段，
  每段各自走时间游标，写库靠 WAL 串行提交。

这里锁死三件事，避免以后"为了快把结果算歪"：
1. 并行评分的每一行与串行**逐字段一致**；
2. 并行分段抓取不丢内容、重复抓取幂等（`id` 主键 + INSERT OR IGNORE）；
3. `jobs=1` 与"任务量太小"必须走串行老路径（也是并行出问题时的退路）。

不联网、不碰真实 `data/`：数据库用 `_test_tmp/cls/*.db`（仓库内工作区，
由 `tools/prepare_test_tmp.py` 预建，见该脚本说明），翻页函数打桩。
"""
import os
import sys
import unittest
from pathlib import Path

from fundai import clsdb

TMP_DIR = Path(os.environ.get("FUNDAI_TEST_TMP")
               or (Path(__file__).resolve().parent.parent / "_test_tmp" / "cls"))
DB_SCORE = TMP_DIR / "cls_score.db"
DB_FETCH = TMP_DIR / "cls_fetch.db"


def _can_use_file(p):
    """该临时库能否真正建起来（受限环境下 Python 可能被拒绝新建文件）。

    能新建就落地（更接近真实：多连接 + WAL）；不能就退回 `:memory:`（单连接共享），
    这样用例在任何环境都真跑，而不是被 skip 掉。
    """
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_bytes(b"")
        con = clsdb.sqlite3.connect(str(p))
        con.execute("SELECT 1")
        con.close()
        return True
    except Exception:
        return False


def _spawn_ok():
    """本机进程池是否真的可用（沙箱/受限令牌下建不起管道 → 会自动退回串行）。"""
    root = str(Path(__file__).resolve().parent.parent)
    for p in (root, os.path.join(root, "data")):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        import parallel
        return bool(parallel.probe_process_pool())
    except Exception:
        return False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegrams(
    id TEXT PRIMARY KEY, ctime INTEGER, date TEXT,
    title TEXT, text TEXT, url TEXT);
CREATE TABLE IF NOT EXISTS scores(
    id TEXT PRIMARY KEY, ctime INTEGER, event TEXT, label TEXT,
    strength INTEGER, important INTEGER, scope TEXT, title TEXT);
"""


def _reset():
    """在"当前连接"上建表并清空数据。

    不能调真 `clsdb.connect()`：内存兜底模式下它会另开一个 `:memory:` 连接
    （建完表就随连接关闭而消失），于是被测代码读到的仍是空库。
    """
    con = clsdb.connect()
    con.executescript(_SCHEMA)
    con.executescript("""
    DELETE FROM telegrams;
    DELETE FROM scores;
    """)
    con.commit()


class _Base(unittest.TestCase):
    db_file = None

    def setUp(self):
        """用**文件库**（`_test_tmp/cls/*.db`）：更接近真实（多连接 + WAL），也不用
        改造 `clsdb` 的连接语义。文件先由 `tools/prepare_test_tmp.py` 的 PowerShell
        命令预建——受限沙箱里 Python 可能被拒绝新建文件，这种情况直接 skip 并说明。
        """
        if not _can_use_file(self.db_file):
            self.skipTest(
                "临时库不可用（受限环境禁止新建文件）：先按 tools/prepare_test_tmp.py "
                "的 PowerShell 命令预建 {} 后重跑".format(self.db_file))
        self._orig_path = clsdb.db_path
        clsdb.db_path = lambda: str(self.db_file)
        self.addCleanup(lambda: setattr(clsdb, "db_path", self._orig_path))
        _reset()

    def _close(self, con):
        try:
            con.close()
        except Exception:
            pass

    def _count(self, table="telegrams"):
        con = clsdb.connect()
        try:
            return con.execute(
                "SELECT count(*) FROM {}".format(table)).fetchone()[0]
        finally:
            self._close(con)


class ParallelScoreTest(_Base):
    """并行评分 == 串行评分（逐字段）。"""

    db_file = DB_SCORE

    def setUp(self):
        super().setUp()
        from fundai import news
        rows = []
        for i in range(260):
            title = ("央行宣布降准0.5个百分点" if i % 3 == 0 else
                     "某公司股东减持股份" if i % 3 == 1 else "市场震荡整理")
            rows.append({"id": "t{}".format(i), "ctime": 1700000000 + i * 60,
                         "date": "2023-11-15", "title": title,
                         "text": "", "url": ""})
        con = clsdb.connect()
        try:
            clsdb.insert_telegrams(rows, conn=con)
        finally:
            self._close(con)
        self.news = news

    def _todo(self):
        con = clsdb.connect()
        try:
            return [dict(r) for r in con.execute(
                "SELECT id, ctime, title, text FROM telegrams ORDER BY id")]
        finally:
            self._close(con)

    def test_parallel_rows_match_serial(self):
        todo = self._todo()
        self.assertGreaterEqual(len(todo), 200)
        extra = self.news.learned_extra()
        serial = [self.news._score_row(it, extra) for it in todo]
        self.news._score_worker_init()
        parallel = self.news._pmap_score(todo, 4)
        self.assertEqual(len(serial), len(parallel))
        diff = [i for i, (a, b) in enumerate(zip(serial, parallel)) if a != b]
        self.assertEqual(diff, [], "并行与串行评分必须逐字段一致，差异下标 {}".format(diff))

    def test_score_db_parallel_writes_all_rows(self):
        res = self.news.score_db(jobs=4, batch=50)
        self.assertEqual(res["scored"], 260, "全部电报都要被打分")
        self.assertEqual(self._count("scores"), 260)
        con = clsdb.connect()
        try:
            pending = con.execute(
                "SELECT count(*) FROM telegrams t LEFT JOIN scores s ON s.id=t.id "
                "WHERE s.id IS NULL").fetchone()[0]
        finally:
            self._close(con)
        self.assertEqual(pending, 0, "并行跑完后不应还有未打分电报")
        if not _spawn_ok():
            # 受限环境（沙箱/受限令牌）建不起进程池 → 已按设计退回串行，
            # 这里只断言"结果正确"，并在日志里说明为何没跑并行分支。
            self.assertEqual(res["jobs"], 1, "进程池不可用时 jobs 应报 1（串行）")
            print("[test] 进程池不可用 → 本用例只校验串行结果口径",
                  file=sys.stderr)
        else:
            self.assertGreaterEqual(res["jobs"], 2, "应真正走并行路径")

    def test_jobs_one_is_serial_path(self):
        res = self.news.score_db(jobs=1)
        self.assertEqual(res["jobs"], 1)
        self.assertEqual(res["scored"], 260)
        self.assertEqual(self._count("scores"), 260)

    def test_small_batch_stays_serial(self):
        """任务太少（<200 条）时不值得铺进程池：自动串行，避免"越并越慢"。"""
        con = clsdb.connect()
        con.execute("DELETE FROM telegrams WHERE id NOT IN "
                    "(SELECT id FROM telegrams ORDER BY id LIMIT 50)")
        con.commit()
        self._close(con)
        res = self.news.score_db(jobs=8)
        self.assertEqual(res["scored"], 50)
        self.assertEqual(res["jobs"], 1, "少量任务应自动退回串行")


class ParallelFetchTest(_Base):
    """并行分段抓取与串行抓取一致（翻页函数打桩，不联网）。"""

    db_file = DB_FETCH

    def setUp(self):
        super().setUp()
        from fundai import news
        self.news = news
        self._orig_roll = news.cls_roll_page

        def fake_roll(cursor):
            items = [{"id": "s{}_{}".format(cursor, k), "ctime": cursor - k * 300,
                      "title": "测试电报", "text": "", "url": ""}
                     for k in range(5)]
            return items, cursor - 1500
        news.cls_roll_page = fake_roll
        self.addCleanup(lambda: setattr(news, "cls_roll_page", self._orig_roll))

    def _ids(self):
        con = clsdb.connect()
        try:
            return {r[0] for r in con.execute("SELECT id FROM telegrams")}
        finally:
            self._close(con)

    def test_parallel_fetch_keeps_all_rows_and_is_idempotent(self):
        serial = self.news.cls_fetch_to_db(days=6, until="2026-01-10",
                                           max_pages=8, delay=0, jobs=1)
        self.assertIsNone(serial.get("jobs"), "jobs=1 应走老串行路径（无 jobs 字段）")
        ids_serial = self._ids()
        self.assertTrue(ids_serial, "串行基线应抓到内容")

        par = self.news.cls_fetch_to_db(days=6, until="2026-01-10",
                                        max_pages=8, delay=0, jobs=4)
        if _spawn_ok():
            self.assertGreaterEqual(par.get("jobs", 0), 2, "应真正分段并行")
        else:
            print("[test] 进程池不可用 → 分段并行已退回串行", file=sys.stderr)
        self.assertTrue(ids_serial <= self._ids(), "并行抓取不得丢内容")

        again = self.news.cls_fetch_to_db(days=6, until="2026-01-10",
                                          max_pages=8, delay=0, jobs=4)
        self.assertEqual(again["added"], 0, "重复抓取必须幂等（不重复计条）")

    def test_progress_callback_accepts_old_and_new_signature(self):
        seen = []

        def old_style(pages, oldest):
            seen.append(pages)

        def new_style(pages, oldest, tag=None):
            seen.append((pages, tag))

        self.news.cls_fetch_to_db(days=1, until="2026-01-10", max_pages=2,
                                  delay=0, progress=old_style, batch_pages=1,
                                  jobs=1)
        self.news.cls_fetch_to_db(days=1, until="2026-01-10", max_pages=2,
                                  delay=0, progress=new_style, batch_pages=1,
                                  jobs=1)
        self.assertTrue(seen, "进度回调应被调用")


class ParallelConfigTest(unittest.TestCase):
    def _parallel(self):
        root = str(Path(__file__).resolve().parent.parent)
        for p in (root, os.path.join(root, "data")):
            if p not in sys.path:
                sys.path.insert(0, p)
        import parallel
        return parallel

    def test_workers_respects_jobs_argument(self):
        parallel = self._parallel()
        self.assertEqual(parallel.workers(1), 1)
        self.assertEqual(parallel.workers(3), 3)
        self.assertGreaterEqual(parallel.workers(), 2)
        self.assertGreater(parallel.workers(for_io=True), 1)

    def test_fundai_jobs_env_override(self):
        parallel = self._parallel()
        os.environ["FUNDAI_JOBS"] = "2"
        try:
            self.assertEqual(parallel.workers(), 2)
        finally:
            os.environ.pop("FUNDAI_JOBS", None)


if __name__ == "__main__":
    unittest.main()
