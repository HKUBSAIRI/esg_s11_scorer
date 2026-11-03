# -*- coding: utf-8 -*-
"""对标层：把「同业分位」从一个必填参数，变成可计算、可解释、可替换的东西。

三种数据源（优先级从高到低）
--------------------------
1. ``--bench <csv>``       外部对标表（行业库导出），列：code,year,turnover,prior_turnover[,industry]
2. ``--bench auto``（默认） 用本批报告自身构成「批内分位」——5 家同行业公司即可算，
                          报告里标注「样本内参考，n=5，非全市场分位」
3. ``--bench none``        不计算，A/B 记 N/A，打分层按「可观测权重归一」

分位定义（与评分表对齐）
----------------------
流失率越低越好：按升序排名，rank=1 是最低（最好）→ 分位 = rank / N × 100
改善率越高越好：按降序排名，rank=1 是最高（最好）→ 分位 = rank / N × 100
并列取平均位次。分位 ≤20 → 100 分；≤40 → 75；≤60 → 50；≤80 → 25；否则 0。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .config import MIN_PEER_N, PERCENTILE_BANDS
from .metrics import improvement
from .models import PeerRow


@dataclass
class Bench:
    kind: str                                  # auto / csv / none
    rows: list[PeerRow] = field(default_factory=list)
    n: int = 0
    note: str = ""

    def percentile(self, code: str, metric: str, higher_better: bool) -> float | None:
        """返回该公司的分位（1–100）；样本不足或自身无值时返回 None。"""
        if self.kind == "none" or self.n < MIN_PEER_N:
            return None
        vals: list[tuple[str, float]] = []
        for r in self.rows:
            v = r.turnover if metric == "turnover" else improvement(r.turnover, r.prior_turnover)
            if v is not None:
                vals.append((r.code, v))
        me = next((v for c, v in vals if c == code), None)
        if me is None or not vals:
            return None
        ordered = sorted(vals, key=lambda x: x[1], reverse=higher_better)
        # 平均位次处理并列
        ranks = []
        for i, (c, v) in enumerate(ordered, start=1):
            ranks.append((c, v, i))
        same = [r[2] for r in ranks if r[1] == me]
        pos = sum(same) / len(same)
        return round(pos / len(ordered) * 100, 1)

    def band(self, pct: float | None) -> int | None:
        if pct is None:
            return None
        for upper, score in PERCENTILE_BANDS:
            if pct <= upper:
                return score
        return 0


def from_rows(rows: list[PeerRow], kind: str = "auto", note: str = "") -> Bench:
    rows = [r for r in rows if r.turnover is not None]
    return Bench(kind=kind, rows=rows, n=len(rows),
                 note=note or f"批内分位：n={len(rows)}，同批同行业公司构成小样本对标池")


def load_csv(path: str | Path, year: int | None = None,
             industry: str | None = None) -> Bench:
    """读外部对标表；文件不存在时返回空 Bench（调用方退回 auto/none）。"""
    p = Path(path)
    if not p.exists():
        return Bench(kind="none", note=f"对标表不存在：{p}")
    rows: list[PeerRow] = []
    with p.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                y = int(str(r.get("year") or r.get("年度") or "").strip())
            except ValueError:
                continue
            if year is not None and y != year:
                continue
            ind = (r.get("industry") or r.get("行业") or "").strip()
            if industry and ind and industry not in ind:
                continue
            rows.append(PeerRow(
                code=(r.get("code") or r.get("代码") or "").strip(),
                year=y,
                turnover=_num(r.get("turnover") or r.get("流失率")),
                prior_turnover=_num(r.get("prior_turnover") or r.get("上期流失率")),
                industry=ind, source=str(p)))
    return from_rows(rows, kind="csv",
                     note=f"外接对标表：{p.name}，n={len(rows)}，年度={year or '全部'}")


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace("%", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def export_rows(bench: Bench, path: str | Path) -> None:
    """把当前对标池写成 CSV，便于累积成行业库。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "year", "turnover", "prior_turnover", "industry", "source"])
        for r in bench.rows:
            w.writerow([r.code, r.year,
                        "" if r.turnover is None else r.turnover,
                        "" if r.prior_turnover is None else r.prior_turnover,
                        r.industry, r.source])