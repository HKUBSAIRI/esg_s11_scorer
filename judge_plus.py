# -*- coding: utf-8 -*-
"""判定增强层：在 ESG_insight 的 29 要素词法判定之上，补四条「本批实测会误判」的规则。

为什么要这一层
--------------
``ESG_insight/app/s11_judge.py`` 的判定是为「跨行业小盘股 ESG 章节」调出来的，
在这批生物制药报告（篇幅长、附录 KPI 表多、中英混排）上有 4 类系统性偏差，实测各至少 1 例：

| 编号 | 偏差 | 实测现象 |
|---|---|---|
| P1 | 薪酬内部审查漏判 | 规则按「gender pay gap + audit」正序匹配，原文是 `conduct audits of our gender pay gap`（倒序），报「未检索到」 |
| P2 | 劳动合同误判为「是」 | 因「同等合规标准适用于非全职员工」这类培训语被合并升档，实际全篇无劳动合同制度 |
| P3 | 法定社保漏判 | 险种清单（养老/医疗/失业/工伤/生育/公积金）是列举项而非「制度名词句」，被判「部分」 |
| P4 | 育儿假漏判 | 假别清单同理；且 `maternity insurance` 反例会误伤 `paid maternity leave` |

设计取向：**只加不改**。增强层不重写词表、不改动 ESG_insight 仓库，
每条补丁都是「条件 + 新判定 + 理由」，全部落进 `patches`，报告里逐条可查。
判定顺序：先跑词法 → 满足补丁条件才覆盖 → 不被覆盖的保持词法结果。
"""
from __future__ import annotations

import re
from dataclasses import replace

from s11_judge import ElementResult, judge_lexical
from s11_text import Doc

from .models import Patch

CONTRACT = re.compile(r"劳动合同|勞動合同|employment\s+contract|labou?r\s+contract|"
                      r"聘用合约|聘用合約|雇佣合同|僱傭合約", re.I)
# 薪酬内部审查：只认「薪酬/工资」语境，不认董事薪酬委员会、气候挂钩薪酬评估这类同形词
PAY_EQUITY = re.compile(r"gender\s+pay|pay\s+equity|equal\s+pay|同工同酬|同值同酬", re.I)
AUDIT_VERB = re.compile(r"audit|review|assessment|monitor|审查|審查|审计|審計|监测|監測|检讨|檢討", re.I)
PAY_WORD = re.compile(r"薪酬|工资|工資|薪資|salary|salaries|wage|wages|remuneration|compensation", re.I)
PAY_AUDIT = re.compile(
    # 英文：「pay audit」「review of pay」「audits of our gender pay gap」
    r"(?:pay|salary|salaries|wage|wages|remuneration|compensation)\s+(?:equity\s+)?"
    r"(?:audit|review|auditing|reviewing)\w*"
    r"|(?:audit|review)\w*\s+(?:of\s+)?(?:our|the|its|their|all\s+)?\s*"
    r"(?:gender\s+pay|pay\s+equity|equal\s+pay|pay|salaries|remuneration|compensation)"
    # 中文：「薪酬审查」「工资监测」「审查薪酬」
    r"|(?:薪酬|工资|工資)\s*(?:审查|審查|审计|審計|监测|監測|检讨|檢討)"
    r"|(?:审查|審查|审计|審計|监测|監測)\s*(?:薪酬|工资|工資)", re.I)
PEOPLE = re.compile(r"员工|員工|僱員|雇员|employe|staff|worker|personnel", re.I)
# 终止雇佣类词必须落在「人」的语境里，否则「药品生命周期终止」「政策终止」都会命中
TERM_WORD = re.compile(r"severance|redundan|dismiss|termination\s+of\s+employment|"
                       r"裁员|裁員|遣散|解雇|解僱|经济补偿|經濟補償|离职补偿|員工安置|员工安置|"
                       r"outplacement|notice\s+period|通知期", re.I)
# 个案统计：KPI 表里的「Cases 0」是统计结果，与「未接获投诉」这类否定式表述不同。
# 要求计数紧跟 Cases/宗/件，避免把目录页码（GRI 406 … 4）误当计数
CASE_STAT = re.compile(
    r"(?:discrimination|harassment|歧视|歧視|骚扰|騷擾)[^.\n。]{0,40}?"
    r"(?:cases?|incidents?|宗|件|個案|个案)[\s:：=]{0,3}(\d{1,5})", re.I)
# 福利「覆盖/使用」必须落在覆盖率、参保率、使用量上，只给「98 天产假」不算
COVERAGE = re.compile(
    r"覆盖率|覆蓋率|coverage|covered|uptake|utilisation|utilization|使用率|参与率|參與率|"
    r"参保|參保|受惠|惠及|beneficiar", re.I)
INSURANCE = re.compile(r"pension\s+insurance|medical\s+insurance|unemployment\s+insurance|"
                       r"work[- ]related\s+injury|maternity\s+insurance|"
                       r"housing\s+provident\s+fund|養老保險|养老保险|醫療保險|医疗保险|"
                       r"失業保險|失业保险|工傷保險|工伤保险|生育保險|生育保险|住房公積金|住房公积金",
                       re.I)
FAMILY_LEAVE = re.compile(r"maternity\s+leave|paternity\s+leave|parental\s+leave|"
                          r"family\s+(?:or\s+)?care\s+leave|breast-?feeding|lactation|"
                          r"產假|产假|侍產假|侍产假|育兒假|育儿假|哺乳", re.I)


def _sentences_with(doc: Doc, rx: re.Pattern, limit: int = 1) -> list[str]:
    out: list[str] = []
    for p in doc.pages:
        flat = " ".join(l.strip() for l in p.text.splitlines() if l.strip())
        for m in rx.finditer(flat):
            s, e = max(0, m.start() - 60), min(len(flat), m.end() + 60)
            out.append(flat[s:e])
            if len(out) >= limit:
                return out
    return out


def _iter_sentences(doc: Doc):
    """逐句迭代（页内压平后按句末标点切），供补丁层做整句级判断。"""
    for p in doc.pages:
        flat = " ".join(l.strip() for l in p.text.splitlines() if l.strip())
        for s in re.split(r"(?<=[.!?;。；])\s*", flat):
            if s.strip():
                yield s.strip()


def _count_distinct(rx: re.Pattern, text: str) -> int:
    return len({m.group(0).lower() for m in rx.finditer(text)})


def adjudicate(doc: Doc, base: dict[str, ElementResult] | None = None
               ) -> tuple[dict[str, ElementResult], list[Patch]]:
    """返回（增强后的要素判定, 生效的补丁清单）。

    **不修改传入的判定对象**：``s11_judge`` 返回的 ``ElementResult`` 属于别的模块，就地改它的
    ``verdict`` 会让调用方拿到被污染的对象（同一份 doc 二次判定时结果不可预期）。
    这里用 ``dataclasses.replace`` 逐个浅拷贝，补丁只作用在副本上。
    """
    src = base if base is not None else judge_lexical(doc)
    res = {k: replace(v) for k, v in src.items()}
    text = doc.text
    patches: list[Patch] = []

    def setv(eid: str, after: str, reason: str) -> None:
        r = res.get(eid)
        if r is None or r.verdict == after:
            return
        patches.append(Patch(eid, r.name, r.verdict, after, reason))
        r.verdict = after
        r.reason = (r.reason + " ｜增强层：" + reason).strip(" ｜")

    # P1 薪酬内部审查：正序/倒序都认，但必须落在员工薪酬语境
    r5 = res.get("S1.1.2-5")
    if r5 is not None and r5.verdict != "是":
        cand = None
        for s in _iter_sentences(doc):
            if not (PAY_WORD.search(s) and AUDIT_VERB.search(s)):
                continue
            if not (PAY_EQUITY.search(s) or PEOPLE.search(s)):
                continue
            if PAY_AUDIT.search(s):
                cand = s
                break
        if cand:
            setv("S1.1.2-5", "是",
                 f"原文命中薪酬审查/监测语句「{cand[:60]}」（词表只认正序，此处为倒序或近义写法）")

    # P2 劳动合同：有制度判「是」的必须有合同文本类证据
    r4 = res.get("S1.1.1-4")
    if r4 is not None and r4.verdict == "是" and not CONTRACT.search(text):
        setv("S1.1.1-4", "部分",
             "全篇无「劳动合同 / employment contract」表述，原判决来自合规培训等近义句")

    # P3 法定社保：险种清单 ≥3 项即视为已披露具体保障
    r41 = res.get("S1.1.4-1")
    if r41 is not None and r41.verdict != "是":
        n = _count_distinct(INSURANCE, text)
        if n >= 3:
            setv("S1.1.4-1", "是", f"险种逐项列示 {n} 项（养老/医疗/失业/工伤/生育/公积金）")

    # P4 育儿假：假别 ≥2 类即视为已披露具体假期安排
    r44 = res.get("S1.1.4-4")
    if r44 is not None and r44.verdict != "是":
        n = _count_distinct(FAMILY_LEAVE, text)
        if n >= 2:
            setv("S1.1.4-4", "是", f"家庭友好假别逐项列示 {n} 类")

    # P5 歧视/骚扰个案统计：KPI 表里「Cases 0」是统计结果，不是否定式表述
    r56 = res.get("S1.1.5-6")
    if r56 is not None and r56.verdict != "是":
        hit = None
        for s in _iter_sentences(doc):
            m = CASE_STAT.search(s)
            if m:
                hit = (s, m.group(1))
                break
        if hit:
            setv("S1.1.5-6", "是",
                 f"披露歧视/骚扰个案计数（{hit[1]}）：「{hit[0][:60]}」")

    # P6 福利覆盖：只给假期天数/金额不算覆盖或使用数据
    if r_cov := res.get("S1.1.4-5"):
        if r_cov.verdict == "是" and not any(COVERAGE.search(h.sent) for h in r_cov.hits):
            setv("S1.1.4-5", "部分", "命中句只有假期天数/条款，无覆盖率或使用量")

    # P7 解雇与安置：终止类词必须与「人」同句，否则是药品生命周期/政策终止
    if r_ter := res.get("S1.1.1-5"):
        if r_ter.verdict == "是" and not any(
                TERM_WORD.search(h.sent) and PEOPLE.search(h.sent) for h in r_ter.hits):
            setv("S1.1.1-5", "部分", "终止类命中句未落在员工语境（多为产品生命周期/政策终止）")

    return res, patches