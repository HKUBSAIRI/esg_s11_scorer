# -*- coding: utf-8 -*-
"""S1.1 打分模型：读 PDF → 数值抽取 + 29 要素判定 → 五项指标打分 → 可解释报告。

对外入口
--------
命令行：``python -m esg_s11_scorer --pdf <目录或文件> --out <输出目录>``
编程：``from esg_s11_scorer.pipeline import score_batch``

依赖
----
解析（``s11_text``）与要素词法判定（``s11_judge`` / ``s11_elements``）直接复用
``50-应用/ESG_insight/app``（只读，不修改该仓库）；本包只新增数值抽取、分位对标、
打分编排与报告层。
"""
from __future__ import annotations

import sys

from .config import INSIGHT_APP

if str(INSIGHT_APP) not in sys.path:
    sys.path.insert(0, str(INSIGHT_APP))

__version__ = "1.0"