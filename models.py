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

    def to_dict(self) -> dict:
        return {"field": self.field, "value": self.value, "year": self.year,
                "page": self.page, "method": self.method, "quote": self.quote,
                "confidence": self.confidence, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "NumFact":
        return cls(field=str(d["field"]), value=float(d["value"]),
                   year=None if d.get("year") is None else int(d["year"]),
                   page=int(d.get("page") or 0), method=str(d.get("method") or ""),
                   quote=str(d.get("quote") or ""),
                   confidence=str(d.get("confidence") or "低"),
                   note=str(d.get("note") or ""))


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
        """序列化成阶段产物（``build/FY{year}/{code}/facts.json``）。

        ``best`` 用「facts 下标」存，而不是再抄一份数值：落地文件与内存对象一一对应，
        否则同一个 NumFact 会存成两份，取值时到底是哪一份就成了新问题。
        """
        idx = {id(f): i for i, f in enumerate(self.facts)}
        return {
            "report_year": self.report_year,
            "denom_note": self.denom_note,
            "warnings": list(self.warnings),
            "facts": [f.to_dict() for f in self.facts],
            "best": {fld: {str(y): idx[id(f)] for y, f in m.items()}
                     for fld, m in self.best.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Extraction":
        facts = [NumFact.from_dict(x) for x in d.get("facts") or []]
        best: dict[str, dict[int, NumFact]] = {}
        for fld, m in (d.get("best") or {}).items():
            try:
                best[fld] = {int(y): facts[i] for y, i in m.items() if 0 <= i < len(facts)}
            except (AttributeError, TypeError, ValueError):
                continue
        ex = cls(report_year=int(d.get("report_year") or 0),
                 facts=facts,
                 denom_note=str(d.get("denom_note") or ""),
                 warnings=list(d.get("warnings") or []),
                 best=best)
        if not ex.best:                     # 老产物没存 best：按 facts 兜出来
            for f in facts:
                if f.year is not None:
                    ex.best.setdefault(f.field, {}).setdefault(f.year, f)
        return ex


# ---------------------------------------------------------------- 判定补丁
@dataclass
class Patch:
    """增强层对词法判定的一次覆盖，留痕用（谁、从什么改成什么、为什么）。"""
    eid: str
    name: str
    before: str
    after: str
    reason: str

    def to_dict(self) -> dict:
        return {"eid": self.eid, "name": self.name, "before": self.before,
                "after": self.after, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict) -> "Patch":
        return cls(eid=str(d.get("eid") or ""), name=str(d.get("name") or ""),
                   before=str(d.get("before") or ""), after=str(d.get("after") or ""),
                   reason=str(d.get("reason") or ""))


# ---------------------------------------------------------------- 要素判定
def render_quote(page: int, sent: str, suspect: bool = False) -> str:
    """证据句的展示格式（唯一实现）。

    改造前这段在 ``ElementResult.top_quote()`` 里，属于 ESG_insight 仓库；落地成产物后
    证据句必须能独立存活（不能依赖上游对象），所以在这里固定下来。两边格式必须一致，
    否则「内存跑出的报告」和「读表跑出的报告」会给出不同的证据串。
    """
    text = sent if len(sent) <= 220 else sent[:220] + "…"
    return f"[p{page}] {text}{'（证据可疑）' if suspect else ''}"


@dataclass
class ElementJudgement:
    """一个 29 要素判定的可落地形态（``s11_judge.ElementResult`` 的稳定快照）。

    为什么转成自有类型
    ------------------
    下游（打分层、报告层、表）只要 ``verdict`` / ``name`` / ``reason`` / 证据句，不需要
    ``hits`` 这种上游内部结构。转完之后产物里存的是**已渲染好的证据串**，重建时不再
    import ``s11_judge``——判定层换了实现，历史产物依然读得出来。
    """
    eid: str
    ind: str
    name: str
    verdict: str
    reason: str = ""
    confidence: float = 0.0
    quotes: list[str] = field(default_factory=list)
    doubt: bool = False              # 全部证据句都没有人员词 → 证据可疑（只提示，不影响判定）

    def top_quote(self, n: int = 1) -> str:
        return self.quotes[n - 1] if len(self.quotes) >= n else ""

    def to_dict(self) -> dict:
        return {"eid": self.eid, "ind": self.ind, "name": self.name,
                "verdict": self.verdict, "reason": self.reason,
                "confidence": self.confidence, "quotes": list(self.quotes),
                "doubt": self.doubt}

    @classmethod
    def from_dict(cls, d: dict) -> "ElementJudgement":
        return cls(eid=str(d.get("eid") or ""), ind=str(d.get("ind") or ""),
                   name=str(d.get("name") or ""), verdict=str(d.get("verdict") or "否"),
                   reason=str(d.get("reason") or ""),
                   confidence=float(d.get("confidence") or 0.0),
                   quotes=[str(q) for q in (d.get("quotes") or [])],
                   doubt=bool(d.get("doubt")))

    @classmethod
    def from_result(cls, r) -> "ElementJudgement":
        """从 ``s11_judge.ElementResult`` 转换（唯一接触上游类型的入口）。"""
        return cls(eid=r.eid, ind=getattr(r, "ind", ""), name=r.name,
                   verdict=r.verdict, reason=r.reason,
                   confidence=float(getattr(r, "confidence", 0.0) or 0.0),
                   quotes=[render_quote(h.page, h.sent, h.suspect) for h in r.hits],
                   doubt=bool(getattr(r, "doubt", False)))


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
        # 分制由各项权重现算，不写死「85」：权重口径一变，导出字段名跟着变
        scale = sum(i.weight for i in self.indicators.values())
        return {
            "代码": self.code, "公司": self.name, "报告年度": self.report_year,
            "文件": self.file,
            f"总分（严格/{scale:.0f} 分制）": self.total_strict,
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
    res: dict[str, "ElementJudgement"] = field(default_factory=dict)
    patches: list[Patch] = field(default_factory=list)
    has_voluntary: bool = False          # 是否披露自愿离职口径（判定阶段产出，打分要用）
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


# ---------------------------------------------------------------- 已发布视图
@dataclass
class CompanyView:
    """一家公司的**只读视图**：全部字段来自已发布的表，报告层只认它。

    与 ``CompanyRun`` 的区别在于「谁能改」：``CompanyRun`` 是编排过程中的可变中间态
    （会话里挂着 Doc、阶段结果、状态位）；``CompanyView`` 建出来之后只有读操作，
    报告层拿不到任何改写入口——这就是「报告只渲染、不计算」的落地方式。
    """
    code: str
    name: str
    file: str
    card: ScoreCard
    facts: list[NumFact] = field(default_factory=list)
    elements: dict[str, ElementJudgement] = field(default_factory=dict)
    patches: list[Patch] = field(default_factory=list)
    pct_a: float | None = None          # 流失率分位（低者优）
    pct_b: float | None = None          # 改善率分位（高者优）
    band_a: int | None = None
    band_b: int | None = None


@dataclass
class Failure:
    """一份没能出分的输入（解析失败/无数据），台账里要写明原因，不能静默消失。"""
    code: str
    file: str
    status: str
    error: str = ""


@dataclass
class Batch:
    """一个年度批次的已发布结果：报告层的唯一输入。"""
    year: int
    views: list[CompanyView] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    bench_kind: str = "none"
    bench_note: str = ""
    bench_n: int = 0
    bench_snapshot: str = ""
    model_version: str = ""

    @property
    def total(self) -> int:
        return len(self.views) + len(self.failures)