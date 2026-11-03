# -*- coding: utf-8 -*-
"""S1.1 打分模型：读 PDF → 数值抽取 + 29 要素判定 → 五项指标打分 → 可解释报告。

七段管道（parse → facts → elements → prior → bench → score → report），每段产物落地到
``build/FY{year}/``，按指纹判断能否复用。

对外入口
--------
命令行：``python -m esg_s11_scorer``（路径与年度写在 ``.env`` 里，见 ``settings.py``）
编程：``from esg_s11_scorer.pipeline import run_pipeline``  ← 全流程，返回 ``PipelineResult``
      ``from esg_s11_scorer.pipeline import score_batch``   ← 兼容入口，只返回 ``(runs, bench)``
      ``from esg_s11_scorer.tables import load_batch``      ← 只读已发布的 gold 表

配置
----
**本机路径**（PDF 目录、输出目录、词表仓库、报告年度）走环境变量 / ``.env``，
不写进代码：``settings.build()`` 是纯函数式装配，``settings.get_settings()`` 取生效值，
``python -m esg_s11_scorer --show-config`` 打印每个值的来源。
**模型口径**（权重、阈值、判分规则）仍在 ``config.py``，一改必须升 ``MODEL_VERSION``。

依赖
----
解析（``s11_text``）与要素词法判定（``s11_judge`` / ``s11_elements``）直接复用
``50-应用/ESG_insight/app``（只读，不修改该仓库）；本包只新增数值抽取、分位对标、
打分编排与报告层。该目录由 ``settings.configure()`` 在导入期注入 ``sys.path``。
"""
from __future__ import annotations

from .settings import configure, get_settings

# 先定配置再导其它模块：config / pipeline 在导入期就要拿到绝对的词表仓库路径
SETTINGS = configure()

__version__ = "1.1"

__all__ = ["SETTINGS", "configure", "get_settings", "__version__"]