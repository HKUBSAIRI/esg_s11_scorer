# -*- coding: utf-8 -*-
"""画「判档体检 · 一页看懂」：只有两段，不带任何权重/影响分。

    1）96 项里 75 项三家完全一致（可以直接用），21 项有分歧（要人看）；
    2）这 21 项按指标分组列出来，每项写清三家各给了几档、多数派是几档。

不画「改档影响」「期望影响」这类折算——那是排工作量的工具，不是判档本身的问题。
细节版（20 家 × 5 项矩阵、引用页码）在 ``docs/make_ensemble_chart.py``。

    python docs/make_ensemble_simple.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "reports" / "ensemble.json"
OUT = ROOT / "reports" / "ensemble_simple.png"

# 与 rubric.RUBRICS 的 name 一致；改名时同步这里（只是图上的标题）
NAMES = {
    "S1.1.1-C": "雇佣关系与就业保障（定性分项）",
    "S1.1.2-C": "薪酬保障（定性分项）",
    "S1.1.3": "工作时间与休息休假保障",
    "S1.1.4": "员工福利与支持",
    "S1.1.5": "平等机会、反歧视与反骚扰",
}
ORDER = ["S1.1.1-C", "S1.1.2-C", "S1.1.3", "S1.1.4", "S1.1.5"]

C_OK = ("#E8F3E6", "#2F6B2A")
C_SPLIT = ("#FBEFD3", "#8A5A12")
C_HEAD = "#F4F6F9"
INK = "#16324F"
BODY = "#2A2A2A"
MUTED = "#66707C"


def main() -> int:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    s, rows = data["summary"], data["rows"]
    todo = [r for r in rows if r["split_type"] not in ("none", "gate")]

    groups: dict[str, list[dict]] = {}
    for r in todo:
        groups.setdefault(r["key"], []).append(r)
    keys = sorted(groups, key=lambda k: (-len(groups[k]), ORDER.index(k)))
    for k in keys:
        groups[k].sort(key=lambda r: (r["code"], r["key"]))

    # 版面：每组 1 行表头 + n 行明细
    units: list[tuple[str, object]] = []
    for k in keys:
        units.append(("head", (k, len(groups[k]))))
        units += [("item", r) for r in groups[k]]
    n_units = len(units)

    fig = plt.figure(figsize=(13.2, 1.85 + 0.365 * n_units), dpi=150)
    fig.patch.set_facecolor("white")

    # ---------------- 上：一句话总账 ----------------
    top = fig.add_axes([0.06, 0.878, 0.90, 0.062])
    top.set_xlim(0, s["judgeable"])
    top.set_ylim(0, 1)
    top.axis("off")
    agree = s["judgeable"] - s["split"]
    top.add_patch(FancyBboxPatch((0, 0.18), agree, 0.62,
                                 boxstyle="round,pad=0,rounding_size=0.4",
                                 facecolor=C_OK[0], edgecolor=C_OK[1], linewidth=1.6))
    top.add_patch(FancyBboxPatch((agree, 0.18), s["split"], 0.62,
                                 boxstyle="round,pad=0,rounding_size=0.4",
                                 facecolor=C_SPLIT[0], edgecolor=C_SPLIT[1], linewidth=1.6))
    top.text(agree / 2, 0.49, f"{agree} 项：三家完全一致 → 可以直接用",
             ha="center", va="center", fontsize=14, color=C_OK[1], fontweight="bold")
    top.text(s["judgeable"] - s["split"] / 2, 0.49, f"{s['split']} 项\n有分歧",
             ha="center", va="center", fontsize=10.5, color=C_SPLIT[1],
             fontweight="bold", linespacing=1.35)
    fig_caption = (f"共 {s['judgeable']} 项可判档；另有 {s['gate']} 项闸门："
                   f"没有披露离职率，规则直接判 0，不问模型")

    # ---------------- 下：按指标分组的分歧清单 ----------------
    ax = fig.add_axes([0.055, 0.042, 0.905, 0.80])
    ax.set_xlim(0, 10)
    ax.set_ylim(-0.25, n_units + 1.0)
    ax.axis("off")

    ax.text(0.05, n_units + 0.72, "公司", fontsize=10, color=MUTED, fontweight="bold")
    ax.text(1.15, n_units + 0.72, "三家档位（deepseek / qwen-max / qwen-plus）→ 多数派",
            fontsize=10, color=MUTED, fontweight="bold")
    ax.text(9.95, n_units + 0.72, "分歧项", fontsize=10, color=MUTED,
            fontweight="bold", ha="right")
    ax.plot([0.05, 9.95], [n_units + 0.5, n_units + 0.5], color="#C9CED6", linewidth=1.0)

    y = n_units - 0.15
    for kind, payload in units:
        if kind == "head":
            key, cnt = payload
            ax.add_patch(Rectangle((0.05, y - 0.46), 9.9, 0.82,
                                   facecolor=C_HEAD, edgecolor="none", zorder=1))
            ax.text(0.16, y - 0.02, f"{key}　{NAMES.get(key, '')}", va="center",
                    fontsize=12, color=INK, fontweight="bold", zorder=2)
            ax.text(9.9, y - 0.02, f"{cnt} 项", va="center", ha="right",
                    fontsize=10.5, color=C_SPLIT[1], fontweight="bold", zorder=2)
            y -= 1.02
        else:
            r = payload
            ax.text(0.16, y, r["code"], va="center", fontsize=11, color=BODY)
            ax.text(1.15, y, f"{r['t1']} / {r['t2']} / {r['t3']}", va="center",
                    fontsize=11, color=BODY)
            ax.text(3.05, y, "→", va="center", fontsize=11, color=MUTED)
            ax.text(3.35, y, f"多数派 {r['majority']}", va="center",
                    fontsize=11, color=C_SPLIT[1], fontweight="bold")
            y -= 0.86

    fig.text(0.055, 0.985, "S1.1 判档体检：96 项里 75 项可以直接用，21 项有分歧",
             fontsize=18, fontweight="bold", color=INK, ha="left", va="top")
    fig.text(0.055, 0.958,
             f"投票人：{'、'.join(v['name'] for v in data['voters'])}　|　"
             f"分歧率 {s['rate']:.1%}　|　三票一致性 κ {s['kappa']:.2f}　|　"
             f"下面按指标分组，同组里按公司代码排",
             fontsize=10.5, color=BODY, ha="left", va="top")
    fig.text(0.055, 0.856, fig_caption, fontsize=9.5, color=MUTED, ha="left", va="top")
    fig.text(0.055, 0.016,
             "读法：一行 = 一家公司的一个指标；「a / b / c」是三家模型各给的档位（100 / 50 / 0），"
             "多数派就是三票里出现两次的那个档。",
             fontsize=9.6, color=MUTED, ha="left", va="bottom")

    fig.savefig(OUT, facecolor="white")
    print(f"[写出] {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())