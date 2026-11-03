# -*- coding: utf-8 -*-
"""数值抽取层：从报告文本里取出 S1.1 加权指标需要的数字。

为什么单独做一层
----------------
现有 ``ESG_insight/app/s11_score.py`` 的 ``find_turnover`` 只覆盖「单年 + 括注上期」两种写法。
实际报告（本批 5 家）用的全是**多年份表格**：表头一行年份、下面一行数值：

    Type            Unit  2022 2023 2024 2025
    Total employee turnover42 %  10   11   11   10      ← 4 个值对 4 个年份

    ESG Indicator   Unit  2023 2024 2025
    Total Employee Turnover Rate % 13.5 10.4 9.1        ← 3 个值对 3 个年份

    Social performance indicator unit versus 2025 data
    Employee turnover rate* % 14.4                       ← 1 个值对 1 个年份

所以本层的核心是「**表头年份对齐**」：先认页内所有候选年份序列，再用「数值个数 == 年份个数」
匹配，最后按「报告年度」取值、按「上一年度」取上期。对不齐就宁可不取（记低置信），不猜。

三条硬约束
----------
1. 不做单位换算、不做估算：抽不到就是 ``None``，由打分层输出 N/A 而不是 0。
2. 每个数字都带页码、原文句、抽取方式，供报告层引用（防幻觉）。
3. 抽取结果必须过一致性校验：离职人数 ÷ 员工总数 应≈流失率，偏差 >3pp 记冲突。
"""
from __future__ import annotations

import re

from s11_text import Doc, sentences

from .models import Extraction, NumFact

# ---------------------------------------------------------------- 字段词表
FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "turnover": (
        r"total\s+employee\s+turnover(?:\s+rate)?",
        r"(?:overall|employee|total)\s+turnover\s+rate",
        r"employee\s+turnover\s+rate",
        r"turnover\s+rate",
        r"總?流失率", r"離職率", r"离职率",
    ),
    "leavers": (
        r"total\s+employees?\s+lost",
        r"number\s+of\s+(?:leavers|departures|resignations)",
        r"total\s+number\s+of\s+(?:turnovers|departures)",
        r"离职(?:员工)?(?:人数|總數|总数)", r"離職人數",
    ),
    "headcount": (
        r"number\s+of\s+employees",
        r"total\s+(?:number\s+of\s+)?employees",
        r"员工(?:总)?数", r"員工人數",
    ),
    "gender_gap": (
        r"median\s+gender\s+pay\s+gap",
        r"gender\s+pay\s+gap",
        r"gender\s+pay\s+(?:ratio|difference)",
        r"性别薪酬差距", r"性別薪酬差距",
    ),
    "gender_gap_avg": (
        r"average\s+gender\s+pay\s+gap",
        r"mean\s+gender\s+pay\s+gap",
    ),
    "cb": (
        r"percentage\s+of\s+employees\s+covered\s+by\s+collective\s+bargaining",
        r"collective\s+bargaining",
        r"集体谈判", r"集體談判",
    ),
    "female_pct": (
        r"percentage\s+of\s+female\s+employees\s+in\s+(?:the\s+)?overall\s+workforce",
        r"女性员工占比", r"女性僱員比例",
    ),
}

YEAR = re.compile(r"20[12]\d")
# 字段取值上下界：比率类 0–100，人数类放宽（离职人数常上千，用百分比界会全被滤掉）
FIELD_BOUNDS: dict[str, tuple[float, float]] = {
    "turnover": (0.0, 100.0),
    "gender_gap": (0.0, 100.0),
    "gender_gap_avg": (0.0, 100.0),
    "cb": (0.0, 100.0),
    "female_pct": (0.0, 100.0),
    "leavers": (0.0, 1e7),
    "headcount": (0.0, 1e7),
}
NUMLIKE = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?")
# 紧跟字母/汉字的 1–2 位数字是脚注号（turnover42 / 流失率12），先剔掉
FOOTNOTE_LETTER = re.compile(r"(?<=[A-Za-z\u4e00-\u9fff])\d{1,2}(?=\s|$|[%％)）*])")
# 标签与首个数字之间只允许这些「单位/计数」词，出现别的词就说明这行不是该字段的总量
UNIT_TOKENS = re.compile(
    r"^(?:\s|[:：%,，、*•·|/()（）\-–—]"
    r"|number|numbers|persons?|people|person|employee|employees|unit|units|"
    r"times?|day|days|hour|hours|year|years|ratio|人|名|宗|个|個|次|天|小时|小時|倍|%|％|na)+$",
    re.I,
)
# 行内括号补充（如「Employee turnover rate (Executive Director and above)」）在比对单位词前先剔除
PAREN = re.compile(r"\([^)]{0,80}\)|（[^）]{0,80}）")

# 人数类字段必须是「行标签」：行首的 Number of employees / Total employees lost。
# 否则会被「Percentage of trained employees out of total employees」这类比率行误命中。
ROW_LABEL_ONLY = {"leavers", "headcount"}
ROW_LABEL_INDENT = 12
SENT_TURNOVER = re.compile(
    r"(?:overall\s+|total\s+|employee\s+|group\s*'?s?\s+)*turnover\s+rate"
    r"[^.%]{0,30}?(\d{1,3}(?:\.\d{1,2})?)\s*[%％]", re.I)
SENT_TURNOVER_PRIOR = re.compile(
    r"(\d{1,3}(?:\.\d{1,2})?)\s*[%％]\s*[（(]\s*(20\d{2})\s*[:：]\s*(\d{1,3}(?:\.\d{1,2})?)\s*[%％]")
SENT_GAP = re.compile(
    r"gender\s+pay\s+gap[^.%\n]{0,60}?(\d{1,3}(?:\.\d{1,2})?)\s*[%％]", re.I)
SENT_GAP_CN = re.compile(r"(?:性别|性別)薪酬差距[^。\n]{0,40}?(\d{1,3}(?:\.\d{1,2})?)\s*[%％]")
CN_TURNOVER = re.compile(r"(?:總?流失率|離職率|离职率)[^。\n%％]{0,20}?(\d{1,3}(?:\.\d{1,2})?)\s*[%％]")

DENOM_NOTE = re.compile(
    r"(?:turnover\s+rate|流失率|離職率)[^.\n。]{0,10}=\s*[^.\n。]{5,120}", re.I)


# ---------------------------------------------------------------- 工具
def _lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip()]


def _clean(line: str) -> str:
    """去掉紧跟字母的脚注号（turnover42 → turnover），保留原行结构。"""
    return FOOTNOTE_LETTER.sub("", line)


def _nums(tail: str) -> list[tuple[float, int]]:
    """从标签之后的片段里取 (数值, 起始位置)；剔除 4 位年份与紧邻的脚注小整数。"""
    out: list[tuple[float, int, str]] = []
    for m in NUMLIKE.finditer(tail):
        tok = m.group(0)
        if YEAR.fullmatch(tok):
            continue
        out.append((float(tok.replace(",", "")), m.start(), tok))
    cleaned: list[tuple[float, int, str]] = []
    for val, pos, tok in out:
        # 与前一个数字紧邻（无分隔）且只有 1–2 位 → 脚注号，丢弃
        if (cleaned and pos == cleaned[-1][1] + len(cleaned[-1][2])
                and re.fullmatch(r"\d{1,2}", tok)):
            continue
        cleaned.append((val, pos, tok))
    return [(c[0], c[1]) for c in cleaned]


def _year_seqs(lines: list[str]) -> list[list[int]]:
    """页内候选年份序列（按出现顺序去重），取前 40 行。"""
    seqs: list[list[int]] = []
    for line in lines[:40]:
        ys = [int(y) for y in YEAR.findall(line)]
        if not ys:
            continue
        seen, order = set(), []
        for y in ys:
            if y not in seen:
                seen.add(y)
                order.append(y)
        if order not in seqs:
            seqs.append(order)
    # 长序列优先（主表通常是多年份），但保留全部候选供逐行匹配
    seqs.sort(key=len, reverse=True)
    return seqs


def detect_report_year(doc: Doc, filename: str = "") -> int:
    m = re.search(r"FY(20\d{2})", filename)
    if m:
        return int(m.group(1))
    head = " ".join(p.text for p in doc.pages[:4])
    cand = re.findall(r"(?:Report|報告|报告|Sustainability\s+Report)\s*(20\d{2})", head)
    if cand:
        return int(max(int(c) for c in cand))
    years = [int(y) for y in YEAR.findall(head)]
    return max(years) if years else 0


# ---------------------------------------------------------------- 主流程
def extract(doc: Doc, filename: str = "", report_year: int | None = None) -> Extraction:
    ry = report_year or detect_report_year(doc, filename)
    ex = Extraction(report_year=ry)

    # ---- 1. 表格：数值个数与页内年份序列对齐
    for page in doc.pages:
        lines = [_clean(l) for l in _lines(page.text)]
        seqs = _year_seqs(lines)
        if not seqs:
            continue
        for line in lines:
            for fld, labels in FIELD_LABELS.items():
                # 同一行可能并列多个标签（「Employee turnover rate (Executive Director and above)」），
                # 逐个候选试，取第一个通过校验的
                cands: list[re.Match] = []
                for pat in labels:
                    cands.extend(re.finditer(pat, line, re.I))
                cands.sort(key=lambda m: m.start())
                for m in cands:
                    # 「average / mean gender pay gap」归 gender_gap_avg，不混入中位数口径
                    prefix = line[max(0, m.start() - 28):m.start()]
                    if fld == "gender_gap" and re.search(r"average|mean|平均", prefix, re.I):
                        continue
                    if fld in ROW_LABEL_ONLY and (
                            m.start() > ROW_LABEL_INDENT
                            or re.search(r"of$|out\s+of|percentage|proportion|ratio|%",
                                         prefix, re.I)):
                        continue
                    tail = line[m.end():]
                    pairs = _nums(tail)
                    if not pairs:
                        continue
                    # 标签与首个数字之间只允许单位词（行内括号补充先剔除）：
                    # 否则是「by gender / with disabilities」等维度行或比率行
                    head = PAREN.sub(" ", tail[:pairs[0][1]])
                    if not UNIT_TOKENS.match(head.strip() or " "):
                        continue
                    vals = [v for v, _ in pairs
                            if FIELD_BOUNDS.get(fld, (0.0, 100.0))[0]
                            <= v <= FIELD_BOUNDS.get(fld, (0.0, 100.0))[1]]
                    if not vals:
                        continue
                    hit = None
                    for seq in seqs:
                        if len(seq) == len(vals):
                            hit = (seq, "高", "table:year-align")
                            break
                    if hit is None and len(vals) == 1:
                        seq = seqs[0]
                        hit = ([seq[0]], "中", "table:single")
                    if hit is None:
                        ex.warnings.append(
                            f"p{page.no} {fld} 数值 {len(vals)} 个与页内年份序列 "
                            f"{[len(s) for s in seqs]} 均不符，跳过")
                        continue
                    seq, conf, meth = hit
                    # 摘录取「标签附近窗口」而不是整行开头：PDF 文本层常把同一页的
                    # 两张表并进一行，line[:170] 会摘到无关的上一张表
                    snip = line[max(0, m.start() - 20): m.start() + 150]
                    for y, v in zip(seq, vals):
                        ex.facts.append(NumFact(
                            fld, float(v), y, page.no, meth,
                            f"[p{page.no}] …{snip}… | 年份序列 {seq} 对位 → {y}: {v}",
                            conf))
                    break

    # ---- 2. 正文句式兜底
    for s in sentences(doc):
        t = s.text
        low = t.lower()
        if "turnover" in low or "流失率" in t or "離職率" in t:
            pm = SENT_TURNOVER_PRIOR.search(t)
            if pm:
                cur, py, prev = float(pm.group(1)), int(pm.group(2)), float(pm.group(3))
                ex.facts.append(NumFact("turnover", cur, py + 1, s.page, "sentence:bracket",
                                        f"[p{s.page}] {t[:200]}", "中"))
                ex.facts.append(NumFact("turnover", prev, py, s.page, "sentence:bracket",
                                        f"[p{s.page}] {t[:200]}", "中"))
            else:
                for rx, meth in ((SENT_TURNOVER, "sentence:en"), (CN_TURNOVER, "sentence:cn")):
                    mm = rx.search(t)
                    if mm:
                        v = float(mm.group(1))
                        if 0 < v <= 100:
                            ex.facts.append(NumFact("turnover", v, ry, s.page, meth,
                                                    f"[p{s.page}] {t[:200]}", "中"))
                        break
        if "gender pay gap" in low or "薪酬差距" in t:
            for rx in (SENT_GAP, SENT_GAP_CN):
                mm = rx.search(t)
                if mm:
                    v = float(mm.group(1))
                    if 0 <= v <= 100:
                        ex.facts.append(NumFact("gender_gap", v, ry, s.page, "sentence",
                                                f"[p{s.page}] {t[:200]}", "中"))
                    break

    # ---- 3. 去重择优（同字段同年：高 > 中 > 低；数值不一致则降级并记冲突）
    rank = {"高": 3, "中": 2, "低": 1}
    best: dict[str, dict[int, NumFact]] = {}
    for f in ex.facts:
        if f.year is None:
            continue
        cur = best.setdefault(f.field, {})
        old = cur.get(f.year)
        if old is None:
            cur[f.year] = f
            continue
        if abs(old.value - f.value) > 1e-9:
            ex.warnings.append(
                f"{f.field}@{f.year} 存在不一致取值 {old.value}（p{old.page}）与 "
                f"{f.value}（p{f.page}），按较低置信度处理")
            # 两个都是高置信但数值不一致 → 整条降为低，避免用错数
            keep = old if rank[old.confidence] >= rank[f.confidence] else f
            keep.confidence = "低"
            keep.note = (keep.note + "；存在冲突").strip("；")
            cur[f.year] = keep
            continue
        if rank[f.confidence] > rank[old.confidence]:
            cur[f.year] = f
    ex.best = best

    # ---- 4. 一致性校验：离职人数 / 员工总数 ≈ 流失率
    t = ex.value("turnover", ry)
    lv = ex.value("leavers", ry)
    hc = ex.value("headcount", ry)
    if t and lv and hc:
        calc = lv / hc * 100
        if abs(calc - t) > 3.0:
            ex.warnings.append(
                f"口径冲突：{lv:,.0f}/{hc:,.0f}={calc:.2f}% 与披露流失率 {t:.2f}% 差 "
                f"{abs(calc - t):.2f}pp（可能分母用平均人数或口径不同）")
        else:
            ex.warnings.append(
                f"交叉校验通过：{lv:,.0f}/{hc:,.0f}={calc:.2f}% ≈ 披露 {t:.2f}%")

    # ---- 5. 分母口径脚注
    for s in sentences(doc):
        mm = DENOM_NOTE.search(s.text)
        if mm and ("turnover" in mm.group(0).lower() or "流失" in mm.group(0)
                   or "離職" in mm.group(0)):
            ex.denom_note = s.text[:200]
            break

    if not ex.facts:
        ex.warnings.append("未抽取到任何数值，S1.1.1/1.1.2 将按 N/A 处理")
    return ex
