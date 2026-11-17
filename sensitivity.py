# -*- coding: utf-8 -*-
"""细则影响分析（只读）：量化每条证据组对档位的作用。

不调模型、不写 ``build/``，只读已经落地的 ``qualitative-<mode>.json``。回答三件事：

1. 这条细则在 100 分路径里是什么角色（必需 / 任一 / 事件 / 仅佐证）；
2. 在现有判档里翻转它，会不会改变档位（掉档 / 翻档）；
3. 卡在 50 分的公司，补哪一组就能到 100。

边界
----
只对 **100 分路径**精确。50↔0 之间还混着「仅有原则性声明」「经核实的负面事实」等
尚未规则化的判断，本工具不声称能算那一段；它也不会把 0 分「猜」成 50。

为什么不做成权重表
------------------
路径是合取，影响不可加：同一条细则在 A 公司是必要条件，在 B 公司可能一次都不影响。
所以这里输出的是**反事实**（把这一组翻掉，档位会不会变），不是回归系数。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import SPECS
from .qualitative import check_path
from .rubric import RUBRICS, Rubric

ROLE_ALL = "必需"
ROLE_ANY = "任一"
ROLE_EVENT = "事件"
ROLE_SUPPORT = "仅佐证"

_SPEC = {s.ind: s for s in SPECS}


def roles(rubric: Rubric) -> dict[str, str]:
    """组 id → 在 100 分路径里的角色。不在路径里的组只作佐证。"""
    out = {s.id: ROLE_SUPPORT for s in rubric.specs}
    rule = rubric.path100
    if rule is not None:
        for gid in rule.require_all:
            out[gid] = ROLE_ALL
        for gid in rule.require_any:
            out[gid] = ROLE_ANY
    for gid in rubric.event_groups:
        if out.get(gid) == ROLE_SUPPORT:
            out[gid] = ROLE_EVENT
    return out


def impact_points(key: str) -> int:
    """翻一档对**指标分**的影响。

    S1.1.1 / S1.1.2 的 C 项占 30%，档位差 50 → 15 分；其余三项是纯三档制 → 50 分。
    这个 0.3 抄自 ``scoring.py`` 的 ``Component`` 权重；改权重时本函数的测试会失败。
    """
    spec = _SPEC[RUBRICS[key].ind]
    return 50 if spec.kind == "tiered" else 15


@dataclass
class Flips:
    base_ok: bool
    drop: list[str] = field(default_factory=list)     # 翻成 no 会掉出 100
    unlock: list[str] = field(default_factory=list)   # 补成 yes 就能进 100
    gap: list[str] = field(default_factory=list)      # 到 100 还缺的组


def analyse(rubric: Rubric, groups: dict[str, str],
            event: bool | None) -> Flips:
    """对一家的分组判定做反事实：哪些组是「少了就掉」、哪些是「补上就进」。"""
    base_ok, gap = check_path(rubric, groups, event)
    out = Flips(base_ok=base_ok, gap=list(gap))
    for spec in rubric.specs:
        cur = groups.get(spec.id, "no")
        if base_ok and cur == "yes":
            trial = dict(groups)
            trial[spec.id] = "no"
            if not check_path(rubric, trial, event)[0]:
                out.drop.append(spec.id)
        elif not base_ok and cur != "yes":
            trial = dict(groups)
            trial[spec.id] = "yes"
            if check_path(rubric, trial, event)[0]:
                out.unlock.append(spec.id)
    return out


def load(roots: list[str], year: int, mode: str) -> tuple[dict, list[str]]:
    """读多个 build 根目录，按 (code, key) 去重；返回（判档表, 无组数据的条目）。"""
    data: dict[str, dict[str, dict]] = defaultdict(dict)
    stale: list[str] = []
    for root in roots:
        base = Path(root)
        for path in sorted(base.glob(f"FY{year}/*/qualitative-{mode}.json")):
            code = path.parent.name
            payload = json.loads(path.read_text(encoding="utf-8")).get("payload") or {}
            items = payload.get("verdicts") or payload
            rows = items.items() if isinstance(items, dict) else [
                (x.get("key"), x) for x in items if isinstance(x, dict)]
            for key, val in rows:
                if not isinstance(val, dict) or key not in RUBRICS:
                    continue
                if not val.get("groups"):
                    if val.get("source") == "gate":
                        continue          # 0 分前置条件本就不调模型，没有分组是正常的
                    stale.append(f"{code}/{key}")
                    continue
                data[code][key] = val
    return {c: v for c, v in data.items() if v}, stale


def summarize(data: dict, stale: list[str], mode: str) -> dict:
    """把逐家判档压成「按指标 × 组」与「按公司缺口」两张表。"""
    per_key: dict[str, dict] = {}
    gaps: list[dict] = []
    for key, rubric in RUBRICS.items():
        role = roles(rubric)
        tally = {s.id: Counter() for s in rubric.specs}
        drops = Counter()
        unlocks = Counter()
        for code, verdicts in sorted(data.items()):
            v = verdicts.get(key)
            if not v:
                continue
            groups = {str(k): str(x) for k, x in (v.get("groups") or {}).items()}
            event = v.get("event") if isinstance(v.get("event"), bool) else None
            for gid in tally:
                tally[gid][groups.get(gid, "no")] += 1
            res = analyse(rubric, groups, event)
            drops.update(res.drop)
            unlocks.update(res.unlock)
            if not res.base_ok:
                gaps.append({"code": code, "key": key, "tier": v.get("tier"),
                             "status": v.get("status") or "ok",
                             "gap": res.gap, "unlock": res.unlock})
        per_key[key] = {
            "role": role,
            "tally": {g: dict(c) for g, c in tally.items()},
            "drops": dict(drops),
            "unlocks": dict(unlocks),
        }
    return {"mode": mode, "companies": len(data), "stale": stale,
            "per_key": per_key, "gaps": gaps,
            "points": {k: impact_points(k) for k in RUBRICS}}


def to_markdown(summary: dict) -> str:
    lines = [f"# 细则影响（mode={summary['mode']}，{summary['companies']} 家有组数据）", ""]
    if summary["stale"]:
        lines += [f"另有 {len(summary['stale'])} 项是旧口径产物、没有分组判定，未计入：`"
                  + "`, `".join(summary["stale"][:8]) + "`"
                  + (" …" if len(summary["stale"]) > 8 else ""), ""]
    lines += ["## 一、每条细则的作用", ""]
    for key, block in summary["per_key"].items():
        pts = summary["points"][key]
        lines += [f"### {key}（翻一档 ≈ {pts} 分）", "",
                  "| 组 | 角色 | yes | partial | no | 翻 no 掉档 | 补 yes 翻档 |",
                  "|---|---|---|---|---|---|---|"]
        for gid, role in block["role"].items():
            t = block["tally"].get(gid, {})
            lines.append(
                f"| {gid} | {role} | {t.get('yes', 0)} | {t.get('partial', 0)} | "
                f"{t.get('no', 0)} | {block['drops'].get(gid, 0)} | "
                f"{block['unlocks'].get(gid, 0)} |")
        lines.append("")
    lines += ["## 二、卡在 100 分以下的公司", ""]
    if not summary["gaps"]:
        lines += ["无：当前样本的 100 分路径均已成立。", ""]
    else:
        lines += ["| 公司 | 指标 | 档 | 到 100 还缺 | 补哪一组可翻档 |",
                  "|---|---|---|---|---|"]
        for row in sorted(summary["gaps"], key=lambda r: (r["key"], r["code"])):
            gap = "、".join(row["gap"]) or "-"
            unlock = "、".join(row["unlock"]) or "无（缺多项）"
            lines.append(f"| {row['code']} | {row['key']} | {row['tier']} | {gap} | {unlock} |")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m esg_s11_scorer.sensitivity",
        description="只读分析：每条证据细则对定性档位的影响（不调模型、不写 build）")
    ap.add_argument("--build-dir", "-b", nargs="+", action="append",
                    help="一个或多个 build 根目录；可重复。默认包内 build/")
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--mode", default="llm", choices=["llm", "keyword"])
    ap.add_argument("--code", action="append", help="只看这些公司（可重复）")
    ap.add_argument("--json", dest="json_path", help="把结构化结果另存为 JSON")
    args = ap.parse_args(argv)

    roots = [p for group in (args.build_dir or [[str(Path(__file__).resolve().parent / "build")]])
             for p in group]
    data, stale = load(roots, args.year, args.mode)
    if args.code:
        keep = set(args.code)
        data = {c: v for c, v in data.items() if c in keep}
        stale = [s for s in stale if s.split("/")[0] in keep]
    summary = summarize(data, stale, args.mode)
    print(to_markdown(summary))
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


__all__ = ["roles", "analyse", "impact_points", "load", "summarize",
           "to_markdown", "Flips", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
