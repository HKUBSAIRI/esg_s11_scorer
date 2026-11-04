# -*- coding: utf-8 -*-
"""定性指标关键词清单：两列，一列指标，一列这个词表里的全部关键词。

就这一件事。之前那版分了「概览／明细／使用说明」三张表，信息全但看不清全貌；
评审时要的是「这个指标到底用了哪些词」，一句话能看完最好，所以这里把每个指标的
关键词合成一个单元格，顿号分隔。

数据仍取自 ``rubric.RUBRICS``（按证据组存的那份），本模块只做打字排版。
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import PatternFill

from .rubric import RUBRICS
from .xlstyle import BORDER, F_B, F_BB, F_H, HEAD, LEF, LEFC, lines_for, title

SEP = "、"
COL_IND, COL_WORDS = 26, 118


def table_rows() -> list[tuple[str, str]]:
    """``[(指标, 关键词串), ...]``，顺序与 SPECS 一致（RUBRICS 的声明顺序）。"""
    return [(f"{key} {rub.name}", SEP.join(rub.keywords))
            for key, rub in RUBRICS.items()]


def write(out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = table_rows()

    wb = Workbook()
    ws = wb.active
    ws.title = "关键词"
    title(ws, "S1.1 定性指标关键词", 2, height=26)

    for j, (text, width) in enumerate((("定性指标", COL_IND), ("关键词", COL_WORDS)), start=1):
        c = ws.cell(row=2, column=j, value=text)
        c.font, c.alignment, c.border = F_H, LEFC, BORDER
        c.fill = PatternFill("solid", start_color=HEAD)
        ws.column_dimensions[c.column_letter].width = width
    ws.row_dimensions[2].height = 24

    for i, (ind, words) in enumerate(rows, start=3):
        a = ws.cell(row=i, column=1, value=ind)
        a.font, a.alignment, a.border = F_BB, LEFC, BORDER
        b = ws.cell(row=i, column=2, value=words)
        b.font, b.alignment, b.border = F_B, LEF, BORDER
        # 行高按“这串词在 118 列宽下要折几行”算，避免长词表被裁掉
        ws.row_dimensions[i].height = max(28, lines_for(words, COL_WORDS) * 15 + 10)

    ws.freeze_panes = "A3"
    wb.properties.title = "S1.1 定性指标关键词"
    wb.save(out)
    return out


__all__ = ["write", "table_rows", "SEP"]