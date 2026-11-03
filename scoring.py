# -*- coding: utf-8 -*-
"""打分层：要素判定 + 数值 → 五项指标分（含分项、档位、可达上限、缺口说明）。

三条口径约定（与《S1.1 指标与判断标准》一一对应，改动须同步 MODEL_VERSION）
------------------------------------------------------------------------
1. **N/A ≠ 0**：分项不可评时记 ``None``，同时给出「严格（N/A 按 0 计）」与
   「归一（按可观测权重折算）」两个数，并标明可达上限。只看一个数必然误读。
2. **C 项要「制度」不是「提到了」**：S1.1.1 的 C 与 S1.1.2 的 C，100 分档要求
   「建立明确的…制度」，故只有要素判「是」才算建成；「部分」只能到 50。
3. **A/B 分位优先用对标池**：有对标池 → 按同业分位取档；无对标池 → A 记 N/A，
   B 退化为「改善率方向」（与 10 份样本专家口径一致，便于横向比较）。
"""
from __future__ import annotations

from s11_judge import ElementResult

from .bench import Bench
from .config import (GAP_BANDS, GAP_IMPROVE_FULL, GAP_NEAR_ZERO, IND_NAMES,
                     IND_WEIGHTS, SPECS)
from .metrics import improvement
from .models import Component, IndScore, ScoreCard, ScoreData


def _v(res: dict[str, ElementResult], eid: str) -> str:
    r = res.get(eid)
    return r.verdict if r else "否"


def _ev(res: dict[str, ElementResult], eid: str) -> str:
    r = res.get(eid)
    return r.top_quote() if r else ""


# ---------------------------------------------------------------- S1.1.1
def score_s111(res, turnover, prior, has_vol, bench: Bench, code: str) -> IndScore:
    comps: list[Component] = []
    # A 同业流失率对标
    pct_a = bench.percentile(code, "turnover", higher_better=False) if turnover else None
    if pct_a is None:
        comps.append(Component("A", "同业流失率对标", 0.4, None,
                               "无对标池或缺本期流失率，A 不评" if turnover is None
                               else f"对标池样本不足（n={bench.n}）或本家无值，A 不评",
                               _ev(res, "S1.1.1-1")))
    else:
        comps.append(Component("A", "同业流失率对标", 0.4, bench.band(pct_a),
                               f"流失率 {turnover:.2f}% → 同业分位 {pct_a:.0f}%（{bench.kind}, n={bench.n}）",
                               _ev(res, "S1.1.1-1")))
    # B 同业趋势对标
    imp = improvement(turnover, prior)
    if turnover is None or prior is None:
        comps.append(Component("B", "同业趋势对标", 0.3, None,
                               "缺本期或上期流失率，B 不评", _ev(res, "S1.1.1-2")))
    elif prior == 0:
        comps.append(Component("B", "同业趋势对标", 0.3, 100 if turnover == 0 else 0,
                               f"上期为 0，本期 {turnover}%", _ev(res, "S1.1.1-2")))
    else:
        pct_b = bench.percentile(code, "improvement", higher_better=True)
        if pct_b is not None:
            comps.append(Component("B", "同业趋势对标", 0.3, bench.band(pct_b),
                                   f"改善率 {imp:+.2f}% → 同业分位 {pct_b:.0f}%（{bench.kind}, n={bench.n}）",
                                   _ev(res, "S1.1.1-2")))
        else:
            # 无对标池：退化为「改善率方向」，与 10 份样本专家口径一致
            comps.append(Component("B", "同业趋势对标", 0.3, 100 if imp > 0 else 0,
                                   f"改善率 {imp:+.2f}%（无对标池，按改善方向取档）",
                                   _ev(res, "S1.1.1-2")))
    # C 披露质量与就业终止保障
    lab, ter = _v(res, "S1.1.1-4"), _v(res, "S1.1.1-5")
    if lab == "是" and ter == "是" and has_vol:
        c, note = 100, "离职率构成披露 + 劳动合同与解雇安置制度齐备"
    elif lab != "否" or ter != "否" or has_vol:
        miss = []
        if lab != "是":
            miss.append(f"劳动合同（{lab}）")
        if ter != "是":
            miss.append(f"解雇与安置（{ter}）")
        if not has_vol:
            miss.append("自愿离职率构成")
        c, note = 50, "缺：" + "、".join(miss) if miss else "有制度但缺保障成效证据"
    else:
        c, note = 0, "未识别到合同/终止保障相关内容"
    comps.append(Component("C", "披露质量与就业终止保障", 0.3, c, note,
                           _ev(res, "S1.1.1-4") or _ev(res, "S1.1.1-5")))
    return IndScore("S1.1.1", IND_NAMES["S1.1.1"], IND_WEIGHTS["S1.1.1"], "weighted", comps)


# ---------------------------------------------------------------- S1.1.2
def score_s112(res, gap, prior_gap, gap_basis: str = "") -> IndScore:
    comps: list[Component] = []
    if gap is None:
        comps.append(Component("A", "薪酬差距水平", 0.4, None, "未披露性别薪酬差距", ""))
        comps.append(Component("B", "趋势得分", 0.3, None, "无差距数据，B 不评", ""))
    else:
        g = abs(gap)
        a = next((s for upper, s in GAP_BANDS if g <= upper), 0)
        comps.append(Component("A", "薪酬差距水平", 0.4, a,
                               f"差距 {g:.2f}%" + ("（中位数）" if gap_basis != "avg" else "（平均值口径代入）"),
                               _ev(res, "S1.1.2-1")))
        if prior_gap is None:
            comps.append(Component("B", "趋势得分", 0.3, None, "缺上期差距，B 不评",
                                   _ev(res, "S1.1.2-1")))
        else:
            pg = abs(prior_gap)
            if pg == 0:
                b = 100 if g <= GAP_NEAR_ZERO else 0
                note = f"上期为 0，本期 {g:.2f}%"
            else:
                imp = improvement(g, pg)
                b = 100 if (imp >= GAP_IMPROVE_FULL or g <= GAP_NEAR_ZERO) else (50 if imp > 0 else 0)
                note = f"改善率 {imp:+.2f}%（{pg:.2f}% → {g:.2f}%）"
            comps.append(Component("B", "趋势得分", 0.3, b, note, _ev(res, "S1.1.2-1")))
    # C 薪酬支付保障
    pay, timely, audit = _v(res, "S1.1.2-2"), _v(res, "S1.1.2-3"), _v(res, "S1.1.2-5")
    if audit == "是" and pay == "是":
        c, note = 100, "有薪酬制度 + 内部审查/监测结果（性别薪酬差距审计等）"
    elif pay == "是" or timely == "是":
        c, note = 50, "有薪酬制度与执行措施，缺内部审查结果或纠正成效"
    elif pay == "部分" or timely == "部分":
        c, note = 50, "仅原则性表述，未达制度级"
    else:
        c, note = 0, "未识别到薪酬支付保障"
    comps.append(Component("C", "薪酬支付保障", 0.3, c, note, _ev(res, "S1.1.2-2")))
    return IndScore("S1.1.2", IND_NAMES["S1.1.2"], IND_WEIGHTS["S1.1.2"], "weighted", comps)


# ---------------------------------------------------------------- 三档制
def score_s113(res) -> IndScore:
    leaves = _v(res, "S1.1.3-5")
    trio = [_v(res, f"S1.1.3-{i}") for i in (1, 2, 3)]
    if leaves == "是":
        s, note = 100, "有休假执行数据/覆盖（100 分档入口）"
    elif any(v == "是" for v in trio) or sum(1 for v in trio if v == "部分") >= 2:
        s, note = 50, "有工时/加班/排班措施，缺休假执行数据"
    else:
        s, note = 0, "未识别到具体措施"
    comps = [Component("", "执行证据", 0.0, None,
                       f"休假执行数据={leaves}；工时={trio[0]}/加班={trio[1]}/排班={trio[2]}",
                       _ev(res, "S1.1.3-5") or _ev(res, "S1.1.3-1"))]
    return IndScore("S1.1.3", IND_NAMES["S1.1.3"], IND_WEIGHTS["S1.1.3"], "tiered", comps, raw=s, note=note)


def score_s114(res) -> IndScore:
    cov, base = _v(res, "S1.1.4-5"), _v(res, "S1.1.4-2")
    items = [_v(res, f"S1.1.4-{i}") for i in (1, 3, 4)]
    if cov == "是":
        s, note = 100, "有福利覆盖/使用数据（100 分档入口）"
    elif any(v != "否" for v in items) and base != "否":
        s, note = 50, "有具体福利措施，缺覆盖范围或使用证据"
    else:
        s, note = 0, "仅原则性关怀声明或未识别"
    comps = [Component("", "覆盖证据", 0.0, None,
                       f"福利覆盖={cov}；社保={items[0]}/补充={items[1]}/育儿={items[2]}",
                       _ev(res, "S1.1.4-5") or _ev(res, "S1.1.4-2"))]
    return IndScore("S1.1.4", IND_NAMES["S1.1.4"], IND_WEIGHTS["S1.1.4"], "tiered", comps, raw=s, note=note)


def score_s115(res) -> IndScore:
    har, eq, disc, cases = (_v(res, "S1.1.5-4"), _v(res, "S1.1.5-1"),
                            _v(res, "S1.1.5-3"), _v(res, "S1.1.5-6"))
    if har == "是" and cases == "是":
        s, note = 100, "反骚扰机制 + 事件处置证据（100 分档入口）"
    elif disc != "否" and (eq != "否" or har != "否"):
        s, note = 50, "有具名反歧视/反骚扰制度，缺专项监测或个案处置结果"
    else:
        s, note = 0, "仅笼统声明或未识别"
    comps = [Component("", "制度与个案", 0.0, None,
                       f"反骚扰={har}；平等机会={eq}；反歧视={disc}；事件统计={cases}",
                       _ev(res, "S1.1.5-4") or _ev(res, "S1.1.5-3"))]
    return IndScore("S1.1.5", IND_NAMES["S1.1.5"], IND_WEIGHTS["S1.1.5"], "tiered", comps, raw=s, note=note)


SCORERS = {"score_s111": score_s111, "score_s112": score_s112,
           "score_s113": score_s113, "score_s114": score_s114, "score_s115": score_s115}

# 规格里声明的指标 ↔ 实现，缺一即报错（避免「加了规格忘了实现」静默无分）
_MISSING = [s.ind for s in SPECS if s.scorer not in SCORERS]
if _MISSING:
    raise RuntimeError(f"指标规格里声明的判分函数未实现：{_MISSING}")


def build_card(code: str, name: str, report_year: int, file: str,
               res: dict[str, ElementResult], data: ScoreData,
               bench: Bench, has_vol: bool) -> ScoreCard:
    """把五项指标装配成一张记分卡（判分函数只负责单指标，装配在这一处）。"""
    card = ScoreCard(code=code, name=name, report_year=report_year, file=file, data=data)
    card.indicators["S1.1.1"] = score_s111(res, data.turnover, data.prior_turnover,
                                           has_vol, bench, code)
    card.indicators["S1.1.2"] = score_s112(res, data.gap, data.prior_gap, data.gap_basis)
    card.indicators["S1.1.3"] = score_s113(res)
    card.indicators["S1.1.4"] = score_s114(res)
    card.indicators["S1.1.5"] = score_s115(res)
    return card