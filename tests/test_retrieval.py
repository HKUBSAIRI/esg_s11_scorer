# -*- coding: utf-8 -*-
"""混合检索单测：组配额、语义候选与失败回退，不联网。"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from esg_s11_scorer import retrieval  # noqa: E402
from esg_s11_scorer.embedding import EmbeddingError  # noqa: E402
from esg_s11_scorer.llm import LLM  # noqa: E402
from esg_s11_scorer.rubric import RUBRICS  # noqa: E402
from s11_text import Doc, Page  # noqa: E402


def _doc(*texts: str) -> Doc:
    return Doc(path="t.pdf", pages=[
        Page(no=i, text=text, cjk=0) for i, text in enumerate(texts, start=1)
    ])


class _FakeEmbedding:
    model = "fake-embedding"

    @property
    def ready(self) -> bool:
        return True

    def fingerprint(self) -> str:
        return "fakefingerpr"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            group = re.search(r"(?:证据组：)?([WEPTD]\d+)", text)
            if group:
                index = int(group.group(1)[1:]) % 10
            elif "社会保险" in text or "住房公积金" in text:
                index = 1
            elif "补充医疗" in text:
                index = 2
            elif "家庭支持" in text:
                index = 3
            elif "福利申请" in text or "取得流程" in text:
                index = 4
            else:
                index = 9
            vector = [0.0] * 10
            vector[index] = 1.0
            out.append(vector)
        return out


class _BrokenEmbedding(_FakeEmbedding):
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("offline")


class TestHybridRetrieval(unittest.TestCase):
    def test_keeps_group_quota_and_semantic_only_candidate(self):
        doc = _doc(
            "公司为员工缴纳社会保险和住房公积金。",
            "公司提供补充医疗保险及年度健康体检。",
            "公司提供家庭支持假与育儿支持。",
            "公司制定福利申请、审批和取得流程。",
            "本页只介绍董事会治理架构。",
        )
        passages, stats = retrieval.retrieve_hybrid_with_stats(
            doc,
            RUBRICS["S1.1.4"],
            _FakeEmbedding(),
            config=retrieval.RetrievalConfig(top_k=6, group_quota=1),
        )
        groups = {passage.group for passage in passages}
        self.assertTrue({"W1", "W2", "W3", "W4"} <= groups)
        self.assertEqual(stats["chunks"], 5)
        self.assertIn("W4", stats["selected_groups"])

    def test_embedding_failure_falls_back_to_keyword(self):
        doc = _doc("公司为员工缴纳社会保险和住房公积金。")
        llm = LLM(base_url="https://example.test/v1", model="fake", api_key="k")
        verdict = retrieval.judge_hybrid(
            doc, RUBRICS["S1.1.4"], llm, _BrokenEmbedding())
        self.assertEqual(verdict.source, "keyword")
        self.assertIn("混合检索降级", verdict.reason)

    def test_keyword_only_extra_group_is_kept_without_score_row(self):
        doc = _doc("The company supports collective wage negotiation for all employees.")
        passages = retrieval.retrieve_hybrid(
            doc,
            RUBRICS["S1.1.2-C"],
            _FakeEmbedding(),
            config=retrieval.RetrievalConfig(top_k=6, group_quota=1),
        )
        self.assertEqual([passage.page for passage in passages], [1])


if __name__ == "__main__":
    unittest.main()
