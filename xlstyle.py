# -*- coding: utf-8 -*-
"""工作簿的公共样式：配色、标题行、表头、按文字宽度估行高。

为什么单独一层
-------------
判档图册、得分构成图册、关键词表三本册子要看起来是"一套东西"：同一个深蓝表头、
同一种边框、同一套行高估算。之前每本各写一份，改颜色就得改三处，改漏一处外观立刻不一致
（第一版就出现过：明细页行高按"字符数"算，中英混排时文字被截断，而另一本正常）。

另一个被收进来的东西是 ``text_units``：Excel 列宽单位是"字符"，一个汉字占两个字符宽。
按 ``len()`` 估行高在中英混排的单元格里必然算少——这是之前几处文字溢出的根因。
"""
from __future__ import annotations

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

# 配色：深蓝标题、蓝表头、浅蓝强调；绿/琥珀/红用于档位
TITLE, HEAD, LIGHT = "FF2F5597", "FF4472C4", "FFD9E2F3"
BD = "FFBFBFBF"
TIER_FILL = {100: ("FFC6EFCE", "FF006100"),
             50: ("FFFFEB9C", "FF9C6500"),
             0: ("FFFFC7CE", "FF9C0006")}

_thin = Side(style="thin", color=BD)
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
F_T = Font(name="微软雅黑", size=13, bold=True, color="FFFFFFFF")
F_H = Font(name="微软雅黑", size=10, bold=True, color="FFFFFFFF")
F_B = Font(name="微软雅黑", size=10)
F_BB = Font(name="微软雅黑", size=10, bold=True)
F_BIG = Font(name="微软雅黑", size=12, bold=True)
F_N = Font(name="微软雅黑", size=9, color="FF595959")
CEN = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEF = Alignment(horizontal="left", vertical="top", wrap_text=True)
LEFC = Alignment(horizontal="left", vertical="center", wrap_text=True)


def title(ws, text: str, ncol: int, height: int = 30) -> None:
    """合并首行的标题条。"""
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    c = ws.cell(row=1, column=1, value=text)
    c.font, c.fill, c.alignment = F_T, PatternFill("solid", start_color=TITLE), CEN
    ws.row_dimensions[1].height = height


def head(ws, row: int, heads: list[str], widths: list[int], height: int = 30) -> None:
    """表头行 + 顺手把列宽定下来（列宽是报告外观的主要变量，放这里统一管）。"""
    for j, (t, w) in enumerate(zip(heads, widths), start=1):
        c = ws.cell(row=row, column=j, value=t)
        c.font = F_H
        c.fill = PatternFill("solid", start_color=HEAD)
        c.alignment, c.border = CEN, BORDER
        ws.column_dimensions[c.column_letter].width = w
    ws.row_dimensions[row].height = height


def text_units(text) -> int:
    """估算显示宽度：CJK 记 2、ASCII 记 1（与 Excel 列宽单位一致）。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(text or ""))


def lines_for(text, width: int) -> int:
    """在给定列宽下大约要折几行。"""
    return max(1, -(-text_units(text) // max(8, width - 2)))


def row_height(*pairs: tuple[object, int], minimum: int = 20, base: int = 14) -> int:
    """按「文字 / 列宽」的点对里最费空间的那个算行高。"""
    lines = max([lines_for(t, w) for t, w in pairs] or [1])
    return max(minimum, lines * base + 8)


__all__ = ["TITLE", "HEAD", "LIGHT", "BD", "TIER_FILL", "BORDER",
           "F_T", "F_H", "F_B", "F_BB", "F_BIG", "F_N", "CEN", "LEF", "LEFC",
           "title", "head", "text_units", "lines_for", "row_height"]