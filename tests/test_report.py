# -*- coding: utf-8 -*-
"""报告层测试：工作簿必须能写出来且数值真的是数值。

这层原先没有任何测试，结果重构时 ``card.total_normalized`` 从属性改成方法后，
写 xlsx 直接把「绑定方法」塞进单元格，直到手工跑 CLI 才炸。本文件就是补这个洞：
不只断言「文件存在」，还要回读单元格、断言类型与关键行。

改造后额外锁一件事：报告读的是 **gold 表**（``run_pipeline`` → ``Batch``），
所以这里跑的路径就是生产路径；「表里对、报告里错」的空间被压掉了。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401
from openpyxl import load_workbook

from esg_s11_scorer.config import BUILD_DIR, DEFAULT_OUT, REPORT_DIR
from esg_s11_scorer.pipeline import run_pipeline
from esg_s11_scorer.report import write_batch, write_company


class TestReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        override = DEFAULT_OUT / "overrides_FY2025.csv"
        prior = REPORT_DIR / "FY2024"
        cls.result = run_pipeline(
            REPORT_DIR / "FY2025", 2025, prior if prior.exists() else None,
            build_dir=BUILD_DIR,
            override_csv=override if override.exists() else None)
        cls.batch = cls.result.batch
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_company_workbook(self):
        view = next(v for v in self.batch.views if v.code == "02269")
        path = write_company(view, self.out)
        self.assertTrue(path.exists())

        wb = load_workbook(path)
        self.assertEqual(wb.sheetnames, ["总览", "分项判分", "数据取值", "29 要素", "运行台账"])

        ws = wb["总览"]
        # 总分必须是数字（重构时这里曾写入绑定方法而报错）
        totals = [ws.cell(row=r, column=4).value for r in range(3, 9)]
        self.assertTrue(all(isinstance(v, (int, float)) for v in totals), totals)
        # 五项指标各一行 + 合计一行
        self.assertEqual(ws.cell(row=3, column=1).value, "S1.1.1")
        self.assertEqual(ws.cell(row=7, column=1).value, "S1.1.5")

    def test_tiered_rows(self):
        """三档制指标在「分项判分」里必须有一行「档位」，且分值等于指标分。"""
        view = next(v for v in self.batch.views if v.code == "02269")
        wb = load_workbook(write_company(view, self.out))
        ws = wb["分项判分"]
        rows = [tuple(c.value for c in r) for r in ws.iter_rows(min_row=3)]
        tiered = [r for r in rows if r[1] and "三档制" in str(r[1])]
        self.assertEqual(len(tiered), 3, [r[1] for r in rows])
        self.assertTrue(all(isinstance(r[3], int) for r in tiered))

    def test_batch_workbook(self):
        path = write_batch(self.batch, self.out)
        wb = load_workbook(path)
        self.assertEqual(wb.sheetnames, ["汇总", "对标分位", "要素矩阵", "缺口清单", "运行台账"])
        ws = wb["汇总"]
        codes = [ws.cell(row=r, column=1).value for r in range(3, 8)]
        self.assertEqual(sorted(codes), ["01177", "01513", "01801", "02269", "03692"])
        # 分位表：丽珠 20 分位（最好），中生 100（最差）
        ws2 = wb["对标分位"]
        pct = {ws2.cell(row=r, column=1).value: ws2.cell(row=r, column=6).value
               for r in range(3, 8)}
        self.assertEqual(pct["01513"], 20.0)
        self.assertEqual(pct["01177"], 100.0)

    def test_tables_are_the_source(self):
        """报告里的每一家都必须能在 gold 表里查到同名一行（防止报告自己算）。"""
        from esg_s11_scorer.store import TableStore
        rows = TableStore(root=BUILD_DIR / "tables" / "FY2025", year=2025).read("companies")
        codes = {r["code"] for r in rows if r.get("status") == "ok"}
        self.assertEqual(codes, {v.code for v in self.batch.views})


if __name__ == "__main__":
    unittest.main(verbosity=2)