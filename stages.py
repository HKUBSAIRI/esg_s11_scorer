# -*- coding: utf-8 -*-
"""各阶段的**纯计算**：进 payload/路径，出 payload/对象，不碰文件、不碰调度。

为什么把「算什么」和「存哪、什么时候算」分开
------------------------------------------
``store.py`` 管状态，``pipeline.py`` 管顺序，本模块只回答一个问题：
「给定一份解析结果，数值该抽成什么、分该怎么算」。

好处是每一步都能单测：喂一份人造 payload，断言输出，不需要 PDF、不需要目录、
不需要跑批。碰巧这也是缓存能成立的前提——只有纯函数，同一个 key 才能对应同一份结果。
"""
from __future__ import annotations

import csv
from pathlib import Path

from s11_text import Doc, Page, load_pdf

from .extract import extract
from .models import Extraction, NumFact


# ---------------------------------------------------------------- 解析
def doc_to_payload(doc: Doc) -> dict:
    """``Doc`` → 可落地的页级文本。

    页级文本是整条链路里最贵的一步（pypdf 解 + 去页眉页脚），所以它必须能存下来。
    存的是**清洗后**的文本：后续每个阶段看到的都是同一份，重算不会因为重新解析
    而漂移。
    """
    return {
        "path": doc.path,
        "meta": dict(doc.meta),
        "pages": [{"no": p.no, "text": p.text, "cjk": p.cjk} for p in doc.pages],
    }


def doc_from_payload(payload: dict) -> Doc:
    pages = [Page(no=int(p.get("no") or i), text=str(p.get("text") or ""),
                  cjk=int(p.get("cjk") or 0))
             for i, p in enumerate(payload.get("pages") or [], start=1)]
    return Doc(path=str(payload.get("path") or ""), pages=pages,
               meta=dict(payload.get("meta") or {}))


def run_parse(pdf: str | Path) -> dict:
    """读 PDF → 页级文本 payload。失败时由调用方接 ``PDFLoadError``。"""
    return doc_to_payload(load_pdf(str(pdf)))


# ---------------------------------------------------------------- 抽数
def run_extract(parse_payload: dict, filename: str, year: int) -> Extraction:
    """解析产物 → 数值抽取结果（此时还没有人工兜底）。"""
    return extract(doc_from_payload(parse_payload), filename, report_year=year)


OVERRIDE_ALIASES = {
    "turnover": ("turnover", "流失率", "当期流失率"),
    "prior_turnover": ("prior_turnover", "上期流失率", "上期"),
    "gender_gap": ("gender_gap", "性别薪酬差距"),
    "prior_gender_gap": ("prior_gender_gap", "上期性别薪酬差距"),
}
# CSV 里的「上期 X」是给人填的写法，落到抽取结果时仍是同一个字段的不同年份
_OVERRIDE_FIELD = {"prior_turnover": "turnover", "prior_gender_gap": "gender_gap"}


def read_overrides(path: str | Path | None) -> list[dict]:
    """人工兜底表 → 规整的行。文件不存在返回空表（不是错误）。

    列：``code, field, year, value, reason``（也接受中文表头）。
    拆成「读」与「用」两步，是为了让 override 表也进指纹：表一改，facts 就重算。
    """
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    rows: list[dict] = []
    with p.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            code = (r.get("code") or r.get("代码") or "").strip()
            raw_field = (r.get("field") or r.get("字段") or "").strip()
            canonical = next((k for k, alias in OVERRIDE_ALIASES.items() if raw_field in alias),
                             raw_field)
            field = _OVERRIDE_FIELD.get(canonical, canonical)
            try:
                year = int(str(r.get("year") or r.get("年度") or "").strip())
                value = float(str(r.get("value") or r.get("值") or "").replace("%", "").strip())
            except ValueError:
                continue
            if not code or not field:
                continue
            rows.append({"code": code, "field": field, "year": year, "value": value,
                         "reason": (r.get("reason") or r.get("依据") or "人工填入").strip()})
    return rows


def apply_overrides(ex: Extraction, rows: list[dict], code: str) -> int:
    """把人工值塞进抽取结果：``prior_turnover`` 落到 ``turnover`` 的上一年。"""
    n = 0
    for r in rows:
        if r["code"] != code:
            continue
        fact = NumFact(r["field"], r["value"], r["year"], 0, "override",
                       f"人工填入：{r['reason']}", "人工")
        ex.facts.append(fact)
        ex.best.setdefault(r["field"], {})[r["year"]] = fact
        n += 1
    return n


# ---------------------------------------------------------------- 上期值
def resolve_prior(facts: Extraction, prior: Extraction | None, year: int,
                  prior_label: str = "") -> dict:
    """上期值三级回退（本报告附录 → 上一年报告 → N/A），返回回退得到的那部分。

    与 ``CompanyRun.prior_turnover()`` 的分工：本函数只负责「本报告里没有时去哪儿拿」，
    「拿到的值优先于谁」由 ``prior_values()`` 决定。拆开是因为前者要读上一年产物
    （跨年依赖），后者只用本次产物（可缓存）。
    """
    ry = facts.report_year or year
    need_t = (facts.value("turnover", ry) is not None
              and facts.value("turnover", ry - 1) is None)
    has_cur_gap = (facts.value("gender_gap", ry) is not None
                   or facts.value("gender_gap_avg", ry) is not None)
    need_g = (has_cur_gap
              and facts.value("gender_gap", ry - 1) is None
              and facts.value("gender_gap_avg", ry - 1) is None)

    out = {"prior_turnover": None, "prior_gap": None, "prior_source": ""}
    if not (need_t or need_g):
        out["prior_source"] = "本报告附录"
        return out
    if prior is None:
        out["prior_source"] = "缺失"
        return out

    got = []
    if need_t:
        v = prior.value("turnover", ry - 1)
        if v is not None:
            out["prior_turnover"] = v
            got.append("流失率")
    if need_g:
        v = prior.value("gender_gap", ry - 1)
        if v is None:
            v = prior.value("gender_gap_avg", ry - 1)
        if v is not None:
            out["prior_gap"] = v
            got.append("薪酬差距")
    label = prior_label or f"FY{ry - 1}"
    out["prior_source"] = f"上一年报告（{label}）" if got else "缺失"
    return out


def prior_values(facts: Extraction, year: int, resolved: dict) -> tuple[float | None, float | None]:
    """本期报告给的上期值优先，其次上一年报告，最后 None（不猜）。"""
    ry = facts.report_year or year
    t = facts.value("turnover", ry - 1)
    pt = t if t is not None else resolved.get("prior_turnover")
    g = facts.value("gender_gap", ry - 1)
    if g is None:
        g = facts.value("gender_gap_avg", ry - 1)
    pg = g if g is not None else resolved.get("prior_gap")
    return pt, pg


def prior_source_label(facts: Extraction, attach_source: str, year: int,
                       field: str = "turnover") -> str:
    """上期值的出处：人工兜底 > 本报告附录 > 上一年报告 > 缺失（按可靠性降序）。"""
    ry = facts.report_year or year
    fact = facts.best.get(field, {}).get(ry - 1)
    if fact is not None:
        return "人工填入（overrides.csv）" if fact.method == "override" else "本报告附录"
    if attach_source and attach_source != "本报告附录":
        return attach_source
    return "缺失"


__all__ = [
    "doc_to_payload", "doc_from_payload", "run_parse", "run_extract",
    "read_overrides", "apply_overrides", "OVERRIDE_ALIASES",
    "resolve_prior", "prior_values", "prior_source_label",
]