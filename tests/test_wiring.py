# -*- coding: utf-8 -*-
"""定性判档 → 记分卡的接线测试（**不联网**）。

接进打分链路的关键约束只有一条：**判档结论必须原样变成分数**，中途不能被要素逻辑
覆盖或二次解释。这里用构造的 ``Verdict`` 直接调 ``build_card``，把两条路径都钉住：

- 给了 verdict → C 项 / 三档制指标的分 = 判档档位（哪怕是 0 或 100）；
- 没给 verdict → 仍走 29 要素老路径（``--qualitative element`` 必须还能复现旧分）。
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


NO_BENCH = Bench(kind="none", note="测试：无对标池")


class TestVerdictDrivesScore(unittest.TestCase):
    def test_tiered_indicators_take_verdict_tier(self):
        card = build_card("X", "测试", 2025, "x.pdf", {}, _data(), NO_BENCH, True,
                          verdicts={"S1.1.3": _v("S1.1.3", 50),
                                    "S1.1.4": _v("S1.1.4", 100),
                                    "S1.1.5": _v("S1.1.5", 0)})
        self.assertEqual(card.indicators["S1.1.3"].raw, 50)
        self.assertEqual(card.indicators["S1.1.4"].raw, 100)
        self.assertEqual(card.indicators["S1.1.5"].raw, 0)
        for k in ("S1.1.3", "S1.1.4", "S1.1.5"):
            self.assertEqual(card.indicators[k].strict, float(card.indicators[k].raw))

    def test_weighted_c_takes_verdict_tier(self):
        card = build_card("X", "测试", 2025, "x.pdf", {}, _data(), NO_BENCH, True,
                          verdicts={"S1.1.1-C": _v("S1.1.1-C", 50),
                                    "S1.1.2-C": _v("S1.1.2-C", 100)})
        s111 = card.indicators["S1.1.1"]
        c = next(x for x in s111.components if x.key == "C")
        self.assertEqual(c.score, 50)
        self.assertEqual(c.weight, 0.3)
        self.assertIn("判为 50", c.note)
        self.assertIn("证据原文", c.evidence)
        c2 = next(x for x in card.indicators["S1.1.2"].components if x.key == "C")
        self.assertEqual(c2.score, 100)

    def test_weighted_total_is_formula(self):
        card = build_card("X", "测试", 2025, "x.pdf", {}, _data(), NO_BENCH, True,
                          verdicts={"S1.1.1-C": _v("S1.1.1-C", 100),
                                    "S1.1.2-C": _v("S1.1.2-C", 100)})
        # A 无对标池 → N/A；B 为改善方向 → 100；C=100 → 0.3*100+0.3*100 = 60
        self.assertEqual(card.indicators["S1.1.1"].strict, 60.0)
        # A=50（差距 5%）、B=100（改善 16.7%）、C=100 → 20+30+30 = 80
        self.assertEqual(card.indicators["S1.1.2"].strict, 80.0)

    def test_no_verdict_keeps_element_path(self):
        """不给 verdict 时必须回到要素逻辑（否则 element 模式就没法复现旧分）。"""
        card = build_card("X", "测试", 2025, "x.pdf", {}, _data(), NO_BENCH, True)
        self.assertEqual(card.indicators["S1.1.3"].raw, 0, "无要素 → 老路径判 0")
        self.assertEqual(card.indicators["S1.1.4"].raw, 0)
        self.assertEqual(card.indicators["S1.1.5"].raw, 0)
        c = next(x for x in card.indicators["S1.1.1"].components if x.key == "C")
        # 老路径的已知行为：只要披露了自愿离职构成（has_vol）就给 50，不看合同/终止制度。
        # 这正是新链路要替掉它的原因之一；这里把行为钉住，改老路径时测试会红。
        self.assertEqual(c.score, 50, "披露自愿离职构成但无任何制度 → 老路径给 50")

    def test_no_verdict_without_voluntary_is_zero(self):
        card = build_card("X", "测试", 2025, "x.pdf", {}, _data(), NO_BENCH, False)
        c = next(x for x in card.indicators["S1.1.1"].components if x.key == "C")
        self.assertEqual(c.score, 0)

    def test_gate_source_is_recorded(self):
        card = build_card("X", "测试", 2025, "x.pdf", {}, _data(), NO_BENCH, True,
                          verdicts={"S1.1.1-C": _v("S1.1.1-C", 0, source="gate")})
        c = next(x for x in card.indicators["S1.1.1"].components if x.key == "C")
        self.assertEqual(c.score, 0)
        self.assertIn("0 分前置条件", c.note)


if __name__ == "__main__":
    unittest.main(verbosity=2)