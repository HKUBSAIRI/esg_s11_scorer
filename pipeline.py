# -*- coding: utf-8 -*-
"""编排层：一次批处理 = 扫描 → 解析 → 抽数 → 判定 → 对标 → 打分。

为什么要两遍
------------
A/B 两项是**相对**分位：必须先拿到同批所有公司的流失率，才能算某一家排第几。
所以流程是「先全批抽数与判定，再统一建对标池，最后逐家打分」，单份 PDF 无法自洽。

上期值的三级回退（显式建模在 ``CompanyRun.prior_turnover()``）
--------------------------------------------------------------
1. 本份报告的附录表里直接有上一年度列 → 直接用；
2. 没有 → 去 ``--prior-dir``（默认同级 FY{year-1}）找同一代码的上一年报告，取其报告年度值；
3. 仍没有 → 记 N/A，B 项不评（不拿「行业均值」冒充上期）。

重构要点：上两级的承载从「``setattr(run, '_prior_turnover')`` 影子属性」改成
``CompanyRun.prior_turnover_fallback`` 正式字段——影子属性不进 ``to_dict``、不进台账、
IDE 也跳不到，调试时凭空多出字段。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from s11_text import PDFLoadError, load_pdf

from .bench import Bench, from_rows, load_csv
from .config import REPORT_DIR  # noqa: F401  （对外沿用，便于脚本直接引用）
from .extract import extract
from .judge_plus import adjudicate
from .metrics import improvement
from .models import CompanyRun, NumFact, PeerRow, ScoreData
from .scoring import build_card

CODE_RE = re.compile(r"^(\d{5})[_-]([A-Za-z0-9_]+)")

OVERRIDE_ALIASES = {
    "turnover": ("turnover", "流失率", "当期流失率"),
    "prior_turnover": ("prior_turnover", "上期流失率", "上期"),
    "gender_gap": ("gender_gap", "性别薪酬差距"),
    "prior_gender_gap": ("prior_gender_gap", "上期性别薪酬差距"),
}


def _parse_name(token: str) -> str:
    return token.replace("_", " ").strip()


def scan(pdf: str | Path) -> list[Path]:
    """PDF 文件或目录 → PDF 列表（目录递归）。"""
    p = Path(pdf)
    if p.is_file():
        return [p]
    return sorted(x for x in p.rglob("*.pdf") if x.is_file())


def _load(run: CompanyRun) -> bool:
    try:
        run.doc = load_pdf(str(run.pdf))
        return True
    except PDFLoadError as exc:
        run.status, run.error = "解析失败", str(exc)
        return False


def run_extract_judge(pdfs: list[Path], year: int, report_year: int | None = None,
                      progress=None) -> list[CompanyRun]:
    """第一遍：全批解析 + 抽数 + 判定（此时还不知道分位，不打分）。"""
    runs: list[CompanyRun] = []
    for pdf in pdfs:
        m = CODE_RE.match(pdf.name)
        code = m.group(1) if m else pdf.stem[:5]
        name = _parse_name(m.group(2)) if m else pdf.stem
        run = CompanyRun(code=code, name=name, year=year, pdf=pdf)
        if progress:
            progress(f"[解析] {pdf.name}")
        if not _load(run):
            runs.append(run)
            continue
        run.ex = extract(run.doc, pdf.name, report_year=report_year or year)
        run.res, run.patches = adjudicate(run.doc)
        runs.append(run)
    return runs


def _prior_extraction(run: CompanyRun, prior_pdfs: list[Path]):
    for pdf in prior_pdfs:
        m = CODE_RE.match(pdf.name)
        if m and m.group(1) == run.code:
            doc = load_pdf(str(pdf))
            return extract(doc, pdf.name), run.year - 1
    return None, run.year - 1


def attach_prior_from_reports(runs: list[CompanyRun], prior_dir: Path | None) -> None:
    """上期值缺失时，从上一年的同代码报告里取（第 2 级回退）。"""
    if not prior_dir or not Path(prior_dir).exists():
        return
    prior_pdfs = scan(prior_dir)
    for run in runs:
        if run.ex is None:
            continue
        ry = run.ex.report_year
        has_cur_t = run.ex.value("turnover", ry) is not None
        need_t = has_cur_t and run.ex.value("turnover", ry - 1) is None
        has_cur_g = (run.ex.value("gender_gap", ry) is not None
                     or run.ex.value("gender_gap_avg", ry) is not None)
        need_g = has_cur_g and (run.ex.value("gender_gap", ry - 1) is None
                                and run.ex.value("gender_gap_avg", ry - 1) is None)
        if not (need_t or need_g):
            run.prior_source = "本报告附录"
            continue
        ex2, y2 = _prior_extraction(run, prior_pdfs)
        if ex2 is None:
            run.prior_source = "缺失"
            continue
        got = []
        if need_t:
            v = ex2.value("turnover", y2)
            if v is not None:
                run.prior_turnover_fallback = v
                got.append("流失率")
        if need_g:
            v = ex2.value("gender_gap", y2)
            if v is None:
                v = ex2.value("gender_gap_avg", y2)
            if v is not None:
                run.prior_gap_fallback = v
                got.append("薪酬差距")
        run.prior_source = f"上一年报告（{Path(prior_dir).name}）" if got else "缺失"


def apply_overrides(runs: list[CompanyRun], csv_path: str | Path | None) -> int:
    """人工兜底：把抽不到的数值从 CSV 补进来（记 method=override，置信度「人工」）。

    典型场景：附表是转置排版（标签列与数值列分离）、数值在图片里、或跨版本口径调整。
    CSV 列：code, field, year, value, reason
    """
    if not csv_path:
        return 0
    p = Path(csv_path)
    if not p.exists():
        return 0
    n = 0
    with p.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            code = (r.get("code") or r.get("代码") or "").strip()
            fld_raw = (r.get("field") or r.get("字段") or "").strip()
            fld = next((k for k, alias in OVERRIDE_ALIASES.items() if fld_raw in alias), fld_raw)
            try:
                year = int(str(r.get("year") or r.get("年度") or "").strip())
                val = float(str(r.get("value") or r.get("值") or "").replace("%", "").strip())
            except ValueError:
                continue
            target = next((x for x in runs if x.code == code), None)
            if target is None or target.ex is None:
                continue
            reason = (r.get("reason") or r.get("依据") or "人工填入").strip()
            real_field = {"prior_turnover": "turnover",
                          "prior_gender_gap": "gender_gap"}.get(fld, fld)
            fact = NumFact(real_field, val, year, 0, "override",
                           f"人工填入：{reason}", "人工")
            target.ex.facts.append(fact)
            target.ex.best.setdefault(real_field, {})[year] = fact
            n += 1
    return n


def make_bench(runs: list[CompanyRun], mode: str = "auto",
               csv_path: str | Path | None = None, year: int | None = None) -> Bench:
    """建对标池。``auto`` 用本批报告互为对标；``csv`` 读外部表；``none`` 关闭。"""
    if mode == "none":
        return Bench(kind="none", note="按参数关闭对标（A/B 记 N/A）")
    if mode == "csv":
        if csv_path is None:
            raise ValueError("--bench csv 需要同时给 --bench-csv")
        return load_csv(csv_path, year=year)
    rows: list[PeerRow] = []
    for r in runs:
        if r.ex is None:
            continue
        ry = r.ex.report_year
        cur = r.ex.value("turnover", ry)
        if cur is None:                     # 没有本期值就没有分位可言
            continue
        rows.append(PeerRow(code=r.code, year=ry, turnover=cur,
                            prior_turnover=r.prior_turnover(ry), source=r.pdf.name))
    return from_rows(rows, kind="auto")


def _has_voluntary(doc) -> bool:
    """是否披露了自愿离职口径（留存率或自愿离职率均可）。"""
    t = doc.text
    if re.search(r"voluntary\s+(?:employee\s+)?turnover", t, re.I):
        return True
    if re.search(r"retention\s+rate\s*=\s*1\s*[-−]", t, re.I):
        return True
    return bool(re.search(r"(?:人才)?留存率\s*(?:=|为|達|达)", t))


def _prior_source(run: CompanyRun, ry: int, field: str = "turnover") -> str:
    """上期值的出处：人工兜底 > 本报告附录 > 上一年报告 > 缺失（按可靠性降序）。"""
    fact = run.ex.best.get(field, {}).get(ry - 1) if run.ex else None
    if fact is not None:
        return "人工填入（overrides.csv）" if fact.method == "override" else "本报告附录"
    if run.prior_source and run.prior_source != "本报告附录":
        return run.prior_source
    return "缺失"


def run_score(runs: list[CompanyRun], bench: Bench) -> list[CompanyRun]:
    """第二遍：按已有对标池逐家打分。"""
    for run in runs:
        if run.doc is None or run.ex is None:
            run.status = run.status if run.status != "ok" else "无数据"
            continue
        ry = run.ex.report_year
        turnover = run.ex.value("turnover", ry)
        prior = run.prior_turnover(ry)

        gap = run.ex.value("gender_gap", ry)
        gap_basis = "median"
        if gap is None:
            gap = run.ex.value("gender_gap_avg", ry)
            gap_basis = "avg"

        data = ScoreData(
            report_year=ry,
            turnover=turnover,
            prior_turnover=prior,
            improvement=(None if improvement(turnover, prior) is None
                         else round(improvement(turnover, prior), 2)),
            leavers=run.ex.value("leavers", ry),
            headcount=run.ex.value("headcount", ry),
            gap=gap,
            gap_basis=gap_basis if gap is not None else "",
            prior_gap=run.prior_gap(ry),
            cb_coverage=run.ex.value("cb", ry),
            turnover_denom_note=run.ex.denom_note,
            bench_note=bench.note,
            prior_source=_prior_source(run, ry),
            turnover_quote=(run.ex.get("turnover", ry).quote if run.ex.get("turnover", ry) else ""),
            gap_quote=((run.ex.get("gender_gap", ry) or run.ex.get("gender_gap_avg", ry)).quote
                       if (run.ex.get("gender_gap", ry) or run.ex.get("gender_gap_avg", ry)) else ""),
        )
        has_vol = bool(turnover is not None and _has_voluntary(run.doc))
        run.card = build_card(run.code, run.name, ry, run.pdf.name,
                              run.res, data, bench, has_vol)
        run.card.warnings = list(run.ex.warnings)
        run.status = "ok"
    return runs


def score_batch(pdf: str | Path, year: int = 2025, prior_dir: str | Path | None = None,
                bench_mode: str = "auto", bench_csv: str | Path | None = None,
                report_year: int | None = None, override_csv: str | Path | None = None,
                progress=None) -> tuple[list[CompanyRun], Bench]:
    """一行跑完：扫描 → 抽取判定 → 上期回退 → 人工兜底 → 建池 → 打分。"""
    pdfs = scan(pdf)
    if not pdfs:
        raise FileNotFoundError(f"目录下没有 PDF：{pdf}")
    runs = run_extract_judge(pdfs, year, report_year=report_year, progress=progress)
    if prior_dir is None:
        guess = Path(pdf).parent / f"FY{year - 1}" if Path(pdf).is_dir() else None
        prior_dir = guess if guess and guess.exists() else None
    attach_prior_from_reports(runs, Path(prior_dir) if prior_dir else None)
    apply_overrides(runs, override_csv)
    bench = make_bench(runs, mode=bench_mode, csv_path=bench_csv, year=year)
    return run_score(runs, bench), bench