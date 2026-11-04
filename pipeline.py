# -*- coding: utf-8 -*-
"""编排层：一次批处理 = 七段，每段产物落地、可复用、可单独重跑。

    parse(code)      PDF → 页级文本                    （最贵，必须能存）
    facts(code)      文本 → 数值 + 人工兜底              （依赖 parse）
    elements(code)   文本 → 29 要素判定 + 增强补丁        （依赖 parse）
    prior(code)      上一年 facts → 上期值三级回退        （跨年边！）
    bench(year)      全批数值 → 对标池                   ← 唯一的屏障（全对全）
    score(code)      数值+要素+对标 → 记分卡              （依赖 facts/elements/prior/bench）
    publish(year)    产物 → gold 表
    report(year)     gold 表 → 工作簿

为什么要两遍
-----------
A/B 是**相对**分位：必须先拿到同批所有公司的流失率，才能算某一家排第几。所以
``bench`` 是一个真实的年批次屏障，单份 PDF 无法自洽。改造前后的区别只在于：
改造前这个屏障藏在一个函数调用顺序里，改造后它是一段显式的、有产物的阶段。

跨年依赖也是真实的边
------------------
FY2025 的 ``score`` 需要 FY2024 的数值。改造前是「回到 FY2024 目录把 PDF 再解析一遍」，
现在是「读 FY2024 已落地的事实」：同一份 PDF 全生命周期只解析一次，且两年的抽取
结果不可能漂移。

哪些复用、哪些必须重算
--------------------
判据在 ``store.py`` 的指纹里，这里只说结论：改阈值 → 只有 score 重算；改抽取正则 →
facts 之后重算、parse 复用；换一份 PDF → 该公司从 parse 起全部重算；加一家公司 →
对标快照变，全部 score 重算（这是批内分位的固有语义，不是缺陷，快照 id 让它可见）。

上期值三级回退（实现在 ``stages.resolve_prior`` / ``prior_values``）
-----------------------------------------------------------------
1. 本份报告的附录表里直接有上一年度列 → 直接用；
2. 没有 → 去 ``--prior-dir``（默认同级 FY{year-1}）找同代码报告的**已落地事实**；
3. 仍没有 → 记 N/A，B 项不评（不拿「行业均值」冒充上期）。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import s11_elements
import s11_judge
import s11_text
from s11_text import PDFLoadError, load_pdf

from . import bench as bench_mod
from . import blind as blind_mod
from . import config as config_mod
from . import extract as extract_mod
from . import judge_plus as judge_plus_mod
from . import metrics as metrics_mod
from . import qualitative as qual_mod
from . import scoring as scoring_mod
from . import stages, tables
from .bench import Bench, from_rows, load_csv
from .config import (BENCH_VERSION, EXTRACT_VERSION, JUDGE_PLUS_VERSION,
                     MODEL_VERSION, PARSE_VERSION, TABLE_VERSION)
from .judge_plus import adjudicate
from .llm import LLMError
from .llm import from_settings as llm_from_settings
from .metrics import improvement
from .models import (Batch, CompanyRun, ElementJudgement, Extraction, Patch,
                     PeerRow, ScoreCard, ScoreData)
from .qualitative import Verdict
from .rubric import QUALITATIVE_PARTS, RUBRICS, rubric_hash
from .scoring import build_card
from .settings import get_settings
from .store import (ArtifactStore, TableStore, combine, digest_file, short,
                    source_digest)

CODE_RE = re.compile(r"^(\d{5})[_-]([A-Za-z0-9_]+)")
PRODUCER = f"esg_s11_scorer/{MODEL_VERSION}"


# ---------------------------------------------------------------- 小工具
def _parse_name(token: str) -> str:
    return token.replace("_", " ").strip()


def scan(pdf: str | Path) -> list[Path]:
    """PDF 文件或目录 → PDF 列表（目录递归）。"""
    p = Path(pdf)
    if p.is_file():
        return [p]
    return sorted(x for x in p.rglob("*.pdf") if x.is_file())


def unit_identity(path: str | Path) -> tuple[str, str]:
    """``02269_WUXI_BIO_ESG_FY2025.pdf`` → ``("02269", "WUXI BIO ESG FY2025")``。"""
    p = Path(path)
    m = CODE_RE.match(p.name)
    return (m.group(1), _parse_name(m.group(2))) if m else (p.stem[:5], p.stem)


@dataclass
class StageStat:
    """一段的复用/重算计数，CLI 台账靠它说话。"""
    name: str
    reused: int = 0
    rebuilt: int = 0
    seconds: float = 0.0

    def touch(self, reused: bool, dt: float = 0.0) -> None:
        if reused:
            self.reused += 1
        else:
            self.rebuilt += 1
            self.seconds += dt

    @property
    def label(self) -> str:
        if not (self.reused or self.rebuilt):
            return "—"
        txt = f"复用 {self.reused} / 重算 {self.rebuilt}"
        return txt + (f"（{self.seconds:.1f}s）" if self.rebuilt else "")


def _stage(store: ArtifactStore, name: str, code: str, key: str, compute,
           *, force: bool = False, stats: dict[str, StageStat] | None = None) -> dict:
    """「指纹一致就复用，否则重算并落盘」——整条链路里唯一的复用判据入口。"""
    if not force and store.fresh(code, name, key):
        if stats and name in stats:
            stats[name].touch(True)
        return store.payload(code, name) or {}
    t0 = time.perf_counter()
    payload = compute()
    store.write(code, name, key, payload, producer=PRODUCER)
    if stats and name in stats:
        stats[name].touch(False, time.perf_counter() - t0)
    return payload


# ---------------------------------------------------------------- 单公司各段
def parse_unit(store: ArtifactStore, code: str, pdf: Path, *,
               force: bool = False, stats=None) -> dict:
    key = combine(digest_file(pdf), PARSE_VERSION, source_digest(stages, s11_text))
    return _stage(store, "parse", code, key, lambda: stages.run_parse(pdf),
                  force=force, stats=stats)


def facts_unit(store: ArtifactStore, code: str, pdf: Path, year: int, *,
               force: bool = False, stats=None, overrides=(),
               parse_payload: dict | None = None) -> dict:
    """抽数 + 人工兜底。parse 的指纹进 key：文本变了，数值必须重抽。

    override 只把**本公司那几行**放进 key（而不是整张表的字节）：给 A 家补一个数，
    不该让 B 家跟着重抽。
    """
    parse_key = store.key(code, "parse") or "-"
    mine = [[r["code"], r["field"], r["year"], r["value"]] for r in overrides
            if r["code"] == code]
    key = combine(parse_key, EXTRACT_VERSION, source_digest(extract_mod, stages), mine)

    def compute() -> dict:
        doc_payload = parse_payload if parse_payload is not None else store.payload(code, "parse")
        ex = stages.run_extract(doc_payload or {}, pdf.name, year)
        stages.apply_overrides(ex, list(overrides), code)
        return ex.to_dict()

    return _stage(store, "facts", code, key, compute, force=force, stats=stats)


def elements_unit(store: ArtifactStore, code: str, *, force: bool = False,
                  stats=None, report_year: int = 0) -> dict:
    """要素判定。词表仓库（``s11_elements``/``s11_judge``）与增强层都进指纹：
    词表一动，判定与分数就该重算。"""
    parse_key = store.key(code, "parse") or "-"
    key = combine(parse_key, JUDGE_PLUS_VERSION,
                  source_digest(s11_elements, s11_judge, judge_plus_mod, stages))

    def compute() -> dict:
        judgements, patches, has_vol = stages.run_judge(store.payload(code, "parse") or {})
        return {"report_year": report_year, "has_voluntary": has_vol,
                "elements": {k: v.to_dict() for k, v in judgements.items()},
                "patches": [p.to_dict() for p in patches]}

    return _stage(store, "elements", code, key, compute, force=force, stats=stats)


def judgements_from(payload: dict) -> dict[str, ElementJudgement]:
    return {k: ElementJudgement.from_dict(v)
            for k, v in (payload.get("elements") or {}).items()}


def verdicts_from(payload: dict) -> dict[str, Verdict]:
    return {k: Verdict.from_dict(v) for k, v in (payload or {}).items() if isinstance(v, dict)}


# ---------------------------------------------------------------- 定性判档
QUAL_MODES = ("element", "llm", "keyword")


def qualitative_unit(store: ArtifactStore, code: str, filename: str, *,
                     mode: str, blinder, llm, has_turnover: bool,
                     force: bool = False, stats=None) -> dict:
    """定性判档（一指标一次）。指纹含 rubric、模型、别名表与模式。

    为什么这些都要进指纹：改一个档位判据、换一个模型、往别名表里补一个品牌名，
    都会改变「模型看到了什么」或「依据什么判」，旧结论必须作废。这是 LLM 参与打分后
    唯一能保证「同一份报告同分」的手段——把影响输出的东西全压进 key。
    """
    parse_key = store.key(code, "parse") or "-"
    facts_key = store.key(code, "facts") or "-"      # gate 依赖「是否披露本期流失率」
    use_llm = mode != "keyword" and llm is not None and llm.ready
    key = combine(parse_key, facts_key, rubric_hash(), mode,
                  llm.fingerprint() if use_llm else "-", blinder.digest(),
                  source_digest(qual_mod, blind_mod))

    def compute() -> dict:
        doc = stages.doc_from_payload(store.payload(code, "parse") or {})
        out: dict[str, dict] = {}
        for k, rub in RUBRICS.items():
            gate = bool(rub.gate) and not has_turnover
            v = qual_mod.judge(doc, rub, llm if use_llm else None,
                               blinder=blinder, gate_hit=gate,
                               mode="auto" if use_llm else "keyword")
            out[k] = v.to_dict()
        return out

    return _stage(store, "qualitative", code, key, compute, force=force, stats=stats)


def blinder_for(code: str, filename: str, extra) -> blind_mod.Blinder:
    """按「文件名 + 代码 + 人工别名表」建一台脱敏器（盲评用）。"""
    return blind_mod.build(code, filename=filename, extra=extra)


def patches_from(payload: dict) -> list[Patch]:
    return [Patch.from_dict(x) for x in (payload.get("patches") or [])]


# ---------------------------------------------------------------- 打分（共享）
def build_scorecard(code: str, name: str, ry: int, filename: str,
                    facts: Extraction, judgements: dict, bench: Bench,
                    has_vol: bool, *, prior_turnover, prior_gap,
                    prior_source: str, verdicts: dict | None = None) -> ScoreCard:
    """量化事实 + 要素判定/定性判档 → 一张记分卡。内存路径与落地路径共用这一处。"""
    turnover = facts.value("turnover", ry)
    gap = facts.value("gender_gap", ry)
    gap_basis = "median"
    if gap is None:
        gap = facts.value("gender_gap_avg", ry)
        gap_basis = "avg"
    imp = improvement(turnover, prior_turnover)
    turnover_fact = facts.get("turnover", ry)
    gap_fact = facts.get("gender_gap", ry) or facts.get("gender_gap_avg", ry)
    data = ScoreData(
        report_year=ry,
        turnover=turnover,
        prior_turnover=prior_turnover,
        improvement=None if imp is None else round(imp, 2),
        leavers=facts.value("leavers", ry),
        headcount=facts.value("headcount", ry),
        gap=gap,
        gap_basis=gap_basis if gap is not None else "",
        prior_gap=prior_gap,
        cb_coverage=facts.value("cb", ry),
        turnover_denom_note=facts.denom_note,
        bench_note=bench.note,
        prior_source=prior_source,
        turnover_quote=turnover_fact.quote if turnover_fact else "",
        gap_quote=gap_fact.quote if gap_fact else "",
    )
    return build_card(code, name, ry, filename, judgements, data, bench,
                      bool(turnover is not None and has_vol), verdicts=verdicts)


# ---------------------------------------------------------------- 编排器
@dataclass
class Unit:
    """一家公司在本次批处理里的全部中间态（只在内存里活一次运行）。"""
    code: str
    name: str
    pdf: Path
    run: CompanyRun
    parse: dict | None = None
    facts: Extraction | None = None
    judgements: dict[str, ElementJudgement] = field(default_factory=dict)
    patches: list[Patch] = field(default_factory=list)
    has_voluntary: bool = False
    resolved: dict = field(default_factory=dict)
    prior_key: str = "none"
    verdicts: dict[str, Verdict] = field(default_factory=dict)


@dataclass
class PipelineResult:
    batch: Batch
    runs: list[CompanyRun]
    bench: Bench
    stats: dict[str, StageStat] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    snapshot: str = ""
    artifacts: ArtifactStore | None = None      # 本次的产物目录（调试/回查用）


class Pipeline:
    """一次年度批处理。构造时只知道「算哪些、存哪、能不能复用」。"""

    STAGES = ("parse", "facts", "elements", "qualitative", "score")

    def __init__(self, year: int, build_dir: str | Path, *, force: bool = False,
                 progress=None, override_csv: str | Path | None = None,
                 bench_mode: str = "auto", bench_csv: str | Path | None = None,
                 qualitative: str = "element"):
        self.year = int(year)
        self.build_dir = Path(build_dir)
        self.force = force
        self.progress = progress
        self.override_csv = override_csv
        self.bench_mode = bench_mode
        self.bench_csv = bench_csv
        if qualitative not in QUAL_MODES:
            raise ValueError(f"--qualitative 只能是 {QUAL_MODES}，收到 {qualitative!r}")
        self.qualitative = qualitative
        settings = get_settings()
        self.llm = llm_from_settings(settings)
        self.aliases = blind_mod.load_alias_csv(settings.alias_csv)
        if qualitative == "llm" and not self.llm.ready:
            self._say("[提示] 没配 LLM（ESG_LLM_*），本次退回关键词兜底")
            self.qualitative = qualitative = "keyword"
        self.overrides = stages.read_overrides(override_csv)
        self.store = self._store_for(self.year)
        self.tables = TableStore(root=self.build_dir / "tables" / f"FY{self.year}",
                                 year=self.year)
        self.stats = {k: StageStat(k) for k in self.STAGES}
        self._stores: dict[int, ArtifactStore] = {self.year: self.store}
        self._prior_index: dict[str, Path] | None = None

    # -- 基础设施 --------------------------------------------------------
    def _store_for(self, year: int) -> ArtifactStore:
        return ArtifactStore(root=self.build_dir / f"FY{year}", year=year)

    def store_for(self, year: int) -> ArtifactStore:
        if year not in self._stores:
            self._stores[year] = self._store_for(year)
        return self._stores[year]

    def _say(self, msg: str) -> None:
        if self.progress:
            self.progress(msg)

    # -- 1 解析 ----------------------------------------------------------
    def step_parse(self, u: Unit) -> None:
        u.parse = parse_unit(self.store, u.code, u.pdf, force=self.force, stats=self.stats)

    # -- 2 抽数 ----------------------------------------------------------
    def step_facts(self, u: Unit) -> None:
        payload = facts_unit(self.store, u.code, u.pdf, self.year, force=self.force,
                             stats=self.stats, overrides=self.overrides,
                             parse_payload=u.parse)
        u.facts = Extraction.from_dict(payload)

    # -- 3 判定 ----------------------------------------------------------
    def step_elements(self, u: Unit) -> None:
        payload = elements_unit(self.store, u.code, force=self.force, stats=self.stats,
                                report_year=self.year)
        u.judgements = judgements_from(payload)
        u.patches = patches_from(payload)
        u.has_voluntary = bool(payload.get("has_voluntary"))

    # -- 4 上期值（跨年边）------------------------------------------------
    def prior_pdf(self, code: str, prior_dir: str | Path | None) -> Path | None:
        if not prior_dir or not Path(prior_dir).is_dir():
            return None
        if self._prior_index is None:
            self._prior_index = {}
            for p in scan(prior_dir):
                self._prior_index.setdefault(unit_identity(p)[0], p)
        return self._prior_index.get(code)

    def step_prior(self, u: Unit, prior_dir: str | Path | None) -> None:
        """上期值：读上一年**已落地的事实**，而不是把上一年 PDF 再解析一遍。"""
        pdf = self.prior_pdf(u.code, prior_dir)
        prior_ex: Extraction | None = None
        prior_key = "none"
        if pdf is not None and u.facts is not None:
            store = self.store_for(self.year - 1)
            parse_unit(store, u.code, pdf, force=self.force)
            payload = facts_unit(store, u.code, pdf, self.year - 1, force=self.force,
                                 overrides=())
            prior_ex = Extraction.from_dict(payload)
            prior_key = store.key(u.code, "facts") or "none"
        label = Path(prior_dir).name if prior_dir else ""
        u.resolved = stages.resolve_prior(u.facts, prior_ex, self.year, prior_label=label)
        u.prior_key = prior_key
        # 上期值的出处也落盘：这条记录回答「这个上期数是哪来的」
        self.store.write(u.code, "prior", combine(prior_key, self.year),
                         {"resolved": u.resolved, "prior_key": prior_key,
                          "prior_file": pdf.name if pdf else "", "label": label},
                         producer=PRODUCER)

    # -- 5 定性判档（LLM / 关键词兜底）-------------------------------------
    def step_qualitative(self, u: Unit) -> None:
        """一指标一次判档；``element`` 模式跳过（走 29 要素老路径）。"""
        if self.qualitative == "element":
            return
        turnover = None
        if u.facts is not None:
            ry = u.facts.report_year or self.year
            turnover = u.facts.value("turnover", ry)
        payload = qualitative_unit(
            self.store, u.code, u.pdf.name, mode=self.qualitative,
            blinder=blinder_for(u.code, u.pdf.name, self.aliases),
            llm=self.llm, has_turnover=turnover is not None,
            force=self.force, stats=self.stats)
        u.verdicts = verdicts_from(payload)

    # -- 6 对标（年批次屏障）----------------------------------------------
    def make_bench(self, units: list[Unit]) -> Bench:
        if self.bench_mode == "none":
            return Bench(kind="none", note="按参数关闭对标（A/B 记 N/A）")
        if self.bench_mode == "csv":
            if not self.bench_csv:
                raise ValueError("--bench csv 需要同时给 --bench-csv")
            return load_csv(self.bench_csv, year=self.year)
        rows: list[PeerRow] = []
        for u in units:
            if u.facts is None:
                continue
            ry = u.facts.report_year
            cur = u.facts.value("turnover", ry)
            if cur is None:                     # 没有本期值就没有分位可言
                continue
            prior_t, _ = stages.prior_values(u.facts, self.year, u.resolved)
            rows.append(PeerRow(code=u.code, year=ry, turnover=cur,
                                prior_turnover=prior_t, source=u.pdf.name))
        return from_rows(rows, kind="auto")

    def snapshot_id(self, bench: Bench) -> str:
        """对标池的指纹。A/B 是相对量：池子一变分数就该变，这个 id 让它**可见**。"""
        rows = sorted((r.code, r.year, r.turnover, r.prior_turnover) for r in bench.rows)
        return short(combine(BENCH_VERSION, bench.kind, rows), 16)

    # -- 7 打分 ----------------------------------------------------------
    def step_score(self, u: Unit, bench: Bench, snapshot: str) -> None:
        key = combine(self.store.key(u.code, "facts"), self.store.key(u.code, "elements"),
                      self.store.key(u.code, "qualitative"), self.store.key(u.code, "prior"),
                      snapshot, MODEL_VERSION, TABLE_VERSION,
                      source_digest(scoring_mod, config_mod, metrics_mod, bench_mod,
                                    tables, stages))
        prior_t, prior_g = stages.prior_values(u.facts, self.year, u.resolved)
        label = stages.prior_source_label(u.facts,
                                          u.resolved.get("prior_source") or "",
                                          self.year)

        def compute() -> dict:
            ry = u.facts.report_year
            card = build_scorecard(u.code, u.name, ry, u.pdf.name, u.facts,
                                   u.judgements, bench, u.has_voluntary,
                                   prior_turnover=prior_t, prior_gap=prior_g,
                                   prior_source=label, verdicts=u.verdicts)
            card.warnings = list(u.facts.warnings)
            pct_a = bench.percentile(u.code, "turnover", higher_better=False)
            pct_b = bench.percentile(u.code, "improvement", higher_better=True)
            company, inds, comps = tables.score_rows(
                u.code, u.name, u.pdf.name, card,
                pct_a=pct_a, pct_b=pct_b,
                band_a=bench.band(pct_a), band_b=bench.band(pct_b),
                bench_snapshot=snapshot, has_voluntary=u.has_voluntary)
            return {"company": company, "indicators": inds, "components": comps}

        payload = _stage(self.store, "score", u.code, key, compute,
                         force=self.force, stats=self.stats)
        u.run.card = tables.card_from_payload(payload)

    # -- 7 发布 ----------------------------------------------------------
    def publish(self, runs: list[CompanyRun], bench: Bench, snapshot: str) -> dict[str, int]:
        return tables.publish(self.tables, self.store, runs,
                              model_version=MODEL_VERSION, bench=bench, snapshot=snapshot)

    # -- 全流程 ----------------------------------------------------------
    def run(self, pdf: str | Path, prior_dir: str | Path | None = None) -> PipelineResult:
        pdfs = scan(pdf)
        if not pdfs:
            raise FileNotFoundError(f"目录下没有 PDF：{pdf}")

        units: list[Unit] = []
        runs: list[CompanyRun] = []
        for p in pdfs:
            code, name = unit_identity(p)
            run = CompanyRun(code=code, name=name, year=self.year, pdf=p)
            units.append(Unit(code=code, name=name, pdf=p, run=run))
            runs.append(run)

        # 1) 解析（失败即记账，不拖累同批其他公司）
        for u in units:
            self._say(f"[解析] {u.pdf.name}")
            try:
                self.step_parse(u)
            except (PDFLoadError, OSError) as exc:
                u.run.status, u.run.error = "解析失败", str(exc)

        live = [u for u in units if u.run.status == "ok"]

        # 2) 抽数  3) 判定
        for u in live:
            self._say(f"[抽数] {u.code}")
            self.step_facts(u)
        for u in live:
            self._say(f"[判定] {u.code}")
            self.step_elements(u)

        # 4) 上期值（跨年：读上一年已落地的事实，不重新解析 PDF）
        for u in live:
            self._say(f"[上期] {u.code}")
            self.step_prior(u, prior_dir)

        # 4.5) 定性判档（LLM 或关键词兜底；element 模式跳过）
        if self.qualitative != "element":
            for u in live:
                self._say(f"[定性] {u.code} ({self.qualitative})")
                self.step_qualitative(u)

        # 5) 对标池 —— 全批屏障，必须等所有公司抽完
        bench = self.make_bench(live)
        snapshot = self.snapshot_id(bench)
        self._say(f"[对标] {bench.kind} n={bench.n} 快照 {snapshot}")

        # 6) 打分
        for u in live:
            self._say(f"[打分] {u.code}")
            self.step_score(u, bench, snapshot)

        # 7) 发布 + 装配只读视图
        counts = self.publish(runs, bench, snapshot)
        batch = tables.load_batch(self.tables)
        batch.model_version = MODEL_VERSION
        batch.year = self.year

        for u in units:
            if u.run.status == "ok" and u.run.card is None:
                u.run.status = "无数据"
        return PipelineResult(batch=batch, runs=runs, bench=bench, stats=self.stats,
                              counts=counts, snapshot=snapshot, artifacts=self.store)


# ---------------------------------------------------------------- 对外入口
def run_pipeline(pdf: str | Path, year: int, prior_dir: str | Path | None = None,
                 *, build_dir: str | Path, bench_mode: str = "auto",
                 bench_csv: str | Path | None = None,
                 override_csv: str | Path | None = None, force: bool = False,
                 qualitative: str = "element", progress=None) -> PipelineResult:
    """跑完一年的全流程（含发布）。报告写不写由调用方决定。

    ``qualitative``：``element``（29 要素，默认，离线可复现）/ ``llm`` / ``keyword``。
    """
    pipe = Pipeline(year, build_dir, force=force, progress=progress,
                    override_csv=override_csv, bench_mode=bench_mode,
                    bench_csv=bench_csv, qualitative=qualitative)
    return pipe.run(pdf, prior_dir)


def score_batch(pdf: str | Path, year: int = 2025, prior_dir: str | Path | None = None,
                bench_mode: str = "auto", bench_csv: str | Path | None = None,
                report_year: int | None = None, override_csv: str | Path | None = None,
                progress=None, build_dir: str | Path | None = None,
                force: bool = False, qualitative: str = "element"
                ) -> tuple[list[CompanyRun], Bench]:
    """兼容入口：``(runs, bench)``。全量能力请直接用 ``run_pipeline``。"""
    if prior_dir is None:
        guess = Path(pdf).parent / f"FY{year - 1}" if Path(pdf).is_dir() else None
        prior_dir = guess if guess and guess.exists() else None
    result = run_pipeline(pdf, report_year or year, prior_dir,
                          build_dir=build_dir or config_mod.BUILD_DIR,
                          bench_mode=bench_mode, bench_csv=bench_csv,
                          override_csv=override_csv, force=force,
                          qualitative=qualitative, progress=progress)
    return result.runs, result.bench


# ---------------------------------------------------------------- 内存路径
def run_extract_judge(pdfs: list[Path], year: int, report_year: int | None = None,
                      progress=None) -> list[CompanyRun]:
    """第一遍：全批解析 + 抽数 + 判定（不落地、不算分位）。

    保留这个纯内存入口是为了校准脚本（``calibrate.py``）跑小样本：它不需要对标池、
    不需要历史，也就不该被缓存和表牵着走。
    """
    runs: list[CompanyRun] = []
    for pdf in pdfs:
        code, name = unit_identity(pdf)
        run = CompanyRun(code=code, name=name, year=year, pdf=Path(pdf))
        if progress:
            progress(f"[解析] {Path(pdf).name}")
        try:
            run.doc = load_pdf(str(pdf))
        except PDFLoadError as exc:
            run.status, run.error = "解析失败", str(exc)
            runs.append(run)
            continue
        run.ex = extract_mod.extract(run.doc, Path(pdf).name, report_year=report_year or year)
        res, patches = adjudicate(run.doc)
        run.res = {k: ElementJudgement.from_result(v) for k, v in res.items()}
        run.patches = patches
        run.has_voluntary = stages.has_voluntary(run.doc)
        runs.append(run)
    return runs


def run_score(runs: list[CompanyRun], bench: Bench) -> list[CompanyRun]:
    """第二遍：按已有对标池逐家打分（内存路径）。"""
    for run in runs:
        if run.doc is None or run.ex is None:
            run.status = run.status if run.status != "ok" else "无数据"
            continue
        ry = run.ex.report_year
        label = stages.prior_source_label(run.ex, run.prior_source, ry)
        run.card = build_scorecard(run.code, run.name, ry, Path(run.pdf).name, run.ex,
                                   run.res, bench, run.has_voluntary,
                                   prior_turnover=run.prior_turnover(ry),
                                   prior_gap=run.prior_gap(ry),
                                   prior_source=label)
        run.card.warnings = list(run.ex.warnings)
        run.status = "ok"
    return runs


__all__ = [
    "scan", "unit_identity", "Pipeline", "PipelineResult", "Unit", "StageStat",
    "run_pipeline", "score_batch", "run_extract_judge", "run_score",
    "build_scorecard", "parse_unit", "facts_unit", "elements_unit",
    "qualitative_unit", "verdicts_from", "blinder_for", "QUAL_MODES",
]