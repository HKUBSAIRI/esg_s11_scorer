# -*- coding: utf-8 -*-
"""S1.1 打分模型 · 全局配置与指标规格。

设计要点
--------
1. **指标规格是数据，不是代码**：``INDICATORS`` 里每个指标只声明「分项 → 权重 → 判分函数名」，
   加新指标（S1.2 / S2 等）时在 ``SPECS`` 里追加一段规格 + 词表即可，编排层不改。
2. **判分函数名是字符串**（在 ``scoring.py`` 里注册），避免为每个指标写一套 if。
3. **所有阈值集中在 ``BANDS``**，与《S1.1 指标与判断标准》一一对应，改口径只改这里。

版本号规则
----------
``MODEL_VERSION`` 只增不改：改词表、阈值、判分规则、抽取正则都必须 +0.1，
否则同一版本号可能产出两套分数，导出文件无法对齐（与 ESG_insight 的 RULESET_VERSION 同源约定）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MODEL_VERSION = "s11-model-1.0"
EXTRACT_VERSION = "extract-1.0"
SCHEMA_VERSION = "1"

# 指标标准原文（唯一信源）与要素词表所在仓库（只读复用，不改动该仓库）
PROJECT_ROOT = Path(__file__).resolve().parents[1].parent
INSIGHT_APP = PROJECT_ROOT / "50-应用" / "ESG_insight" / "app"
STANDARD_DOC = PROJECT_ROOT / "50-应用" / "sample" / "02-指标标准" / "S1.1_指标与判断标准.md"
REPORT_DIR = PROJECT_ROOT / "50-应用" / "good_sample" / "分析指标" / "ESG报告"
DEFAULT_OUT = PROJECT_ROOT / "50-应用" / "good_sample" / "分析指标" / "打分结果"

# ---------------------------------------------------------------- 量表阈值
# 分位档（A/B 共用）：score = 100 / 75 / 50 / 25 / 0
PERCENTILE_BANDS = ((20.0, 100), (40.0, 75), (60.0, 50), (80.0, 25), (100.0, 0))

# S1.1.2 A 项：性别薪酬差距绝对值
GAP_BANDS = ((3.0, 100), (15.0, 50))          # ≤3% → 100；≤15% → 50；其余 0
GAP_IMPROVE_FULL = 5.0                        # 改善率 ≥5% → B=100
GAP_NEAR_ZERO = 2.0                           # 本期差距 ≤2% 亦可 B=100

# 批内分位的最小样本量：少于这个数不计算分位（防范 n=3 的分位噪声）
MIN_PEER_N = 3

# ---------------------------------------------------------------- 指标规格
@dataclass(frozen=True)
class IndicatorSpec:
    ind: str
    name: str
    weight: float
    kind: str                                  # weighted / tiered
    weights: dict[str, float] = field(default_factory=dict)
    scorer: str = ""                           # scoring.py 中注册的函数名
    note: str = ""


SPECS: tuple[IndicatorSpec, ...] = (
    IndicatorSpec("S1.1.1", "雇佣关系与就业保障", 25, "weighted",
                  {"A": 0.4, "B": 0.3, "C": 0.3}, "score_s111",
                  "A/B 依赖同业分位；C 依赖 29 要素的 -4/-5/-1"),
    IndicatorSpec("S1.1.2", "薪酬保障", 30, "weighted",
                  {"A": 0.4, "B": 0.3, "C": 0.3}, "score_s112",
                  "A/B 依赖性别薪酬差距本期与上期；C 依赖 -2/-3/-5"),
    IndicatorSpec("S1.1.3", "工作时间与休息休假保障", 10, "tiered", {}, "score_s113",
                  "100 分档唯一入口 = 休假执行数据（-5）"),
    IndicatorSpec("S1.1.4", "员工福利与支持", 5, "tiered", {}, "score_s114",
                  "100 分档唯一入口 = 福利覆盖数据（-5）"),
    IndicatorSpec("S1.1.5", "反歧视与反骚扰", 15, "tiered", {}, "score_s115",
                  "100 分档 = 反骚扰机制（-4）+ 事件统计（-6）"),
)

IND_NAMES = {s.ind: s.name for s in SPECS}
IND_WEIGHTS = {s.ind: s.weight for s in SPECS}
WEIGHT_SUM = sum(IND_WEIGHTS.values())
WEIGHT_NOTE = (f"五项披露权重合计 {WEIGHT_SUM}%（25+30+10+5+15），"
               "标准原文缺 15%，总分口径需确认")

__all__ = [
    "MODEL_VERSION", "EXTRACT_VERSION", "SCHEMA_VERSION",
    "PROJECT_ROOT", "INSIGHT_APP", "STANDARD_DOC", "REPORT_DIR", "DEFAULT_OUT",
    "PERCENTILE_BANDS", "GAP_BANDS", "GAP_IMPROVE_FULL", "GAP_NEAR_ZERO", "MIN_PEER_N",
    "IndicatorSpec", "SPECS", "IND_NAMES", "IND_WEIGHTS", "WEIGHT_SUM", "WEIGHT_NOTE",
]