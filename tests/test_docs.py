# -*- coding: utf-8 -*-
"""README 的口径表必须与 ``config.SPECS`` 逐格一致。

为什么值得一条测试
------------------
权重曾经散在几处（``config.SPECS`` / 要素词表 / 报告页脚字符串 / README）。把它们统一成
25 / 30 / 15 / 15 / 15 时，正是靠人工比对才发现漏改——而「文档写的口径」与「跑出来的分数」
不一致比分数错更难查，因为两边都自称权威。这里把 README 第 4 小节的表当接口钉住：
改了权重却忘了改文档，这条测试立刻红。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from esg_s11_scorer.config import IND_NAMES, IND_WEIGHTS, WEIGHT_SUM  # noqa: E402

README = Path(__file__).resolve().parents[1] / "README.md"
HEAD = "### 4. 权重与阈值速查"
# | S1.1.1 | 雇佣关系与就业保障 | 25 | A 40% + B 30% + C 30% | … |
ROW = re.compile(r"^\|\s*(S1\.1\.\d)\s*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|")


def _rows() -> list[tuple[str, str, int]]:
    """README 口径表里的 (指标, 名称, 权重)；表格缺失时直接判失败。"""
    lines = README.read_text(encoding="utf-8").splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith(HEAD)), None)
    if start is None:
        raise AssertionError(f"README 缺少小节：{HEAD}")
    out: list[tuple[str, str, int]] = []
    for line in lines[start + 1:]:
        if line.startswith("## ") or line.startswith("---"):
            break
        m = ROW.match(line)
        if m:
            out.append((m.group(1), m.group(2), int(m.group(3))))
    return out


class TestReadmeMatchesConfig(unittest.TestCase):
    def test_rows_cover_all_indicators(self):
        self.assertEqual(len(_rows()), len(IND_WEIGHTS), "口径表行数与指标数不符")

    def test_weights_match_specs(self):
        self.assertEqual({ind: w for ind, _, w in _rows()}, IND_WEIGHTS,
                         "README 权重与 config.SPECS 不一致")

    def test_names_match_specs(self):
        self.assertEqual({ind: n for ind, n, _ in _rows()}, IND_NAMES,
                         "README 指标名与 config.SPECS 不一致")

    def test_no_stale_weight_claims(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn(f"合计 {WEIGHT_SUM}%", text, "README 未写权重合计")
        for stale in ("10+5+15", "缺 15%", "85 分制"):
            self.assertNotIn(stale, text, f"README 仍残留旧口径：{stale}")


if __name__ == "__main__":
    unittest.main(verbosity=2)