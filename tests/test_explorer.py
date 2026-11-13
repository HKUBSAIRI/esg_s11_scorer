# -*- coding: utf-8 -*-
"""段落浏览器（``explorer``）的纯函数：高亮区间与质量文案。

不发 HTTP、不读 PDF：这里只锁「合并区间不重叠」「英文词首/中文子串」「英文报告不报中文层告警」
这三件容易悄悄改坏的事。真正的解析链路由 ``test_matching`` / ``test_qualitative`` 覆盖。
"""
from __future__ import annotations

import unittest

from esg_s11_scorer import explorer
from esg_s11_scorer.qualitative import Passage
from esg_s11_scorer.rubric import RUBRICS


class TestHighlight(unittest.TestCase):
    def test_merge_keeps_spans_ordered_and_disjoint(self):
        text = "Our employee benefits include a benefit plan."
        spans = explorer._highlight(text, ["employee benefit", "benefit"])
        self.assertEqual(spans[0][0], text.index("employee benefit"))
        self.assertEqual(text[spans[0][0]:spans[0][1]], "employee benefit")
        for prev, cur in zip(spans, spans[1:]):
            self.assertLess(prev[1], cur[0], "区间必须不重叠，前端直接包 <mark>")

    def test_english_matches_at_word_head_only(self):
        text = "wage bill and sewage treatment"
        spans = explorer._highlight(text, ["wage"])
        self.assertEqual(len(spans), 1)
        self.assertEqual(text[spans[0][0]:spans[0][1]], "wage")

    def test_chinese_matches_substring(self):
        text = "公司实行标准工时制并记录加班"
        spans = explorer._highlight(text, ["工时"])
        self.assertEqual([text[s:e] for s, e, _ in spans], ["工时"])

    def test_no_words_no_spans(self):
        self.assertEqual(explorer._highlight("任何文本", []), [])


class TestIndicator(unittest.TestCase):
    def test_passage_is_kept_with_page_words_and_spans(self):
        rub = RUBRICS["S1.1.4"]
        word = rub.groups[0][1][0]
        text = f"公司就{word}作出安排。"
        data = explorer._indicator(rub, [Passage(idx=1, page=7, text=text)])
        self.assertEqual(data["key"], "S1.1.4")
        self.assertEqual(data["n_passages"], 1)
        row = data["passages"][0]
        self.assertEqual(row["page"], 7)
        self.assertGreaterEqual(row["hits"], 1)
        self.assertIn(word, row["words"])
        self.assertTrue(row["spans"])
        self.assertEqual(row["spans"][0][2], word)

    def test_passage_without_keyword_is_dropped(self):
        data = explorer._indicator(RUBRICS["S1.1.4"], [Passage(idx=1, page=1, text="zzz")])
        self.assertEqual(data["n_passages"], 0)


class _FakeDoc:
    def __init__(self, quality: dict):
        self._q = quality

    def quality(self) -> dict:
        return dict(self._q)


class TestQualityCopy(unittest.TestCase):
    def test_english_report_drops_chinese_layer_warning(self):
        q = explorer._quality_for_view(_FakeDoc({
            "chars": 300000, "cjk": 2, "cjk_ratio": 0.0, "level": "中",
            "reasons": ["中文层不可用，建议走英文层判分"],
        }))
        self.assertEqual(q["lang"], "英文")
        self.assertEqual(q["level"], "好")
        self.assertEqual(q["reasons"], [])

    def test_chinese_report_keeps_its_reasons(self):
        q = explorer._quality_for_view(_FakeDoc({
            "chars": 300000, "cjk": 120000, "cjk_ratio": 0.4, "level": "中",
            "reasons": ["3 页几乎无可提取文本，需图像复核"],
        }))
        self.assertEqual(q["lang"], "中英混排")
        self.assertEqual(q["level"], "中")
        self.assertEqual(len(q["reasons"]), 1)


if __name__ == "__main__":
    unittest.main()