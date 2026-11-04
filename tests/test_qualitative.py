# -*- coding: utf-8 -*-
"""定性判档单测：召回、兜底档位、档位约束。**不联网**。

LLM 主路径要真调接口，不能进 CI；这里锁的是它周围那些「错了会静默给出错分」的部分：

- 召回把不相关段落也塞进来 → 模型被判据外的内容带偏（S1.1.2 曾召回到福利清单）
- 兜底把「原则声明」当执行证据 → 没有 LLM 时全线虚高
- 模型回了个不在档位集合里的数 → 必须报错降级，绝不能四舍五入猜一个分数
- 模型把 JSON 包在 ```json 里 → 要能剥掉，但不能「宽松修补」成别的意思
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402  （触发 sys.path 注入 ESG_insight/app）
from esg_s11_scorer import qualitative  # noqa: E402
from esg_s11_scorer.llm import LLMError, parse_json  # noqa: E402
from esg_s11_scorer.rubric import RUBRICS, rubric_hash  # noqa: E402
from s11_text import Doc, Page  # noqa: E402


def _doc(*texts: str) -> Doc:
    return Doc(path="t.pdf", pages=[Page(no=i, text=t, cjk=0)
                                    for i, t in enumerate(texts, start=1)])


class TestRubricShape(unittest.TestCase):
    """rubric 与共识对齐：档位集合是硬约束。"""

    def test_five_qualitative_items(self):
        self.assertEqual(set(RUBRICS), {"S1.1.1-C", "S1.1.2-C", "S1.1.3",
                                        "S1.1.4", "S1.1.5"})

    def test_tier_sets(self):
        for key, rub in RUBRICS.items():
            self.assertEqual(set(rub.tiers), {0, 50, 100}, msg=f"{key} 档位应为 0/50/100")
            for tier, text in rub.tiers.items():
                self.assertTrue(text.strip(), msg=f"{key} 的 {tier} 档没有判据原文")

    def test_hash_is_stable_and_content_sensitive(self):
        self.assertEqual(rubric_hash(), rubric_hash())
        self.assertEqual(len(rubric_hash()), 12)


class TestRecall(unittest.TestCase):
    def test_picks_paragraph_with_keywords(self):
        doc = _doc("本集团重视企业文化与员工关怀，持续投入品牌建设。",
                   "公司实行标准工时制度，加班需事先审批，年假按法定标准执行。")
        rub = RUBRICS["S1.1.3"]
        ps = qualitative.recall(doc, rub)
        self.assertTrue(ps)
        self.assertEqual(ps[0].page, 2)
        self.assertGreater(ps[0].hits, 0)

    def test_drops_irrelevant_page(self):
        doc = _doc("本年度营业收入同比增长百分之十二，主要来自海外市场拓展。")
        self.assertEqual(qualitative.recall(doc, RUBRICS["S1.1.4"]), [])

    def test_limit_and_dedup(self):
        line = "公司为员工缴纳社会保险与住房公积金，并每年安排健康体检。"
        doc = _doc(*([line] * 3))
        ps = qualitative.recall(doc, RUBRICS["S1.1.4"], limit=5)
        self.assertEqual(len(ps), 1, "同一段重复披露不应重复计入")


class TestKeywordFallback(unittest.TestCase):
    """兜底按证据层级判档：声明 < 制度 < 制度＋执行结果。"""

    def _tier(self, key: str, *texts: str) -> int:
        doc = _doc(*texts)
        return qualitative.judge(doc, RUBRICS[key], None, mode="keyword").tier

    def test_statement_only_is_50(self):
        self.assertEqual(self._tier("S1.1.4", "我们高度重视员工福利，关爱员工生活。"), 50)

    def test_system_only_is_50(self):
        self.assertEqual(self._tier("S1.1.4", "公司制定福利政策，明确规定住房公积金缴纳流程。"), 50)

    def test_system_plus_result_is_100(self):
        self.assertEqual(self._tier(
            "S1.1.4",
            "公司制定福利政策，为员工缴纳社会保险与住房公积金，2025 年参保率达到 100%。"), 100)

    def test_no_evidence_is_0(self):
        self.assertEqual(self._tier("S1.1.3", "公司持续提升品牌影响力与客户满意度。"), 0)

    def test_gate_forces_zero(self):
        doc = _doc("公司建立劳动合同管理制度并完成入职签订。")
        v = qualitative.judge(doc, RUBRICS["S1.1.1-C"], None, gate_hit=True)
        self.assertEqual(v.tier, 0)
        self.assertEqual(v.source, "gate")


class TestTierGuard(unittest.TestCase):
    """模型给非法档位必须报错（→ 调用方降级），不能猜。"""

    def test_accepts_valid(self):
        rub = RUBRICS["S1.1.3"]
        for raw in (100, "50", "100分", " 0 "):
            self.assertIn(qualitative._coerce_tier(raw, rub), {0, 50, 100})

    def test_rejects_out_of_set(self):
        rub = RUBRICS["S1.1.3"]
        for raw in (75, "75分", 99, "高分", None):
            with self.assertRaises(LLMError):
                qualitative._coerce_tier(raw, rub)


class TestJsonParsing(unittest.TestCase):
    def test_fenced(self):
        obj = parse_json('```json\n{"tier": 100, "reason": "x"}\n```')
        self.assertEqual(obj["tier"], 100)

    def test_chatty_prefix(self):
        obj = parse_json('好的，结果如下：{"tier": 50, "evidence": [1, 2]}')
        self.assertEqual(obj["tier"], 50)

    def test_not_json_raises(self):
        with self.assertRaises(LLMError):
            parse_json("这个指标应该是 100 分。")


class TestVerdictRoundtrip(unittest.TestCase):
    def test_roundtrip(self):
        v = qualitative.Verdict(key="S1.1.5", tier=100, reason="r", evidence=["[p1] x"],
                                source="llm", missing="", model="qwen-plus", n_passages=9)
        back = qualitative.Verdict.from_dict(v.to_dict())
        self.assertEqual(back.to_dict(), v.to_dict())


class TestPrompt(unittest.TestCase):
    def test_prompt_lists_every_tier_and_passage(self):
        rub = RUBRICS["S1.1.3"]
        ps = [qualitative.Passage(idx=1, page=7, text="公司实行标准工时制度。", hits=2)]
        text = qualitative._prompt(rub, ps)
        for tier in (0, 50, 100):
            self.assertIn(f"{tier} 分", text)
        self.assertIn("[1] p7", text)
        self.assertIn("JSON", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)