# -*- coding: utf-8 -*-
"""得分构成图册测试：算术必须自洽（这是它存在的唯一理由）。

核对分数时最容易错的一步就是「分项得分 × 分项权重 × 指标权重」，
所以这里断言：每张卡的各分项折合分之和 == 指标贡献之和 == 加权总分。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from openpyxl import load_workbook  # noqa: E402

from esg_s11_scorer.bench import Bench  # noqa: E402
from esg_s11_scorer.models import ScoreData  # noqa: E402
from esg_s11_scorer.qualitative import Verdict  # noqa: E402
from esg_s11_scorer.scoring import build_card  # noqa: E402
from esg_s11_scorer.scoreviz import KEYS, contribution, rows_of, write  # noqa: E402


def _card(code: str, name: str, tiers: dict[str, int]):
    data = ScoreData(report_year=2025, turnover=10.0, prior_turnover=11.0,
                     gap=5.0, prior_gap=6.0)
    return build_card(code, name, 2025, "x.pdf", {}, data,
                      Bench(kind="none", note="测试：无对标池"), True,
                      verdicts={k: Verdict(key=k, tier=v, reason="测试", source="llm")
                                for k, v in tiers.items()})


CARDS = [
    _card("A001", "甲公司", {"S1.1.1-C": 50, "S1.1.2-C": 100,
                             "S1.1.3": 50, "S1.1.4": 100, "S1.1.5": 50}),
    _card("B002", "乙公司", {"S1.1.1-C": 100, "S1.1.2-C": 50,
                             "S1.1.3": 100, "S1.1.4": 50, "S1.1.5": 100}),
]


class _View:
    def __init__(self, card):
        self.code, self.name, self.card = card.code, card.name, card


class TestArithmetic(unittest.TestCase):
    def test_contribution_formula(self):
        # 25% × 40% × 75 = 7.5
        self.assertEqual(contribution(25, 0.4, 75), 7.5)
        # 三档制：15% × 100 = 15
        self.assertEqual(contribution(15, 1.0, 100), 15)
        self.assertEqual(contribution(25, 0.4, None), 0.0)

    def test_rows_sum_to_indicator_strict(self):
        for card in CARDS:
            rows = rows_of(card)
            for k in KEYS:
                ind = card.indicators[k]
                got = round(sum(r["contrib"] for r in rows if r["ind"] == k), 2)
                self.assertAlmostEqual(got, round(ind.weight / 100 * ind.strict, 2),
                                       places=2, msg=f"{card.code} {k} 分项之和对不上")

    def test_rows_sum_to_total(self):
        for card in CARDS:
            total = round(sum(r["contrib"] for r in rows_of(card)), 2)
            self.assertAlmostEqual(total, card.total_strict, delta=0.06,
                                   msg=f"{card.code} 折合分合计与总分不符")

    def test_tiered_row_uses_raw_tier(self):
        rows = [r for r in rows_of(CARDS[0]) if r["ind"] == "S1.1.3"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], 50)
        self.assertEqual(rows[0]["contrib"], 7.5)


class TestWorkbook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        views = [_View(c) for c in CARDS]
        cls.path = write(views, Path(cls.tmp.name) / "s.xlsx", year=2025)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_sheets(self):
        wb = load_workbook(self.path)
        self.assertEqual(wb.sheetnames, ["总分与构成", "分项明细", "口径"])

    def test_totals_row(self):
        ws = load_workbook(self.path)["总分与构成"]
        # 表头在第 2 行，第一家公司第 3 行；总分列 = 2+5+1 = 8
        self.assertEqual(ws.cell(row=2, column=1).value, "代码")
        self.assertAlmostEqual(ws.cell(row=3, column=8).value,
                               CARDS[0].total_strict, places=1)

    def test_chart_present(self):
        ws = load_workbook(self.path)["总分与构成"]
        self.assertEqual(len(ws._charts), 1)

    def test_detail_sheet_row_count(self):
        ws = load_workbook(self.path)["分项明细"]
        want = sum(len(rows_of(c)) for c in CARDS)
        filled = sum(1 for r in range(3, 3 + want) if ws.cell(row=r, column=1).value)
        self.assertEqual(filled, want)


if __name__ == "__main__":
    unittest.main(verbosity=2)