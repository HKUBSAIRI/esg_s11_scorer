# -*- coding: utf-8 -*-
"""指标看板的数据导出：build 产物 → ``web/data.js``（页面只渲染，不判分）。

为什么单独一层
--------------
看板要回答的是「每个指标取了什么值、这分是怎么来的」，因此它必须和交付工作簿用
**同一套判分函数**：分数由 ``pipeline.build_scorecard`` 现算，本模块只做搬运与摊平——
从阶段产物里读 facts / 上期值 / 判档，按同一批代码重建对标池，再把记分卡摊成前端要的
形状（指标 → 分项 → 依据原文）。这里不新增任何计分规则，改口径仍然只改 ``config``。

数据从哪来
----------
``build/FY{year}/{code}/`` 下的 ``facts`` / ``prior`` / ``qualitative-*`` 三类产物，都带
指纹信封；读它们不触发 PDF 解析，也不调模型。定性判档默认整批取 ``llm``，只有整批都
有 LLM 产物时才用（避免同一页里一半 LLM、一半关键词，分数不可比）。

用法（工作目录 = ``50-应用``）
-----------------------------
    python -m esg_s11_scorer.board --year 2025 \
        --codes 02269,03692,01513,01801,01177

默认写 ``esg_s11_scorer/web/data.js``；打开 ``web/index.html`` 即可查看（无依赖、无需起服务）。
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from . import stages
from .bench import from_rows
from .config import MODEL_VERSION, SPECS
from .models import Extraction, PeerRow
from .pipeline import build_scorecard, verdicts_from
from .store import ArtifactStore

IND_ORDER = [s.ind for s in SPECS]
PAGE_RE = re.compile(r"^\[p(\d+)\]\s*(.*)$", re.S)

# 港股中文简称（只影响显示；英文名仍从产物里取）。没有的代码显示英文名。
CN_NAME = {
    "00013": "和黄医药", "00512": "远大医药", "00867": "康哲药业", "01093": "石药集团",
    "01177": "中国生物制药", "01513": "丽珠医药", "01530": "三生制药", "01548": "金斯瑞生物科技",
    "01681": "康臣药业", "01801": "信达生物", "02096": "先声药业", "02196": "复星医药",
    "02269": "药明生物", "03320": "华润医药", "03692": "翰森制药", "03759": "康龙化成",
    "03933": "联邦制药", "06160": "百济神州", "06185": "康希诺生物", "06821": "凯莱英",
}


# ---------------------------------------------------------------- 小工具
def _page(quote: str) -> int | None:
    m = PAGE_RE.match(quote or "")
    return int(m.group(1)) if m else None


def _quote(text: str) -> dict:
    """``[p131] …原文…`` → ``{"page": 131, "text": "…原文…"}``。"""
    m = PAGE_RE.match((text or "").strip())
    if m:
        return {"page": int(m.group(1)), "text": m.group(2).strip()}
    return {"page": None, "text": (text or "").strip()}


def _evidence(quotes) -> list[dict]:
    """去重保序：A/B 常引用同一段，C 的依据也不该把同一句列两遍。"""
    seen: set[str] = set()
    out: list[dict] = []
    for q in quotes:
        item = _quote(q)
        if not item["text"] or item["text"] in seen:
            continue
        seen.add(item["text"])
        out.append(item)
    return out


# ---------------------------------------------------------------- 摊平
def _values(card) -> list[dict]:
    """四个取值：流失率、性别薪酬差距、集体谈判覆盖、员工数。"""
    d = card.data
    year = card.report_year
    out: list[dict] = []

    if d and d.turnover is not None:
        sub = []
        if d.prior_turnover is not None:
            sub.append(f"{year - 1} 年 {d.prior_turnover:.2f}%")
        if d.improvement is not None:
            sub.append(f"改善 {d.improvement:+.2f}%")
        out.append({"label": "流失率", "value": f"{d.turnover:.2f}%",
                    "sub": " · ".join(sub), "page": _page(d.turnover_quote)})
    else:
        out.append({"label": "流失率", "value": "未披露", "sub": "", "page": None,
                    "missing": True})

    if d and d.gap is not None:
        basis = "中位数" if d.gap_basis != "avg" else "平均值"
        sub = [basis]
        if d.prior_gap is not None:
            sub.append(f"{year - 1} 年 {d.prior_gap:.2f}%")
        out.append({"label": "性别薪酬差距", "value": f"{abs(d.gap):.2f}%",
                    "sub": " · ".join(sub), "page": _page(d.gap_quote)})
    else:
        out.append({"label": "性别薪酬差距", "value": "未披露", "sub": "", "page": None,
                    "missing": True})

    if d and d.cb_coverage is not None:
        out.append({"label": "集体谈判覆盖", "value": f"{d.cb_coverage:g}%",
                    "sub": "", "page": None})
    else:
        out.append({"label": "集体谈判覆盖", "value": "未披露", "sub": "", "page": None,
                    "missing": True})

    if d and d.headcount is not None:
        sub = f"离职 {d.leavers:,.0f}" if d.leavers is not None else ""
        out.append({"label": "员工总数", "value": f"{d.headcount:,.0f}",
                    "sub": sub, "page": None})
    else:
        out.append({"label": "员工总数", "value": "未披露", "sub": "", "page": None,
                    "missing": True})
    return out


def _ind_row(card, ind: str, verdicts: dict) -> dict:
    score = card.indicators[ind]
    weighted = score.kind == "weighted"
    parts: list[dict] = []
    quotes: list[str] = []

    if weighted:
        for comp in score.components:
            item = {"key": comp.key, "name": comp.name,
                    "weight": round(comp.weight * 100) if comp.weight else 0,
                    "score": comp.score, "note": "", "missing": ""}
            if comp.key == "C":
                v = verdicts.get(f"{ind}-C")
                if v is not None:
                    item["note"] = v.reason
                    item["missing"] = v.missing
                    quotes.extend(v.evidence)
                else:
                    item["note"] = comp.note
            else:
                item["note"] = comp.note
                if comp.evidence:
                    quotes.append(comp.evidence)
            parts.append(item)
    else:
        v = verdicts.get(ind)
        parts.append({"key": "", "name": "档位依据", "weight": 0,
                      "score": score.raw,
                      "note": v.reason if v else score.note,
                      "missing": v.missing if v else ""})
        if v is not None:
            quotes.extend(v.evidence)

    return {
        "ind": ind, "name": score.name, "weight": score.weight,
        "kind": score.kind, "score": score.strict, "norm": score.normalized,
        "reachable": score.reachable,
        "contribution": round(score.weight / 100 * score.strict, 2),
        "parts": parts, "evidence": _evidence(quotes),
    }


def _mode_for(store: ArtifactStore, codes: list[str], mode: str) -> str:
    """整批一个口径：全部有 LLM 产物才用 llm，否则退回 keyword。"""
    if mode in ("llm", "keyword"):
        return mode
    if all(store.path(c, "qualitative-llm").is_file() for c in codes):
        return "llm"
    return "keyword"


# ---------------------------------------------------------------- 主装配
def build(codes: list[str] | None = None, *, year: int = 2025,
          build_dir: str | Path | None = None, mode: str = "auto") -> dict:
    root = Path(build_dir) if build_dir else Path(__file__).resolve().parent / "build"
    store = ArtifactStore(root=root / f"FY{year}", year=year)

    # 默认只导「真正出过分」的公司：有 score 产物才算本批成员，
    # 只有 parse/facts 的分区（解析失败、无数据）不进看板。
    default = store.units_with("score") or store.units()
    codes = [c for c in (codes or default) if store.path(c, "facts").is_file()]
    if not codes:
        raise FileNotFoundError(f"{root / f'FY{year}'} 下没有可用的 facts 产物")
    mode = _mode_for(store, codes, mode)
    qual_name = f"qualitative-{mode}"

    facts: dict[str, Extraction] = {}
    resolved: dict[str, dict] = {}
    missing: list[str] = []
    for code in codes:
        payload = store.payload(code, qual_name)
        if payload is None:
            missing.append(code)
            continue
        facts[code] = Extraction.from_dict(store.payload(code, "facts") or {})
        resolved[code] = (store.payload(code, "prior") or {}).get("resolved") or {}
    if missing:
        raise FileNotFoundError(f"这些代码没有 {qual_name} 产物：{missing}")
    codes = [c for c in codes if c in facts]

    # 对标池与批跑同构：同一批代码、同一个年度，只从 facts 读数值
    peers = []
    for code in codes:
        ex = facts[code]
        ry = ex.report_year
        prior_t, _ = stages.prior_values(ex, year, resolved[code])
        peers.append(PeerRow(code=code, year=ry, turnover=ex.value("turnover", ry),
                             prior_turnover=prior_t))
    bench = from_rows(peers, kind="auto")

    companies = []
    for code in codes:
        ex = facts[code]
        verdicts = verdicts_from(store.payload(code, qual_name) or {})
        prior_t, prior_g = stages.prior_values(ex, year, resolved[code])
        label = stages.prior_source_label(ex, resolved[code].get("prior_source") or "", year)
        # score 产物带公司英文名；没有就退回代码（facts 里没有名字）
        published = (store.payload(code, "score") or {}).get("company") or {}
        name_en = re.sub(r"\s+ESG\s+FY\d{4}$", "", published.get("name") or "").strip() or code
        card = build_scorecard(code, name_en, ex.report_year or year, f"{code}.pdf", ex, bench,
                               prior_turnover=prior_t, prior_gap=prior_g,
                               prior_source=label, verdicts=verdicts)
        companies.append({
            "code": code,
            "name": CN_NAME.get(code, name_en),
            "name_en": name_en,
            "file": card.file,
            "total": card.total_strict,
            "total_norm": card.total_normalized(100),
            "values": _values(card),
            "inds": [_ind_row(card, ind, verdicts) for ind in IND_ORDER],
        })

    companies.sort(key=lambda c: (-c["total"], c["code"]))
    return {
        "meta": {
            "year": year,
            "model": MODEL_VERSION,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "mode": mode,
            "bench": bench.note,
            "bench_n": bench.n,
            # 改善率的可比家数单列：分母只数「本期与上期都有值」的公司，
            # 与 A 项常常不是一个数（当前 16 vs 9），页面直接写池子规模会骗人。
            "bench_n_improve": bench.peer_n("improvement"),
            "count": len(companies),
        },
        "companies": companies,
    }


def write(data: dict, out: str | Path) -> Path:
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = "window.BOARD = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n"
    p.write_text(blob, encoding="utf-8")
    return p


# ---------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="esg_s11_scorer.board",
                                description="把已落地的阶段产物导出为看板数据（不跑 PDF、不调模型）")
    p.add_argument("--year", type=int, default=2025, help="报告年度（默认 2025）")
    p.add_argument("--codes", default="",
                   help="逗号分隔的公司代码；默认取该年度已出分（有 score 产物）的全部公司")
    p.add_argument("--build-dir", default=None, help="管道中间态目录（默认 包目录/build）")
    p.add_argument("--mode", choices=["auto", "llm", "keyword"], default="auto",
                   help="定性判档取哪份产物；auto=整批都有 LLM 产物才用 LLM")
    p.add_argument("--out", default=None, help="输出文件（默认 web/data.js）")
    args = p.parse_args(argv)

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    data = build(codes, year=args.year, build_dir=args.build_dir, mode=args.mode)
    out = Path(args.out) if args.out else Path(__file__).resolve().parent / "web" / "data.js"
    path = write(data, out)

    meta = data["meta"]
    print(f"[写出] {path}")
    print(f"{meta['year']} · {meta['count']} 家 · 判档 {meta['mode']} · 对标 n={meta['bench_n']}")
    for c in data["companies"]:
        print(f"  {c['code']}  {c['name']:<8} 严格 {c['total']:>5.1f}  归一 {c['total_norm']:>5.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())