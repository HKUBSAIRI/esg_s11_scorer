# -*- coding: utf-8 -*-
"""生成 pipeline 流程图（docs/pipeline_flow.png）。

只是画图脚本：坐标写死在这里，改图就改这里再跑一次。

    python docs/make_pipeline_diagram.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

HERE = Path(__file__).resolve().parent

C_IN = ("#E9EFF8", "#5C7FA8")
C_QTY = ("#DCE9F8", "#3C6DA5")      # 定量支路
C_QLT = ("#E4F1E2", "#4B8C45")      # 定性支路
C_LLM = ("#D8EFD5", "#3E7B3A")      # LLM 判档
C_SCORE = ("#FBE8D3", "#B96A1C")    # 合流 / 屏障
C_OUT = ("#FAF3D8", "#A07A16")      # 产出

INK = "#16324F"
BODY = "#2A2A2A"
MUTED = "#66707C"


def box(ax, cx, cy, w, h, title, lines=(), foot="", pal=("#fff", "#333"),
        ls="-", lw=1.8, tfs=11.2, bfs=8.2):
    fc, ec = pal
    ax.add_patch(FancyBboxPatch(
        (cx - w / 2, cy - h / 2), w, h,
        boxstyle="round,pad=0,rounding_size=1.6",
        facecolor=fc, edgecolor=ec, linewidth=lw,
        linestyle=ls, zorder=2))
    ax.text(cx, cy + h / 2 - 1.5, title, ha="center", va="top",
            fontsize=tfs, fontweight="bold", color=INK, zorder=3)
    y = cy + h / 2 - 4.0
    for ln in lines:
        ax.text(cx, y, ln, ha="center", va="top", fontsize=bfs, color=BODY, zorder=3)
        y -= 2.35
    if foot:
        ax.text(cx, cy - h / 2 + 1.2, foot, ha="center", va="bottom",
                fontsize=7.2, color=MUTED, style="italic", zorder=3)


def arrow(ax, p0, p1, color="#44506B", lw=1.9, rad=0.0, ls="-"):
    ax.add_patch(FancyArrowPatch(
        p0, p1, arrowstyle="-|>", mutation_scale=14, linewidth=lw,
        color=color, linestyle=ls, shrinkA=0, shrinkB=0,
        connectionstyle=f"arc3,rad={rad}", zorder=1))


def main() -> Path:
    fig, ax = plt.subplots(figsize=(14.2, 8.4))
    ax.set_xlim(0, 180)
    ax.set_ylim(0, 106)
    ax.set_aspect("equal")
    ax.axis("off")

    ax.text(90, 101.2, "esg_s11_scorer · 当前管道：定量分档 + 定性判档（s11-model-1.3）",
            ha="center", va="center", fontsize=16.5, fontweight="bold", color=INK)
    ax.text(90, 96.6,
            "输入 1–N 份 ESG 报告 PDF  →  五项指标分（严格 / 归一 / 可达上限）+ 分项证据 + 批内分位",
            ha="center", va="center", fontsize=9.0, color="#4B5563")

    # 支路说明
    ax.text(58, 89.6, "定量支路：数值 → 档位", ha="left", va="center",
            fontsize=8.6, color="#2F6395", fontweight="bold")
    ax.text(18, 89.6, "共用前缀", ha="left", va="center", fontsize=8.6, color="#66707C")
    ax.text(18, 18.6, "定性支路：段落 → 三档", ha="left", va="center",
            fontsize=8.6, color="#3D7A38", fontweight="bold")
    ax.text(18, 14.8, "（5 个 rubric：S1.1.1-C / S1.1.2-C / S1.1.3 / S1.1.4 / S1.1.5）",
            ha="left", va="center", fontsize=7.8, color="#66707C")

    # 判档硬约束（填左侧空白，也是对「为什么不许四舍五入」的说明）
    box(ax, 22, 56, 32, 12,
        "判档硬约束",
        ["档位只能 100 / 50 / 0", "模型给别的值 → 无效，降级兜底"],
        "", pal=("#FBFBFC", "#9AA3AF"), ls=(0, (4, 3)), lw=1.2, tfs=9.6, bfs=7.9)

    # ---- 共用前缀 --------------------------------------------------------
    box(ax, 16, 76, 26, 14,
        "输入 PDF × N", ["ESG_PDF_DIR", "目录或单文件"],
        "文件名→代码+公司名", pal=C_IN)
    box(ax, 44, 76, 26, 14,
        "parse 解析", ["pypdf→页级文本", "s11_text 只读"],
        "parse.json", pal=C_IN)

    # ---- 定量支路 --------------------------------------------------------
    box(ax, 72, 76, 26, 14,
        "facts 抽数", ["流失率 / 差距", "年份对齐+override"],
        "facts.json", pal=C_QTY)
    box(ax, 100, 76, 26, 14,
        "prior 上期值", ["附录→上年→N/A", "跨年：读上年 facts"],
        "prior.json", pal=C_QTY)
    box(ax, 128, 76, 26, 14,
        "bench 分位", ["n 家互为对标", "快照 id（屏障）"],
        "bench_rows", pal=C_SCORE)

    # ---- 定性支路 --------------------------------------------------------
    box(ax, 44, 30, 26, 14,
        "召回段落", ["E/P/T/W/D 证据组", "命中数取前 12 段"],
        "keywords.csv", pal=C_QLT)
    box(ax, 72, 30, 26, 14,
        "盲评脱敏", ["公司名→[COMPANY]", "有残留即不发送"],
        "blind.py + 别名表", pal=C_QLT)
    box(ax, 100, 30, 26, 14,
        "LLM 判档", ["rubric 三档判据", "失败→关键词兜底"],
        "qualitative-<mode>.json", pal=C_LLM)

    # ---- 合流与产出 ------------------------------------------------------
    box(ax, 128, 53, 26, 15,
        "score 合成", ["0.4A+0.3B+0.3C", "三档 100/50/0", "N/A≠0 双口径"],
        "score.json", pal=C_SCORE)
    box(ax, 128, 31, 26, 13,
        "publish 落表", ["parquet / JSONL", "报告只读表"],
        "tables/FY{y}/*", pal=C_SCORE)
    box(ax, 160, 44, 26, 13,
        "report 报告", ["批次汇总（默认）", "单家 xlsx 按需"],
        "打分结果/FY{y}/", pal=C_OUT)
    box(ax, 160, 22, 26, 13,
        "viz 图册", ["得分构成", "定性判档"],
        "*.png", pal=C_OUT)

    # ---- 箭头 ------------------------------------------------------------
    arrow(ax, (29, 76), (31, 76))
    arrow(ax, (57, 76), (59, 76))
    arrow(ax, (85, 76), (87, 76))
    arrow(ax, (113, 76), (115, 76))
    arrow(ax, (44, 69), (44, 37))          # parse → 定性支路
    arrow(ax, (57, 30), (59, 30))
    arrow(ax, (85, 30), (87, 30))
    arrow(ax, (128, 69), (128, 60.5))      # bench → score
    arrow(ax, (104, 37), (122, 45.5), rad=-0.18)   # 判档 → score
    arrow(ax, (128, 45.5), (128, 37.5))    # score → publish
    arrow(ax, (141, 34), (147, 40.5), rad=0.15)
    arrow(ax, (141, 28), (147, 25.5), rad=-0.15)

    # ---- 底部说明 --------------------------------------------------------
    ax.add_patch(FancyBboxPatch((2, 0.5), 137, 11.4,
                                boxstyle="round,pad=0,rounding_size=1.0",
                                facecolor="#F7F8FA", edgecolor="#C9CED6",
                                linewidth=1.0, zorder=0))
    notes = [
        ("权重", "S1.1.1 25 / S1.1.2 30 / S1.1.3 15 / S1.1.4 15 / S1.1.5 15；前两项 = 0.4A+0.3B+0.3C，后三项 = 三档"),
        ("定性与定量", "关键词只决定「给模型看哪几段」，档位由 rubric 三档判据 + LLM 判；兜底按证据层级（制度+执行才 100）"),
        ("可复现", "判档指纹含 rubric + 词表 + 模型 + 别名表 + 模式 + 源码；改任一项旧结论作废，一致则整段复用"),
        ("判档模式", "--qualitative llm（默认，盲评）/ keyword（离线兜底）；判档结论直接是分（100/50/0）"),
    ]
    y = 9.4
    for tag, txt in notes:
        ax.text(5.0, y, tag, ha="left", va="center", fontsize=7.9,
                fontweight="bold", color="#374151")
        ax.text(23.0, y, txt, ha="left", va="center", fontsize=7.9, color="#4B5563")
        y -= 2.55

    out = HERE / "pipeline_flow.png"
    fig.savefig(out, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"[写出] {out}")
    return out


if __name__ == "__main__":
    main()