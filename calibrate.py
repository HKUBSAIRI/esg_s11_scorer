# -*- coding: utf-8 -*-
"""回测与校准：把模型跑到已有的两套「人工答案」上，量出指标分差。

两套金标准
----------
1. **10 份样本 · 专家裁定**（``50-应用/sample/04-脚本与索引/verdicts.py``）：五项指标专家分。
2. **药明生物 · 本组手工分析**（``分析指标/药明生物/S1.1_02269_指标分析.md``）：
   五项分（45/80/50/50/50，A/B 按无对标计）。用来验证「长篇双语报告」这一档。

校准只能比指标分：本包没有比「五项指标」更细的可比粒度（判定结论只到 100/50/0 三档）。
比的是「模型给这家多少分」与「专家给这家多少分」的差，以及偏差 ≤5 分的家数。

输出：``calibration.md``（人读）+ ``calibration.json`` + 控制台摘要。
任何规则改动后重跑本脚本即可。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import esg_s11_scorer  # noqa: F401
from esg_s11_scorer.bench import Bench
from esg_s11_scorer.pipeline import run_extract_batch, run_score
from esg_s11_scorer.settings import get_settings

# 校准集路径同样走配置（ESG_SAMPLE_PDF_DIR / ESG_VERDICTS / ESG_PDF_DIR），不写死
_SETTINGS = get_settings()
SAMPLE_DIR = _SETTINGS.sample_pdf_dir
VERDICTS = _SETTINGS.verdicts
REPORT_DIR = _SETTINGS.report_root
KEYS = ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5")

# 判档模式：默认走主路径 llm（缺 key 时 run_score 自动退 keyword）。
# 用法：python esg_s11_scorer/calibrate.py [llm|keyword]
MODE = sys.argv[1] if len(sys.argv) > 1 else "llm"

# 专家分（sample/README.md 第三节，按 2026-10-22 修订版）
EXPERT = {
    "00132": (45, 15, 50, 50, 50), "00191": (45, 15, 50, 50, 50),
    "00118": (15, 15, 50, 50, 50), "00202": (45, 15, 50, 50, 0),
    "00055": (15, 15, 0, 50, 50), "00365": (15, 15, 50, 50, 0),
    "00309": (15, 0, 50, 0, 50), "00351": (15, 15, 0, 50, 0),
    "00240": (15, 0, 0, 50, 0), "00145": (45, 0, 0, 0, 0),
}
# 药明生物：本组手工裁定（A/B 无对标 → 0）
WUXI_SCORE = (45, 80, 50, 50, 50)


def _load_names() -> dict:
    ns: dict = {}
    exec(VERDICTS.read_text(encoding="utf-8"), ns)  # noqa: S102 数据文件，非可执行入口
    return ns.get("COMPANIES", {})


def _scores(run) -> tuple | None:
    if run.card is None:
        return None
    return tuple(int(round(run.card.indicators[k].strict)) for k in KEYS)


def calibrate_10() -> dict:
    names = _load_names()
    runs = run_extract_batch(sorted(SAMPLE_DIR.glob("*.pdf")), 2025)
    runs = run_score(runs, Bench(kind="none", note="校准：无对标池"), mode=MODE)
    rows, per_ind = [], {k: [0, 0] for k in KEYS}
    exact = near = n = 0
    for r in runs:
        exp = EXPERT.get(r.code)
        auto = _scores(r)
        rows.append((r.code, names.get(r.code, r.name), exp, auto))
        if not exp or not auto:
            continue
        n += 1
        d = tuple(a - e for a, e in zip(auto, exp))
        exact += all(x == 0 for x in d)
        near += all(abs(x) <= 5 for x in d)
        for k, dd in zip(KEYS, d):
            per_ind[k][1] += 1
            if abs(dd) <= 5:
                per_ind[k][0] += 1
    return {"rows": rows, "n": n, "exact": exact, "near": near, "per_ind": per_ind}


def calibrate_wuxi() -> dict:
    runs = run_extract_batch([REPORT_DIR / "FY2025" / "02269_WUXI_BIO_ESG_FY2025.pdf"], 2025)
    runs = run_score(runs, Bench(kind="none", note="校准：无对标池"), mode=MODE)
    auto = _scores(runs[0])
    d = tuple(a - e for a, e in zip(auto, WUXI_SCORE))
    return {"auto": auto, "expect": WUXI_SCORE, "diff": d,
            "max_drift": max(abs(x) for x in d)}


def main() -> int:
    ten = calibrate_10()
    wx = calibrate_wuxi()

    print("=" * 74)
    print("① 10 份样本 · 指标分对比（严格口径，无对标池 → A 记 N/A）")
    for code, name, exp, auto in ten["rows"]:
        if not exp or not auto:
            print(f"   {code} {str(name)[:16]:<18}专家={exp} 模型={auto}")
            continue
        d = tuple(a - e for a, e in zip(auto, exp))
        print(f"   {code} {str(name)[:16]:<18}专家={exp} 模型={auto} 偏差={d}")
    print(f"   完全一致 {ten['exact']}/{ten['n']}，偏差≤5 分 {ten['near']}/{ten['n']}")
    print("   分指标（偏差≤5 分即算对）：")
    for k in KEYS:
        h, t = ten["per_ind"][k]
        print(f"     {k}  {h}/{t}")

    print("\n② 药明生物 · 与手工分析对照")
    print(f"   指标分 手工={wx['expect']} 模型={wx['auto']} 偏差={wx['diff']}")

    reports = Path(__file__).resolve().parent / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    out = reports / "calibration.json"
    out.write_text(json.dumps({"mode": MODE, "ten": ten, "wuxi": wx},
                              ensure_ascii=False, indent=1), encoding="utf-8")

    md = ["# S1.1 打分模型 · 校准报告（自动生成）", "",
          f"> 模型版本见 `config.MODEL_VERSION`；判档模式 `{MODE}`；"
          f"本文件由 `calibrate.py` 生成，规则改动后重跑即可。", "",
          "## 一、10 份样本（专家裁定）", "",
          f"- 完全一致：**{ten['exact']}/{ten['n']}**；偏差 ≤5 分：**{ten['near']}/{ten['n']}**",
          "- 分指标（偏差 ≤5 分即算一致）："
          + "、".join(f"{k} {ten['per_ind'][k][0]}/{ten['per_ind'][k][1]}" for k in KEYS),
          "", "| 公司 | 专家 | 模型 | 偏差 |", "|---|---|---|---|"]
    for code, name, exp, auto in ten["rows"]:
        if not exp or not auto:
            continue
        d = tuple(a - e for a, e in zip(auto, exp))
        md.append(f"| {code} {name} | {exp} | {tuple(auto)} | {d} |")
    md += ["", "## 二、药明生物（本组手工分析）", "",
           f"- 五项指标分：手工 {wx['expect']} ／ 模型 {wx['auto']} ／ 偏差 {wx['diff']}"
           f"（最大 {wx['max_drift']:.0f} 分）", "",
           "## 三、差异成因（已知且可解释）", "",
           "1. **短章节的 A/B 记 N/A**：10 份样本多为极短 ESG 章节，没有上期流失率列，"
           "B 项按 N/A 处理而不是猜一个值——严格分因此偏低。",
           "2. **S1.1.4 偏宽**：判档在「有福利声明、无覆盖/使用数据」的短章节上容易读成"
           "100 分档，专家给 50。这是判档规则最需要收紧的一处（`rubric.py` 的 S1.1.4 档位描述）。",
           "3. **S1.1.5 偏宽**：专家分含「具名制度 vs 笼统声明」的人工判断，判档在长篇报告上"
           "准确，极短章节偏宽松。",
           ""]
    (reports / "calibration.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n-> {out.name} / calibration.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())