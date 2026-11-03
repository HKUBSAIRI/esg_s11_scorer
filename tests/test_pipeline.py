# -*- coding: utf-8 -*-
"""编排层测试：整批跑通、结构化数据齐备、比分项自洽。

也把「人读得懂的摘要」保留在 ``__main__`` 分支里：直接运行这个文件即可当 CLI 的轻量替代。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401
from esg_s11_scorer.config import DEFAULT_OUT, WEIGHT_SUM, REPORT_DIR
from esg_s11_scorer.models import ScoreData
from esg_s11_scorer.pipeline import score_batch


def _run():
    override = DEFAULT_OUT / "overrides_FY2025.csv"
    return score_batch(REPORT_DIR / "FY2025", year=2025,
                       override_csv=override if override.exists() else None,
                       progress=None)


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs, cls.bench = _run()

    def test_batch_ok(self):
        self.assertEqual(len(self.runs), 5)
        for r in self.runs:
            self.assertEqual(r.status, "ok", msg=f"{r.code}: {r.error}")
            self.assertIsNotNone(r.card)

    def test_data_is_typed(self):
        """量化事实必须是 ScoreData 而不是自由 dict（重构后不再出现中文键访问）。"""
        for r in self.runs:
            self.assertIsInstance(r.card.data, ScoreData)

    def test_totals_in_range(self):
        for r in self.runs:
            c = r.card
            self.assertGreaterEqual(c.total_strict, 0)
            self.assertLessEqual(c.total_strict, WEIGHT_SUM)
            self.assertLessEqual(c.total_normalized(WEIGHT_SUM), 100)

    def test_prior_fallback(self):
        """信达的上期值来自 FY2024 报告；翰森来自人工兜底。"""
        by = {r.code: r for r in self.runs}
        self.assertEqual(by["01801"].card.data.prior_turnover, 14.0)
        self.assertIn("上一年报告", by["01801"].card.data.prior_source)
        self.assertEqual(by["03692"].card.data.prior_turnover, 13.57)
        self.assertIn("人工填入", by["03692"].card.data.prior_source)

    def test_zero_prior_not_treated_as_missing(self):
        """上期值为 0 必须当有效值（不能用 ``or`` 兜底当 None）。"""
        from esg_s11_scorer.models import CompanyRun
        run = CompanyRun(code="00001", name="t", year=2025, pdf=Path("x.pdf"))
        run.prior_turnover_fallback = 0.0
        self.assertEqual(run.prior_turnover(2025), 0.0)

    def test_bench_auto(self):
        self.assertEqual(self.bench.kind, "auto")
        self.assertEqual(self.bench.n, 5)


def main() -> None:
    runs, bench = _run()
    print("bench:", bench.kind, "n=", bench.n, "|", bench.note)
    print(f"\n{'代码':<7}{'公司':<22}{'严格':>6}{'归一':>7}  分项")
    for r in runs:
        c = r.card
        if c is None:
            print(r.code, "NO CARD", r.status, r.error)
            continue
        parts = [f"{k[-3:]}:{v.strict}("
                 + " ".join(f"{cc.key or '-'}={cc.score}" for cc in v.components) + ")"
                 for k, v in c.indicators.items()]
        print(f"{r.code:<7}{r.name[:20]:<22}{c.total_strict:>6}"
              f"{c.total_normalized(WEIGHT_SUM):>7}  " + " | ".join(parts))
        d = c.data
        print(f"        流失 {d.turnover} <- {d.prior_turnover} 改善 {d.improvement}"
              f" | 差距 {d.gap} | 上期 {d.prior_source}")
        for p in r.patches:
            print(f"        [补丁] {p.eid} {p.before}→{p.after}：{p.reason[:60]}")


if __name__ == "__main__":
    main()