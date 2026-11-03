# -*- coding: utf-8 -*-
"""抽取层测试：5 家 FY2025 的流失率/上期/薪酬差距必须与人工读到的数一致。

这里断言的是**绝对值**（从原报告人工核对的数），不是「跑一遍存下来的输出」——
否则抽取逻辑改错也会通过。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401
from esg_s11_scorer.config import REPORT_DIR
from esg_s11_scorer.extract import extract
from esg_s11_scorer.metrics import improvement
from s11_text import load_pdf

# 公司 → (本期流失率, 上期流失率, 性别薪酬差距)
EXPECT = {
    "01177": (16.83, 18.48, None),
    "01513": (9.1, 10.4, None),
    "01801": (13.3, None, None),      # 本期只在正文给 13.3%，上期须取 FY2024 报告
    "02269": (10.0, 11.0, 5.7),
    "03692": (14.4, None, 6.6),       # 披露「平均 3.9% / 中位 6.6%」，取中位数
}


class TestExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ex = {}
        for pdf in sorted((REPORT_DIR / "FY2025").glob("*.pdf")):
            doc = load_pdf(str(pdf))
            cls.ex[pdf.name[:5]] = extract(doc, pdf.name)

    def test_values(self):
        for code, (t, tp, g) in EXPECT.items():
            e = self.ex[code]
            ry = e.report_year
            self.assertEqual(e.value("turnover", ry), t, msg=f"{code} 本期流失率")
            self.assertEqual(e.value("turnover", ry - 1), tp, msg=f"{code} 上期流失率")
            self.assertEqual(e.value("gender_gap", ry), g, msg=f"{code} 性别薪酬差距")

    def test_report_year(self):
        for code, e in self.ex.items():
            self.assertEqual(e.report_year, 2025, msg=code)

    def test_cross_validation(self):
        """药明的离职人数/员工总数 应能交叉验证披露的流失率。"""
        e = self.ex["02269"]
        self.assertEqual(e.value("leavers", 2025), 1352)
        self.assertEqual(e.value("headcount", 2025), 13352)
        self.assertTrue(any("交叉校验通过" in w for w in e.warnings), e.warnings)

    def test_improvement(self):
        self.assertAlmostEqual(improvement(10.0, 11.0), 9.0909, places=3)
        self.assertIsNone(improvement(10.0, None))
        self.assertIsNone(improvement(10.0, 0))

    def test_year_alignment_四列(self):
        """药明是「% 10 11 11 10」四列对 2022–2025，必须逐列对上，不能只取第一列。"""
        e = self.ex["02269"]
        got = {y: e.value("turnover", y) for y in (2022, 2023, 2024, 2025)}
        self.assertEqual(got, {2022: 10.0, 2023: 11.0, 2024: 11.0, 2025: 10.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)