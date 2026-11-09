# -*- coding: utf-8 -*-
"""定性判档的可视化产物：一个工作簿，把 LLM/兜底的判档结果摆成人能一眼看懂的形。

四张表 + 两张图
---------------
1. ``档位矩阵``：行＝5 个定性评分点，列＝公司，格子填 0/50/100 并上色——**这就是热图**。
   一眼能看出「哪一项全批一个档」和「哪一家在某一项上掉队」。
2. ``判档明细``：一行一个判档，带出处（llm/keyword/gate）、模型、脱敏处数、理由、缺口、
   依据原文。理由和依据是复核时唯一要看的东西，所以不截断成摘要。
3. ``档位图``：分组柱状图（指标 × 公司）+ 各公司定性均分柱状图。用 openpyxl 原生图表，
   不引绘图库——本包的可视化不该多一个依赖。
4. ``口径与台账``：三档判据原文 + rubric/模型指纹 + 生成时间。分数离开口径就没法解释，
   所以口径要跟图放在同一本册子里。

为什么不直接画到批次汇总里：批次汇总只讲「分数」，而判档讲「依据」。两者更新节奏不同
（改 rubric 会同时变，改报告样式只动汇总），分开更省事，也便于单独发给业务方复核。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Font, PatternFill          # 条件格式用；样式基元取自 xlstyle

from .rubric import RUBRICS
from .xlstyle import (BORDER, CEN, F_B, F_BB, F_H, F_N, HEAD, LEF, LIGHT, TIER_FILL,
                      head, title)

SOURCE_CN = {"llm": "LLM 判档", "keyword": "关键词兜底", "gate": "0 分前置条件"}


def collect(views, verdicts_by_code: dict) -> list[dict]:
    """把「公司视图 + 判档产物」摊平成行（按公司、再按 rubric 声明顺序）。"""
    rows: list[dict] = []
    for v in views:
        vs = verdicts_by_code.get(v.code, {})
        for key, rub in RUBRICS.items():
            vd = vs.get(key)
            rows.append({
                "code": v.code, "name": v.name, "key": key,
                "label": f"{key} {rub.name}",
                "tier": None if vd is None else int(vd.tier),
                "source": "" if vd is None else SOURCE_CN.get(vd.source, vd.source),
                "model": "" if vd is None else vd.model,
                "alias_hits": 0 if vd is None else vd.alias_hits,
                "n_passages": 0 if vd is None else vd.n_passages,
                "reason": "" if vd is None else vd.reason,
                "missing": "" if vd is None else vd.missing,
                "evidence": [] if vd is None else list(vd.evidence),
            })
    return rows


def write(rows: list[dict], out_path: str | Path, *, year: int, mode: str = "",
          model: str = "", fingerprint: str = "") -> Path:
    """写出工作簿。``rows`` 来自 :func:`collect`。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    codes = list(dict.fromkeys(r["code"] for r in rows))
    names = {r["code"]: r["name"] for r in rows}
    keys = list(RUBRICS)

    wb = Workbook()
    ws = wb.active
    ws.title = "档位矩阵"
    title(ws, f"S1.1 定性判档矩阵（{year}，{len(codes)} 家 × {len(keys)} 项）"
               f"　模式={mode or '—'}　模型={model or '—'}", len(codes) + 1)
    head(ws, 2, ["评分点"] + codes, [30] + [14] * len(codes))

    r = 3
    for key in keys:
        rub = RUBRICS[key]
        c1 = ws.cell(row=r, column=1, value=f"{key} {rub.name}")
        c1.font, c1.alignment, c1.border = F_BB, LEF, BORDER
        for j, code in enumerate(codes, start=2):
            val = next((x["tier"] for x in rows
                        if x["code"] == code and x["key"] == key), None)
            cell = ws.cell(row=r, column=j, value=val)
            cell.font, cell.alignment, cell.border = F_BB, CEN, BORDER
        ws.row_dimensions[r].height = 30
        r += 1

    rng = f"B3:{ws.cell(row=r - 1, column=len(codes) + 1).coordinate}"
    for tier, (bg, fg) in TIER_FILL.items():
        ws.conditional_formatting.add(rng, CellIsRule(
            operator="equal", formula=[str(tier)],
            fill=PatternFill("solid", bgColor=bg),
            font=Font(name="微软雅黑", size=14, bold=True, color=fg)))
    # 图例：口径一句话写在图下面，看图的人不必翻到最后一页
    ws.merge_cells(start_row=r + 1, start_column=1, end_row=r + 1, end_column=len(codes) + 1)
    note = ws.cell(row=r + 1, column=1,
                   value="绿=100（制度 + 已实施行动）　黄=50（有制度或仅声明，缺执行证据）　"
                         "红=0（未识别或触发 0 分前置条件）。逐项理由与依据见「判档明细」。")
    note.font, note.alignment = F_N, LEF
    ws.row_dimensions[r + 1].height = 34
    ws.freeze_panes = "B3"

    # ---- 判档明细
    ws2 = wb.create_sheet("判档明细")
    title(ws2, "逐项判档明细（理由与依据原文，复核看这张）", 11)
    head(ws2, 2, ["代码", "公司", "评分点", "档位", "出处", "模型", "脱敏", "召回",
                  "理由", "缺口", "依据原文（首条）"],
         [8, 20, 26, 7, 12, 11, 7, 7, 46, 46, 60])
    r = 3
    for row in sorted(rows, key=lambda x: (x["code"], keys.index(x["key"]))):
        ev = row["evidence"][0] if row["evidence"] else ""
        vals = [row["code"], row["name"], row["label"], row["tier"], row["source"],
                row["model"], row["alias_hits"], row["n_passages"],
                row["reason"], row["missing"], ev]
        for j, v in enumerate(vals, start=1):
            c = ws2.cell(row=r, column=j, value=v)
            c.border, c.font = BORDER, (F_BB if j == 4 else F_B)
            c.alignment = LEF if j in (9, 10, 11) else CEN
        # 行高按「最长的那个折行列」算：只按理由算会让依据原文溢出到下一行
        lines = max(-(-len(row["reason"]) // 42), -(-len(row["missing"]) // 42),
                    -(-len(ev) // 52), 1)
        ws2.row_dimensions[r].height = max(30, lines * 14 + 6)
        r += 1
    for tier, (bg, fg) in TIER_FILL.items():
        ws2.conditional_formatting.add(f"D3:D{r - 1}", CellIsRule(
            operator="equal", formula=[str(tier)],
            fill=PatternFill("solid", bgColor=bg),
            font=Font(name="微软雅黑", size=11, bold=True, color=fg)))
    ws2.freeze_panes = "D3"

    # ---- 档位图（原生图表，不引绘图库）
    ws3 = wb.create_sheet("档位图")
    title(ws3, "定性档位分布（柱高＝档位分，绿/黄/红同矩阵）", 8)
    chart = BarChart()
    chart.type, chart.style, chart.grouping = "col", 10, "clustered"
    chart.title = "各评分点档位（按公司）"
    chart.y_axis.title, chart.x_axis.title = "档位分", "评分点"
    chart.y_axis.scaling.min, chart.y_axis.scaling.max = 0, 110
    data = Reference(ws, min_col=2, max_col=len(codes) + 1, min_row=2, max_row=2 + len(keys))
    cats = Reference(ws, min_col=1, min_row=3, max_row=2 + len(keys))
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    chart.legend.position = "b"          # 没有图例，5 条同色系的柱子等于没编码
    chart.height, chart.width = 10.5, 20
    ws3.add_chart(chart, "B3")

    # 各公司定性均分：表格 + 图，排在两张图之间，避免浮动图互相遮挡
    ws3.column_dimensions["A"].width = 24
    ws3.column_dimensions["B"].width = 12
    h1 = ws3.cell(row=24, column=1, value="公司")
    h2 = ws3.cell(row=24, column=2, value="定性均分")
    for c in (h1, h2):
        c.font, c.fill, c.alignment, c.border = F_H, PatternFill(
            "solid", start_color=HEAD), CEN, BORDER
    for i, code in enumerate(codes, start=25):
        vals = [x["tier"] for x in rows if x["code"] == code and x["tier"] is not None]
        ws3.cell(row=i, column=1, value=f"{code} {names[code][:14]}").font = F_B
        ws3.cell(row=i, column=2,
                 value=round(sum(vals) / len(vals), 1) if vals else None).font = F_B

    chart2 = BarChart()
    chart2.type, chart2.style = "bar", 11
    chart2.title = "各公司定性均分（5 项平均）"
    # openpyxl 里 x_axis 恒为「分类轴」，条形图的值轴是 y_axis——起点/标题都设在这里，
    # 否则 LibreOffice 按数据自动取 54~72，视觉上把 60 和 70 拉成天壤之别。
    chart2.y_axis.title = "平均档位分"
    chart2.y_axis.scaling.min, chart2.y_axis.scaling.max = 0, 100
    chart2.add_data(Reference(ws3, min_col=2, min_row=24, max_row=24 + len(codes)),
                    titles_from_data=True)
    chart2.set_categories(Reference(ws3, min_col=1, min_row=25, max_row=24 + len(codes)))
    chart2.height, chart2.width = 9, 16
    ws3.add_chart(chart2, "B32")

    # ---- 口径与台账
    ws4 = wb.create_sheet("口径与台账")
    title(ws4, "判档口径与运行台账", 2)
    ws4.column_dimensions["A"].width = 24
    ws4.column_dimensions["B"].width = 104
    meta = [("报告年度", year), ("判档模式", mode or "—"), ("模型", model or "—"),
            ("rubric 指纹", fingerprint or "—"),
            ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("盲评", "是（送模型的段落已去除公司名，占位符 [COMPANY]）"),
            ("", ""),
            ("说明", "档位只有 100 / 50 / 0。100＝制度+已实施行动；50＝有制度/仅声明；"
                     "0＝未识别，或触发前置条件（如未披露离职率）。"),
            ("", "")]
    r = 3
    for k, v in meta:
        a = ws4.cell(row=r, column=1, value=k)
        a.font, a.fill, a.alignment, a.border = F_BB, PatternFill(
            "solid", start_color=LIGHT), LEF, BORDER
        b = ws4.cell(row=r, column=2, value=v)
        b.font, b.border = F_B, BORDER
        b.alignment = LEF
        ws4.row_dimensions[r].height = max(20, -(len(str(v)) // -70) * 15 + 4)
        r += 1
    r += 1
    for key in keys:
        rub = RUBRICS[key]
        ws4.cell(row=r, column=1, value=f"{key} {rub.name}").font = F_BB
        r += 1
        for tier in (100, 50, 0):
            a = ws4.cell(row=r, column=1, value=f"　{tier} 分")
            a.font, a.alignment = F_B, LEF
            b = ws4.cell(row=r, column=2, value=rub.tiers[tier])
            b.font, b.alignment = F_B, LEF
            ws4.row_dimensions[r].height = max(20, -(len(rub.tiers[tier]) // -70) * 15 + 4)
            r += 1
        if rub.note:
            ws4.cell(row=r, column=1, value="　判档提醒").font = F_N
            c = ws4.cell(row=r, column=2, value=rub.note)
            c.font, c.alignment = F_N, LEF
            r += 1
        r += 1

    wb.properties.title = f"S1.1 定性判档 {year}"
    wb.save(out)
    return out


def from_pipeline_result(result, verdicts_by_code: dict, out_dir: str | Path, *,
                         mode: str = "", model: str = "", fingerprint: str = "") -> Path:
    """从一次批跑的结果直接出图册（报告层调用这一个入口）。"""
    rows = collect(result.batch.views, verdicts_by_code)
    year = result.batch.year
    return write(rows, Path(out_dir) / f"S1.1_定性判档_{year}.xlsx",
                 year=year, mode=mode, model=model, fingerprint=fingerprint)


__all__ = ["collect", "write", "from_pipeline_result", "TIER_FILL"]