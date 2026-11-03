# -*- coding: utf-8 -*-
"""领域模型：全包共享的数据结构，只放**数据与只读派生**，不放流程。

为什么单独一层
--------------
重构前 ``CompanyRun`` 住在 ``pipeline``、``ScoreCard`` 住在 ``scoring``、``Extraction`` 住在
``extract``，导致「报告层要从编排层 import 一个 dataclass」这种反向依赖。集中到本模块后，
依赖关系变成：

    models  ←  extract / judge_plus / bench / scoring / pipeline / report / calibrate

即所有层都只依赖 models，彼此之间不再为了「借一个数据结构」而互相 import。

约定
----
- 本模块**不 import 本包任何模块**（除 typing），因此永不参与循环依赖。
- 外部类型（``Doc`` / ``ElementResult``）只在类型检查时引入，运行时不产生耦合。
- 派生属性（如 ``IndScore.strict``）必须是纯函数，不得有副作用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:                                   # 仅类型检查，运行时不 import
    from s11_judge import ElementResult
    from s11_text import Doc

NA = None


# ---------------------------------------------------------------- 数值抽取
@dataclass
class NumFact:
    """一个抽到的数值：值 + 归属年份 + 来源位置 + 抽取方式 + 置信度。"""
    field: str
    value: float
    year: int | None
    page: int
    method: str          # table:year-align / table:single / sentence[:xx] / override
    quote: str
    confidence: str      # 高 / 中 / 低 / 人工
    note: str = ""


@dataclass
class Extraction:
    """一份报告的数值抽取结果（含诊断信息）。"""
    report_year: int
    facts: list[NumFact] = field(default_factory=list)
    denom_note: str = ""
    warnings: list[str] = field(default_factory=list)
    best: dict[str, dict[int, NumFact]] = field(default_factory=dict)

    def get(self, fld: str, year: int | None = None) -> NumFact | None:
        m = self.best.get(fld, {})
        if year is not None:
            return m.get(year)
        return m[max(m)] if m else None

    def value(self, fld: str, year: int | None = None) -> float | None:
        f = self.get(fld, year)
        return None if f is None else f.value

    def to_dict(self) -> dict:
        return {
            "report_year": self.report_year,
            "denom_note": self.denom_note,
            "warnings": self.warnings,
            "facts": [
                {"field": f.field, "year": f.year, "value": f.value, "page": f.page,
                 "method": f.method, "confidence": f.confidence,
                 "note": f.note, "quote": f.quote}
                for f in self.facts
            ],
        }


# ---------------------------------------------------------------- 判定补丁
@dataclass
class Patch:
    """增强层对词法判定的一次覆盖，留痕用（谁、从什么改成什么、为什么）。"""
    eid: str
    name: str
    before: str
    after: str
    reason: str


# ---------------------------------------------------------------- 打分
@dataclass
class Component:
    """加权指标的一个分项（A/B/C）；``score=None`` 表示不可评（N/A），不是 0。"""
    key: str
    name: str
    weight: float
    score: int | None
    note: str
    evidence: str = ""

    @property
    def available(self) -> bool:
        return self.score is not None


@dataclass
class IndScore:
    """单个指标得分：加权制给 components，三档制给 raw。"""
    ind: str
    name: str
    weight: float
    kind: str                                   # weighted / tiered
    components: list[Component] = field(default_factory=list)
    raw: int | None = None
    note: str = ""

    @property
    def strict(self) -> float:
        """严格口径：不可评分项按 0 计入（与历史 10 样本口径可比）。"""
        if self.kind == "tiered":
            return float(self.raw or 0)
        return round(sum(c.weight * (c.score or 0) for c in self.components), 1)

    @property
    def reachable(self) -> float:
        """可达上限：可观测权重 ×100；三档制恒为 100。"""
        if self.kind == "tiered":
            return 100.0
        return round(sum(c.weight for c in self.components if c.available) * 100, 1)

    @property
    def normalized(self) -> float | None:
        """归一：只按可观测权重折算成百分制；全不可观测时 None。"""
        if self.kind == "tiered":
            return float(self.raw or 0)
        obs = sum(c.weight for c in self.components if c.available)
        if obs <= 0:
            return None
        got = sum(c.weight * (c.score or 0) for c in self.components if c.available)
        return round(got / obs, 1)

    @property
    def band(self) -> str:
        v = self.normalized
        if v is None:
            return "不可评"
        return "达标" if v >= 80 else ("部分达标" if v >= 40 else "未达标")

    def to_dict(self) -> dict:
        return {
            "指标": self.ind, "名称": self.name, "权重": self.weight,
            "得分（严格）": self.strict, "得分（归一）": self.normalized,
            "可达上限": self.reachable, "档位": self.band, "说明": self.note,
            "分项": [{"分项": c.key, "名称": c.name, "权重": c.weight,
                      "得分": c.score, "说明": c.note, "证据": c.evidence}
                     for c in self.components],
        }


@dataclass
class ScoreData:
    """参与打分的量化事实（替代原来的中文键自由 dict）。

    重构前这里是 ``dict[str, Any]``，报告层靠 ``d["流失率（本期）"]`` 取值——键改一个字就
    静默变 None。改成显式字段后，拼错即 AttributeError，且在编辑器里可跳转。
    """
    report_year: int
    turnover: float | None = None
    prior_turnover: float | None = None
    improvement: float | None = None
    leavers: float | None = None
    headcount: float | None = None
    gap: float | None = None
    gap_basis: str = ""                 # median / avg
    prior_gap: float | None = None
    cb_coverage: float | None = None
    turnover_denom_note: str = ""
    bench_note: str = ""
    prior_source: str = ""
    turnover_quote: str = ""            # 抽取该数值时的原文（A/B 项的证据引用）
    gap_quote: str = ""

    def to_dict(self) -> dict:
        """导出用中文键（JSON / 台账），字段名与报告表头一致。"""
        return {
            "报告年度": self.report_year,
            "流失率（本期）": self.turnover,
            "流失率（上期）": self.prior_turnover,
            "流失率改善率": self.improvement,
            "离职人数": self.leavers,
            "员工总数": self.headcount,
            "性别薪酬差距": self.gap,
            "性别薪酬差距口径": {"median": "中位数", "avg": "平均值"}.get(self.gap_basis, self.gap_basis),
            "性别薪酬差距（上期）": self.prior_gap,
            "集体谈判覆盖率": self.cb_coverage,
            "离职率分母口径": self.turnover_denom_note,
            "对标池": self.bench_note,
            "上期值来源": self.prior_source,
        }


@dataclass
class ScoreCard:
    """一家公司的完整打分结果。"""
    code: str
    name: str
    report_year: int
    file: str
    indicators: dict[str, IndScore] = field(default_factory=dict)
    data: ScoreData | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def total_strict(self) -> float:
        return round(sum(self.indicators[k].weight * self.indicators[k].strict
                         for k in self.indicators) / 100, 1)

    def total_normalized(self, weight_sum: float) -> float:
        """百分制总分：需要权重合计（由 config 提供，避免此处再算一遍口径）。"""
        if not weight_sum:
            return 0.0
        return round(sum(self.indicators[k].weight * (self.indicators[k].normalized or 0)
                         for k in self.indicators) / weight_sum, 1)

    def to_dict(self, weight_sum: float) -> dict:
        return {
            "代码": self.code, "公司": self.name, "报告年度": self.report_year,
            "文件": self.file,
            "总分（严格/85 分制）": self.total_strict,
            "总分（归一/百分制）": self.total_normalized(weight_sum),
            "指标": [self.indicators[k].to_dict() for k in self.indicators],
            "数据": self.data.to_dict() if self.data else {},
            "提示": self.warnings,
        }


# ---------------------------------------------------------------- 对标
@dataclass
class PeerRow:
    """对标池里的一行（一家公司一个年度）。"""
    code: str
    year: int
    turnover: float | None
    prior_turnover: float | None
    industry: str = ""
    source: str = ""


# ---------------------------------------------------------------- 编排
@dataclass
class CompanyRun:
    """一次批处理里的一家公司：输入 + 中间结果 + 最终分数。

    上期值的三级回退在这里显式建模（``prior_*_fallback``），不再用 ``setattr`` 挂影子属性。
    """
    code: str
    name: str
    year: int
    pdf: Path
    doc: "Doc | None" = None
    ex: Extraction | None = None
    res: dict[str, "ElementResult"] = field(default_factory=dict)
    patches: list[Patch] = field(default_factory=list)
    card: ScoreCard | None = None
    status: str = "ok"
    error: str = ""
    prior_source: str = ""              # 本报告附录 / 上一年报告 / 人工填入 / 缺失
    prior_turnover_fallback: float | None = None   # 上一年报告里取到的上期流失率
    prior_gap_fallback: float | None = None        # 上一年报告里取到的上期薪酬差距

    def prior_turnover(self, report_year: int) -> float | None:
        """本期报告给的上期值优先，其次上一年报告，最后 None（不猜）。"""
        if self.ex is None:
            return self.prior_turnover_fallback
        v = self.ex.value("turnover", report_year - 1)
        return v if v is not None else self.prior_turnover_fallback

    def prior_gap(self, report_year: int) -> float | None:
        if self.ex is None:
            return self.prior_gap_fallback
        v = self.ex.value("gender_gap", report_year - 1)
        if v is None:
            v = self.ex.value("gender_gap_avg", report_year - 1)
        return v if v is not None else self.prior_gap_fallback