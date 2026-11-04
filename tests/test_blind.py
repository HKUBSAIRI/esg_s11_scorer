# -*- coding: utf-8 -*-
"""盲评脱敏单测：**送进模型的文本里不能有公司名**。

这条要求不是"尽量"，是硬约束：
- 别名替换要彻底（``WuXi Biologics`` 不能剩下 ``[COMPANY]logics``，也不能漏成 ``WuXi``）；
- 替换不能误伤正文（``develop biologics`` 里的 biologics 不是公司名，
  ``生物制药行业`` 不是公司名，``the Group`` 也不是）；
- 提示词组装完要扫一遍残留，有残留就**不发送**（fail closed），退回关键词兜底；
- 证据（写进报告的那份）保留原文，方便人工回查——脱敏只发生在"给模型看"的那一份。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from esg_s11_scorer import blind, qualitative  # noqa: E402
from esg_s11_scorer.llm import LLM, LLMError  # noqa: E402
from esg_s11_scorer.rubric import RUBRICS  # noqa: E402
from s11_text import Doc, Page  # noqa: E402

WUXI = "02269_WUXI_BIO_ESG_FY2025.pdf"


def _blinder() -> blind.Blinder:
    return blind.build("02269", filename=WUXI,
                       extra={"02269": ("药明生物", "WuXiBody")})


def _doc(*texts: str) -> Doc:
    return Doc(path="t.pdf", pages=[Page(no=i, text=t, cjk=0)
                                    for i, t in enumerate(texts, start=1)])


class TestAliasSources(unittest.TestCase):
    def test_from_filename(self):
        a = blind.aliases_from_filename(WUXI)
        self.assertIn("WUXI BIO", a)
        self.assertIn("WUXI", a)
        self.assertIn("02269", a)
        self.assertNotIn("BIO", a, "BIO 会打掉 biologics/biosimilar，不能当别名")

    def test_from_other_filenames(self):
        for fn, want in (("01513_LIVZON_PHARMA_ESG_FY2025.pdf", "LIVZON"),
                         ("03692_HANSOH_PHARMA_ESG_FY2025.pdf", "HANSOH"),
                         ("01801_INNOVENT_BIO_ESG_FY2025.pdf", "INNOVENT")):
            self.assertIn(want, blind.aliases_from_filename(fn), msg=fn)

    def test_alias_csv(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.csv"
            p.write_text("code,alias\n02269,药明生物\n02269,WuXiBody\n", encoding="utf-8")
            got = blind.load_alias_csv(p)
        self.assertEqual(set(got["02269"]), {"药明生物", "WuXiBody"})

    def test_missing_csv_is_empty(self):
        self.assertEqual(blind.load_alias_csv(None), {})
        self.assertEqual(blind.load_alias_csv("不存在.csv"), {})


class TestScrub(unittest.TestCase):
    def test_removes_full_brand_without_residue(self):
        text, n = _blinder().scrub("At WuXi Biologics, we are committed to quality.")
        self.assertEqual(text, "At [COMPANY], we are committed to quality.")
        self.assertEqual(n, 1)

    def test_case_and_spacing_tolerant(self):
        text, _ = _blinder().scrub("WUXI   BIO 2025 sustainability report")
        self.assertNotIn("WUXI", text)
        self.assertNotIn("BIO", text.replace("[COMPANY]", ""))

    def test_chinese_and_product_alias(self):
        text, n = _blinder().scrub("药明生物依托 WuXiBody 平台开展研发。")
        self.assertNotIn("药明生物", text)
        self.assertNotIn("WuXiBody", text)
        self.assertGreaterEqual(n, 2)

    def test_does_not_eat_generic_words(self):
        for s in ("We develop biologics and biosimilars for global clients.",
                  "生物制药行业整体保持增长。",
                  "The Group's Board reviewed the policy.",
                  "Our Company has a zero-tolerance policy."):
            out, n = _blinder().scrub(s)
            self.assertEqual((out, n), (s, 0), msg=f"误伤正文：{s} → {out}")

    def test_stock_code(self):
        text, _ = _blinder().scrub("Stock Code: 02269 (HKEX)")
        self.assertNotIn("02269", text)

    def test_generic_net_catches_unlisted_company(self):
        text, n = _blinder().scrub("Fosun Pharma announced a new plant.")
        self.assertNotIn("Fosun", text)
        self.assertEqual(text, "[COMPANY] announced a new plant.")
        self.assertEqual(n, 1)

    def test_leaks_reports_residue(self):
        b = blind.Blinder(aliases=("ACME",), generic=False)
        self.assertEqual(b.leaks("ACME Corp"), ["ACME"])
        self.assertEqual(b.leaks("nothing here"), [])


class TestPromptIsBlind(unittest.TestCase):
    """端到端：真正发出去的提示词里没有公司名。"""

    def _judge_with_capture(self):
        captured: dict = {}

        def fake_chat(llm, system, user, **kw):
            captured["system"] = system
            captured["user"] = user
            return {"tier": 100, "reason": "有制度与执行证据", "evidence": [1]}

        orig = qualitative.chat_json
        qualitative.chat_json = fake_chat
        try:
            doc = _doc("At WuXi Biologics we maintain a working time management system "
                       "and monitor overtime; in 2025 leave utilisation reached 78%.")
            v = qualitative.judge(doc, RUBRICS["S1.1.3"],
                                  LLM(base_url="http://x", model="m", api_key="k"),
                                  blinder=_blinder())
        finally:
            qualitative.chat_json = orig
        return v, captured

    def test_prompt_has_no_company_name(self):
        v, cap = self._judge_with_capture()
        prompt = cap["user"] + cap["system"]
        for bad in ("WuXi", "WUXI", "02269", "药明"):
            self.assertNotIn(bad, prompt, msg=f"提示词里残留公司名：{bad}")
        self.assertIn("[COMPANY]", cap["user"])

    def test_verdict_records_redaction_and_keeps_original_evidence(self):
        v, _ = self._judge_with_capture()
        self.assertEqual(v.tier, 100)
        self.assertGreater(v.alias_hits, 0, "应记录脱敏次数，供审计")
        self.assertTrue(any("WuXi" in e for e in v.evidence),
                        "证据应保留原文，便于回查")

    def test_fail_closed_when_residue_left(self):
        """脱敏器失效（残留公司名）时必须不发送、退回关键词兜底。"""

        class BrokenBlind(blind.Blinder):
            def scrub(self, text):
                return text, 0                      # 故意不脱敏

        called = {"n": 0}

        def fake_chat(*a, **kw):
            called["n"] += 1
            return {"tier": 100}

        orig = qualitative.chat_json
        qualitative.chat_json = fake_chat
        try:
            doc = _doc("WuXi Biologics monitors working hours and annual leave.")
            v = qualitative.judge(doc, RUBRICS["S1.1.3"],
                                  LLM(base_url="http://x", model="m", api_key="k"),
                                  blinder=BrokenBlind(aliases=("WUXI BIO",)))
        finally:
            qualitative.chat_json = orig
        self.assertEqual(called["n"], 0, "带公司名的文本绝不能发出去")
        self.assertEqual(v.source, "keyword")
        self.assertIn("降级", v.reason)

    def test_judge_llm_raises_before_sending(self):
        class BrokenBlind(blind.Blinder):
            def scrub(self, text):
                return text, 0

        ps = [qualitative.Passage(idx=1, page=1, text="WuXi Biologics does X.")]
        with self.assertRaises(LLMError):
            qualitative.judge_llm(RUBRICS["S1.1.3"], ps,
                                  LLM(base_url="http://x", model="m", api_key="k"),
                                  BrokenBlind(aliases=("WUXI BIO",), generic=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)