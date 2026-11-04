# -*- coding: utf-8 -*-
"""报告层：每家公司一份工作簿 + 全批一份汇总工作簿。

设计取向
--------
- **只渲染，不计算**：本模块的输入是已发布的 ``Batch``（来自 gold 表），不是编排过程中的
  中间态。分数、分位、档位在进本模块之前就已经定稿，这里只负责摆到格子里——
  于是「报告里的数」与「表里的数」不可能不一致。
- **一格都能反查**：分数旁边永远有分项说明与证据页码；数据表带抽取方式与置信度。
- **N/A 与 0 分开写**：分项不可评显示「N/A」，不显示 0。
- 不画图表、不用公式：任何一格的数值都能由「数据」「要素」两表手算复现。
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import (EXTRACT_VERSION, IND_WEIGHTS, MODEL_VERSION, SCHEMA_VERSION,
                     SPECS, WEIGHT_NOTE, WEIGHT_SUM)
from .models import Batch, CompanyView

XL_HEAD, XL_TITLE = "FF4472C4", "FF2F5597"
XL_LIGHT, XL_BD = "FFD9E2F3", "FFBFBFBF"
XL_GB, XL_GF = "FFC6EFCE", "FF006100"
XL_RB, XL_RF = "FFFFC7CE", "FF9C0006"
XL_AB, XL_AF = "FFFFEB9C", "FF9C6500"
XL_WHITE, XL_TXT, XL_MUT = "FFFFFFFF", "FF1A1A1A", "FF595959"

thin = Side(style="thin", color=XL_BD)
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
F_T = Font(name="微软雅黑", size=13, bold=True, color=XL_WHITE)
F_H = Font(name="微软雅黑", size=10, bold=True, color=XL_WHITE)
F_B = Font(name="微软雅黑", size=10, color=XL_TXT)
F_BB = Font(name="微软雅黑", size=10, bold=True, color=XL_TXT)
F_SC = Font(name="微软雅黑", size=12, bold=True, color=XL_TXT)
F_N = Font(name="微软雅黑", size=9, color=XL_MUT)
CEN = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEF = Alignment(horizontal="left", vertical="top", wrap_text=True)
LEFC = Alignment(horizontal="left", vertical="center", wrap_text=True)


def _title(ws, text: str, ncol: int, h: int = 30) -> None:
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    c = ws.cell(row=1, column=1, value=text)
    c.font, c.fill, c.alignment = F_T, PatternFill("solid", start_color=XL_TITLE), CEN
    ws.row_dimensions[1].height = h


def _head(ws, row: int, heads: list[str], widths: list[int], h: int = 34) -> None:
    for j, (t, w) in enumerate(zip(heads, widths), start=1):
        c = ws.cell(row=row, column=j, value=t)
        c.font, c.fill, c.alignment, c.border = F_H, PatternFill("solid", start_color=XL_HEAD), CEN, BORDER
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.row_dimensions[row].height = h


def _note(ws, row: int, ncol: int, text: str, height: int = 60) -> None:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncol)
    c = ws.cell(row=row, column=1, value=text)
    c.font, c.alignment = F_N, LEF
    ws.row_dimensions[row].height = height


def _fmt(v) -> object:
    return "N/A" if v is None else v


# ---------------------------------------------------------------- 单家公司
def write_company(view: CompanyView, out_dir: Path) -> Path:
    """一家公司的五个工作表。``view`` 来自 gold 表，本函数不重算任何分数。"""
    card = view.card
    data = card.data
    wb = Workbook()
    ws = wb.active
    ws.title = "总览"
    _title(ws, f"{view.code} {view.name} · S1.1 五项指标打分（报告年度 {card.report_year}）", 8)
    _head(ws, 2, ["指标", "名称", "权重", "得分（严格/N/A 计 0）", "得分（归一/百分制）",
                  "可达上限", "档位", "主要缺口"], [9, 22, 8, 15, 15, 10, 10, 62])
    r = 3
    for k in ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5"):
        ind = card.indicators[k]
        miss = "；".join(c.note for c in ind.components if c.score is None) or ind.note
        if ind.kind == "weighted" and not miss:
            miss = "；".join(f"{c.key}：{c.note}" for c in ind.components[-1:])
        vals = [k, ind.name, ind.weight, ind.strict, _fmt(ind.normalized),
                ind.reachable, ind.band, miss]
        for j, v in enumerate(vals, start=1):
            c = ws.cell(row=r, column=j, value=v)
            c.border, c.font = BORDER, (F_BB if j in (4, 5) else F_B)
            c.alignment = LEF if j == 8 else (LEFC if j == 2 else CEN)
        ws.row_dimensions[r].height = 34
        r += 1
    for j, v in enumerate(["—", "加权总分", WEIGHT_SUM, card.total_strict,
                           card.total_normalized(WEIGHT_SUM), "—", "—", WEIGHT_NOTE], start=1):
        c = ws.cell(row=r, column=j, value=v)
        c.fill, c.border = PatternFill("solid", start_color=XL_LIGHT), BORDER
        c.font = F_SC if j in (4, 5) else F_BB
        c.alignment = LEF if j == 8 else CEN
    ws.row_dimensions[r].height = 34
    for col in "DE":
        ws.conditional_formatting.add(f"{col}3:{col}{r-1}", CellIsRule(
            operator="lessThan", formula=["20"], fill=PatternFill("solid", bgColor=XL_RB),
            font=Font(name="微软雅黑", size=12, bold=True, color=XL_RF)))
        ws.conditional_formatting.add(f"{col}3:{col}{r-1}", CellIsRule(
            operator="between", formula=["20", "49.999"], fill=PatternFill("solid", bgColor=XL_AB),
            font=Font(name="微软雅黑", size=12, bold=True, color=XL_AF)))
        ws.conditional_formatting.add(f"{col}3:{col}{r-1}", CellIsRule(
            operator="greaterThanOrEqual", formula=["50"], fill=PatternFill("solid", bgColor=XL_GB),
            font=Font(name="微软雅黑", size=12, bold=True, color=XL_GF)))
    _note(ws, r + 1, 8,
          "「严格」按标准公式计，不可评分项按 0 计入；「归一」只按可观测权重折算成百分制。"
          "两个数一起看：相差越大，说明该指标缺的分项越多，分数越不该横向比较。")
    ws.freeze_panes = "C3"

    # 分项
    ws2 = wb.create_sheet("分项判分")
    _title(ws2, "分项判分与证据（A/B/C 或三档制）", 6)
    _head(ws2, 2, ["指标", "分项", "权重", "得分", "判据与证据", "原文摘录"],
          [9, 22, 8, 8, 62, 54], h=28)
    r = 3
    for k in ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5"):
        ind = card.indicators[k]
        # 三档制：分项组件只是「判定依据摘要」，真正的得分在 IndScore.raw 上，单独两行呈现
        if ind.kind == "tiered":
            rows = [(c.key, c.name, 0.0, None, c.note, c.evidence) for c in ind.components]
            rows.append(("档位", f"{ind.name}（三档制）", 1.0, ind.raw,
                         ind.note or "—", ""))
        else:
            rows = [(c.key, c.name, c.weight, c.score, c.note, c.evidence)
                    for c in ind.components]
        for key, name, w, sc, note, ev in rows:
            for j, v in enumerate([k, name, w if w else "—", _fmt(sc), note, ev], start=1):
                c = ws2.cell(row=r, column=j, value=v)
                c.border, c.font = BORDER, F_BB if j == 4 else F_B
                c.alignment = LEF if j in (5, 6) else (LEFC if j == 2 else CEN)
            ws2.row_dimensions[r].height = max(30, -(len(note or "") // -40) * 15 + 6)
            r += 1
    ws2.freeze_panes = "C3"

    # 数据
    ws3 = wb.create_sheet("数据取值")
    _title(ws3, "抽取到的数值（含页码、抽取方式、置信度）", 6)
    _head(ws3, 2, ["字段", "年份", "数值", "页码", "抽取方式", "置信度"], [26, 8, 12, 8, 18, 10], h=26)
    r = 3
    for f in sorted(view.facts, key=lambda x: (x.field, -(x.year or 0))):
        for j, v in enumerate([f.field, f.year, f.value, f.page or "—", f.method, f.confidence], start=1):
            c = ws3.cell(row=r, column=j, value=v)
            c.border, c.font = BORDER, F_B
            c.alignment = CEN if j != 6 else CEN
        ws3.row_dimensions[r].height = 18
        r += 1
    r += 1
    _note(ws3, r, 6, "上表只列抽取成功的数值；抽不到即在下游记 N/A，不做估算、不做单位换算。")
    ws3.freeze_panes = "A3"

    # 要素
    ws4 = wb.create_sheet("29 要素")
    _title(ws4, "29 个核查要素判定（含增强层补丁）", 5)
    _head(ws4, 2, ["要素", "名称", "判定", "判定理由", "首条证据（页码 + 原文）"],
          [11, 24, 8, 44, 74], h=28)
    patched = {p.eid: p for p in view.patches}
    r = 3
    for eid in sorted(view.elements, key=lambda e: (e.split("-")[0], int(e.split("-")[1]))):
        res = view.elements[eid]
        reason = res.reason + (f"｜补丁：{patched[eid].reason}" if eid in patched else "")
        for j, v in enumerate([eid, res.name, res.verdict, reason, res.top_quote()], start=1):
            c = ws4.cell(row=r, column=j, value=v)
            c.border, c.font = BORDER, F_BB if j == 3 else F_B
            c.alignment = LEF if j in (4, 5) else (LEFC if j == 2 else CEN)
        ws4.row_dimensions[r].height = max(28, -(len(reason) // -46) * 15 + 6)
        r += 1
    for val, bg, fg in [("是", XL_GB, XL_GF), ("部分", XL_AB, XL_AF), ("否", XL_RB, XL_RF)]:
        ws4.conditional_formatting.add(f"C3:C{r-1}", FormulaRule(
            formula=[f'C3="{val}"'], fill=PatternFill("solid", bgColor=bg),
            font=Font(name="微软雅黑", size=10, bold=True, color=fg)))
    ws4.freeze_panes = "C3"

    # 台账
    ws5 = wb.create_sheet("运行台账")
    _title(ws5, "运行台账（版本、文件、对标池、提示）", 2)
    ws5.column_dimensions["A"].width = 22
    ws5.column_dimensions["B"].width = 96
    meta = [
        ("模型版本", MODEL_VERSION), ("抽取版本", EXTRACT_VERSION),
        ("导出字段版本", SCHEMA_VERSION),
        ("文件", view.file), ("报告年度", card.report_year),
        ("对标池", data.bench_note if data else ""),
        ("上期值来源", data.prior_source if data else ""),
        ("离职率分母口径", (data.turnover_denom_note if data else "") or "未识别到公式说明"),
        ("权重口径", WEIGHT_NOTE),
        ("提示", "\n".join(card.warnings) or "无"),
        ("增强层补丁", "\n".join(f"{p.eid} {p.before}→{p.after}：{p.reason}" for p in view.patches) or "无"),
    ]
    r = 3
    for k, v in meta:
        a = ws5.cell(row=r, column=1, value=k)
        a.font, a.fill, a.alignment, a.border = F_BB, PatternFill("solid", start_color=XL_LIGHT), LEF, BORDER
        b = ws5.cell(row=r, column=2, value=v)
        b.font, b.alignment, b.border = F_B, LEF, BORDER
        ws5.row_dimensions[r].height = max(20, -(len(str(v)) // -60) * 15 + 4)
        r += 1

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"S1.1_{view.code}_打分报告.xlsx"
    wb.properties.title = f"{view.code} S1.1 打分报告"
    wb.save(path)
    return path


# ---------------------------------------------------------------- 批次汇总
def write_batch(batch: Batch, out_dir: Path) -> Path:
    """批次汇总。所有数值来自 gold 表，本函数不调用任何算分/分位函数。"""
    ok = batch.views
    year = batch.year
    wb = Workbook()
    ws = wb.active
    ws.title = "汇总"
    _title(ws, f"S1.1 打分模型 · 批次汇总（{year}，n={len(ok)}，对标={batch.bench_kind}）", 10)
    _head(ws, 2, ["代码", "公司", "报告年度", "S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5",
                  "加权总分（严格）", "加权总分（归一）"],
          [9, 24, 10, 10, 10, 10, 10, 10, 15, 15])
    r = 3
    for run in sorted(ok, key=lambda x: -x.card.total_strict):
        inds = [run.card.indicators[k] for k in ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5")]
        vals = [run.code, run.name, run.card.report_year] + [i.strict for i in inds] + \
               [run.card.total_strict, run.card.total_normalized(WEIGHT_SUM)]
        for j, v in enumerate(vals, start=1):
            c = ws.cell(row=r, column=j, value=v)
            c.border, c.font = BORDER, F_SC if j >= 9 else F_B
            c.alignment = LEF if j == 2 else CEN
        ws.row_dimensions[r].height = 22
        r += 1
    for col in "DEFGH":
        for lo, hi, bg, fg in ((0, 19.999, XL_RB, XL_RF), (20, 49.999, XL_AB, XL_AF),
                               (50, 100, XL_GB, XL_GF)):
            ws.conditional_formatting.add(f"{col}3:{col}{r-1}", CellIsRule(
                operator="between", formula=[str(lo), str(hi)],
                fill=PatternFill("solid", bgColor=bg),
                font=Font(name="微软雅黑", size=11, bold=True, color=fg)))
    # 权重串由 SPECS 现算：写死「25/30/10/5/15」会在口径变更后继续印旧数字
    _wdesc = "/".join(str(IND_WEIGHTS[s.ind]) for s in SPECS)
    _note(ws, r + 1, 10, f"对标：{batch.bench_note}。总分按权重 {_wdesc}（合计 {WEIGHT_SUM}%）"
                         + ("" if WEIGHT_SUM == 100 else "加权，权重不足 100%，百分制总分仅供参考"))
    ws.freeze_panes = "C3"

    # 分位
    ws2 = wb.create_sheet("对标分位")
    _title(ws2, "A/B 项的对标池与分位（可导出为行业库）", 8)
    _head(ws2, 2, ["代码", "公司", "流失率（本期）", "流失率（上期）", "改善率",
                   "A 分位（低者优）", "B 分位（改善优）", "A/B 得分"], [9, 24, 13, 13, 11, 14, 14, 14])
    r = 3
    for run in sorted(ok, key=lambda x: (x.card.data.turnover is None,
                                        x.card.data.turnover or 0)):
        d = run.card.data
        # 分位/档位在打分阶段就落进了 gold 表，这里只摆值（不再回头问对标池）
        pa, pb = run.pct_a, run.pct_b
        vals = [run.code, run.name, d.turnover, d.prior_turnover, d.improvement, pa, pb,
                f"{run.band_a if run.band_a is not None else 'N/A'} / "
                f"{run.band_b if run.band_b is not None else 'N/A'}"]
        for j, v in enumerate(vals, start=1):
            c = ws2.cell(row=r, column=j, value=v)
            c.border, c.font = BORDER, F_B
            c.alignment = LEF if j == 2 else CEN
            if j in (3, 4) and isinstance(v, (int, float)):
                c.number_format = "0.00\\%"
            if j == 5 and isinstance(v, (int, float)):
                c.number_format = "+0.00\\%;-0.00\\%"
        ws2.row_dimensions[r].height = 22
        r += 1
    _note(ws2, r + 1, 8, "分位＝按指标方向排名后的百分位（越小越好＝升序第一）。"
                         "n<3 时不算分位；修改对标池等价于换一套 A/B 口径，务必在报告里注明。", 40)
    ws2.freeze_panes = "C3"

    # 要素矩阵
    ws3 = wb.create_sheet("要素矩阵")
    eids = sorted({e for r0 in ok for e in r0.elements},
                  key=lambda e: (e.split("-")[0], int(e.split("-")[1])))
    _title(ws3, f"29 要素 × {len(ok)} 家（是/部分/否）", len(ok) + 3)
    _head(ws3, 2, ["要素", "名称"] + [r0.code for r0 in ok] + ["支撑家数"],
          [11, 24] + [11] * len(ok) + [11])
    r = 3
    for eid in eids:
        vals = [eid, ok[0].elements[eid].name] + [r0.elements[eid].verdict for r0 in ok]
        vals.append(sum(1 for v in vals[2:] if v == "是"))
        for j, v in enumerate(vals, start=1):
            c = ws3.cell(row=r, column=j, value=v)
            c.border, c.font = BORDER, F_B
            c.alignment = LEFC if j == 2 else CEN
        ws3.row_dimensions[r].height = 20
        r += 1
    for val, bg, fg in [("是", XL_GB, XL_GF), ("部分", XL_AB, XL_AF), ("否", XL_RB, XL_RF)]:
        ws3.conditional_formatting.add(f"C3:{get_column_letter(len(ok) + 2)}{r-1}", FormulaRule(
            formula=[f'C3="{val}"'], fill=PatternFill("solid", bgColor=bg),
            font=Font(name="微软雅黑", size=10, bold=True, color=fg)))
    ws3.freeze_panes = "C3"

    # 缺口
    ws4 = wb.create_sheet("缺口清单")
    _title(ws4, "全批缺口：哪些要素没人能给出证据", 4)
    _head(ws4, 2, ["要素", "名称", "本批判定分布（是/部分/否）", "含义"], [11, 26, 26, 72], h=26)
    r = 3
    for eid in eids:
        vs = [r0.elements[eid].verdict for r0 in ok]
        yes, part, no = vs.count("是"), vs.count("部分"), vs.count("否")
        hint = ok[0].elements[eid].reason[:40]
        for j, v in enumerate([eid, ok[0].elements[eid].name, f"{yes} / {part} / {no}",
                               f"{'全批缺证据' if yes == 0 else ''} {hint}".strip()], start=1):
            c = ws4.cell(row=r, column=j, value=v)
            c.border, c.font = BORDER, F_B
            c.alignment = LEFC if j in (2, 4) else CEN
            if j == 3 and yes == 0:
                c.font = Font(name="微软雅黑", size=10, bold=True, color=XL_RF)
        ws4.row_dimensions[r].height = 34
        r += 1
    ws4.freeze_panes = "C3"

    # 台账
    ws5 = wb.create_sheet("运行台账")
    _title(ws5, "批次台账", 2)
    ws5.column_dimensions["A"].width = 22
    ws5.column_dimensions["B"].width = 100
    rows = [("模型版本", MODEL_VERSION), ("抽取版本", EXTRACT_VERSION),
            ("导出字段版本", SCHEMA_VERSION), ("报告年度", year),
            ("对标快照", batch.bench_snapshot or "—"),
            ("文件数", batch.total), ("成功", len(ok)),
            ("失败/跳过", "\n".join(f"{f.code} {f.file}：{f.error or f.status}"
                                 for f in batch.failures) or "无"),
            ("对标池", batch.bench_note), ("权重口径", WEIGHT_NOTE)]
    r = 3
    for k, v in rows:
        a = ws5.cell(row=r, column=1, value=k)
        a.font, a.fill, a.alignment, a.border = F_BB, PatternFill("solid", start_color=XL_LIGHT), LEF, BORDER
        b = ws5.cell(row=r, column=2, value=v)
        b.font, b.alignment, b.border = F_B, LEF, BORDER
        ws5.row_dimensions[r].height = max(20, -(len(str(v)) // -70) * 15 + 4)
        r += 1

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"S1.1_批次汇总_{year}.xlsx"
    wb.properties.title = f"S1.1 打分模型批次汇总 {year}"
    wb.save(path)
    return path