# -*- coding: utf-8 -*-
"""关键词清单测试：两列、五行、一个词都不许丢。

表本身没什么逻辑，但它是**给人看的唯一入口**——少一个词、串错一行，评审就会以为
某个指标没考虑某类证据。所以断言：每行关键词串按顿号切开后，必须与 rubric 完全一致。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from openpyxl import load_workbook  # noqa: E402

from esg_s11_scorer.keyword_table import SEP, table_rows, write  # noqa: E402
from esg_s11_scorer.rubric import RUBRICS  # noqa: E402


class TestRows(unittest.TestCase):
    def test_one_row_per_rubric_in_order(self):
        rows = table_rows()
        self.assertEqual([r[0].split()[0] for r in rows], list(RUBRICS))

    def test_words_roundtrip_exactly(self):
        for (label, words), (key, rub) in zip(table_rows(), RUBRICS.items()):
            self.assertEqual(tuple(words.split(SEP)), rub.keywords, msg=f"{label} 词表不一致")

    def test_all_words_present_once(self):
        total = sum(len(r.keywords) for r in RUBRICS.values())
        self.assertEqual(len(SEP.join(w for _, w in table_rows()).split(SEP)), total)
        # 381 = v1.4 的 350 + 31 个 GT 对账补的英文同义词（internal transfer / 晋升 /
        # human rights / 产假 / tuition / competency / child labour）。
        # 注意：这些词只进「送 LLM」的召回，兜底仍用 v1.4 的 350 词（rubric.BASELINE_EXCLUDED），
        # 所以加词不会动回归基线。
        self.assertEqual(total, 381)


class TestWorkbook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = write(Path(cls.tmp.name) / "k.xlsx")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_single_sheet_two_columns(self):
        ws = load_workbook(self.path)["关键词"]
        self.assertEqual(ws.max_column, 2)
        self.assertEqual(ws.cell(row=2, column=1).value, "定性指标")
        self.assertEqual(ws.cell(row=2, column=2).value, "关键词")
        self.assertEqual(ws.max_row, 2 + len(RUBRICS))

    def test_cells_match_rubric(self):
        ws = load_workbook(self.path)["关键词"]
        for i, (key, rub) in enumerate(RUBRICS.items(), start=3):
            self.assertIn(key, ws.cell(row=i, column=1).value)
            self.assertEqual(ws.cell(row=i, column=2).value, SEP.join(rub.keywords))


if __name__ == "__main__":
    unittest.main(verbosity=2)