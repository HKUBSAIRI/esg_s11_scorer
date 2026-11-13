# -*- coding: utf-8 -*-
"""画「判档分歧矩阵」：20 家 × 5 项，一眼看出哪一格不稳、该先看哪。

只是画图脚本：数据全部读 ``reports/ensemble.json``（``python -m esg_s11_scorer.ensemble``
的产物），坐标与配色写死在这里，改图就改这里再跑一次。

    python docs/make_ensemble_chart.py

左图（矩阵）回答「这一格稳不稳」：
    绿 = 三票一致；琥珀 = 2/1 分歧；灰 = 闸门跳过（不调模型）；红框 = 主模型是少数派
右图（条形）回答「先看哪一类」：
    期望影响 = 分裂率 × 实测平均改档影响（分/项），并列单次翻转的总分代价
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch, Rectangle

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "reports" / "ensemble.json"
OUT = ROOT / "reports" / "ensemble_matrix.png"

# 与 xlstyle 的取色一致的浅底 + 深字；红了才说明「主模型与多数派相反」
C_AGREE = ("#E8F3E6", "#2F6B2A")
C_SPLIT = ("#FBEFD3", "#8A5A12")
C_GATE = ("#EFF1F3", "#8892A0")
C_EDGE = "#C0392B"          # 红框：今天第一票是少数派
C_DRIFT = "#2F5D8C"         # 角标：现行交付档位与今天不一致（旧口径）
INK = "#16324F"
BODY = "#2A2A2A"
MUTED = "#66707C"


def load() -> dict:
    if not DATA.is_file():
        raise SystemExit(f"找不到 {DATA}：先跑 python -m esg_s11_scorer.ensemble")
    return json.loads(DATA.read_text(encoding="utf-8"))


def cell_color(row: dict) -> tuple[str, str]:
    if row["split_type"] == "gate":
        return C_GATE
    if row["agreement"] == "3/3":
        return C_AGREE
    return C_SPLIT


def tiers_text(row: dict) -> str:
    """一致时只写一个档位，出现分歧才把三家并排写出来——让分歧自己跳出来。"""
    if row["split_type"] == "gate":
        return "—"
    vals = [row.get("t1"), row.get("t2"), row.get("t3")]
    if len(set(vals)) == 1:
        return "✗" if vals[0] is None else str(vals[0])
    return "·".join("✗" if v is None else str(v) for v in vals)


def main() -> int:
    data = load()
    rows, sumry = data["rows"], data["summary"]
    primary = data.get("primary") or {}
    voters = [v["name"] for v in data["voters"]]
    order = sorted(sumry["per_key"], key=lambda k: -sumry["per_key"][k]["exp"])
    codes = sorted({r["code"] for r in rows})
    by = {(r["code"], r["key"]): r for r in rows}

    def n_split(code: str) -> int:
        return sum(1 for k in order
                   if by[(code, k)]["agreement"] != "3/3"
                   and by[(code, k)]["split_type"] not in ("gate", "incomplete"))

    def impact(code: str) -> float:
        return sum(by[(code, k)].get("delta") or 0 for k in order)

    codes.sort(key=lambda c: (-n_split(c), -impact(c), c))

    fig = plt.figure(figsize=(15.4, 9.6), dpi=150)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.5, 1.0], wspace=0.16,
                          left=0.055, right=0.985, top=0.828, bottom=0.075)
    ax = fig.add_subplot(gs[0, 0])
    bx = fig.add_subplot(gs[0, 1])

    # ---- 左：判档矩阵 --------------------------------------------------
    cw, ch = 1.0, 1.0
    for j, key in enumerate(order):
        head = sumry["per_key"][key]
        ax.text(j + cw / 2, -1.62, key, ha="center", va="center",
                fontsize=10.5, fontweight="bold", color=INK)
        ax.text(j + cw / 2, -1.18, f"分裂 {head['split']}/{head['n']}",
                ha="center", va="center", fontsize=8.4, color=MUTED)
        ax.text(j + cw / 2, -0.82, f"改一档 {head['flip']:g} 分",
                ha="center", va="center", fontsize=8.0, color=MUTED)

    n_dot = n_drift = 0
    for i, code in enumerate(codes):
        ax.text(-0.14, i + ch / 2, code, ha="right", va="center",
                fontsize=9.6, color=BODY)
        ns = n_split(code)
        ax.text(len(order) + 0.14, i + ch / 2, f"{ns} 项 · {impact(code):g} 分",
                ha="left", va="center", fontsize=8.4,
                color=(C_EDGE if ns else MUTED), fontweight="bold" if ns else "normal")
        for j, key in enumerate(order):
            row = by[(code, key)]
            fc, tc = cell_color(row)
            split = row["agreement"] != "3/3" and row["split_type"] != "gate"
            # 红框只标"今天第一票与多数派不同"：格子里就能看见，读者能自己核对。
            # 现行交付档位（旧口径）另用角标写出来，不混进颜色语义。
            red = split and row.get("t1") is not None and row["t1"] != row["majority"]
            if red:
                n_dot += 1
            ax.add_patch(Rectangle((j + 0.03, i + 0.03), cw - 0.06, ch - 0.06,
                                   facecolor=fc,
                                   edgecolor=(C_EDGE if red else "#FFFFFF"),
                                   linewidth=2.0 if red else 0.8,
                                   zorder=2))
            ax.text(j + cw / 2, i + ch / 2, tiers_text(row), ha="center", va="center",
                    fontsize=8.6, color=tc, zorder=3,
                    fontweight="bold" if split else "normal")
            was = primary.get(f"{code}|{key}")
            if was is not None and row.get("t1") is not None and was != row["t1"]:
                n_drift += 1
                ax.text(j + cw - 0.06, i + 0.18, f"现{was}", ha="right", va="center",
                        fontsize=6.6, color=C_DRIFT, zorder=4)

    ax.set_xlim(-1.35, len(order) + 1.55)
    ax.set_ylim(len(codes) - 0.15, -1.95)
    ax.axis("off")
    ax.set_title("① 每一格稳不稳：三票给的是哪一档", loc="left", fontsize=13,
                 fontweight="bold", color=INK, pad=6)
    ax.text(-1.35, len(codes) + 0.42,
            "格内 = 档位；一致时只写一档，出现分歧才并排写三家（deepseek / qwen-max / qwen-plus）",
            fontsize=8.2, color=MUTED)

    # ---- 右：期望影响 --------------------------------------------------
    names = order[::-1]
    exps = [sumry["per_key"][k]["exp"] for k in names]
    bars = bx.barh(names, exps, height=0.5, color=C_SPLIT[0],
                   edgecolor=C_SPLIT[1], linewidth=1.1)
    for b, k in zip(bars, names):
        d = sumry["per_key"][k]
        y = b.get_y() + b.get_height() / 2
        bx.text(b.get_width() + 0.07, y + 0.12, f"{d['exp']:g} 分/项",
                va="center", fontsize=10.5, color=BODY, fontweight="bold")
        bx.text(b.get_width() + 0.07, y - 0.15,
                f"分裂 {d['split']}/{d['n']}（{d['rate']:.0%}）· 改一档 {d['flip']:g} 分",
                va="center", fontsize=8.2, color=MUTED)
    bx.set_xlim(0, max(exps) * 1.62)
    bx.set_xlabel("期望影响（分/项）= 分裂率 × 实测平均改档影响", fontsize=9.5, color=BODY)
    bx.set_title("② 先看哪一类：按「改档能不能改分」排", loc="left", fontsize=13,
                 fontweight="bold", color=INK, pad=6)
    bx.grid(axis="x", color="#E3E6EA", linewidth=0.8, zorder=0)
    bx.set_axisbelow(True)
    for side in ("top", "right", "left"):
        bx.spines[side].set_visible(False)
    bx.spines["bottom"].set_color("#C9CED6")
    bx.tick_params(axis="y", labelsize=11, colors=BODY, length=0)
    bx.tick_params(axis="x", labelsize=8.5, colors=MUTED)
    if "S1.1.4" in names:                       # 饱和项：分裂率最低，但一致地高
        bars[names.index("S1.1.4")].set_hatch("//")

    fig.suptitle("S1.1 定性判档分歧矩阵 · 20 家 × 5 项（只采集、不出分）",
                 x=0.055, ha="left", fontsize=16.5, fontweight="bold", color=INK, y=0.955)
    fig.text(0.055, 0.905,
             f"投票人：{'、'.join(voters)}　|　可判 {sumry['judgeable']} 项，"
             f"分裂 {sumry['split']} 项（{sumry['rate']:.1%}），闸门 {sumry['gate']} 项"
             f"　|　Fleiss' κ {sumry['kappa']:.2f}　|　"
             f"21 项若全改档合计 {sumry['delta_total']:g} 个总分点，"
             f"前 {sumry.get('first80', 0)} 项覆盖 80%",
             fontsize=9.6, color=BODY)
    fig.text(0.055, 0.876,
             "注意 S1.1.4（右图斜线）：分裂率最低（1/20），但 60 票里 55 票判 100、"
             "你们手工分析判 50 —— 「稳定」不等于「对」，一致性看不见系统性偏差。",
             fontsize=9.0, color=C_EDGE)
    ax.legend(handles=[
        Patch(facecolor=C_AGREE[0], edgecolor=C_AGREE[1], label="三票一致（3/3）"),
        Patch(facecolor=C_SPLIT[0], edgecolor=C_SPLIT[1], label="出现分歧（2/1 或 1-1-1）"),
        Patch(facecolor=C_GATE[0], edgecolor=C_GATE[1], label="闸门跳过（未披露离职率→0）"),
        Patch(facecolor="white", edgecolor=C_EDGE, linewidth=2,
              label=f"红框 = 第一票（今日模型）是少数派（{n_dot} 项）"),
        Patch(facecolor="white", edgecolor=C_DRIFT, linewidth=0.8,
              label=f"角标「现X」= 现行交付档位与今日不同（{n_drift} 项，旧口径）"),
    ], loc="lower left", bbox_to_anchor=(0.0, -0.095), ncol=3, frameon=False, fontsize=8.6)

    fig.savefig(OUT, facecolor="white")
    print(f"[写出] {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())