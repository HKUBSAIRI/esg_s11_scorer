# -*- coding: utf-8 -*-
"""打分层：定性判档 + 数值 → 五项指标分（含分项、档位、可达上限、缺口说明）。

三条口径约定（改动须同步 ``MODEL_VERSION``）
------------------------------------------------------------------------
1. **N/A ≠ 0**：分项不可评时记 ``None``，同时给出「严格（N/A 按 0 计）」与
   「归一（按可观测权重折算）」两个数，并标明可达上限。只看一个数必然误读。
2. **判档结论是必填项**：C 项与 S1.1.3/4/5 一律取 ``Verdict``（LLM 或关键词）。
   缺判档就抛错——静默拼分只会产出与交付物不一致的数。
3. **A/B 分位优先用对标池**：有对标池 → 按同业分位取档；无对标池 → A 记 N/A，
   B 退化为「改善率方向」（与 10 份样本专家口径一致，便于横向比较）。

本模块只做算术：输入是抽取到的数值与判档结论，输出是分数。不再有词法判定层。
"""
from __future__ import annotations

from .bench import Bench
from .config import (GAP_B_FLOOR, GAP_BANDS, GAP_IMPROVE_FULL, GAP_NEAR_ZERO,
                     IND_NAMES, IND_WEIGHTS, SPECS)
from .metrics import improvement
from .models import Component, IndScore, ScoreCard, ScoreData
from .qualitative import Verdict
from .rubric import QUALITATIVE_PARTS


# 打分必须拿到的判档键（S1.1.1-C / S1.1.2-C / S1.1.3 / S1.1.4 / S1.1.5）
REQUIRED_VERDICTS = tuple(k for keys in QUALITATIVE_PARTS.values() for k in keys)


def _verdict_note(v: Verdict) -> str:
    """判档结论在报告里的说明：档位 + 理由 + 出处（llm/keyword/gate）。

    兜底理由本身就以「关键词兜底：」开头时不再套一层前缀，否则会写成
    「关键词兜底：50 分｜关键词兜底：…」。
    """
    src = {"llm": "LLM 判档", "keyword": "关键词兜底", "gate": "0 分前置条件"}.get(v.source, v.source)
    if v.reason.startswith(src):
        txt = v.reason
    else:
        txt = f"{src}：{v.tier} 分｜{v.reason}"
    return txt + (f"｜缺口：{v.missing}" if v.missing else "")


def _verdict_evidence(v: Verdict) -> str:
    return v.evidence[0] if v.evidence else ""


def _tiered(ind: str, v: Verdict) -> IndScore:
    """三档制指标：档位直接取判档结论。"""
    comps = [Component("", "判档依据", 0.0, None, _verdict_note(v), _verdict_evidence(v))]
    return IndScore(ind, IND_NAMES[ind], IND_WEIGHTS[ind], "tiered", comps,
                    raw=int(v.tier), note=f"{v.tier} 分（{v.source}）")


# ---------------------------------------------------------------- S1.1.1
def score_s111(turnover, prior, bench: Bench, code: str,
               ev: str = "", *, verdict: Verdict) -> IndScore:
    """S1.1.1。``ev`` 是抽取流失率时的原文，用作 A/B 的证据引用。

    ``verdict`` 是 C 项的定性判档结论，**必填**。
    """
    comps: list[Component] = []
    # A 同业流失率对标
    # 用 ``is not None`` 而不是真值判断：流失率 0% 是「最好」，不是「没值」。
    # 写成 ``if turnover`` 会让零流失的公司 A 项直接 N/A（实测 S1.1.1 由 100 掉到 60）。
    pct_a = (bench.percentile(code, "turnover", higher_better=False)
             if turnover is not None else None)
    if pct_a is None:
        comps.append(Component("A", "同业流失率对标", 0.4, None,
                               "无对标池或缺本期流失率，A 不评" if turnover is None
                               else f"对标池样本不足（n={bench.n}）或本家无值，A 不评",
                               ev))
    else:
        comps.append(Component("A", "同业流失率对标", 0.4, bench.band(pct_a),
                               f"流失率 {turnover:.2f}% → 同业分位 {pct_a:.0f}%（{bench.kind}, n={bench.n}）",
                               ev))
    # B 同业趋势对标
    imp = improvement(turnover, prior)
    if turnover is None or prior is None:
        comps.append(Component("B", "同业趋势对标", 0.3, None,
                               "缺本期或上期流失率，B 不评", ev))
    elif prior == 0:
        comps.append(Component("B", "同业趋势对标", 0.3, 100 if turnover == 0 else 0,
                               f"上期为 0，本期 {turnover}%", ev))
    else:
        pct_b = bench.percentile(code, "improvement", higher_better=True)
        if pct_b is not None:
            comps.append(Component("B", "同业趋势对标", 0.3, bench.band(pct_b),
                                   f"改善率 {imp:+.2f}% → 同业分位 {pct_b:.0f}%（{bench.kind}, n={bench.n}）",
                                   ev))
        else:
            # 无对标池：退化为「改善率方向」，与 10 份样本专家口径一致
            comps.append(Component("B", "同业趋势对标", 0.3, 100 if imp > 0 else 0,
                                   f"改善率 {imp:+.2f}%（无对标池，按改善方向取档）",
                                   ev))
    # C 披露质量与就业终止保障：取判档结论
    comps.append(Component("C", "披露质量与就业终止保障", 0.3, int(verdict.tier),
                           _verdict_note(verdict), _verdict_evidence(verdict)))
    return IndScore("S1.1.1", IND_NAMES["S1.1.1"], IND_WEIGHTS["S1.1.1"], "weighted", comps)


# ---------------------------------------------------------------- S1.1.2
def score_s112(gap, prior_gap, gap_basis: str = "", ev: str = "",
               *, verdict: Verdict) -> IndScore:
    """S1.1.2。`ev` 是抽取薪酬差距时的原文，用作 A/B 的证据引用；``verdict`` 必填。"""
    comps: list[Component] = []
    if gap is None:
        comps.append(Component("A", "薪酬差距水平", 0.4, None, "未披露性别薪酬差距", ""))
        comps.append(Component("B", "趋势得分", 0.3, None, "无差距数据，B 不评", ""))
    else:
        g = abs(gap)
        a = next((s for upper, s in GAP_BANDS if g <= upper), 0)
        comps.append(Component("A", "薪酬差距水平", 0.4, a,
                               f"差距 {g:.2f}%" + ("（中位数）" if gap_basis != "avg" else "（平均值口径代入）"),
                               ev))
        if prior_gap is None:
            comps.append(Component("B", "趋势得分", 0.3, None, "缺上期差距，B 不评",
                                   ev))
        else:
            pg = abs(prior_gap)
            if pg == 0:
                # 补充规则：上期为 0 时不算改善率，只看本期是否维持在 GAP_NEAR_ZERO(2%) 以内
                b = 100 if g <= GAP_NEAR_ZERO else 0
                note = f"上期为 0，本期 {g:.2f}%"
            else:
                imp = improvement(g, pg)
                # 100 分档有两条入口：改善率 ≥5%，或本期差距已落进 A 的 100 分档（≤3%）。
                # 后者不能用 GAP_NEAR_ZERO(2%)——那是「上期为 0」的专用阈值。
                if g <= GAP_B_FLOOR:
                    b = 100
                    note = (f"本期差距 {g:.2f}% ≤ {GAP_B_FLOOR:.0f}%，"
                            f"已落进 A 的 100 分档（改善率 {imp:+.2f}%）")
                elif imp >= GAP_IMPROVE_FULL:
                    b = 100
                    note = f"改善率 {imp:+.2f}% ≥ {GAP_IMPROVE_FULL:.0f}%（{pg:.2f}% → {g:.2f}%）"
                else:
                    b = 50 if imp > 0 else 0
                    note = f"改善率 {imp:+.2f}%（{pg:.2f}% → {g:.2f}%）"
            comps.append(Component("B", "趋势得分", 0.3, b, note, ev))
    # C 薪酬支付保障：取判档结论
    comps.append(Component("C", "薪酬支付保障", 0.3, int(verdict.tier),
                           _verdict_note(verdict), _verdict_evidence(verdict)))
    return IndScore("S1.1.2", IND_NAMES["S1.1.2"], IND_WEIGHTS["S1.1.2"], "weighted", comps)


# ---------------------------------------------------------------- 三档制
def score_s113(verdict: Verdict) -> IndScore:
    """三档制：档位直接取判档结论（100 / 50 / 0）。"""
    return _tiered("S1.1.3", verdict)


def score_s114(verdict: Verdict) -> IndScore:
    return _tiered("S1.1.4", verdict)


def score_s115(verdict: Verdict) -> IndScore:
    return _tiered("S1.1.5", verdict)


SCORERS = {"score_s111": score_s111, "score_s112": score_s112,
           "score_s113": score_s113, "score_s114": score_s114, "score_s115": score_s115}

# 规格里声明的指标 ↔ 实现，缺一即报错（避免「加了规格忘了实现」静默无分）
_MISSING = [s.ind for s in SPECS if s.scorer not in SCORERS]
if _MISSING:
    raise RuntimeError(f"指标规格里声明的判分函数未实现：{_MISSING}")


def build_card(code: str, name: str, report_year: int, file: str,
               data: ScoreData, bench: Bench,
               verdicts: dict[str, Verdict]) -> ScoreCard:
    """把五项指标装配成一张记分卡（判分函数只负责单指标，装配在这一处）。

    ``verdicts`` 是定性判档结果（键见 ``rubric.QUALITATIVE_PARTS``），**五项缺一不可**。
    缺判档说明链路出了问题，此时抛错而不是静默拼一个「看着正常、口径却过期」的分。
    """
    v = dict(verdicts or {})
    missing = [k for k in REQUIRED_VERDICTS if k not in v]
    if missing:
        raise ValueError(
            f"{code} 缺定性判档结论 {missing}："
            f"请用 --qualitative llm（或 llm 不可用时的 keyword）重跑")
    card = ScoreCard(code=code, name=name, report_year=report_year, file=file, data=data)
    card.indicators["S1.1.1"] = score_s111(data.turnover, data.prior_turnover,
                                           bench, code, ev=data.turnover_quote,
                                           verdict=v["S1.1.1-C"])
    card.indicators["S1.1.2"] = score_s112(data.gap, data.prior_gap,
                                           data.gap_basis, ev=data.gap_quote,
                                           verdict=v["S1.1.2-C"])
    card.indicators["S1.1.3"] = score_s113(v["S1.1.3"])
    card.indicators["S1.1.4"] = score_s114(v["S1.1.4"])
    card.indicators["S1.1.5"] = score_s115(v["S1.1.5"])
    return card