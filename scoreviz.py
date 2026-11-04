# -*- coding: utf-8 -*-
"""得分构成图册：每个公司 S1.1 得几分、这分由哪些项凑出来的。

为什么单独出一本
----------------
批次汇总只给「五项各多少分」，读者仍要自己乘权重才能对上总分；而核对分数时最容易出错的
恰恰是这一步（0.4×75 写成 0.4×80 之类）。所以这里把**每一分钱的来路**摊开：

    分项得分 × 分项权重 × 指标权重 = 折合综合分，五项加总 = 总分

三张表 + 一张堆叠图
------------------
1. ``总分与构成``：上表＝五项得分与总分；下表＝各指标对总分的折合贡献；图＝堆叠条形，
   一条 = 一家公司，色块 = 五个指标的贡献，长度 = 总分。谁的分高、高在哪一目了然。
2. ``分项明细``：A/B/C 三级，每行一个「分项得分 → 折合综合分」，带原说明。
3. ``口径``：权重与三档制说明。数字离开权重就没法复核。

所有数字都取自已发布的 ``ScoreCard``，本模块只做算术的**展示**，不再判分。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .config import IND_WEIGHTS, MODEL_VERSION, SPECS, WEIGHT_SUM

HEAD, TITLE, LIGHT = "FF4472C4", "FF2F5597", "FFD9E2F3"
BD = "FFBFBFBF"
_thin = Side(style="thin", color=BD)
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
F_T = Font(name="微软雅黑", size=13, bold=True, color="FFFFFFFF")
F_H = Font(name="微软雅黑", size=10, bold=True, color="FFFFFFFF")
F_B = Font(name="微软雅黑", size=10)
F_BB = Font(name="微软雅黑", size=10, bold=True)
F_BIG = Font(name="微软雅黑", size=12, bold=True)
F_N = Font(name="微软雅黑", size=9, color="FF595959")
CEN = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEF = Alignment(horizontal="left", vertical="center", wrap_text=True)

KEYS = [s.ind for s in SPECS]


def _text_units(text: str) -> int:
    """估算显示宽度：CJK 记 2，ASCII 记 1（与 Excel 列宽单位一致）。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(text))


def _lines_for(text: str, width: int) -> int:
    return max(1, -(-_text_units(text) // max(8, width - 2)))


def _title(ws, text: str, ncol: int) -> None:
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    c = ws.cell(row=1, column=1, value=text)
    c.font, c.fill, c.alignment = F_T, PatternFill("solid", start_color=TITLE), CEN
    ws.row_dimensions[1].height = 30


def _head(ws, row: int, heads: list[str], widths: list[int]) -> None:
    for j, (t, w) in enumerate(zip(heads, widths), start=1):
        c = ws.cell(row=row, column=j, value=t)
        c.font, c.fill, c.alignment, c.border = F_H, PatternFill(
            "solid", start_color=HEAD), CEN, BORDER
        ws.column_dimensions[c.column_letter].width = w
    ws.row_dimensions[row].height = 30


def contribution(ind_weight: float, comp_weight: float, score: float | None) -> float:
    """分项折合到百分制总分的贡献：指标权重 × 分项权重 × 分项得分。"""
    if score is None:
        return 0.0
    return round(ind_weight / 100 * comp_weight * score, 2)


def rows_of(card) -> list[dict]:
    """一张记分卡 → 分项明细行（含折合综合分）。"""
    out: list[dict] = []
    for k in KEYS:
        ind = card.indicators[k]
        for c in ind.components:
            if ind.kind == "tiered":
                w, sc = 1.0, (ind.raw or 0)
                label = "档位"
            else:
                w, sc = c.weight, c.score
                label = c.key or "档位"
            out.append({
                "code": card.code, "name": card.name, "ind": k, "ind_name": ind.name,
                "ind_weight": ind.weight, "comp": label, "comp_name": c.name,
                "comp_weight": w, "score": sc,
                "contrib": contribution(ind.weight, w, sc),
                "note": c.note,
            })
    return out


def write(views, out_path: str | Path, *, year: int) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    codes = [v.code for v in views]
    names = {v.code: v.name for v in views}

    wb = Workbook()
    ws = wb.active
    ws.title = "总分与构成"
    _title(ws, f"S1.1 得分及构成（{year}，{len(codes)} 家）　"
               f"总分 = Σ 指标权重 × 指标得分　（模型 {MODEL_VERSION}）", len(KEYS) + 4)

    # ---- 上表：五项得分与总分
    heads = ["代码", "公司"] + [f"{k}\n({IND_WEIGHTS[k]}%)" for k in KEYS] + \
            ["加权总分\n(严格)", "加权总分\n(归一)"]
    _head(ws, 2, heads, [9, 28] + [11] * len(KEYS) + [12, 12])
    r = 3
    for v in views:
        card = v.card
        vals = [v.code, v.name] + [card.indicators[k].strict for k in KEYS] + \
               [card.total_strict, card.total_normalized(WEIGHT_SUM)]
        for j, val in enumerate(vals, start=1):
            c = ws.cell(row=r, column=j, value=val)
            c.border = BORDER
            c.font = F_BIG if j in (2 + len(KEYS) + 1,) else F_B
            c.alignment = LEF if j == 2 else CEN
        ws.row_dimensions[r].height = 24
        r += 1

    # ---- 下表：各指标对总分的折合贡献（与上表同一批数字，只是换了个加法顺序）
    r += 1
    head_r = r
    c0 = ws.cell(row=head_r, column=1, value="构成：折合综合分（权重×得分）")
    c0.font = F_BB
    ws.merge_cells(start_row=head_r, start_column=1, end_row=head_r, end_column=2)
    for j, k in enumerate(KEYS, start=3):
        c = ws.cell(row=head_r, column=j, value=f"{k}\n{IND_WEIGHTS[k]}%")
        c.font, c.alignment, c.border = F_BB, CEN, BORDER
    c = ws.cell(row=head_r, column=3 + len(KEYS), value="合计")
    c.font, c.alignment, c.border = F_BB, CEN, BORDER
    r = head_r + 1
    for v in views:
        card = v.card
        ws.cell(row=r, column=1, value=v.code).font = F_B
        ws.cell(row=r, column=2, value=v.name).font = F_B
        total = 0.0
        for j, k in enumerate(KEYS, start=3):
            ind = card.indicators[k]
            val = round(ind.weight / 100 * ind.strict, 2)
            total += val
            cell = ws.cell(row=r, column=j, value=val)
            cell.font, cell.alignment = F_B, CEN
            cell.border = BORDER
        cell = ws.cell(row=r, column=3 + len(KEYS), value=round(total, 2))
        cell.font, cell.alignment, cell.border = F_BB, CEN, BORDER
        ws.row_dimensions[r].height = 20
        r += 1

    chart = BarChart()
    chart.type, chart.style, chart.grouping, chart.overlap = "bar", 10, "stacked", 100
    chart.title = "总分构成（色块＝各指标贡献，长度＝加权总分）"
    # 条形图里分类轴（公司）在左、值轴（得分）在下；openpyxl 的 x_axis 恒指分类轴
    chart.x_axis.title, chart.y_axis.title = "公司", "得分"
    chart.y_axis.scaling.min, chart.y_axis.scaling.max = 0, 100
    chart.add_data(Reference(ws, min_col=3, max_col=2 + len(KEYS),
                            min_row=head_r, max_row=head_r + len(codes)),
                   titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=2, min_row=head_r + 1,
                                  max_row=head_r + len(codes)))
    chart.legend.position = "b"
    chart.height, chart.width = 9, 20
    ws.add_chart(chart, f"A{r + 1}")

    # ---- 分项明细
    ws2 = wb.create_sheet("分项明细")
    _title(ws2, "分项明细：分项得分 × 分项权重 × 指标权重 = 折合综合分", 10)
    _head(ws2, 2, ["代码", "公司", "指标", "指标权重", "分项", "分项权重", "分项得分",
                   "折合综合分", "说明"],
          [9, 34, 8, 10, 8, 10, 10, 12, 74])
    r = 3
    for v in views:
        for row in rows_of(v.card):
            vals = [row["code"], row["name"], row["ind"], f"{row['ind_weight']}%",
                    row["comp"], row["comp_weight"], row["score"], row["contrib"],
                    row["note"]]
            for j, val in enumerate(vals, start=1):
                c = ws2.cell(row=r, column=j, value=val)
                c.border, c.font = BORDER, (F_BB if j == 8 else F_B)
                c.alignment = LEF if j == 9 else CEN
            ws2.row_dimensions[r].height = max(
                20, max(_lines_for(row["note"], 74),
                        _lines_for(row["name"], 34)) * 14 + 8)
            r += 1
    ws2.freeze_panes = "C3"

    # ---- 口径
    ws3 = wb.create_sheet("口径")
    _title(ws3, "权重与计分口径", 2)
    ws3.column_dimensions["A"].width = 26
    ws3.column_dimensions["B"].width = 104
    rows = [("模型版本", MODEL_VERSION),
            ("权重合计", f"{WEIGHT_SUM}%（" + "+".join(str(IND_WEIGHTS[k]) for k in KEYS) + "）"),
            ("加权项", "S1.1.1 / S1.1.2：得分 = 0.4×A + 0.3×B + 0.3×C；"
                       "A/B 为同业分位（100/75/50/25/0），C 为定性判档（100/50/0）"),
            ("三档制", "S1.1.3 / S1.1.4 / S1.1.5：直接取档位 100 / 50 / 0，不再加权"),
            ("总分（严格）", "分项不可评按 0 计入，与历史口径可比"),
            ("总分（归一）", "只按可观测权重折算成百分制；两者相等说明没有 N/A 项"),
            ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("", ""),
            ("怎么复核", "任取一格：分项得分 × 分项权重 × 指标权重，应等于「折合综合分」；"
                        "同一指标的各项折合分相加，应等于该指标对上表的贡献。"),
            ("舍入", "折合综合分保留两位小数、加权总分保留一位；两者相差 0.05 以内属正常舍入，"
                     "不是算错。")]
    r = 3
    for k, val in rows:
        a = ws3.cell(row=r, column=1, value=k)
        a.font, a.fill, a.alignment, a.border = F_BB, PatternFill(
            "solid", start_color=LIGHT), LEF, BORDER
        b = ws3.cell(row=r, column=2, value=val)
        b.font, b.alignment, b.border = F_B, LEF, BORDER
        ws3.row_dimensions[r].height = max(20, -(-len(str(val)) // 66) * 15 + 4)
        r += 1

    wb.properties.title = f"S1.1 得分构成 {year}"
    wb.save(out)
    return out


def from_batch(batch, out_dir: str | Path) -> Path:
    year = batch.year
    return write(batch.views, Path(out_dir) / f"S1.1_得分构成_{year}.xlsx", year=year)


__all__ = ["rows_of", "contribution", "write", "from_batch", "KEYS"]