# -*- coding: utf-8 -*-
"""匹配规则测试：英文卡词首、中文走子串。

这张表是「召回为什么会这样」的说明书。左列是真实踩过的坑（十份报告实测）：
``diversity`` 命中 ``biodiversity`` 92 段、``wage`` 命中 ``sewage`` 22 段、
``EAP`` 命中 ``cheaper``/``leapfrog`` 16 段——都不是「词太泛」，是匹配方式错了。
右半边守的是反面：别把单复数一起挡掉（``employee benefits`` 必须命中 ``employee benefit``）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from esg_s11_scorer.qualitative import _hits, _match  # noqa: E402


class TestHeadBoundary(unittest.TestCase):
    """英文词只从词首命中。"""

    BLOCKED = [
        ("wage", "The domestic sewage is treated on site."),
        ("EAP", "The drug is cheaper than alternatives."),
        ("EAP", "WuXi made a leapfrog breakthrough."),
        ("diversity", "biodiversity protection measures"),
        ("termination", "determination principles of inspection"),
        ("employment", "unemployment insurance is mandatory"),
        ("pension", "forced suspension of operations"),
        ("severance", "a spirit of perseverance and progress"),
        ("disciplinary", "interdisciplinary collaboration"),
        ("voluntary turnover", "involuntary turnover rate was 3%"),
    ]

    KEPT = [
        ("wage", "The minimum wage is reviewed annually."),
        ("wage", "We pay wages on the 5th."),
        ("EAP", "Our EAP covers counselling."),
        ("diversity", "gender diversity on the board"),
        ("termination", "termination of employment contracts"),
        ("employment", "employment contract signed by all"),
        ("pension", "pension scheme contributions"),
        ("statutory holiday", "statutory holidays including New Year"),
        ("rest day", "rest days and public holidays"),
        ("employee benefit", "employee benefits and remuneration"),
        ("overtime", "overtime approval is required"),
    ]

    def test_blocked(self):
        for kw, text in self.BLOCKED:
            self.assertFalse(_match(text.lower(), kw), f"{kw!r} 不该命中：{text}")

    def test_kept(self):
        for kw, text in self.KEPT:
            self.assertTrue(_match(text.lower(), kw), f"{kw!r} 应该命中：{text}")


class TestChineseSubstring(unittest.TestCase):
    def test_chinese_is_plain_substring(self):
        self.assertTrue(_match("公司实行标准工时制", "工时"))
        self.assertTrue(_match("员工享有带薪年休假", "年休假"))
        self.assertFalse(_match("工時記錄", "工时"))      # 简繁不同字，靠繁体词条兜

    def test_hits_counts_distinct_words(self):
        low = "公司实行标准工时制，加班需审批，年假按法定标准执行。"
        n = _hits(low, ("工时", "加班", "年假", "overtime"))
        self.assertEqual(n, 3)


class TestNoisepenalty(unittest.TestCase):
    """套话与索引行命中多但要降到后面：它们能一句话塞进 4–5 个关键词。"""

    BOILER = ("and (b) compliance with relevant laws and regulations that have a significant impact "
              "on the issuer relating to compensation and dismissal, recruitment and promotion, "
              "working hours, rest periods, equal opportunity, diversity, anti-discrimination")
    INDEX = ("GRI 403-2 2024 2023 2022 rest days, statutory holidays, annual leave, "
             "working hours, overtime, sick leave")
    REAL = "We conducted diversity and inclusion training for 100% of new hires during the year."

    def _doc(self, *texts):
        from s11_text import Doc, Page
        return Doc(path="t.pdf", pages=[Page(no=i, text=t, cjk=0)
                                        for i, t in enumerate(texts, start=1)])

    def test_real_evidence_outranks_boilerplate(self):
        from esg_s11_scorer.qualitative import recall
        from esg_s11_scorer.rubric import RUBRICS
        ps = recall(self._doc(self.BOILER, self.INDEX, self.REAL), RUBRICS["S1.1.5"])
        self.assertTrue(ps)
        self.assertEqual(ps[0].text, self.REAL, "真证据应排在套话/索引之前")

    def test_index_line_does_not_take_first_slot(self):
        from esg_s11_scorer.qualitative import recall
        from esg_s11_scorer.rubric import RUBRICS
        ps = recall(self._doc(self.INDEX, self.REAL), RUBRICS["S1.1.5"])
        self.assertEqual(ps[0].text, self.REAL)


class TestGroupAwareOrder(unittest.TestCase):
    """召回先按「计分组命中数」排序：不在 specs 里的组（T7 人权）不能靠词多霸榜。"""

    HR = "We respect human rights and 人权 obligations across the group."
    LEAVE = "员工依法享有年假。"

    def _doc(self, *texts):
        from s11_text import Doc, Page
        return Doc(path="t.pdf", pages=[Page(no=i, text=t, cjk=0)
                                        for i, t in enumerate(texts, start=1)])

    def test_llm_path_drops_unscored_group(self):
        from esg_s11_scorer.qualitative import recall
        from esg_s11_scorer.rubric import RUBRICS
        ps = recall(self._doc(self.HR, self.LEAVE), RUBRICS["S1.1.3"])
        self.assertEqual([p.text for p in ps], [self.LEAVE])

    def test_keyword_baseline_keeps_aux_words(self):
        from esg_s11_scorer.qualitative import recall
        from esg_s11_scorer.rubric import RUBRICS
        ps = recall(self._doc(self.HR, self.LEAVE), RUBRICS["S1.1.3"],
                    llm_path=False)
        self.assertEqual(ps[0].text, self.HR)

    def test_unscored_group_skipped_from_recall(self):
        from esg_s11_scorer.qualitative import recall
        from esg_s11_scorer.rubric import RUBRICS
        self.assertFalse(any(g.startswith("T7")
                             for g, _ in RUBRICS["S1.1.3"].recall_groups))
        self.assertFalse(any(g.startswith("D0")
                             for g, _ in RUBRICS["S1.1.5"].recall_groups))
        ps = recall(self._doc(self.HR), RUBRICS["S1.1.3"])
        self.assertEqual(ps, [], "只有人权词的段落不该进 LLM 召回")
        ps = recall(self._doc(self.HR), RUBRICS["S1.1.3"], llm_path=False)
        self.assertEqual(len(ps), 1, "keyword 基线仍要看到 T7 词")


class TestRecallExtra(unittest.TestCase):
    """S1.1.2 的通用薪酬词只进 LLM 召回，不能动 keyword 兜底的 350 词基线。"""

    PAY = "The Group negotiated collective wage agreements with unions in 2025."

    def _doc(self):
        from s11_text import Doc, Page
        return Doc(path="t.pdf", pages=[Page(no=1, text=self.PAY, cjk=0)])

    def test_extra_words_reach_llm_recall(self):
        from esg_s11_scorer.qualitative import recall
        from esg_s11_scorer.rubric import RUBRICS
        self.assertEqual(len(recall(self._doc(), RUBRICS["S1.1.2-C"])), 1)

    def test_keyword_baseline_unchanged(self):
        from esg_s11_scorer.qualitative import recall
        from esg_s11_scorer.rubric import RUBRICS
        self.assertEqual(recall(self._doc(), RUBRICS["S1.1.2-C"], llm_path=False), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
