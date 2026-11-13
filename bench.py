# -*- coding: utf-8 -*-
"""对标层：把「同业分位」从一个必填参数，变成可计算、可解释、可替换的东西。

三种数据源（优先级从高到低）
--------------------------
1. ``--bench <csv>``       外部对标表（行业库导出），列：code,year,turnover,prior_turnover[,industry]
2. ``--bench auto``（默认） 用本批报告自身构成「批内分位」——5 家同行业公司即可算，
                          报告里标注「样本内参考，n=5，非全市场分位」
3. ``--bench none``        不计算，A/B 记 N/A，打分层按「可观测权重归一」

第四种是快照：``auto``/``csv`` 建的池子会落成
``build/bench/FY{year}/{snapshot}.json``，之后用 ``--bench-snapshot`` 读回来重算。
冻结的意义不是省时间，而是让「补一家公司」不再改写已交付的分数。

分位定义（与评分表对齐）
----------------------
流失率越低越好：按升序排名，rank=1 是最低（最好）→ 分位 = rank / N × 100
改善率越高越好：按降序排名，rank=1 是最高（最好）→ 分位 = rank / N × 100
并列取平均位次。分位 ≤20 → 100 分；≤40 → 75；≤60 → 50；≤80 → 25；否则 0。

分母只数「该指标有值的家数」
--------------------------
N 不是批次家数，而是**该指标上有效**的家数 N_m：一家有本期流失率、却没有上期值
（改善率算不出来），它进 A 的分母、不进 B 的分母。把它算进 B 的分母，等于让一个
不可比的公司占了一个位次——别人会凭空退后一档，而它自己也没得到分。

由此两条配套规则：
- 某指标的 N_m < ``MIN_PEER_N`` 时，该指标记 N/A，**不退回方向判定**（有池子却不可比，
  和「压根没有池子」是两回事，后者见 ``scoring`` 的方向兜底）；
- 报告里写「n=」必须写 N_m（``peer_n()``），不是池子规模 ``n``——两者在 B 项上常常不同。
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field
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

    def values(self, metric: str) -> list[tuple[str, float]]:
        """该指标上的有效样本 ``(code, value)``。

        n/a 的公司根本不进这个列表：它既不占位次，也不进分母（见模块文档）。
        """
        out: list[tuple[str, float]] = []
        for r in self.rows:
            v = r.turnover if metric == "turnover" else improvement(r.turnover, r.prior_turnover)
            if v is not None:
                out.append((r.code, v))
        return out

    def peer_n(self, metric: str) -> int:
        """该指标的分母（有效家数）。报告里的「n=」用它，不用池子规模 ``n``。"""
        return len(self.values(metric))

    def percentile(self, code: str, metric: str, higher_better: bool) -> float | None:
        """返回该公司的分位（1–100）；该指标有效家数不足或自身无值时返回 None。"""
        if self.kind == "none":
            return None
        vals = self.values(metric)
        if len(vals) < MIN_PEER_N:
            return None
        me = next((v for c, v in vals if c == code), None)
        if me is None:
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


# ---------------------------------------------------------------- 快照（可重放的对标池）
# 为什么池子要落成文件：A/B 是相对量，换一个池子就是另一个分。只记哈希
# （``bench_meta.snapshot``）能证明「跟谁比的」，却没法「再比一次」——补一家公司、
# 半年后复核、客户问「当时那个分怎么来的」，都需要把池子原样读回来。
# 放 JSON 而不是只导 CSV：CSV 是给人改的行业库，快照是给机器重放的存证，两者格式
# 与用途都不同（CSV 缺 ``kind``/``note``，读回来会丢掉「批内 vs 外接」这个口径）。
SNAPSHOT_FORMAT = 1


def as_dict(bench: Bench) -> dict:
    """池子 → 可序列化结构（行序即池子内容，不做排序；排序只在算指纹时做）。"""
    return {
        "format": SNAPSHOT_FORMAT,
        "kind": bench.kind,
        "n": bench.n,
        "note": bench.note,
        "rows": [asdict(r) for r in bench.rows],
    }


def save_snapshot(bench: Bench, path: str | Path, snapshot: str = "") -> Path:
    """把当前池子写成可重放的 JSON（每行的分数与出处一起存）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps({**as_dict(bench), "snapshot": snapshot},
                      ensure_ascii=False, indent=1)
    tmp = p.with_name(p.name + ".tmp")          # 先写临时文件再替换，半份快照不该存在
    tmp.write_text(blob, encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_snapshot(path: str | Path) -> Bench:
    """读回冻结点。行集与 note 原样恢复；n 按「有本期值」重数，不信任文件里的 n。"""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"对标快照读不了：{p}（{exc}）") from exc
    rows = [PeerRow(code=str(r.get("code") or ""),
                    year=int(r.get("year") or 0),
                    turnover=_num(r.get("turnover")),
                    prior_turnover=_num(r.get("prior_turnover")),
                    industry=str(r.get("industry") or ""),
                    source=str(r.get("source") or ""))
            for r in data.get("rows") or []]
    rows = [r for r in rows if r.turnover is not None]
    return Bench(kind=str(data.get("kind") or "csv"), rows=rows, n=len(rows),
                 note=str(data.get("note") or ""))


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