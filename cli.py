# -*- coding: utf-8 -*-
"""命令行入口：一份或多份 PDF → 打分报告。

用法
----
    # 最常用：一个目录的 FY2025 报告，批内分位，报告写到默认目录
    python -m esg_s11_scorer --pdf "50-应用/good_sample/分析指标/ESG报告/FY2025" --year 2025

    # 单份报告（A/B 记 N/A，除非给 --bench-csv）
    python -m esg_s11_scorer --pdf path/to/report.pdf

    # 指定输出目录 + 外部对标表 + 人工兜底值
    python -m esg_s11_scorer --pdf <dir> --out <dir> --bench csv --bench-csv bench.csv \
        --override overrides.csv

    # 只看结果不写文件
    python -m esg_s11_scorer --pdf <dir> --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import DEFAULT_OUT, MODEL_VERSION, WEIGHT_SUM
from .pipeline import score_batch
from .report import write_batch, write_company


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="esg_s11_scorer",
        description="S1.1 五项指标打分模型：读 PDF → 数值抽取 + 29 要素判定 → 打分 → 报告")
    p.add_argument("--pdf", required=True, help="PDF 文件或目录（目录下递归取 *.pdf）")
    p.add_argument("--year", type=int, default=2025, help="报告年度（默认 2025）")
    p.add_argument("--prior-dir", default=None,
                   help="上一年报告目录（默认自动找同级 FY{year-1}）")
    p.add_argument("--bench", choices=["auto", "csv", "none"], default="auto",
                   help="A/B 对标来源：auto=批内分位（默认）；csv=外部对标表；none=不评")
    p.add_argument("--bench-csv", default=None, help="外部对标表（code,year,turnover,prior_turnover）")
    p.add_argument("--override", default=None, help="人工兜底值 CSV（code,field,year,value,reason）")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    p.add_argument("--dry-run", action="store_true", help="只打印分数，不写文件")
    p.add_argument("--quiet", action="store_true", help="不打印解析进度")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    args = build_parser().parse_args(argv)
    progress = None if args.quiet else (lambda s: print(s, file=sys.stderr))
    try:
        runs, bench = score_batch(
            args.pdf, year=args.year, prior_dir=args.prior_dir,
            bench_mode=args.bench, bench_csv=args.bench_csv,
            override_csv=args.override, progress=progress)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    ok = [r for r in runs if r.card]
    print(f"\n{MODEL_VERSION} | 报告年度 {args.year} | 文件 {len(runs)} 份，成功 {len(ok)} 份")
    print(f"对标：{bench.note}")
    print(f"\n{'代码':<7}{'公司':<24}{'S1.1.1':>8}{'S1.1.2':>8}{'S1.1.3':>8}{'S1.1.4':>8}"
          f"{'S1.1.5':>8}{'严格':>8}{'归一':>8}")
    for r in sorted(ok, key=lambda x: -x.card.total_strict):
        v = [r.card.indicators[k].strict for k in
             ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5")]
        print(f"{r.code:<7}{r.name[:22]:<24}" + "".join(f"{x:>8.1f}" for x in v)
              + f"{r.card.total_strict:>8.1f}{r.card.total_normalized(WEIGHT_SUM):>8.1f}")
    for r in runs:
        if not r.card:
            print(f"  [跳过] {r.pdf.name}：{r.error or r.status}")

    if args.dry_run:
        return 0
    out = Path(args.out)
    per = out / f"FY{args.year}"
    for r in ok:
        print(f"[写出] {write_company(r, per)}")
    print(f"[写出] {write_batch(runs, bench, out, args.year)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())