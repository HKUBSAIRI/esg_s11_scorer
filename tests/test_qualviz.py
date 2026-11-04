# -*- coding: utf-8 -*-
"""判档图册测试（不联网）：结构、热图格式、图表存在、口径可追溯。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from openpyxl import load_workbook  # noqa: E402

from esg_s11_scorer.qualitative import Verdict  # noqa: E402
from esg_s11_scorer.qualviz import collect, write  # noqa: E402
from esg_s11_scorer.rubric import RUBRICS  # noqa: E402


class _View:
    def __init__(self, code, name):
        self.code, self.name = code, name


def _verdicts(code: str, tiers: dict[str, int]) -> dict:
    return {k: Verdict(key=k, tier=t, reason=f"{code} {k} 判 {t}",
                       evidence=[f"[p1] {code} {k} 原文"], source="llm",
                       model="qwen-plus", n_passages=12, alias_hits=2)
            for k, t in tiers.items()}


VIEWS = [_View("A001", "甲公司"), _View("B002", "乙公司")]
DATA = {
    "A001": _verdicts("A001", {"S1.1.1-C": 50, "S1.1.2-C": 50, "S1.1.3": 50,
                               "S1.1.4": 100, "S1.1.5": 100}),
    "B002": _verdicts("B002", {"S1.1.1-C": 0, "S1.1.2-C": 50, "S1.1.3": 50,
                               "S1.1.4": 100, "S1.1.5": 50}),
}


class TestCollect(unittest.TestCase):
    def test_rows_one_per_company_per_rubric(self):
        rows = collect(VIEWS, DATA)
        self.assertEqual(len(rows), 2 * len(RUBRICS))
        self.assertEqual({r["key"] for r in rows}, set(RUBRICS))

    def test_missing_verdict_is_none_not_zero(self):
        rows = collect(VIEWS, {"A001": {}})
        self.assertTrue(all(r["tier"] is None for r in rows if r["code"] == "A001"))
        self.assertTrue(all(r["source"] == "" for r in rows if r["code"] == "A001"))

    def test_source_is_translated(self):
        rows = collect(VIEWS, DATA)
        self.assertEqual(rows[0]["source"], "LLM 判档")
        self.assertTrue(all(r["alias_hits"] == 2 for r in rows))


class TestWorkbook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = write(collect(VIEWS, DATA), Path(cls.tmp.name) / "q.xlsx",
                         year=2025, mode="llm", model="qwen-plus", fingerprint="abc123")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_sheets(self):
        wb = load_workbook(self.path)
        self.assertEqual(wb.sheetnames, ["档位矩阵", "判档明细", "档位图", "口径与台账"])

    def test_matrix_values_and_heatmap(self):
        wb = load_workbook(self.path)
        ws = wb["档位矩阵"]
        # 第 3 行 = S1.1.1-C，B 列 = A001，C 列 = B002
        self.assertEqual(ws.cell(row=3, column=1).value, "S1.1.1-C 披露质量与就业终止保障")
        self.assertEqual(ws.cell(row=3, column=2).value, 50)
        self.assertEqual(ws.cell(row=3, column=3).value, 0)
        self.assertTrue(ws.conditional_formatting, "热图条件格式丢了")

    def test_detail_sheet_keeps_reason(self):
        wb = load_workbook(self.path)
        ws = wb["判档明细"]
        reasons = [ws.cell(row=r, column=9).value for r in range(3, 3 + 2 * len(RUBRICS))]
        self.assertTrue(all(reasons))
        self.assertIn("判", reasons[0])

    def test_charts_exist(self):
        wb = load_workbook(self.path)
        self.assertGreaterEqual(len(wb["档位图"]._charts), 2)

    def test_ledger_has_fingerprint(self):
        wb = load_workbook(self.path)
        ws = wb["口径与台账"]
        flat = {ws.cell(row=r, column=1).value: ws.cell(row=r, column=2).value
                for r in range(3, 30)}
        self.assertEqual(flat.get("rubric 指纹"), "abc123")
        self.assertEqual(flat.get("模型"), "qwen-plus")
        self.assertIn("盲评", "".join(str(k) for k in flat))

    def test_rubric_text_is_reproduced(self):
        wb = load_workbook(self.path)
        ws = wb["口径与台账"]
        blob = " ".join(str(ws.cell(row=r, column=2).value) for r in range(3, 80))
        for rub in RUBRICS.values():
            self.assertIn(rub.tiers[100][:20], blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)