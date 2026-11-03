# -*- coding: utf-8 -*-
"""命令行入口：一份或多份 PDF → 打分报告。

用法（路径都写在 ``.env`` 里，所以命令可以短到只有模块名）
---------------------------------------------------------
    # 看现在生效的输入/输出在哪、每个值的来源
    python -m esg_s11_scorer --show-config

    # 跑 .env 里配好的那一批（ESG_PDF_DIR + ESG_YEAR）
    python -m esg_s11_scorer

    # 临时换一批（命令行永远覆盖环境变量）
    python -m esg_s11_scorer --pdf "D:/另一批/FY2025" --year 2025 --out "D:/结果"

    # 单份报告（A/B 记 N/A，除非给 --bench-csv）
    python -m esg_s11_scorer --pdf path/to/report.pdf --bench none

    # 只看结果不写文件 / 用另一个配置文件
    python -m esg_s11_scorer --dry-run --env-file .env.2026

配置优先级：``--参数`` > 进程环境变量 > ``.env`` > 默认值（详见 ``settings.py``）。
"""
from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path
from typing import Sequence

from .settings import PKG_DIR, Settings, reload
from .store import StoreError

ENV_TEMPLATE = PKG_DIR / ".env.example"


# ---------------------------------------------------------------- 参数
def build_parser(settings: Settings) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="esg_s11_scorer",
        description="S1.1 五项指标打分模型：读 PDF → 数值抽取 + 29 要素判定 → 打分 → 报告",
        epilog="参数未给时取 .env / 环境变量；用 --show-config 查看每个值的来源。")
    p.add_argument("--pdf", default=None,
                   help=f"PDF 文件或目录（默认 ESG_PDF_DIR = {settings.pdf_dir}）")
    p.add_argument("--year", type=int, default=settings.year,
                   help=f"报告年度（默认 {settings.year}，可用 ESG_YEAR 改）")
    p.add_argument("--prior-dir", default=settings.prior_dir,
                   help="上一年报告目录（默认 ESG_PRIOR_DIR，未配则自动找同级 FY{year-1}）")
    p.add_argument("--bench", choices=["auto", "csv", "none"], default=settings.bench_mode,
                   help="A/B 对标来源：auto=批内分位（默认）；csv=外部对标表；none=不评")
    p.add_argument("--bench-csv", default=settings.bench_csv,
                   help="外部对标表（code,year,turnover,prior_turnover）；默认 ESG_BENCH_CSV")
    p.add_argument("--override", default=settings.override_csv,
                   help="人工兜底值 CSV（code,field,year,value,reason）；默认 ESG_OVERRIDE_CSV")
    p.add_argument("--out", default=str(settings.out_dir),
                   help=f"输出目录（默认 ESG_OUT_DIR = {settings.out_dir}）")
    p.add_argument("--build-dir", default=str(settings.build_dir),
                   help=f"管道中间态目录：阶段产物 + gold 表（默认 ESG_BUILD_DIR = {settings.build_dir}）")
    p.add_argument("--force", action="store_true",
                   help="忽略已有产物，所有阶段重算（改了代码但怀疑缓存时用）")
    p.add_argument("--dry-run", action="store_true",
                   help="只算分不写报告（中间产物照常落地，供下次复用）")
    p.add_argument("--quiet", action="store_true", help="不打印解析进度")
    p.add_argument("--env-file", default=None,
                   help="指定配置文件（默认自动找 包目录/.env）")
    p.add_argument("--show-config", action="store_true", help="打印生效配置与来源后退出")
    p.add_argument("--init-env", action="store_true",
                   help="若还没有 .env，就从 .env.example 生成一份")
    return p


def _prescan_env_file(argv: Sequence[str]) -> str | None:
    """先于一切导入读出 --env-file：配置文件决定词表仓库路径，进而决定能 import 什么。"""
    for i, a in enumerate(argv):
        if a == "--env-file" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--env-file="):
            return a.split("=", 1)[1]
    return None


# ---------------------------------------------------------------- 显示
def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _pad(text: str, n: int) -> str:
    return text + " " * max(0, n - _width(text))


def show_config(settings: Settings, env_file: Path | None) -> int:
    print(f"配置文件：{env_file or '（未找到 .env，全部用默认值 / 环境变量）'}")
    print(f"报告年度：{settings.year}    对标：{settings.bench_mode}")
    rows = settings.rows()
    name_w = max(_width(r[0]) for r in rows) + 2
    src_w = max(_width(r[2]) for r in rows) + 2
    # 生效值放最后一列（路径很长）：前面几列对齐，长路径自己换行也不会撑歪表格
    print(f"\n{_pad('变量', name_w)}{_pad('状态', 8)}{_pad('来源', src_w)}生效值")
    print("-" * (name_w + src_w + 20))
    for name, val, src, state in rows:
        print(f"{_pad(name, name_w)}{_pad(state, 8)}{_pad(src, src_w)}{val}")

    pdf = Path(settings.pdf_dir)
    pdfs = sorted(pdf.rglob("*.pdf")) if pdf.exists() else []
    print(f"\n输入：{pdf}  →  {len(pdfs)} 份 PDF")
    for p in pdfs[:5]:
        print(f"  · {p.name}")
    if len(pdfs) > 5:
        print(f"  … 另 {len(pdfs) - 5} 份")
    for w in settings.warnings:
        print(f"[警告] {w}", file=sys.stderr)
    problems = settings.problems()
    for msg in problems:
        print(f"[错误] {msg}", file=sys.stderr)
    return 0 if not problems else 2


def init_env(target: Path) -> int:
    if target.exists():
        print(f"[跳过] {target} 已存在，未覆盖（要重来请先删掉它）")
        return 0
    if not ENV_TEMPLATE.is_file():
        print(f"[错误] 找不到模板 {ENV_TEMPLATE}", file=sys.stderr)
        return 2
    target.write_text(ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[写出] {target}（按需改里面的路径即可）")
    return 0


# ---------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    argv = list(sys.argv[1:] if argv is None else argv)
    settings = reload(_prescan_env_file(argv))
    args = build_parser(settings).parse_args(argv)

    # 这两个开关不碰模型，也不 require 词表仓库存在 —— 新机器上第一条命令就该能用
    if args.init_env:
        return init_env(PKG_DIR / ".env")
    if args.show_config:
        return show_config(settings, settings.env_file)

    # 定完配置才导入：pipeline 在 import 期就要按 INSIGHT_APP 去拿 s11_text
    from .config import MODEL_VERSION, WEIGHT_SUM, check_consistency
    from .pipeline import run_pipeline
    from .report import write_batch, write_company

    pdf = Path(args.pdf) if args.pdf else Path(settings.pdf_dir)
    if not pdf.exists():
        print(f"[错误] 找不到输入：{pdf}\n"
              f"        用 --pdf 指定，或在 .env 里设 ESG_PDF_DIR（当前来源："
              f"{settings.sources.get('ESG_PDF_DIR', '默认')}）", file=sys.stderr)
        return 2

    for w in check_consistency():          # 词表与规格漂移时明确提示，不静默用旧名/旧权重
        print(f"[警告] {w}", file=sys.stderr)
    progress = None if args.quiet else (lambda s: print(s, file=sys.stderr))
    try:
        result = run_pipeline(
            pdf, args.year, args.prior_dir,
            build_dir=Path(args.build_dir),
            bench_mode=args.bench, bench_csv=args.bench_csv,
            override_csv=args.override, force=args.force, progress=progress)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:              # 例如 --bench csv 没给 --bench-csv
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except StoreError as exc:              # 产物/表损坏：明确告诉用户怎么修
        print(f"[错误] {exc}\n        修法：删掉 build 目录重跑，或加 --force", file=sys.stderr)
        return 2

    batch, bench = result.batch, result.bench
    ok = batch.views
    print(f"\n{MODEL_VERSION} | 报告年度 {args.year} | 文件 {batch.total} 份，成功 {len(ok)} 份")
    print(f"对标：{bench.note}（快照 {result.snapshot}）")
    print("阶段：" + " · ".join(f"{name} {st.label}"
                               for name, st in result.stats.items()))
    print(f"表  ：" + " · ".join(f"{k} {v}" for k, v in result.counts.items()))
    print(f"\n{'代码':<7}{'公司':<24}{'S1.1.1':>8}{'S1.1.2':>8}{'S1.1.3':>8}{'S1.1.4':>8}"
          f"{'S1.1.5':>8}{'严格':>8}{'归一':>8}")
    for v in ok:
        card = v.card
        scores = [card.indicators[k].strict for k in
                  ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5")]
        print(f"{v.code:<7}{v.name[:22]:<24}" + "".join(f"{x:>8.1f}" for x in scores)
              + f"{card.total_strict:>8.1f}{card.total_normalized(WEIGHT_SUM):>8.1f}")
    for f in batch.failures:
        print(f"  [跳过] {f.file}：{f.error or f.status}")

    if args.dry_run:
        print(f"\n[中间产物] {Path(args.build_dir)}（--dry-run，未写报告）")
        return 0
    out = Path(args.out)
    per = out / f"FY{args.year}"
    for v in ok:
        print(f"[写出] {write_company(v, per)}")
    print(f"[写出] {write_batch(batch, out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())