# -*- coding: utf-8 -*-
"""用人工证据页评测 S1.1 关键词召回与混合召回。

当前 GT CSV 的每一行代表一条人工确认证据。这里用“召回结果中是否出现同一物理页”
作为页面级代理指标；正式 Recall@K 需要等 GT 补齐原文跨度后再算。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from s11_text import Doc, load_pdf

from . import qualitative
from .embedding import EmbeddingError, from_settings as embedding_from_settings
from .retrieval import RetrievalConfig, retrieve_hybrid_with_stats
from .rubric import RUBRICS, Rubric
from .settings import PKG_DIR, get_settings
from .stages import doc_from_payload

DEFAULT_GT = PKG_DIR / "reports" / "GT采集段落对照_20261110.csv"


def _rubric(ind: str) -> Rubric:
    key = ind if ind in RUBRICS else f"{ind}-C"
    if key not in RUBRICS:
        raise KeyError(f"未知指标 {ind!r}")
    return RUBRICS[key]


def load_gt(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            try:
                page = int((raw.get("gt_page") or "").strip())
            except ValueError:
                continue
            code = (raw.get("code") or "").strip()
            ind = (raw.get("ind") or "").strip()
            if not code or not ind or page < 1:
                continue
            rows.append({
                "company": (raw.get("company") or "").strip(),
                "code": code,
                "id": (raw.get("id") or "").strip(),
                "ind": ind,
                "gt_page": page,
                "quote": (raw.get("quote") or "").strip(),
            })
    return rows


def _pdfs(pdf_dir: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        code = pdf.stem.split("_", 1)[0]
        if code:
            out.setdefault(code, pdf)
    return out


def _ranked_pages_keyword(doc: Doc, rub: Rubric, limit: int) -> list[int]:
    return [passage.page for passage in qualitative.recall(doc, rub, limit=limit)]


def _ranked_pages_hybrid(
    doc: Doc,
    rub: Rubric,
    embedding,
    limit: int,
) -> tuple[list[int], dict]:
    passages, stats = retrieve_hybrid_with_stats(
        doc, rub, embedding, config=RetrievalConfig(top_k=limit))
    return [passage.page for passage in passages], stats


def _aggregate(details: list[dict]) -> dict:
    by_ind: dict[str, list[dict]] = defaultdict(list)
    for row in details:
        by_ind[row["ind"]].append(row)

    def summarize(rows: list[dict]) -> dict:
        n = len(rows)
        hits = sum(1 for row in rows if row["hit"])
        rr = sum(row["reciprocal_rank"] for row in rows)
        return {
            "evidence_rows": n,
            "page_hits": hits,
            "page_recall": round(hits / n, 4) if n else 0.0,
            "mrr": round(rr / n, 4) if n else 0.0,
        }

    return {
        **summarize(details),
        "by_indicator": {
            ind: summarize(rows) for ind, rows in sorted(by_ind.items())
        },
    }


def _cached_doc(build_dir: Path, year: int, code: str) -> Doc | None:
    path = build_dir / f"FY{year}" / code / "parse.json"
    if not path.is_file():
        return None
    artifact = json.loads(path.read_text(encoding="utf-8"))
    payload = artifact.get("payload") or {}
    return doc_from_payload(payload) if payload.get("pages") else None


def evaluate(
    rows: list[dict],
    pdf_dir: Path,
    *,
    mode: str,
    limit: int,
    build_dir: Path | None = None,
    year: int = 2025,
    doc_cache: dict[str, Doc] | None = None,
) -> dict:
    pdf_by_code = _pdfs(pdf_dir)
    missing = sorted({row["code"] for row in rows} - set(pdf_by_code))
    if missing:
        raise FileNotFoundError(f"GT 公司找不到 PDF：{missing}")

    embedding = None
    if mode == "hybrid":
        embedding = embedding_from_settings()
        if not embedding.ready:
            raise EmbeddingError("Embedding 未配置，无法运行 hybrid 评测")

    docs: dict[str, Doc] = doc_cache if doc_cache is not None else {}
    cache: dict[tuple[str, str], tuple[list[int], dict]] = {}
    details: list[dict] = []
    for row in rows:
        code, ind = row["code"], row["ind"]
        if code not in docs:
            cached = _cached_doc(build_dir, year, code) if build_dir else None
            if cached is not None:
                print(f"[parse-cache] {code}", file=sys.stderr)
                docs[code] = cached
            else:
                print(f"[parse] {code} {pdf_by_code[code].name}", file=sys.stderr)
                docs[code] = load_pdf(str(pdf_by_code[code]))
        cache_key = (code, ind)
        if cache_key not in cache:
            rub = _rubric(ind)
            if mode == "keyword":
                pages = _ranked_pages_keyword(docs[code], rub, limit)
                stats = {"retrieval": "keyword", "top_k": limit}
            else:
                pages, stats = _ranked_pages_hybrid(
                    docs[code], rub, embedding, limit)
            cache[cache_key] = (pages, stats)
        pages, stats = cache[cache_key]
        ranks = [rank for rank, page in enumerate(pages, start=1)
                 if page == row["gt_page"]]
        rank = min(ranks) if ranks else None
        details.append({
            **row,
            "hit": rank is not None,
            "rank": rank,
            "reciprocal_rank": round(1.0 / rank, 4) if rank else 0.0,
            "retrieved_pages": pages,
            "retrieval_stats": stats,
        })
    return {"mode": mode, "limit": limit, "summary": _aggregate(details),
            "details": details}


def _print_summary(result: dict) -> None:
    summary = result["summary"]
    print(f"\nmode={result['mode']}  limit={result['limit']}")
    print(f"证据行 {summary['evidence_rows']}，页面命中 {summary['page_hits']}，"
          f"PageRecall={summary['page_recall']:.1%}，MRR={summary['mrr']:.3f}")
    for ind, row in summary["by_indicator"].items():
        print(f"  {ind}: {row['page_hits']}/{row['evidence_rows']} "
              f"({row['page_recall']:.1%}), MRR={row['mrr']:.3f}")


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m esg_s11_scorer.retrieval_eval",
        description="用人工证据页对比 keyword 与 hybrid 检索"
    )
    parser.add_argument("--gt", default=str(DEFAULT_GT), help="GT 证据对照 CSV")
    parser.add_argument("--pdf-dir", default=str(settings.pdf_dir),
                        help="报告 PDF 目录")
    parser.add_argument("--mode", choices=["keyword", "hybrid", "both"], default="both")
    parser.add_argument("--limit", type=int, default=20, help="每种指标取前 K 段")
    parser.add_argument("--build-dir",
                        help="可选：读取 build/FY{year}/<code>/parse.json；"
                             "旧产物可能与当前解析器不同，默认重新解析 PDF")
    parser.add_argument("--year", type=int, default=settings.year)
    parser.add_argument("--json", dest="json_path", help="保存完整结果 JSON")
    parser.add_argument("--code", action="append", help="只评测指定股票代码（可重复）")
    args = parser.parse_args(argv)

    rows = load_gt(args.gt)
    if args.code:
        keep = set(args.code)
        rows = [row for row in rows if row["code"] in keep]
    if not rows:
        parser.error("GT 没有可评测的证据行")

    modes = ("keyword", "hybrid") if args.mode == "both" else (args.mode,)
    results = []
    doc_cache: dict[str, Doc] = {}
    for mode in modes:
        result = evaluate(
            rows, Path(args.pdf_dir), mode=mode, limit=args.limit,
            build_dir=Path(args.build_dir) if args.build_dir else None,
            year=args.year, doc_cache=doc_cache)
        results.append(result)
        _print_summary(result)

    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "gt": str(Path(args.gt).resolve()),
            "pdf_dir": str(Path(args.pdf_dir).resolve()),
            "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n-> {path}")
    return 0


__all__ = ["DEFAULT_GT", "load_gt", "evaluate", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
