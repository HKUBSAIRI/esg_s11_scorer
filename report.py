# -*- coding: utf-8 -*-
"""报告层：每家公司一份工作簿 + 全批一份汇总工作簿。

设计取向
--------
- **只渲染，不计算**：本模块的输入是已发布的 ``Batch``（来自 gold 表），不是编排过程中的
  中间态。分数、分位、档位在进本模块之前就已经定稿，这里只负责摆到格子里——
  于是「报告里的数」与「表里的数」不可能不一致。
- **一格都能反查**：分数旁边永远有分项说明与证据页码；数据表带抽取方式与置信度。
- **N/A 与 0 分开写**：分项不可评显示「N/A」，不显示 0。
- 不画图表、不用公式：任何一格的数值都能由「数据取值」表与打分明细手算复现。
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Font, PatternFill           # 条件格式规则的入参；样式基元一律取自 xlstyle

from .config import (EXTRACT_VERSION, IND_WEIGHTS, MODEL_VERSION, SCHEMA_VERSION,
                     SPECS, WEIGHT_NOTE, WEIGHT_SUM)
from .models import Batch, CompanyView
from .xlstyle import (BORDER, CEN, F_B, F_BB, F_SC, HEAD, LEF, LEFC, LIGHT, TIER_FILL,
                      TITLE, head, note as note_bar, title)

# 正文里原本用 XL_* 这套名字；改为别名指向 xlstyle 的那一份，
# 颜色/字体只此一处定义（改品牌色不用再翻三个文件）。
XL_TITLE, XL_HEAD, XL_LIGHT = TITLE, HEAD, LIGHT
XL_GB, XL_GF = TIER_FILL[100]
XL_AB, XL_AF = TIER_FILL[50]
XL_RB, XL_RF = TIER_FILL[0]


def _fmt(v) -> object:
    return "N/A" if v is None else v


# ---------------------------------------------------------------- 单家公司
def _formula(ind) -> str:
    """把「这个指标的分怎么来的」写成一行算式（报告里唯一的口径说明）。"""
    if ind.kind == "tiered":
        return f"档位 1.00 × {ind.raw} = {ind.strict:.1f}（三档制，无加权）"
    parts = [f"{c.weight:.1f}×N/A" if c.score is None else f"{c.weight:.1f}×{c.score}"
             for c in ind.components]
    txt = " + ".join(parts) + f" = {ind.strict:.1f}"
    if any(c.score is None for c in ind.components):
        txt += "（N/A 按 0 计入严格分）"
    if ind.normalized is not None and (
            abs(ind.normalized - ind.strict) > 1e-9 or ind.reachable < 100):
        txt += f"；归一 {ind.normalized:.1f}（可达上限 {ind.reachable:.0f}）"
    return txt


def _subtotal_note(ind) -> str:
    """小计行的「得分说明」：档位 + 三个口径数，一眼看清这个分该怎么读。"""
    norm = "N/A" if ind.normalized is None else f"{ind.normalized:.1f}"
    return (f"{ind.band or '—'}｜严格 {ind.strict:.1f} / 归一 {norm}"
            f" / 可达上限 {ind.reachable:.0f}")


KEYS5 = ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5")


def _detail_block(card) -> list[tuple[bool, list]]:
    """一家公司的分项明细行：``(是否小计, [指标, 分项, 权重, 得分, 说明, 原文, 算式])``。

    单家报告与批次汇总共用这一份：两边不可能给出不一样的拆分或不一样的算式。
    权重写成百分比（40% / 30%），与「小计 25%」是同一把尺子；得分栏 N/A 是文字，不是 0。
    """
    out: list[tuple[bool, list]] = []
    for k in KEYS5:
        ind = card.indicators[k]
        if ind.kind == "tiered":
            # 三档制没有分项：一行「档位」，理由与证据都挂在这一行上
            v = ind.components[0] if ind.components else None
            out.append((False, [k, "档位（三档制）", "100%", _fmt(ind.raw),
                                (v.note if v and v.note else (ind.note or "")),
                                v.evidence if v else "", ""]))
        else:
            for c in ind.components:
                out.append((False, [k, f"{c.key} {c.name}", f"{c.weight:.0%}",
                                    _fmt(c.score), c.note, c.evidence, ""]))
        out.append((True, [k, "小计（指标得分）", f"{ind.weight:.0f}%", ind.strict,
                           _subtotal_note(ind), "", _formula(ind)]))
    return out


def _weakest(ind) -> str:
    """总览「主要缺口」列：取得分最低的可评分项，而不是永远取 C。"""
    avail = [c for c in ind.components if c.score is not None]
    if not avail:
        return "；".join(c.note for c in ind.components if c.note) or (ind.note or "")
    c = min(avail, key=lambda x: (x.score, x.weight))
    head = f"{c.key} {c.score} 分" if c.key else f"{c.score} 分"
    return f"{head}：{c.note}" if c.note else head


def write_company(view: CompanyView, out_dir: Path) -> Path:
    """一家公司的五个工作表。``view`` 来自 gold 表，本函数不重算任何分数。"""
    card = view.card
    data = card.data
    wb = Workbook()
    ws = wb.active
    ws.title = "总览"
    title(ws, f"{view.code} {view.name} · S1.1 五项指标打分（报告年度 {card.report_year}）", 8)
    head(ws, 2, ["指标", "名称", "权重", "得分（严格/N/A 计 0）", "得分（归一/百分制）",
                 "可达上限", "档位", "主要缺口"], [9, 22, 8, 15, 15, 10, 10, 62], height=34)
    r = 3
    for k in ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5"):
        ind = card.indicators[k]
        miss = _weakest(ind)
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
    note_bar(ws, r + 1, 8,
          "「严格」按标准公式计，不可评分项按 0 计入；「归一」只按可观测权重折算成百分制。"
          "两个数一起看：相差越大，说明该指标缺的分项越多，分数越不该横向比较。")
    ws.freeze_panes = "C3"

    # 分项明细
    ws2 = wb.create_sheet("分项明细")
    title(ws2, "分项明细：每个指标的得分是怎么来的", 7)
    head(ws2, 2, ["指标", "分项", "权重", "得分", "得分说明（判据 / 缺口）", "原文摘录", "算式"],
          [9, 24, 8, 8, 58, 46, 40], height=28)
    r = 3
    for is_sub, vals in _detail_block(card):
        for j, v in enumerate(vals, start=1):
            c = ws2.cell(row=r, column=j, value=v)
            c.border = BORDER
            if is_sub:
                c.fill = PatternFill("solid", start_color=XL_LIGHT)
                c.font = F_SC if j == 4 else F_BB
            else:
                c.font = F_BB if j == 4 else F_B
            c.alignment = LEF if j in (5, 6) else (LEFC if j == 2 else CEN)
        ws2.row_dimensions[r].height = (
            32 if is_sub else max(30, -(len(vals[4] or "") // -40) * 15 + 6))
        r += 1
    note_bar(ws2, r, 7,
             "得分说明：加权制 = 各分项得分 × 权重求和（不可评项按 0 计入严格分，归一按可观测权重折算）；"
             "三档制 = 判档档位（100/50/0）。原文摘录用页码标注出处，可回 PDF 复核。")
    ws2.freeze_panes = "C3"

    # 数据
    ws3 = wb.create_sheet("数据取值")
    title(ws3, "抽取到的数值（含页码、抽取方式、置信度）", 6)
    head(ws3, 2, ["字段", "年份", "数值", "页码", "抽取方式", "置信度"], [26, 8, 12, 8, 18, 10], height=26)
    r = 3
    for f in sorted(view.facts, key=lambda x: (x.field, -(x.year or 0))):
        for j, v in enumerate([f.field, f.year, f.value, f.page or "—", f.method, f.confidence], start=1):
            c = ws3.cell(row=r, column=j, value=v)
            c.border, c.font = BORDER, F_B
            c.alignment = CEN if j != 6 else CEN
        ws3.row_dimensions[r].height = 18
        r += 1
    r += 1
    note_bar(ws3, r, 6, "上表只列抽取成功的数值；抽不到即在下游记 N/A，不做估算、不做单位换算。")
    ws3.freeze_panes = "A3"

    # 台账
    ws5 = wb.create_sheet("运行台账")
    title(ws5, "运行台账（版本、文件、对标池、提示）", 2)
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
    title(ws, f"S1.1 打分模型 · 批次汇总（{year}，n={len(ok)}，对标={batch.bench_kind}）", 10)
    head(ws, 2, ["代码", "公司", "报告年度", "S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5",
                  "加权总分（严格）", "加权总分（归一）"],
          [9, 24, 10, 10, 10, 10, 10, 10, 15, 15], height=34)
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
    note_bar(ws, r + 1, 10, f"对标：{batch.bench_note}。总分按权重 {_wdesc}（合计 {WEIGHT_SUM}%）"
                         + ("" if WEIGHT_SUM == 100 else "加权，权重不足 100%，百分制总分仅供参考"))
    ws.freeze_panes = "C3"

    # 分位
    ws2 = wb.create_sheet("对标分位")
    title(ws2, "A/B 项的对标池与分位（可导出为行业库）", 8)
    head(ws2, 2, ["代码", "公司", "流失率（本期）", "流失率（上期）", "改善率",
                   "A 分位（低者优）", "B 分位（改善优）", "A/B 得分"], [9, 24, 13, 13, 11, 14, 14, 14], height=34)
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
    note_bar(ws2, r + 1, 8, "分位＝按指标方向排名后的百分位（越小越好＝升序第一）。"
                         "n<3 时不算分位；修改对标池等价于换一套 A/B 口径，务必在报告里注明。", 40)
    ws2.freeze_panes = "C3"

    # 分项明细（全批）
    ws3 = wb.create_sheet("分项明细")
    title(ws3, f"分项明细：{len(ok)} 家 × 5 指标 × 各分项（含算式）", 9)
    head(ws3, 2, ["代码", "公司", "指标", "分项", "权重", "得分",
                  "得分说明（判据 / 缺口）", "原文摘录", "算式"],
          [9, 20, 9, 24, 8, 8, 52, 42, 38], height=30)
    r = 3
    for run in sorted(ok, key=lambda x: -x.card.total_strict):
        for is_sub, vals in _detail_block(run.card):
            for j, v in enumerate([run.code, run.name] + vals, start=1):
                c = ws3.cell(row=r, column=j, value=v)
                c.border = BORDER
                if is_sub:
                    c.fill = PatternFill("solid", start_color=XL_LIGHT)
                    c.font = F_SC if j == 6 else F_BB
                else:
                    c.font = F_BB if j == 6 else F_B
                c.alignment = LEF if j in (2, 4, 7, 8, 9) else CEN
            ws3.row_dimensions[r].height = (
                32 if is_sub else max(30, -(len(vals[4] or "") // -40) * 15 + 6))
            r += 1
    note_bar(ws3, r, 9,
             "同一家公司的行连续排列：每个指标先列 A/B/C（或三档位），再一行「小计」写算式与严格/归一/可达上限。"
             "得分说明里的「缺口」= 离更高一档还缺什么；原文摘录带页码，可回 PDF 复核。")
    ws3.freeze_panes = "D3"

    # 台账
    ws5 = wb.create_sheet("运行台账")
    title(ws5, "批次台账", 2)
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