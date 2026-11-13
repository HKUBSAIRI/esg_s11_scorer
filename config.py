# -*- coding: utf-8 -*-
"""S1.1 打分模型 · 全局配置与指标规格。

设计要点
--------
1. **指标规格是数据，不是代码**：``INDICATORS`` 里每个指标只声明「分项 → 权重 → 判分函数名」，
   加新指标（S1.2 / S2 等）时在 ``SPECS`` 里追加一段规格 + 词表即可，编排层不改。
2. **判分函数名是字符串**（在 ``scoring.py`` 里注册），避免为每个指标写一套 if。
3. **所有阈值集中在 ``BANDS``**，权重与分项结构集中在 ``SPECS``——本文件是口径的唯一信源，
   文档只做解释、不另立数字，改口径只改这里。
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

MODEL_VERSION = "s11-model-1.5"
EXTRACT_VERSION = "extract-1.0"
SCHEMA_VERSION = "1"

# 管道各阶段的「规则版本」：指纹（缓存复用判据）用它们区分「哪一段变了」。
# 只增不改：改了某段的实现就必须 +0.1，否则旧产物会被误判为仍有效。
PARSE_VERSION = "parse-1.0"              # 页级文本的落地格式
BENCH_VERSION = "bench-1.1"              # 分位算法（1.1：分母只数该指标有值的家数）
TABLE_VERSION = "tables-1"               # gold 表字段

_SETTINGS = get_settings()

# 路径快照：来源 = settings（环境变量 > .env > 相对本仓库的默认值）
PROJECT_ROOT = _SETTINGS.project_root
INSIGHT_APP = _SETTINGS.insight_app              # 文本解析（s11_text）所在仓库（只读复用）
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
GAP_IMPROVE_FULL = 5.0                        # B 项 100 分档入口之一：改善率 ≥5%
# B 项 100 分档的另一条入口：本期差距已落进 A 的 100 分档（≤3%）直接记 100。
# 与 GAP_BANDS 的首档同源，改一处两处一起变（此前误用 GAP_NEAR_ZERO 的 2%，2–3% 的公司被错判掉档）。
GAP_B_FLOOR = GAP_BANDS[0][0]
GAP_NEAR_ZERO = 2.0                           # 仅用于「上期差距为 0」的补充规则：本期 ≤2% 记 100

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
                  "A/B 依赖同业分位；C 取定性判档（LLM/关键词）"),
    IndicatorSpec("S1.1.2", "薪酬保障", 30, "weighted",
                  {"A": 0.4, "B": 0.3, "C": 0.3}, "score_s112",
                  "A/B 依赖性别薪酬差距本期与上期；C 取定性判档"),
    IndicatorSpec("S1.1.3", "工作时间与休息休假保障", 15, "tiered", {}, "score_s113",
                  "三档制：100 分档要求制度 + 休假执行数据"),
    IndicatorSpec("S1.1.4", "员工福利与支持", 15, "tiered", {}, "score_s114",
                  "三档制：100 分档要求福利覆盖/使用证据"),
    IndicatorSpec("S1.1.5", "平等机会、反歧视与反骚扰", 15, "tiered", {}, "score_s115",
                  "三档制：100 分档要求反骚扰机制 + 事件处置证据"),
)

IND_NAMES = {s.ind: s.name for s in SPECS}
IND_WEIGHTS = {s.ind: s.weight for s in SPECS}
WEIGHT_SUM = sum(IND_WEIGHTS.values())
# 由 SPECS 拼出来，而不是手写「25+30+15+15+15」——权重一改，说明文字自动跟着改
WEIGHT_NOTE = (f"五项披露权重合计 {WEIGHT_SUM}%"
               f"（{'+'.join(str(IND_WEIGHTS[s.ind]) for s in SPECS)}）"
               + ("，无缺项；单项 N/A 时严格分按 0 计，故可能低于归一"
                  if WEIGHT_SUM == 100 else "，权重不足 100%，总分口径需确认"))


__all__ = [
    "MODEL_VERSION", "EXTRACT_VERSION", "SCHEMA_VERSION",
    "PARSE_VERSION", "BENCH_VERSION", "TABLE_VERSION",
    "PROJECT_ROOT", "INSIGHT_APP", "STANDARD_DOC", "REPORT_DIR", "DEFAULT_OUT",
    "BUILD_DIR",
    "DEFAULT_PDF_DIR", "DEFAULT_PRIOR_DIR", "DEFAULT_YEAR", "DEFAULT_BENCH",
    "DEFAULT_BENCH_CSV", "DEFAULT_OVERRIDE_CSV",
    "PERCENTILE_BANDS", "GAP_BANDS", "GAP_IMPROVE_FULL", "GAP_B_FLOOR", "GAP_NEAR_ZERO",
    "MIN_PEER_N",
    "IndicatorSpec", "SPECS", "IND_NAMES", "IND_WEIGHTS", "WEIGHT_SUM", "WEIGHT_NOTE",
]