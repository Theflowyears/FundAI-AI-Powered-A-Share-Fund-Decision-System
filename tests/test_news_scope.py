# -*- coding: utf-8 -*-
"""消息面来源口径（事件优先/词典降权/LLM 定级）与 LLM 解析的测试。"""
import json
import unittest
from unittest import mock

from fundai import llm_news, news, util


def _e(label="bull", strength=2, event="dict", **kw):
    d = {"auto_label": label, "auto_strength": strength, "event_type": event,
         "important": 0, "id": "x"}
    d.update(kw)
    return d


class NewsScopeTest(unittest.TestCase):
    def test_focus_only_excludes_dictionary(self):
        feed = [_e("bull", 2, "geo_conflict"), _e("bull", 6, "dict"),
                _e("bear", -6, "dict")]
        net, raw, n = news.net_score(feed, scale=100.0,
                                     focus=news.DEFAULT_FOCUS_EVENTS,
                                     dict_mode="off")
        self.assertEqual(n, 1)                  # 词典两条都被排除
        self.assertAlmostEqual(raw, 2.0)
        self.assertAlmostEqual(net, 0.02, places=4)

    def test_dict_important_only(self):
        feed = [_e("bull", 6, "dict", important=1), _e("bull", 6, "dict")]
        _, raw, n = news.net_score(feed, scale=100.0,
                                   focus=news.DEFAULT_FOCUS_EVENTS,
                                   dict_mode="important_only")
        self.assertEqual(n, 1)
        self.assertAlmostEqual(raw, 6.0)

    def test_llm_labeled_needs_contribute_flag(self):
        feed = [_e("bull", 6, "dict", llm_label=True), _e("bull", 6, "dict")]
        _, raw, n = news.net_score(feed, scale=100.0,
                                   focus=news.DEFAULT_FOCUS_EVENTS,
                                   dict_mode="off")
        self.assertEqual(n, 0)                  # 默认 LLM 只降噪，其方向不计入
        _, raw2, n2 = news.net_score([_e("bull", 6, "dict", llm_label=True,
                                         llm_contribute=True)],
                                     scale=100.0,
                                     focus=news.DEFAULT_FOCUS_EVENTS,
                                     dict_mode="off")
        self.assertEqual(n2, 1)
        self.assertAlmostEqual(raw2, 6.0)

    def test_llm_dropped_vetoes_focus_event(self):
        feed = [_e("bull", 2, "geo_conflict", llm_label=True, llm_dropped=True)]
        _, raw, n = news.net_score(feed, scale=100.0,
                                   focus=news.DEFAULT_FOCUS_EVENTS,
                                   dict_mode="off")
        self.assertEqual(n, 0)                  # LLM 判中性 → 一票否决

    def test_other_events_optional(self):
        feed = [_e("bear", -2, "earnings")]
        _, raw, n = news.net_score(feed, scale=100.0,
                                   focus=news.DEFAULT_FOCUS_EVENTS,
                                   dict_mode="off", include_other=False)
        self.assertEqual(n, 0)                  # 业绩默认不计入
        _, raw2, n2 = news.net_score(feed, scale=100.0,
                                     focus=news.DEFAULT_FOCUS_EVENTS,
                                     dict_mode="off", include_other=True)
        self.assertEqual(n2, 1)
        self.assertAlmostEqual(raw2, -2.0)

    def test_no_focus_keeps_old_behaviour(self):
        feed = [_e("bull", 6, "dict"), _e("bear", -6, "dict")]
        _, raw, n = news.net_score(feed, scale=100.0, focus=None)
        self.assertEqual(n, 2)
        self.assertAlmostEqual(raw, 0.0)

    def test_net_scope_helper(self):
        self.assertTrue(news.net_scope(_e("bull", 2, "cbank_ease"),
                                       news.DEFAULT_FOCUS_EVENTS))
        self.assertFalse(news.net_scope(_e("bull", 2, "dict"),
                                        news.DEFAULT_FOCUS_EVENTS))
        self.assertFalse(news.net_scope(_e("bull", 2, "dict", llm_label=True),
                                        news.DEFAULT_FOCUS_EVENTS))
        self.assertTrue(news.net_scope(_e("bull", 2, "dict", llm_label=True,
                                          llm_contribute=True),
                                       news.DEFAULT_FOCUS_EVENTS))
        self.assertFalse(news.net_scope(_e("bull", 2, "geo_conflict",
                                           llm_dropped=True),
                                        news.DEFAULT_FOCUS_EVENTS))


class LlmNewsParseTest(unittest.TestCase):
    def _call(self, content):
        fake = {"choices": [{"message": {"content": content}}]}
        with mock.patch.object(util, "http_post_json", return_value=fake):
            return llm_news.classify_batch(
                [{"id": "a", "title": "t", "text": "x", "time": "10:00"}],
                {"llm": {"enabled": True, "api_key": "k",
                         "base_url": "https://example.com/v1", "model": "m"}})

    def test_parses_bare_array(self):
        out = self._call(json.dumps([
            {"id": "a", "label": "big_bull", "confidence": 0.9, "reason": "降息"}]))
        self.assertEqual(out["a"]["label"], "big_bull")
        self.assertEqual(out["a"]["strength"], 6)

    def test_parses_wrapped_items(self):
        out = self._call(json.dumps({"items": [
            {"id": "a", "label": "neutral", "confidence": 0.5, "reason": "个股"}]}))
        self.assertEqual(out["a"]["strength"], 0)

    def test_rejects_unknown_label(self):
        out = self._call(json.dumps([{"id": "a", "label": "maybe"}]))
        self.assertEqual(out, {})

    def test_bad_json_returns_empty(self):
        self.assertEqual(self._call("not-json"), {})

    def test_label_strength_map(self):
        self.assertEqual(llm_news.LABEL_STRENGTH["big_bear"], -6)
        self.assertEqual(llm_news.LABEL_STRENGTH["bull"], 2)
        self.assertEqual(llm_news.LABEL_STRENGTH["irrelevant"], 0)

    def test_apply_labels_keeps_dict_judgement(self):
        feed = [_e("bull", 6, "dict", id="abc")]
        n = llm_news.apply_labels(feed, {"abc": {"label": "neutral", "strength": 0,
                                                 "confidence": 0.8, "reason": "个股"}})
        self.assertEqual(n, 1)
        self.assertEqual(feed[0]["auto_label"], "neutral")
        self.assertEqual(feed[0]["dict_label"], "bull")      # 原词典判断保留便于对照
        self.assertEqual(feed[0]["dict_strength"], 6)

    def test_no_llm_config_is_noop(self):
        with mock.patch.object(util, "http_post_json",
                               side_effect=AssertionError("不应调用")):
            self.assertEqual(llm_news.classify_batch([{"id": "a"}], {}), {})


if __name__ == "__main__":
    unittest.main()
