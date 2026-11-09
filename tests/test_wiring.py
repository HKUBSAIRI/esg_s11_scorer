# -*- coding: utf-8 -*-
"""定性判档 → 记分卡的接线测试（**不联网**）。

接进打分链路的关键约束只有两条：

1. **判档结论必须原样变成分数**：C 项与三档制指标的分 = 判档档位（哪怕是 0 或 100），
   中途不能被任何二次解释改掉。
2. **判档缺一不可**：没有词法兜底路径，少给一项就该抛错，
   而不是悄悄拼一个「看着正常、口径却过期」的数。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from esg_s11_scorer.bench import Bench  # noqa: E402
from esg_s11_scorer.models import ScoreData  # noqa: E402
from esg_s11_scorer.qualitative import Verdict  # noqa: E402
from esg_s11_scorer.scoring import build_card  # noqa: E402


def _data(**kw) -> ScoreData:
    base = dict(report_year=2025, turnover=10.0, prior_turnover=11.0,
                gap=5.0, prior_gap=6.0)
    base.update(kw)
    return ScoreData(**base)


def _v(key: str, tier: int, source: str = "llm") -> Verdict:
    return Verdict(key=key, tier=tier, reason=f"{key} 判为 {tier}",
                   evidence=[f"[p1] {key} 的证据原文"], source=source)


def _all(**over) -> dict[str, Verdict]:
    """五项判档的默认桩（都是 50 分），用 ``over`` 覆盖关心的那一项。"""
    base = {"S1.1.1-C": _v("S1.1.1-C", 50), "S1.1.2-C": _v("S1.1.2-C", 50),
            "S1.1.3": _v("S1.1.3", 50), "S1.1.4": _v("S1.1.4", 50),
            "S1.1.5": _v("S1.1.5", 50)}
    base.update(over)
    return base


NO_BENCH = Bench(kind="none", note="测试：无对标池")


class TestVerdictDrivesScore(unittest.TestCase):
    def test_tiered_indicators_take_verdict_tier(self):
        card = build_card("X", "测试", 2025, "x.pdf", _data(), NO_BENCH,
                          verdicts=_all(**{"S1.1.3": _v("S1.1.3", 50),
                                           "S1.1.4": _v("S1.1.4", 100),
                                           "S1.1.5": _v("S1.1.5", 0)}))
        self.assertEqual(card.indicators["S1.1.3"].raw, 50)
        self.assertEqual(card.indicators["S1.1.4"].raw, 100)
        self.assertEqual(card.indicators["S1.1.5"].raw, 0)
        for k in ("S1.1.3", "S1.1.4", "S1.1.5"):
            self.assertEqual(card.indicators[k].strict, float(card.indicators[k].raw))

    def test_weighted_c_takes_verdict_tier(self):
        card = build_card("X", "测试", 2025, "x.pdf", _data(), NO_BENCH,
                          verdicts=_all(**{"S1.1.1-C": _v("S1.1.1-C", 50),
                                           "S1.1.2-C": _v("S1.1.2-C", 100)}))
        s111 = card.indicators["S1.1.1"]
        c = next(x for x in s111.components if x.key == "C")
        self.assertEqual(c.score, 50)
        self.assertEqual(c.weight, 0.3)
        self.assertIn("判为 50", c.note)
        self.assertIn("证据原文", c.evidence)
        c2 = next(x for x in card.indicators["S1.1.2"].components if x.key == "C")
        self.assertEqual(c2.score, 100)

    def test_weighted_total_is_formula(self):
        card = build_card("X", "测试", 2025, "x.pdf", _data(), NO_BENCH,
                          verdicts=_all(**{"S1.1.1-C": _v("S1.1.1-C", 100),
                                           "S1.1.2-C": _v("S1.1.2-C", 100)}))
        # A 无对标池 → N/A；B 为改善方向 → 100；C=100 → 0.3*100+0.3*100 = 60
        self.assertEqual(card.indicators["S1.1.1"].strict, 60.0)
        # A=50（差距 5%）、B=100（改善 16.7%）、C=100 → 20+30+30 = 80
        self.assertEqual(card.indicators["S1.1.2"].strict, 80.0)

    def test_gate_source_is_recorded(self):
        card = build_card("X", "测试", 2025, "x.pdf", _data(), NO_BENCH,
                          verdicts=_all(**{"S1.1.1-C": _v("S1.1.1-C", 0, source="gate")}))
        c = next(x for x in card.indicators["S1.1.1"].components if x.key == "C")
        self.assertEqual(c.score, 0)
        self.assertIn("0 分前置条件", c.note)


class TestMissingVerdictIsFatal(unittest.TestCase):
    """缺判档必须报错，且错误信息要指得动人。"""

    def test_missing_verdict_raises(self):
        with self.assertRaises(ValueError) as ctx:
            build_card("X", "测试", 2025, "x.pdf", _data(), NO_BENCH,
                       verdicts={"S1.1.3": _v("S1.1.3", 50)})
        msg = str(ctx.exception)
        self.assertIn("X", msg, "错误里要点名是哪家公司")
        self.assertIn("S1.1.1-C", msg, "要列出缺哪几项")
        self.assertIn("--qualitative", msg, "要说明该换哪种模式重跑")

    def test_empty_verdicts_raises(self):
        with self.assertRaises(ValueError):
            build_card("X", "测试", 2025, "x.pdf", _data(), NO_BENCH, verdicts={})


if __name__ == "__main__":
    unittest.main(verbosity=2)