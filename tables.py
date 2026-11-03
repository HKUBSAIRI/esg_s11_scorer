# -*- coding: utf-8 -*-
"""gold 表：字段契约（唯一的 schema 定义）、从产物发布、从表还原视图。

为什么单独一层
--------------
改造前，分数的唯一载体是 xlsx：报告层既算又画，下游想拿分数只能去解析单元格。
这一层把「分数是什么」和「分数怎么展示」分开：

    阶段产物（JSON，一公司一目录） ──publish──▶ gold 表（一年一张） ──load_batch──▶ Batch
                                                                        │
                                                                    report.py（只画）

三张表的定位
-----------
- ``facts`` / ``elements`` / ``patches``：**证据层**。一行一个数值/判定/补丁，
  每行都能回到页码与原文。这是「为什么是这个分」的全部依据。
- ``indicators`` / ``components`` / ``companies``：**结果层**。分项与总分，带口径
  （严格/归一/可达上限/档位）与对标快照 id。
- ``bench_rows`` / ``bench_meta``：**口径层**。这一年的 A/B 到底拿谁比的、n 是多少、
  快照 id 是什么——不存这个，历史分数就无法解释（换了对标池等于换了尺子）。

列名一律英文短名（``comp_key`` 而不是 ``key``），方便直接进 DuckDB / pandas 用 SQL 查。
"""
from __future__ import annotations

from pathlib import Path

from .models import (Batch, CompanyView, Component, ElementJudgement, Failure,
                     Extraction, IndScore, NumFact, Patch, ScoreCard, ScoreData)
from .store import TableStore

# ---------------------------------------------------------------- 字段契约
FACTS = ("code", "report_year", "field", "fact_year", "value", "page",
         "method", "confidence", "note", "quote")
ELEMENTS = ("code", "report_year", "eid", "ind", "name", "verdict", "reason",
            "confidence", "quote", "doubt", "patched")
PATCHES = ("code", "eid", "name", "before", "after", "reason")
INDICATORS = ("code", "report_year", "ind", "name", "weight", "kind",
              "strict", "normalized", "reachable", "band", "note", "raw_score")
COMPONENTS = ("code", "ind", "comp_key", "name", "weight", "score", "note", "evidence")
COMPANIES = ("code", "name", "file", "report_year", "status", "error", "warnings",
             "total_strict", "total_normalized", "weight_sum",
             "pct_a", "pct_b", "band_a", "band_b", "bench_snapshot",
             "prior_source", "bench_note", "denom_note", "has_voluntary",
             "turnover", "prior_turnover", "improvement", "leavers", "headcount",
             "gap", "gap_basis", "prior_gap", "cb_coverage",
             "turnover_quote", "gap_quote")
BENCH_ROWS = ("code", "report_year", "turnover", "prior_turnover", "industry", "source")
BENCH_META = ("year", "kind", "peer_n", "note", "snapshot")

SCHEMAS = {
    "facts": FACTS, "elements": ELEMENTS, "patches": PATCHES,
    "indicators": INDICATORS, "components": COMPONENTS, "companies": COMPANIES,
    "bench_rows": BENCH_ROWS, "bench_meta": BENCH_META,
}


# ---------------------------------------------------------------- 证据层
def fact_rows(code: str, ex: Extraction) -> list[dict]:
    return [{"code": code, "report_year": ex.report_year, "field": f.field,
             "fact_year": f.year, "value": f.value, "page": f.page,
             "method": f.method, "confidence": f.confidence,
             "note": f.note, "quote": f.quote} for f in ex.facts]


def element_rows(code: str, report_year: int, rows: dict[str, ElementJudgement],
                 patches: list[Patch]) -> list[dict]:
    """一行一个要素。``quote`` 只存首条证据（报告展示的就是它），
    全部证据仍留在 ``elements.json`` 产物里，需要时按 code 回查。
    """
    patched = {p.eid for p in patches}
    return [{"code": code, "report_year": report_year, "eid": j.eid, "ind": j.ind,
             "name": j.name, "verdict": j.verdict, "reason": j.reason,
             "confidence": j.confidence, "quote": j.top_quote(),
             "doubt": bool(j.doubt), "patched": j.eid in patched}
            for j in rows.values()]


def patch_rows(code: str, patches: list[Patch]) -> list[dict]:
    return [{"code": code, "eid": p.eid, "name": p.name, "before": p.before,
             "after": p.after, "reason": p.reason} for p in patches]


# ---------------------------------------------------------------- 结果层
def score_rows(code: str, name: str, file: str, card: ScoreCard,
               *, pct_a=None, pct_b=None, band_a=None, band_b=None,
               bench_snapshot: str = "", has_voluntary: bool = False
               ) -> tuple[dict, list[dict], list[dict]]:
    """一张记分卡 → （公司行, 指标行, 分项行）。报告层要用的数都在这里定稿。"""
    d = card.data or ScoreData(report_year=card.report_year)
    company = {
        "code": code, "name": name, "file": file,
        "report_year": card.report_year, "status": "ok", "error": "",
        "warnings": "\n".join(card.warnings),
        "total_strict": card.total_strict,
        "total_normalized": card.total_normalized(sum(
            i.weight for i in card.indicators.values()) or 100),
        "weight_sum": sum(i.weight for i in card.indicators.values()),
        "pct_a": pct_a, "pct_b": pct_b, "band_a": band_a, "band_b": band_b,
        "bench_snapshot": bench_snapshot,
        "prior_source": d.prior_source, "bench_note": d.bench_note,
        "denom_note": d.turnover_denom_note, "has_voluntary": has_voluntary,
        "turnover": d.turnover, "prior_turnover": d.prior_turnover,
        "improvement": d.improvement, "leavers": d.leavers,
        "headcount": d.headcount, "gap": d.gap, "gap_basis": d.gap_basis,
        "prior_gap": d.prior_gap, "cb_coverage": d.cb_coverage,
        "turnover_quote": d.turnover_quote, "gap_quote": d.gap_quote,
    }
    inds: list[dict] = []
    comps: list[dict] = []
    for ind in card.indicators.values():
        inds.append({"code": code, "report_year": card.report_year,
                     "ind": ind.ind, "name": ind.name, "weight": ind.weight,
                     "kind": ind.kind, "strict": ind.strict,
                     "normalized": ind.normalized, "reachable": ind.reachable,
                     "band": ind.band, "note": ind.note, "raw_score": ind.raw})
        for c in ind.components:
            comps.append({"code": code, "ind": ind.ind, "comp_key": c.key,
                          "name": c.name, "weight": c.weight, "score": c.score,
                          "note": c.note, "evidence": c.evidence})
    return company, inds, comps


def _data_from(row: dict) -> ScoreData:
    return ScoreData(
        report_year=int(row.get("report_year") or 0),
        turnover=row.get("turnover"), prior_turnover=row.get("prior_turnover"),
        improvement=row.get("improvement"), leavers=row.get("leavers"),
        headcount=row.get("headcount"), gap=row.get("gap"),
        gap_basis=str(row.get("gap_basis") or ""), prior_gap=row.get("prior_gap"),
        cb_coverage=row.get("cb_coverage"),
        turnover_denom_note=str(row.get("denom_note") or ""),
        bench_note=str(row.get("bench_note") or ""),
        prior_source=str(row.get("prior_source") or ""),
        turnover_quote=str(row.get("turnover_quote") or ""),
        gap_quote=str(row.get("gap_quote") or ""))


def _card_from(company: dict, inds: list[dict], comps: list[dict]) -> ScoreCard:
    card = ScoreCard(code=str(company["code"]), name=str(company.get("name") or ""),
                     report_year=int(company.get("report_year") or 0),
                     file=str(company.get("file") or ""),
                     data=_data_from(company),
                     warnings=[w for w in str(company.get("warnings") or "").splitlines()
                               if w.strip()])
    for row in inds:
        mine = [c for c in comps if c["ind"] == row["ind"]]
        card.indicators[str(row["ind"])] = IndScore(
            ind=str(row["ind"]), name=str(row.get("name") or ""),
            weight=float(row.get("weight") or 0), kind=str(row.get("kind") or "weighted"),
            components=[Component(key=str(c.get("comp_key") or ""),
                                  name=str(c.get("name") or ""),
                                  weight=float(c.get("weight") or 0),
                                  score=None if c.get("score") is None else int(c["score"]),
                                  note=str(c.get("note") or ""),
                                  evidence=str(c.get("evidence") or ""))
                        for c in mine],
            raw=None if row.get("raw_score") is None else int(row["raw_score"]),
            note=str(row.get("note") or ""))
    return card


def card_from_payload(payload: dict) -> ScoreCard:
    """score 产物 → 记分卡。与 ``load_batch`` 走同一个装配函数：
    内存里那张卡和表里那行分数，必须是同一套 schema 出来的。"""
    return _card_from(payload.get("company") or {},
                      payload.get("indicators") or [],
                      payload.get("components") or [])


# ---------------------------------------------------------------- 发布
def publish(tables: TableStore, artifacts, runs, *, model_version: str,
            bench, snapshot: str, bench_meta_note: str = "") -> dict[str, int]:
    """把一年的阶段产物汇成 gold 表（整年覆盖写，幂等）。

    ``runs`` 只用来补「没出分的那些单位」——失败必须进表，否则汇总表里会凭空少几家，
    而看报告的人无从知道少了谁。
    """
    codes = artifacts.units()
    facts: list[dict] = []
    elements: list[dict] = []
    patches: list[dict] = []
    companies: list[dict] = []
    indicators: list[dict] = []
    components: list[dict] = []

    scores = {c: artifacts.payload(c, "score") for c in codes}
    for code in codes:
        payload = artifacts.payload(code, "facts")
        if payload:
            ex = Extraction.from_dict(payload)
            facts.extend(fact_rows(code, ex))
        el = artifacts.payload(code, "elements")
        if el:
            judgements = {k: ElementJudgement.from_dict(v)
                          for k, v in (el.get("elements") or {}).items()}
            plist = [Patch.from_dict(x) for x in (el.get("patches") or [])]
            elements.extend(element_rows(code, int(el.get("report_year") or 0),
                                         judgements, plist))
            patches.extend(patch_rows(code, plist))
        sc = scores.get(code)
        if sc:
            companies.append(sc.get("company") or {})
            indicators.extend(sc.get("indicators") or [])
            components.extend(sc.get("components") or [])

    # 失败单位：只有状态、没有分数（card_* 全空）
    seen = {c.get("code") for c in companies}
    for run in runs:
        if run.code in seen:
            continue
        companies.append({"code": run.code, "name": run.name,
                          "file": Path(str(run.pdf)).name,
                          "report_year": run.year, "status": run.status or "失败",
                          "error": run.error or "", "warnings": "",
                          "weight_sum": 0, "bench_snapshot": snapshot})

    bench_rows = [{"code": r.code, "report_year": r.year, "turnover": r.turnover,
                   "prior_turnover": r.prior_turnover, "industry": r.industry,
                   "source": r.source} for r in bench.rows]
    meta_row = [{"year": artifacts.year, "kind": bench.kind, "peer_n": bench.n,
                 "note": bench.note or bench_meta_note, "snapshot": snapshot}]

    counts = {
        "facts": len(facts), "elements": len(elements), "patches": len(patches),
        "companies": len(companies), "indicators": len(indicators),
        "components": len(components), "bench_rows": len(bench_rows),
        "bench_meta": len(meta_row),
    }
    tables.write("facts", facts, FACTS)
    tables.write("elements", elements, ELEMENTS)
    tables.write("patches", patches, PATCHES)
    tables.write("companies", companies, COMPANIES)
    tables.write("indicators", indicators, INDICATORS)
    tables.write("components", components, COMPONENTS)
    tables.write("bench_rows", bench_rows, BENCH_ROWS)
    tables.write("bench_meta", meta_row, BENCH_META)
    artifacts.write_meta(model_version=model_version, snapshot=snapshot,
                         bench_kind=bench.kind, bench_n=bench.n,
                         companies=len(companies), published_at=None)
    return counts


# ---------------------------------------------------------------- 还原
def load_batch(tables: TableStore) -> Batch:
    """gold 表 → ``Batch``（报告层的唯一输入）。这里不做任何计算，只做装配。"""
    meta = (tables.read("bench_meta") or [{}])[0]
    companies = tables.read("companies")
    indicators = tables.read("indicators")
    components = tables.read("components")
    facts = tables.read("facts")
    elements = tables.read("elements")
    patches = tables.read("patches")

    by_ind: dict[str, list[dict]] = {}
    for r in indicators:
        by_ind.setdefault(str(r["code"]), []).append(r)
    comps_by: dict[str, list[dict]] = {}
    for r in components:
        comps_by.setdefault(str(r["code"]), []).append(r)
    facts_by: dict[str, list[dict]] = {}
    for r in facts:
        facts_by.setdefault(str(r["code"]), []).append(r)
    elem_by: dict[str, list[dict]] = {}
    for r in elements:
        elem_by.setdefault(str(r["code"]), []).append(r)
    patch_by: dict[str, list[dict]] = {}
    for r in patches:
        patch_by.setdefault(str(r["code"]), []).append(r)

    batch = Batch(year=int(meta.get("year") or 0),
                  bench_kind=str(meta.get("kind") or "none"),
                  bench_note=str(meta.get("note") or ""),
                  bench_n=int(meta.get("peer_n") or 0),
                  bench_snapshot=str(meta.get("snapshot") or ""))

    for row in companies:
        code = str(row["code"])
        if str(row.get("status") or "") != "ok":
            batch.failures.append(Failure(code=code, file=str(row.get("file") or ""),
                                          status=str(row.get("status") or ""),
                                          error=str(row.get("error") or "")))
            continue
        card = _card_from(row, by_ind.get(code, []), comps_by.get(code, []))
        batch.views.append(CompanyView(
            code=code, name=str(row.get("name") or ""),
            file=str(row.get("file") or ""), card=card,
            facts=[NumFact(field=str(f["field"]), value=float(f["value"]),
                           year=None if f.get("fact_year") is None else int(f["fact_year"]),
                           page=int(f.get("page") or 0), method=str(f.get("method") or ""),
                           quote=str(f.get("quote") or ""),
                           confidence=str(f.get("confidence") or ""),
                           note=str(f.get("note") or ""))
                   for f in facts_by.get(code, [])],
            elements={str(e["eid"]): ElementJudgement(
                eid=str(e["eid"]), ind=str(e.get("ind") or ""),
                name=str(e.get("name") or ""), verdict=str(e.get("verdict") or "否"),
                reason=str(e.get("reason") or ""),
                confidence=float(e.get("confidence") or 0),
                quotes=[str(e["quote"])] if e.get("quote") else [],
                doubt=bool(e.get("doubt")))
                for e in elem_by.get(code, [])},
            patches=[Patch(eid=str(p["eid"]), name=str(p.get("name") or ""),
                           before=str(p.get("before") or ""), after=str(p.get("after") or ""),
                           reason=str(p.get("reason") or ""))
                     for p in patch_by.get(code, [])],
            pct_a=row.get("pct_a"), pct_b=row.get("pct_b"),
            band_a=None if row.get("band_a") is None else int(row["band_a"]),
            band_b=None if row.get("band_b") is None else int(row["band_b"])))

    # 按代码排序（= 文件名序）：汇总表自己会按分数排，视图不必替它决定顺序，
    # 否则「要素矩阵」的列顺序会随分数变动而跳动，历史报告无法逐格对比。
    batch.views.sort(key=lambda v: v.code)
    batch.failures.sort(key=lambda f: f.code)
    return batch


__all__ = [
    "SCHEMAS", "FACTS", "ELEMENTS", "PATCHES", "INDICATORS", "COMPONENTS",
    "COMPANIES", "BENCH_ROWS", "BENCH_META",
    "fact_rows", "element_rows", "patch_rows", "score_rows", "publish", "load_batch",
]