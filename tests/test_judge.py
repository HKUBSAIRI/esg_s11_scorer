# -*- coding: utf-8 -*-
"""判定增强层测试：7 条补丁各自必须真的生效，且不许污染传入的判定对象。

直接运行本文件（``python tests/test_judge.py S1.1.2-5``）会打印指定要素的命中句，
用于人工校准补丁规则；被 ``unittest`` 发现时只跑断言，不打印。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401
from esg_s11_scorer.config import REPORT_DIR
from esg_s11_scorer.judge_plus import adjudicate
from s11_judge import judge_lexical
from s11_text import load_pdf

DOCS = {}


def _doc(name: str):
    if name not in DOCS:
        DOCS[name] = load_pdf(str(REPORT_DIR / "FY2025" / name))
    return DOCS[name]


class TestPatches(unittest.TestCase):
    def test_p1_pay_audit_reversed(self):
        """药明：conduct audits of our gender pay gap（倒序）→ 必须判是。"""
        res, patches = adjudicate(_doc("02269_WUXI_BIO_ESG_FY2025.pdf"))
        self.assertEqual(res["S1.1.2-5"].verdict, "是")
        self.assertIn("S1.1.2-5", {p.eid for p in patches})

    def test_p2_contract_false_positive(self):
        """药明：无「劳动合同」表述 → 由「是」降为「部分」。"""
        res, _ = adjudicate(_doc("02269_WUXI_BIO_ESG_FY2025.pdf"))
        self.assertEqual(res["S1.1.1-4"].verdict, "部分")

    def test_p5_case_statistic(self):
        """翰森：KPI 表「Cases 0」是统计结果 → 判是。"""
        res, _ = adjudicate(_doc("03692_HANSOH_PHARMA_ESG_FY2025.pdf"))
        self.assertEqual(res["S1.1.5-6"].verdict, "是")

    def test_p7_termination_context(self):
        """翰森：终止类命中都在产品/政策语境 → 由「是」降为「部分」。"""
        res, _ = adjudicate(_doc("03692_HANSOH_PHARMA_ESG_FY2025.pdf"))
        self.assertEqual(res["S1.1.1-5"].verdict, "部分")

    def test_no_mutation_of_base(self):
        """增强层不得就地改 ESG_insight 返回的对象（复制后修改）。"""
        doc = _doc("02269_WUXI_BIO_ESG_FY2025.pdf")
        base = judge_lexical(doc)
        before = {k: v.verdict for k, v in base.items()}
        adjudicate(doc, base=base)
        after = {k: v.verdict for k, v in base.items()}
        self.assertEqual(before, after)


def main(eids: list[str]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    for name in sorted((REPORT_DIR / "FY2025").glob("*.pdf")):
        res, _ = adjudicate(load_pdf(str(name)))
        print(f"\n===== {name.name[:5]} =====")
        for eid in eids:
            r = res[eid]
            print(f"  {eid} → {r.verdict} | {r.reason[:70]}")
            for h in r.hits[:3]:
                print(f"      p{h.page} | {h.sent[:200]}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1:])
    else:
        unittest.main(verbosity=2)