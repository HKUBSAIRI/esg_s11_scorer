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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402  （触发 sys.path 注入 ESG_insight/app）
from esg_s11_scorer import qualitative  # noqa: E402
from esg_s11_scorer.llm import LLM, LLMError, parse_json  # noqa: E402
from esg_s11_scorer.rubric import (BASELINE_EXCLUDED, RUBRICS,  # noqa: E402
                                   baseline_groups, rubric_hash)
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


class TestBaselineVocabulary(unittest.TestCase):
    """兜底词表冻结在 v1.4：召回词表可以继续扩，兜底基线不能跟着漂。

    这条是给「加词」上的保险——2026-11-09 对账 GT 时实测：往 S1.1.1 加 ``child labour``
    会让 02269/03692 的兜底档位从 50 跳到 100（人工举证判 50 / 15）。
    """

    V14_COUNTS = {"S1.1.1-C": 67, "S1.1.2-C": 66, "S1.1.3": 78,
                  "S1.1.4": 63, "S1.1.5": 76}

    def test_baseline_equals_v14(self):
        for key, want in self.V14_COUNTS.items():
            got = sum(len(ws) for _, ws in baseline_groups(RUBRICS[key]))
            self.assertEqual(got, want, msg=f"{key} 兜底词表应为 v1.4 的 {want} 个词")

    def test_every_excluded_word_exists_in_table(self):
        """排除名单不能写错字：每个词都得真的在当前词表里。"""
        for key, words in BASELINE_EXCLUDED.items():
            have = {w for _, ws in RUBRICS[key].groups for w in ws}
            self.assertTrue(words <= have, msg=f"{key} 排除了不存在的词：{sorted(words - have)}")

    def test_baseline_is_subset_of_full(self):
        for key, rub in RUBRICS.items():
            full = {w for _, ws in rub.groups for w in ws}
            self.assertTrue({w for _, ws in baseline_groups(rub) for w in ws} <= full)


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
                                source="llm", missing="", model="qwen-plus", n_passages=9,
                                basis="result")
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

    def test_prompt_lists_groups_and_path(self):
        rub = RUBRICS["S1.1.5"]
        text = qualitative._prompt(rub, [qualitative.Passage(1, 1, "x", 1)])
        for gid in ("D1", "D10"):
            self.assertIn(f"{gid} ", text)
        self.assertIn("【100 分路径】", text)
        self.assertIn("D7、D8、D9、D10", text)


class TestPathGuard(unittest.TestCase):
    """100 分路径复核：只降不升，降档必须标 review。"""

    def _judge(self, key: str, obj: dict):
        rub = RUBRICS[key]
        ps = [qualitative.Passage(idx=1, page=1, text="公司已实施相关工作。", hits=1)]
        with mock.patch.object(qualitative, "chat_json", return_value=obj):
            return qualitative.judge_llm(rub, ps, LLM(model="fake"))

    def test_full_path_keeps_100(self):
        v = self._judge("S1.1.4", {"tier": 100, "groups": [
            {"id": "W4", "hit": "yes"}, {"id": "W1", "hit": "yes"}]})
        self.assertEqual((v.tier, v.status), (100, "ok"))

    def test_100_without_required_group_downgrades(self):
        v = self._judge("S1.1.4", {"tier": 100, "groups": [
            {"id": "W4", "hit": "no"}, {"id": "W1", "hit": "yes"}]})
        self.assertEqual((v.tier, v.status), (50, "review"))
        self.assertIn("W4", v.missing_groups)

    def test_partial_cannot_satisfy_require_all(self):
        v = self._judge("S1.1.2-C", {"tier": 100, "groups": [
            {"id": "P1", "hit": "partial"}, {"id": "P2", "hit": "yes"},
            {"id": "P4", "hit": "yes"}]})
        self.assertEqual((v.tier, v.status), (50, "review"))

    def test_require_any_accepts_partial(self):
        v = self._judge("S1.1.2-C", {"tier": 100, "groups": [
            {"id": "P1", "hit": "yes"}, {"id": "P2", "hit": "yes"},
            {"id": "P6", "hit": "partial"}]})
        self.assertEqual((v.tier, v.status), (100, "ok"))

    def test_invalid_tier_derived_from_groups(self):
        v = self._judge("S1.1.2-C", {"tier": "待复核", "groups": [
            {"id": "P1", "hit": "yes"}, {"id": "P2", "hit": "yes"},
            {"id": "P3", "hit": "yes"}]})
        self.assertEqual((v.tier, v.status), (100, "review"))

    def test_event_needs_d7_to_d10(self):
        v = self._judge("S1.1.5", {"tier": 100, "event": True, "groups": [
            {"id": "D1", "hit": "yes"}, {"id": "D2", "hit": "yes"},
            {"id": "D5", "hit": "yes"}, {"id": "D7", "hit": "yes"},
            {"id": "D8", "hit": "yes"}]})
        self.assertEqual((v.tier, v.status), (50, "review"))
        self.assertIs(v.event, True)
        self.assertIn("D9", v.missing_groups)

    def test_no_event_skips_d8_to_d10(self):
        v = self._judge("S1.1.5", {"tier": 100, "event": False, "groups": [
            {"id": "D1", "hit": "yes"}, {"id": "D2", "hit": "yes"},
            {"id": "D5", "hit": "yes"}]})
        self.assertEqual((v.tier, v.status), (100, "ok"))

    def test_underclaimed_100_is_flagged_not_raised(self):
        v = self._judge("S1.1.4", {"tier": 50, "groups": [
            {"id": "W4", "hit": "yes"}, {"id": "W1", "hit": "yes"}]})
        self.assertEqual((v.tier, v.status), (50, "review"))
        self.assertIn("保留原档", v.reason)


class TestZeroFloor(unittest.TestCase):
    """细则的 0 分档只覆盖「未识别」与「已核实违规」；只有声明/制度时该落 50。"""

    def _judge(self, key_or_obj, obj: dict | None = None, key: str = "S1.1.2-C"):
        if isinstance(key_or_obj, dict):
            obj = key_or_obj
        else:
            key = key_or_obj
        rub = RUBRICS[key]
        ps = [qualitative.Passage(idx=1, page=1,
                                  text="The Group discloses its remuneration policy.", hits=1)]
        with mock.patch.object(qualitative, "chat_json", return_value=obj):
            return qualitative.judge_llm(rub, ps, LLM(model="fake"))

    def test_statement_basis_raises_zero_to_50(self):
        v = self._judge({"tier": 0, "basis": "statement", "groups": [
            {"id": "P1", "hit": "no"}, {"id": "P2", "hit": "no"}]})
        self.assertEqual((v.tier, v.status), (50, "review"))
        self.assertIn("50 分档", v.reason)
        self.assertEqual(v.basis, "statement")

    def test_none_basis_keeps_zero(self):
        v = self._judge({"tier": 0, "basis": "none", "groups": [
            {"id": "P1", "hit": "no"}, {"id": "P2", "hit": "no"}]})
        self.assertEqual(v.tier, 0)

    def test_violation_basis_keeps_zero(self):
        v = self._judge({"tier": 0, "basis": "violation", "reason": "存在经核实的欠薪且未纠正",
                         "groups": [{"id": "P1", "hit": "no"}, {"id": "P6", "hit": "no"}]})
        self.assertEqual(v.tier, 0)

    def test_unknown_basis_keeps_zero(self):
        v = self._judge({"tier": 0, "basis": "未识别",
                         "groups": [{"id": "P1", "hit": "no"}]})
        self.assertEqual((v.tier, v.basis), (0, ""))

    def test_single_int_evidence_is_tolerated(self):
        rub = RUBRICS["S1.1.2-C"]
        hits, ids = qualitative._parse_groups(
            {"groups": [{"id": "P1", "hit": "yes", "evidence": 3}]}, rub)
        self.assertEqual(hits, {"P1": "yes"})
        self.assertEqual(ids, [3])

    def test_single_int_top_level_evidence_is_tolerated(self):
        v = self._judge({"tier": 50, "evidence": 2,
                         "groups": [{"id": "P1", "hit": "yes"}]})
        self.assertEqual(v.tier, 50)
        self.assertTrue(v.evidence)

    def test_missing_groups_marks_review(self):
        v = self._judge("S1.1.4", {"tier": 50})
        self.assertEqual((v.tier, v.status), (50, "review"))
        self.assertIn("未经路径校验", v.reason)

    def test_model_review_flag_passthrough(self):
        v = self._judge("S1.1.4", {"tier": 50, "review": True,
                                   "groups": [{"id": "W4", "hit": "yes"}]})
        self.assertEqual(v.status, "review")

    def test_floor_blocks_zero_when_gate_passed(self):
        """S1.1.1：离职率已披露（gate 未触发）就不该再记「未识别相关内容」的 0。"""
        v = self._judge("S1.1.1-C", {"tier": 0, "groups": [
            {"id": g, "hit": "no"} for g in ("E0", "E1", "E2", "E3", "E4")]})
        self.assertEqual((v.tier, v.status), (50, "review"))
        self.assertIn("0 分档不适用", v.reason)

    def test_floor_does_not_leak_to_other_items(self):
        v = self._judge("S1.1.3", {"tier": 0, "groups": [
            {"id": "T1", "hit": "no"}]})
        self.assertEqual(v.tier, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

