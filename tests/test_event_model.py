# -*- coding: utf-8 -*-
"""事件模型（walk-forward / 选择性预测 / 指标）的纯函数测试。"""
import unittest

from fundai import event_model as em


def synth(n=120, strong=True):
    """合成数据：y 由 sig 决定（strong=True 时完全可分），另加纯噪声列。"""
    rows = []
    for i in range(n):
        x = (i % 7) / 6.0
        y = 1 if x > 0.5 else 0
        rows.append({"date": "d%03d" % i, "next": "n%03d" % i,
                     "ret": (0.01 if y else -0.01), "y": y, "n_items": 10,
                     "f": {"sig": x, "noise": ((i * 37) % 11) / 11.0}})
    return {"rows": rows, "dates": [], "closes": {},
            "features": ["sig", "noise"]}


class EventModelTest(unittest.TestCase):
    def test_walk_forward_learns_signal(self):
        ds = synth(160)
        preds = em.walk_forward(ds, warmup=30, refit_every=5, l2=0.1, iters=300)
        met = em.metrics(ds["rows"], preds, band=0.003, min_edge=0.02)
        self.assertGreater(met["n"], 120)          # 前 warmup 天不出预测
        self.assertEqual(met["n"], 130)
        self.assertGreaterEqual(met["acc"], 0.9)   # 强信号必须样本外学到
        self.assertEqual(met["coverage"], 1.0)

    def test_policy_uses_train_only_threshold(self):
        ds = synth(160)
        pol = em.policy_walk_forward(ds, warmup=30, l2=0.1, targets=(0.3,))
        t = pol["targets"]["0.3"]
        self.assertGreater(t["taken"], 5)
        self.assertGreaterEqual(t["acc"], 0.9)
        self.assertLessEqual(t["coverage"], 1.0)

    def test_metrics_deadband_and_brier(self):
        rows = [{"date": "a", "ret": 0.001, "y": 1},     # 噪声日（<0.3%）不计入命中
                {"date": "b", "ret": 0.02, "y": 1},
                {"date": "c", "ret": -0.02, "y": 0}]
        preds = [0.9, 0.9, 0.1]
        m = em.metrics(rows, preds, band=0.003, min_edge=0.02)
        self.assertEqual(m["n"], 3)
        self.assertEqual(m["movers"], 2)
        self.assertAlmostEqual(m["acc"], 1.0)
        self.assertAlmostEqual(m["brier"], 0.01)   # (0.1²+0.1²+0.1²)/3

    def test_metrics_ignores_warmup_none(self):
        m = em.metrics([{"date": "a", "ret": 0.01, "y": 1}], [None])
        self.assertEqual(m["n"], 0)

    def test_selection_needs_enough_data(self):
        rep = em.select_and_holdout(synth(40), warmup=30)
        self.assertFalse(rep["ok"])
        rep2 = em.select_and_holdout(synth(200), warmup=30)
        self.assertTrue(rep2["ok"])
        self.assertIn("l2", rep2["selected"])
        for k in ("acc", "baseline", "edge", "bt_nav", "bt_bh", "win"):
            self.assertIn(k, rep2["holdout"])
        self.assertGreater(rep2["holdout_period"]["days"], 10)

    def test_fit_logistic_separates(self):
        X = [[0.0], [1.0], [0.1], [0.9]]
        y = [0, 1, 0, 1]
        mean, std, w, b = em.fit_logistic(X, y, l2=0.01, iters=300)
        self.assertLess(em._predict(mean, std, w, b, [0.0]), 0.5)
        self.assertGreater(em._predict(mean, std, w, b, [1.0]), 0.5)


if __name__ == "__main__":
    unittest.main()
