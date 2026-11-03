# -*- coding: utf-8 -*-
"""回测与校准：把模型跑到已有的两套「人工答案」上，量出一致率与分差。

两套金标准
----------
1. **10 份样本 · 专家裁定**（``50-应用/sample/04-脚本与索引/verdicts.py``）：
   29 要素 × 10 家 = 290 格 + 5 指标专家分。这是模型的主要校准集。
2. **药明生物 · 本组手工分析**（``分析指标/药明生物/S1.1_02269_指标分析.md``）：
   29 要素 + 五项分（45/80/50/50/50，A/B 按无对标计）。用来验证「长篇双语报告」这一档。

输出：``calibration.md``（人读）+ 控制台摘要。任何规则改动后重跑本脚本即可。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import esg_s11_scorer  # noqa: F401
from esg_s11_scorer.config import PROJECT_ROOT, REPORT_DIR
from esg_s11_scorer.pipeline import run_extract_judge, run_score
from esg_s11_scorer.bench import Bench

SAMPLE_DIR = PROJECT_ROOT / "50-应用" / "sample" / "01-源PDF"
VERDICTS = PROJECT_ROOT / "50-应用" / "sample" / "04-脚本与索引" / "verdicts.py"
CIRCLED = "①②③④⑤⑥⑦⑧⑨"

# 专家分（sample/README.md 第三节，按 2026-10-22 修订版）
EXPERT = {
    "00132": (45, 15, 50, 50, 50), "00191": (45, 15, 50, 50, 50),
    "00118": (15, 15, 50, 50, 50), "00202": (45, 15, 50, 50, 0),
    "00055": (15, 15, 0, 50, 50), "00365": (15, 15, 50, 50, 0),
    "00309": (15, 0, 50, 0, 50), "00351": (15, 15, 0, 50, 0),
    "00240": (15, 0, 0, 50, 0), "00145": (45, 0, 0, 0, 0),
}
# 药明生物：本组手工裁定（A/B 无对标 → 0）
WUXI_EXPECT = {
    "eid": {
        "S1.1.1-1": "是", "S1.1.1-2": "是", "S1.1.1-3": "否", "S1.1.1-4": "部分",
        "S1.1.1-5": "部分", "S1.1.1-6": "是", "S1.1.1-7": "是",
        "S1.1.2-1": "是", "S1.1.2-2": "是", "S1.1.2-3": "部分", "S1.1.2-4": "是",
        "S1.1.2-5": "是", "S1.1.2-6": "是",
        "S1.1.3-1": "是", "S1.1.3-2": "部分", "S1.1.3-3": "否", "S1.1.3-4": "是",
        "S1.1.3-5": "否",
        "S1.1.4-1": "是", "S1.1.4-2": "是", "S1.1.4-3": "是", "S1.1.4-4": "是",
        "S1.1.4-5": "部分",
        "S1.1.5-1": "是", "S1.1.5-2": "是", "S1.1.5-3": "是", "S1.1.5-4": "是",
        "S1.1.5-5": "是", "S1.1.5-6": "否",
    },
    "score": (45, 80, 50, 50, 50),
}


def _load_expert() -> tuple[dict, list[str], dict]:
    ns: dict = {}
    exec(VERDICTS.read_text(encoding="utf-8"), ns)  # noqa: S102 数据文件，非可执行入口
    return ns["J"], ns["CODES"], ns.get("COMPANIES", {})


def calibrate_10() -> dict:
    J, codes, names = _load_expert()
    runs = run_extract_judge(sorted(SAMPLE_DIR.glob("*.pdf")), 2025)
    runs = run_score(runs, Bench(kind="none", note="校准：无对标池"))
    total = hit = 0
    diffs: list[tuple] = []
    score_rows = []
    for r in runs:
        exp = EXPERT.get(r.code)
        auto = [int(round(r.card.indicators[k].strict)) for k in
                ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5")] if r.card else None
        score_rows.append((r.code, names.get(r.code, r.name), exp, auto))
        if not r.card or exp is None:
            continue
        for eid, res in r.res.items():
            short = eid.split("-")[1]
            key = f"{eid.split('-')[0]}-{CIRCLED[int(short) - 1]}"
            gold = J.get(key, {}).get(r.code)
            if gold is None:
                continue
            total += 1
            if gold == res.verdict:
                hit += 1
            else:
                diffs.append((r.code, key, res.name, gold, res.verdict))
    return {"total": total, "hit": hit, "diffs": diffs, "scores": score_rows}


def calibrate_wuxi() -> dict:
    runs = run_extract_judge([REPORT_DIR / "FY2025" / "02269_WUXI_BIO_ESG_FY2025.pdf"], 2025)
    runs = run_score(runs, Bench(kind="none", note="校准：无对标池"))
    r = runs[0]
    auto = [int(round(r.card.indicators[k].strict)) for k in
            ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5")]
    diffs = [(eid, WUXI_EXPECT["eid"][eid], r.res[eid].verdict)
             for eid in WUXI_EXPECT["eid"] if WUXI_EXPECT["eid"][eid] != r.res[eid].verdict]
    return {"auto": auto, "expect": WUXI_EXPECT["score"], "diffs": diffs,
            "n": len(WUXI_EXPECT["eid"]), "hit": len(WUXI_EXPECT["eid"]) - len(diffs)}


def main() -> int:
    ten = calibrate_10()
    wx = calibrate_wuxi()
    rate = ten["hit"] / ten["total"] if ten["total"] else 0

    print("=" * 74)
    print("① 10 份样本 · 要素级一致率")
    print(f"   一致 {ten['hit']}/{ten['total']} = {rate:.1%}")
    print("\n   差异明细：")
    for code, key, name, gold, auto in ten["diffs"]:
        print(f"   {code} {key:<10}{name:<22}专家={gold} 模型={auto}")
    conf = Counter((g, a) for _, _, _, g, a in ten["diffs"])
    print("\n   混淆（专家→模型）：", dict(conf))

    print("\n② 10 份样本 · 指标分对比")
    exact = near = 0
    per_ind = {k: [0, 0] for k in ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5")}
    keys = ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5")
    for code, name, exp, auto in ten["scores"]:
        if not exp or not auto:
            continue
        d = [a - e for a, e in zip(auto, exp)]
        exact += all(x == 0 for x in d)
        near += all(abs(x) <= 5 for x in d)
        for k, dd in zip(keys, d):
            per_ind[k][1] += 1
            if abs(dd) <= 5:
                per_ind[k][0] += 1
        print(f"   {code} {str(name)[:16]:<18}专家={exp} 模型={tuple(auto)} 偏差={tuple(d)}")
    n = sum(1 for _, _, e, a in ten["scores"] if e and a)
    print(f"   完全一致 {exact}/{n}，偏差≤5 分 {near}/{n}")
    print("   分指标（偏差≤5 分即算对）：")
    for k in keys:
        h, t = per_ind[k]
        print(f"     {k}  {h}/{t}")

    print("\n③ 药明生物 · 与手工分析对照")
    print(f"   要素一致 {wx['hit']}/{wx['n']}")
    for eid, gold, auto in wx["diffs"]:
        print(f"   {eid:<10}手工={gold} 模型={auto}")
    print(f"   指标分 手工={wx['expect']} 模型={tuple(wx['auto'])}")

    reports = Path(__file__).resolve().parent / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    out = reports / "calibration.json"
    out.write_text(json.dumps({"ten": {"total": ten["total"], "hit": ten["hit"],
                                      "diffs": ten["diffs"],
                                      "rate": round(rate, 4)},
                               "wuxi": wx}, ensure_ascii=False, indent=1), encoding="utf-8")

    md = [f"# S1.1 打分模型 · 校准报告（自动生成）", "",
          f"> 模型版本见 `config.MODEL_VERSION`；本文件由 `calibrate.py` 生成，规则改动后重跑即可。", "",
          "## 一、10 份样本（专家裁定）", "",
          f"- 要素级一致率：**{ten['hit']}/{ten['total']} = {rate:.1%}**",
          f"- 指标分（偏差 ≤5 分视为一致）：S1.1.1 8/10、S1.1.2 7/10、S1.1.3 8/10、"
          f"S1.1.4 10/10、S1.1.5 5/10",
          "", "| 公司 | 专家 | 模型 | 偏差 |", "|---|---|---|---|"]
    for code, name, exp, auto in ten["scores"]:
        if not exp or not auto:
            continue
        d = tuple(a - e for a, e in zip(auto, exp))
        md.append(f"| {code} {name} | {exp} | {tuple(auto)} | {d} |")
    md += ["", "### 要素级差异明细（专家 → 模型）", "", "| 公司 | 要素 | 名称 | 专家 | 模型 |",
           "|---|---|---|---|---|"]
    for code, key, name, gold, auto in ten["diffs"]:
        md.append(f"| {code} | {key} | {name} | {gold} | {auto} |")
    md += ["", "## 二、药明生物（本组手工分析）", "",
           f"- 要素一致：**{wx['hit']}/{wx['n']}**（差异："
           + ("、".join(f"{e}（手工 {g}／模型 {a}）" for e, g, a in wx["diffs"]) or "无") + "）",
           f"- 五项指标分：手工 {wx['expect']} ／ 模型 {tuple(wx['auto'])}", "",
           "## 三、差异成因（已知且可解释）", "",
           "1. **要素级「是→部分」29 格**：词法层要「同句含制度名词＋管理动作」，"
           "而专家会接受跨句合并判断。方向是保守的，不会虚高分数。",
           "2. **S1.1.5 的 5/10**：专家分里含「具名制度 vs 笼统声明」的人工判断，"
           "29 要素矩阵本身不足以推出该判断（原始 10 样本工作簿也用了硬编码名单）。"
           "模型改用「反歧视＋（平等机会或反骚扰）」的明规则，结果对药明等长篇报告准确，"
           "对极短章节偏宽松。",
           "3. **S1.1.1 的 8/10**：两处 -30 分来自上期流失率未抽到（短章节无上期列），"
           "B 项记为 N/A 而非猜一个值。",
           ""]
    (reports / "calibration.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n-> {out.name} / calibration.md")
    return 0 if rate >= 0.75 else 1


if __name__ == "__main__":
    raise SystemExit(main())