# -*- coding: utf-8 -*-
"""打分层单测：档位边界与「0 不是缺失」这类**只给错分、不报错**的判分错。

为什么值得单独一条
-----------------
这里锁的两处都极难靠批跑发现：

1. 流失率 0% 是最好的成绩，却因为 ``if turnover`` 被当成「没值」，A 项直接记 N/A；
   本批 5 家没有零流失的公司，所以整条流水线永远碰不到。
2. B 项 100 分档的「本期差距 ≤3%」一度被写成 ``GAP_NEAR_ZERO``（2%），
   2%–3% 之间的公司会掉到 50 分；同样只在特定数值上出现。

两条都按《S1.1 当前具体方案》的档位表构造：A 项 ≤3%→100，B 项「改善率 ≥5% 或本期差距 ≤3%」→100，
「上期差距为 0」的补充规则单独按 2% 判定。改口径时这两条会同时红，逼人回来确认。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401  （触发 sys.path 注入 ESG_insight/app）
from esg_s11_scorer import scoring
from esg_s11_scorer.bench import from_rows
from esg_s11_scorer.config import GAP_B_FLOOR, GAP_NEAR_ZERO
from esg_s11_scorer.models import PeerRow
from esg_s11_scorer.qualitative import Verdict


def _verdict(tier: int = 100, key: str = "S1.1.1-C") -> Verdict:
    """判档桩：C 项与三档制一律吃 Verdict。"""
    return Verdict(key=key, tier=tier, reason="单测桩", evidence=["[p1] 原文"])


def _bench_zero_first() -> "object":
    """五家对标池：Z 家流失率 0%（最低），其余 5%–8%。"""
    rows = [PeerRow(code="Z", year=2025, turnover=0.0, prior_turnover=2.0)]
    rows += [PeerRow(code=f"C{i}", year=2025, turnover=5.0 + i, prior_turnover=6.0 + i)
             for i in range(4)]
    return from_rows(rows, "auto")


class TestZeroTurnoverIsBestNotMissing(unittest.TestCase):
    """流失率 0% 必须当「最好」参与分位，不能当「没值」记 N/A。"""

    @classmethod
    def setUpClass(cls):
        cls.bench = _bench_zero_first()

    def test_percentile_of_zero_is_top_band(self):
        self.assertEqual(self.bench.percentile("Z", "turnover", False), 20.0)
        self.assertEqual(self.bench.band(20.0), 100)

    def test_a_component_scored_instead_of_na(self):
        s = scoring.score_s111(0.0, 2.0, self.bench, "Z", verdict=_verdict(100))
        a = s.components[0]
        self.assertIsNotNone(a.score, msg="流失率 0% 被当成缺失 → A 项记了 N/A")
        self.assertEqual(a.score, 100)
        self.assertEqual(s.strict, 100.0)

    def test_missing_turnover_still_na(self):
        """真的没值仍然是 N/A——修 bug 不能把 N/A 口径一起改掉。"""
        s = scoring.score_s111(None, None, self.bench, "Z", verdict=_verdict(100))
        self.assertIsNone(s.components[0].score)
        self.assertIsNone(s.components[1].score)     # 上期也没有 → B 同样不评
        self.assertEqual(s.reachable, 30.0)          # 只剩 C(30%) 可评


class TestVerdictDrivesC(unittest.TestCase):
    """C 项只认判档结论：没有任何词法判定能再拼出分数。"""

    def test_c_follows_verdict(self):
        s = scoring.score_s111(5.0, 6.0, _bench_zero_first(), "C0", verdict=_verdict(0))
        c = next(x for x in s.components if x.key == "C")
        self.assertEqual(c.score, 0, "C 项必须听判档，不能由别的路径拼出来")

    def test_c_note_carries_verdict_reason(self):
        s = scoring.score_s111(5.0, 6.0, _bench_zero_first(), "C0", verdict=_verdict(50))
        c = next(x for x in s.components if x.key == "C")
        self.assertIn("50 分", c.note)
        self.assertIn("单测桩", c.note)

    def test_tiered_indicators_take_verdict_tier(self):
        for fn, key in ((scoring.score_s113, "S1.1.3"), (scoring.score_s114, "S1.1.4"),
                        (scoring.score_s115, "S1.1.5")):
            self.assertEqual(fn(_verdict(50, key)).raw, 50)
            self.assertEqual(fn(_verdict(0, key)).raw, 0)


class TestGapTrendBands(unittest.TestCase):
    """S1.1.2 B 项的档位边界：两个阈值（3% 与 2%）不能混用。"""

    def _b(self, gap: float, prior_gap: float) -> int | None:
        s = scoring.score_s112(gap, prior_gap, verdict=_verdict(100, "S1.1.2-C"))
        return s.components[1].score

    def test_config_anchors(self):
        self.assertEqual(GAP_B_FLOOR, 3.0, "B 项 100 分档应为本期差距 ≤3%")
        self.assertEqual(GAP_NEAR_ZERO, 2.0, "「上期为 0」的补充规则应为 2%")

    def test_gap_within_three_percent_is_full_score_even_if_worse(self):
        # 本期 2.8% 已在 A 的 100 分档内；即使比上期 3.5% 变差，B 仍记 100
        self.assertEqual(self._b(2.8, 3.5), 100)

    def test_gap_between_two_and_three_percent_not_downgraded(self):
        # 回归点：这里曾被 GAP_NEAR_ZERO(2%) 判成 50
        self.assertEqual(self._b(2.5, 3.0), 100)

    def test_gap_above_three_percent_follows_improvement(self):
        self.assertEqual(self._b(9.0, 10.0), 100)    # 改善 10% ≥ 5%
        self.assertEqual(self._b(9.8, 10.0), 50)     # 改善 2%，不在 0–5% 之外
        self.assertEqual(self._b(10.5, 10.0), 0)     # 反向恶化

    def test_prior_zero_uses_two_percent_rule(self):
        self.assertEqual(self._b(2.0, 0.0), 100)
        self.assertEqual(self._b(2.5, 0.0), 0, "上期为 0 时只认 2%，不能套用 3%")

    def test_gap_missing_keeps_na(self):
        s = scoring.score_s112(None, None, verdict=_verdict(100, "S1.1.2-C"))
        self.assertIsNone(s.components[0].score)
        self.assertIsNone(s.components[1].score)


if __name__ == "__main__":
    unittest.main(verbosity=2)