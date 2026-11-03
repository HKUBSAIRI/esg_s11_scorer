# -*- coding: utf-8 -*-
"""回归测试：锁定本批 5 家 FY2025 的分数与两套校准集，防止重构/改规则悄悄改分。

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
from esg_s11_scorer.pipeline import run_extract_judge, run_score, score_batch

# 本批 5 家 FY2025 的期望分（严格口径，批内分位）
EXPECT_BATCH = {
    "02269": {"S1.1.1": 67.5, "S1.1.2": 80.0, "S1.1.3": 50.0, "S1.1.4": 50.0, "S1.1.5": 50.0},
    "01513": {"S1.1.1": 85.0, "S1.1.2": 15.0, "S1.1.3": 50.0, "S1.1.4": 50.0, "S1.1.5": 50.0},
    "03692": {"S1.1.1": 25.0, "S1.1.2": 35.0, "S1.1.3": 50.0, "S1.1.4": 50.0, "S1.1.5": 100.0},
    "01801": {"S1.1.1": 42.5, "S1.1.2": 15.0, "S1.1.3": 50.0, "S1.1.4": 100.0, "S1.1.5": 50.0},
    "01177": {"S1.1.1": 30.0, "S1.1.2": 15.0, "S1.1.3": 50.0, "S1.1.4": 50.0, "S1.1.5": 50.0},
}

# 药明生物：本组手工核证的 29 要素 + 五项分（无对标池口径）
WUXI_ELEMENTS = {
    "S1.1.1-1": "是", "S1.1.1-2": "是", "S1.1.1-3": "否", "S1.1.1-4": "部分",
    "S1.1.1-5": "部分", "S1.1.1-6": "是", "S1.1.1-7": "是",
    "S1.1.2-1": "是", "S1.1.2-2": "是", "S1.1.2-3": "部分", "S1.1.2-4": "是",
    "S1.1.2-5": "是", "S1.1.2-6": "是",
    "S1.1.3-1": "是", "S1.1.3-2": "部分", "S1.1.3-3": "否", "S1.1.3-4": "是",
    "S1.1.3-5": "否",
    "S1.1.4-1": "是", "S1.1.4-2": "是", "S1.1.4-3": "是", "S1.1.4-4": "是",
    "S1.1.4-5": "部分",
    "S1.1.5-1": "是", "S1.1.5-2": "是", "S1.1.5-3": "是", "S1.1.5-4": "是",
    "S1.1.5-5": "是", "S1.1.5-6": "否",
}
WUXI_SCORES = (45.0, 80.0, 50.0, 50.0, 50.0)

# 模型与手工裁定的已知分歧（3 处，方向均已核对）。这里显式登记而不是放宽断言：
# 一旦某处分歧消失或新增，测试都会失败，逼人回来确认是「改进」还是「回归」。
#   eid: (手工判定, 模型判定, 原因)
KNOWN_DIVERGENCE = {
    "S1.1.2-4": ("是", "部分",
                 "原文「we pay overtime or offer reasonable time off」是措施句；"
                 "词法要求同句含制度/量化证据，保守判部分（不影响 C 项）"),
    "S1.1.3-5": ("否", "部分",
                 "原文「Leave usage is tracked via an online system」被判为执行证据；"
                 "手工认为只有机制无数据。两者在档位上等价，S1.1.3 仍为 50"),
    "S1.1.4-5": ("部分", "否",
                 "Family Day 2,500 人参与属单项活动，词法不认作福利覆盖；"
                 "两者都在 100 分档以下，S1.1.4 仍为 50"),
}


class TestBatchRegression(unittest.TestCase):
    """5 家 FY2025 批跑：分数必须与人工核证/校准时一致。"""

    @classmethod
    def setUpClass(cls):
        override = DEFAULT_OUT / "overrides_FY2025.csv"
        cls.runs, cls.bench = score_batch(
            REPORT_DIR / "FY2025", year=2025,
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
    """药明生物：与上一轮手工分析逐要素对齐（不放宽）。"""

    @classmethod
    def setUpClass(cls):
        runs = run_extract_judge([REPORT_DIR / "FY2025" / "02269_WUXI_BIO_ESG_FY2025.pdf"], 2025)
        cls.wuxi = run_score(runs, Bench(kind="none", note="测试：无对标池"))[0]

    def test_scores(self):
        got = tuple(self.wuxi.card.indicators[k].strict
                    for k in ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5"))
        self.assertEqual(got, WUXI_SCORES)

    def test_elements(self):
        """逐要素核对：除了 3 处已登记分歧，其余必须与手工裁定一致。"""
        got = {eid: self.wuxi.res[eid].verdict for eid in WUXI_ELEMENTS}
        for eid, manual in WUXI_ELEMENTS.items():
            if eid in KNOWN_DIVERGENCE:
                _, model, why = KNOWN_DIVERGENCE[eid]
                self.assertEqual(got[eid], model, msg=f"{eid} 已知分歧应稳定为 {model}：{why}")
            else:
                self.assertEqual(got[eid], manual, msg=f"{eid} 与手工裁定不符")
        agree = len(WUXI_ELEMENTS) - len(KNOWN_DIVERGENCE)
        self.assertGreaterEqual(agree, 26, "要素一致数不应低于 26/29")

    def test_patches_applied(self):
        """增强层必须真的改到 4 处（薪酬审查倒序、劳动合同、险种、假别）。"""
        eids = {p.eid for p in self.wuxi.patches}
        self.assertEqual(eids, {"S1.1.2-5", "S1.1.1-4", "S1.1.4-1", "S1.1.4-4"})


class TestConfigConsistency(unittest.TestCase):
    """本包的指标名/权重必须与 ESG_insight 词表一致（防止两处各写一份漂移）。"""

    def test_specs_match_elements(self):
        from esg_s11_scorer.config import check_consistency
        self.assertEqual(check_consistency(), [])


class TestCalibrationFloor(unittest.TestCase):
    """10 份样本的要素一致率不得低于 75%（低于说明规则退化）。"""

    def test_element_agreement(self):
        from esg_s11_scorer.calibrate import calibrate_10
        res = calibrate_10()
        rate = res["hit"] / res["total"]
        self.assertGreaterEqual(rate, 0.75, f"要素一致率降到 {rate:.1%}")


if __name__ == "__main__":
    unittest.main(verbosity=2)