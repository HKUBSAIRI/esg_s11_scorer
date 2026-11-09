# -*- coding: utf-8 -*-
"""回归测试：锁定本批 5 家 FY2025 的分数与药明手工分析，防止重构/改规则悄悄改分。

跑法（在 ``50-应用`` 下）：

    python -m unittest discover -s esg_s11_scorer/tests -t . -v

设计取舍
--------
- 断言的是**具体分数**，不是「能跑通」——这是本项目的核心回归网：任何词表、阈值、抽取
  改动都会先在这里暴露。
- 期望值来自人工核证（药明生物）与专家裁定（10 份样本），不是「跑一遍把输出存下来」，
  否则改错了也会通过。
- 单份 PDF 的抽取断言与批跑断言分开，失败时能立刻区分「抽取坏了」还是「算分坏了」。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401  （触发 sys.path 注入 ESG_insight/app）
from esg_s11_scorer.bench import Bench
from esg_s11_scorer.config import DEFAULT_OUT, REPORT_DIR
from esg_s11_scorer.pipeline import run_extract_batch, run_score, score_batch

# 本批 5 家 FY2025 的期望分（严格口径，批内分位）
EXPECT_BATCH = {
    "01177": {"S1.1.1": 45.0, "S1.1.2": 30.0, "S1.1.3": 100.0, "S1.1.4": 50.0, "S1.1.5": 100.0},
    "01513": {"S1.1.1": 85.0, "S1.1.2": 0.0, "S1.1.3": 100.0, "S1.1.4": 100.0, "S1.1.5": 100.0},
    "01801": {"S1.1.1": 57.5, "S1.1.2": 15.0, "S1.1.3": 100.0, "S1.1.4": 100.0, "S1.1.5": 100.0},
    "02269": {"S1.1.1": 67.5, "S1.1.2": 65.0, "S1.1.3": 50.0, "S1.1.4": 50.0, "S1.1.5": 50.0},
    "03692": {"S1.1.1": 25.0, "S1.1.2": 35.0, "S1.1.3": 100.0, "S1.1.4": 100.0, "S1.1.5": 100.0},
}

# 药明生物：本组手工核证的五项分（无对标池口径）
WUXI_MANUAL = (45.0, 80.0, 50.0, 50.0, 50.0)
# 回归锁这一组：keyword 判档的实际输出
WUXI_SCORES = (45.0, 65.0, 50.0, 50.0, 50.0)
WUXI_MAX_DRIFT = 15.0


class TestBatchRegression(unittest.TestCase):
    """5 家 FY2025 批跑（``--qualitative keyword``）：分数必须逐格稳定。

    为什么用 keyword 而不是默认的 llm：LLM 的输出不是逐比特确定的，不能当回归基线；
    keyword 离线、确定性，能锁住「除模型自由发挥之外」的全部口径。
    """

    @classmethod
    def setUpClass(cls):
        override = DEFAULT_OUT / "overrides_FY2025.csv"
        cls.runs, cls.bench = score_batch(
            REPORT_DIR / "FY2025", year=2025, qualitative="keyword",
            override_csv=override if override.exists() else None)

    def test_all_parsed(self):
        self.assertEqual(len([r for r in self.runs if r.card]), 5)

    def test_indicator_scores(self):
        got = {r.code: {k: v.strict for k, v in r.card.indicators.items()} for r in self.runs}
        self.assertEqual(got, EXPECT_BATCH)

    def test_bench_percentiles(self):
        # 丽珠最好（9.1%）→ 20 分位；中生最差（16.83%）→ 100 分位
        self.assertEqual(self.bench.percentile("01513", "turnover", False), 20.0)
        self.assertEqual(self.bench.percentile("01177", "turnover", False), 100.0)
        self.assertEqual(self.bench.percentile("02269", "turnover", False), 40.0)

    def test_numeric_extraction(self):
        """流失率/上期/差距 抽出来的数必须与人工读到的完全一致。"""
        want = {"02269": (10.0, 11.0, 5.7), "01513": (9.1, 10.4, None),
                "03692": (14.4, 13.57, 6.6), "01801": (13.3, 14.0, None),
                "01177": (16.83, 18.48, None)}
        for r in self.runs:
            d = r.card.data
            self.assertEqual((d.turnover, d.prior_turnover, d.gap), want[r.code],
                             msg=f"{r.code} 抽取值不符")


class TestWuxiAgainstManual(unittest.TestCase):
    """药明生物：与上一轮手工分析的分差必须留在登记范围内。"""

    @classmethod
    def setUpClass(cls):
        runs = run_extract_batch([REPORT_DIR / "FY2025" / "02269_WUXI_BIO_ESG_FY2025.pdf"], 2025)
        cls.wuxi = run_score(runs, Bench(kind="none", note="测试：无对标池"))[0]

    def test_scores(self):
        got = tuple(self.wuxi.card.indicators[k].strict
                    for k in ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5"))
        self.assertEqual(got, WUXI_SCORES)
        # 与手工分的偏差必须留在登记范围内：口径一改就把基线改成「模型说啥就是啥」，
        # 等于放弃校准；这里同时锁住「模型现在的输出」和「与手工的距离」。
        drift = [a - m for a, m in zip(got, WUXI_MANUAL)]
        self.assertLessEqual(max(abs(x) for x in drift), WUXI_MAX_DRIFT,
                             f"与手工分析偏差扩大到 {drift}")


if __name__ == "__main__":
    unittest.main(verbosity=2)