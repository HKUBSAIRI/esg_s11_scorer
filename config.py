# -*- coding: utf-8 -*-
"""S1.1 打分模型 · 全局配置与指标规格。

设计要点
--------
1. **指标规格是数据，不是代码**：``INDICATORS`` 里每个指标只声明「分项 → 权重 → 判分函数名」，
   加新指标（S1.2 / S2 等）时在 ``SPECS`` 里追加一段规格 + 词表即可，编排层不改。
2. **判分函数名是字符串**（在 ``scoring.py`` 里注册），避免为每个指标写一套 if。
3. **所有阈值集中在 ``BANDS``**，与《S1.1 指标与判断标准》一一对应，改口径只改这里。
4. **路径不写在这里**：PDF 目录、输出目录、词表仓库位置都是「本机事实」，由环境变量 /
   ``.env`` 提供（见 ``settings.py``）。本文件只保留「模型事实」与它们的默认快照。

    想让输入/输出换地方，别改代码：

        $env:ESG_PDF_DIR = "D:/ESG/FY2025"     # 或写进 esg_s11_scorer/.env
        python -m esg_s11_scorer --show-config # 看现在生效的是什么、从哪来

    下面的 ``REPORT_DIR`` / ``DEFAULT_OUT`` 等常量是**导入时的快照**，为兼容旧脚本而保留；
    新代码请用 ``settings.get_settings()``（能跟随 ``--env-file`` 变化）。

版本号规则
----------
``MODEL_VERSION`` 只增不改：改词表、阈值、判分规则、抽取正则都必须 +0.1，
否则同一版本号可能产出两套分数，导出文件无法对齐（与 ESG_insight 的 RULESET_VERSION 同源约定）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .settings import get_settings

MODEL_VERSION = "s11-model-1.0"
EXTRACT_VERSION = "extract-1.0"
SCHEMA_VERSION = "1"

# 管道各阶段的「规则版本」：指纹（缓存复用判据）用它们区分「哪一段变了」。
# 只增不改：改了某段的实现就必须 +0.1，否则旧产物会被误判为仍有效。
PARSE_VERSION = "parse-1.0"              # 页级文本的落地格式
JUDGE_PLUS_VERSION = "judge-plus-1.0"    # 增强层 7 条补丁
BENCH_VERSION = "bench-1.0"              # 分位算法
TABLE_VERSION = "tables-1"               # gold 表字段

_SETTINGS = get_settings()

# 路径快照：来源 = settings（环境变量 > .env > 相对本仓库的默认值）
PROJECT_ROOT = _SETTINGS.project_root
INSIGHT_APP = _SETTINGS.insight_app              # 指标标准原文与要素词表所在仓库（只读复用）
STANDARD_DOC = _SETTINGS.standard_doc
REPORT_DIR = _SETTINGS.report_root               # 报告根目录，其下有 FY2025/、FY2024/
DEFAULT_OUT = _SETTINGS.out_dir
BUILD_DIR = _SETTINGS.build_dir                  # 管道中间态（阶段产物 + gold 表）
DEFAULT_PDF_DIR = _SETTINGS.pdf_dir              # 默认输入（目录或单个 PDF）
DEFAULT_PRIOR_DIR = _SETTINGS.prior_dir          # 默认上一年目录；None = 自动取同级 FY{year-1}
DEFAULT_YEAR = _SETTINGS.year
DEFAULT_BENCH = _SETTINGS.bench_mode
DEFAULT_BENCH_CSV = _SETTINGS.bench_csv
DEFAULT_OVERRIDE_CSV = _SETTINGS.override_csv

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
# 由 SPECS 拼出来，而不是手写「25+30+10+5+15」——权重一改，说明文字自动跟着改
WEIGHT_NOTE = (f"五项披露权重合计 {WEIGHT_SUM}%"
               f"（{'+'.join(str(IND_WEIGHTS[s.ind]) for s in SPECS)}），"
               "标准原文缺 15%，总分口径需确认")


def check_consistency() -> list[str]:
    """检查本包的指标名/权重与 ESG_insight 的 ``s11_elements`` 是否一致。

    两边各写一份会出现「改了词表忘了改规格」的静默漂移：词表那边判 29 要素，这边按
    SPECS 装配分数，名字/权重不一致时报告会显示旧名、总分口径会对不上。这里返回差异
    清单（空 = 一致），由 CLI 启动时提示、由测试断言，**不**在 import 期抛错——
    保持「词表仓库可独立演进」的边界。
    """
    try:
        from s11_elements import IND_NAME, IND_WEIGHT      # 依赖 __init__ 已注入路径
    except Exception as exc:                                # 词表仓库不可用
        return [f"无法导入 s11_elements：{exc}"]
    diffs: list[str] = []
    for ind, name in IND_NAMES.items():
        if IND_NAME.get(ind) and IND_NAME[ind] != name:
            diffs.append(f"{ind} 名称不一致：本包「{name}」vs 词表「{IND_NAME[ind]}」")
        if IND_WEIGHT.get(ind) is not None and IND_WEIGHT[ind] != IND_WEIGHTS[ind]:
            diffs.append(f"{ind} 权重不一致：本包 {IND_WEIGHTS[ind]} vs 词表 {IND_WEIGHT[ind]}")
    return diffs


__all__ = [
    "MODEL_VERSION", "EXTRACT_VERSION", "SCHEMA_VERSION",
    "PARSE_VERSION", "JUDGE_PLUS_VERSION", "BENCH_VERSION", "TABLE_VERSION",
    "PROJECT_ROOT", "INSIGHT_APP", "STANDARD_DOC", "REPORT_DIR", "DEFAULT_OUT",
    "BUILD_DIR",
    "DEFAULT_PDF_DIR", "DEFAULT_PRIOR_DIR", "DEFAULT_YEAR", "DEFAULT_BENCH",
    "DEFAULT_BENCH_CSV", "DEFAULT_OVERRIDE_CSV",
    "PERCENTILE_BANDS", "GAP_BANDS", "GAP_IMPROVE_FULL", "GAP_NEAR_ZERO", "MIN_PEER_N",
    "IndicatorSpec", "SPECS", "IND_NAMES", "IND_WEIGHTS", "WEIGHT_SUM", "WEIGHT_NOTE",
    "check_consistency",
]