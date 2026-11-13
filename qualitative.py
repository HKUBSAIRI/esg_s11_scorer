# -*- coding: utf-8 -*-
"""定性判档：关键词召回段落 → 一次判档（LLM 主路径 / 关键词兜底）。

为什么是「召回 + 一次判档」而不是「拆成多个二元判据 + 组合规则」
-------------------------------------------------------
评分标准（``rubric.py``）本身就是按 100/50/0 三档写的自然语言判据。把它拆成几十个
二元判据再用 if/elif 拼回档位，等于把一个语义判断拆成几十个词法判断再手工合并——每多
拆一格就多一处误判面，而合并规则本身又是一份没人能维护的隐式口径。

现在的链路只有三步，每一步都能独立解释：

    1. 召回：关键词命中多的段落优先（只决定「给模型看哪几段」，不影响分数）
    2. 判档：段落 + 三档判据 → 选一档，并指出依据哪几段（LLM）
    3. 兜底：LLM 不可用/不守格式时，按证据层级（声明 < 制度 < 执行结果）给档

「档位只能取 100/50/0」是硬约束：模型给别的值一律判为无效并降级，绝不四舍五入猜一个分。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from s11_text import Doc, sentences

from .blind import PLACEHOLDER, Blinder
from .llm import LLM, LLMError, chat_json
from .rubric import (LEVEL_WORDS, KEYWORD_RECALL_LIMIT, RECALL_LIMIT, Rubric,
                     baseline_groups)

# ``RECALL_LIMIT``（24）与 ``KEYWORD_RECALL_LIMIT``（12）的取值与理由在 ``rubric.py``：
# 它们是判档口径，要进 ``rubric_hash()``。12→24 的由来见 rubric.py 的常量注释与
# 2026-11-09 的 GT 对账（9 条证据卡在 13–31 名，提到 24 救回 6 条）。
PASSAGE_CHARS = 420        # 单段展示上限：PDF 文本块本身已 ≤380，这里只防异常长块

SYSTEM = (
    "你是 ESG 报告核查员，按给定的评分标准给一个指标判档。\n"
    "只能依据<报告段落>里出现的内容判断，不得使用段落以外的知识，不得臆测报告没写的事。\n"
    "档位只能取给定集合中的一个值；证据不足时按更低档位判。\n"
    "判档时区分三层证据，层级越高越能支持高分：\n"
    "1) 原则声明（重视、关怀、零容忍、依法合规）——只说明立场，不能单独支撑 100 分；\n"
    "2) 制度与措施（政策、程序、负责部门、审批流程）——支持制度成立，不等于已经执行；\n"
    "3) 实际执行与结果（本年度已实施、覆盖率/使用率/人数、检查或监测结论、补发/整改/复核完成）"
    "——才是执行证据。\n"
    "注意：制度、系统、渠道、培训计划、活动名称的「存在」不等于「已实施」，"
    "必须有本期发生的事实或数据；仅仅「用系统跟踪」「设有热线」属措施而非结果。\n"
    f"段落里的 {PLACEHOLDER} 是被隐去的企业名称（盲评要求），判档时忽略它，"
    "不得据此推断是哪家企业。\n"
    "目录页、章节标题、指标编号、纯表头不能作为依据。\n"
    "只输出 JSON，不要输出解释性文字、不要输出 Markdown。"
)


@dataclass
class Passage:
    """召回的一段原文（带页码）。``idx`` 是给模型看的编号，从 1 开始。"""
    idx: int
    page: int
    text: str
    hits: int = 0


@dataclass
class Verdict:
    """一次判档的结论：档位 + 理由 + 依据段落 + 出处（llm/keyword/gate）。"""
    key: str
    tier: int
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    source: str = "keyword"                 # llm / keyword / gate
    missing: str = ""
    model: str = ""
    n_passages: int = 0
    alias_hits: int = 0                     # 送模型前脱敏替换了多少处（0 = 文本本就没有公司名）

    def to_dict(self) -> dict:
        return {"key": self.key, "tier": self.tier, "reason": self.reason,
                "evidence": list(self.evidence), "source": self.source,
                "missing": self.missing, "model": self.model,
                "n_passages": self.n_passages, "alias_hits": self.alias_hits}

    @classmethod
    def from_dict(cls, d: dict) -> "Verdict":
        return cls(key=str(d.get("key") or ""), tier=int(d.get("tier") or 0),
                   reason=str(d.get("reason") or ""),
                   evidence=[str(x) for x in (d.get("evidence") or [])],
                   source=str(d.get("source") or "keyword"),
                   missing=str(d.get("missing") or ""),
                   model=str(d.get("model") or ""),
                   n_passages=int(d.get("n_passages") or 0),
                   alias_hits=int(d.get("alias_hits") or 0))


# ---------------------------------------------------------------- 1 召回
@lru_cache(maxsize=None)
def _head_pattern(kw: str) -> re.Pattern[str] | None:
    """ASCII 开头的词只允许从**词首**命中；返回 None 表示按纯子串（中文走这条）。

    为什么卡词首而不卡词尾：英语里最脏的误命中都是「被包在更长的词里」——
    ``wage`` 命中 ``sewage``、``EAP`` 命中 ``cheaper``/``leapfrog``、``diversity`` 命中
    ``biodiversity``、``termination`` 命中 ``determination``、``employment`` 命中
    ``unemployment``（意思还相反）。这些只要前一个字符是字母就能挡住。
    词尾不设边界，单复数与词形变化照旧命中（``employee benefit`` 仍命中 ``employee benefits``、
    ``statutory holiday`` 仍命中 ``statutory holidays``），否则会把正常写法一起挡掉。
    """
    low = kw.lower()
    if not low or not ("a" <= low[0] <= "z"):
        return None
    return re.compile(r"(?<![a-z0-9])" + re.escape(low))


def _match(low: str, kw: str) -> bool:
    pat = _head_pattern(kw)
    return bool(pat.search(low)) if pat is not None else kw.lower() in low


def _hits(low: str, keywords: tuple[str, ...]) -> int:
    """命中多少个**不同**关键词（重复披露不增加权重，与细则「同义词不增证据组数」一致）。"""
    return sum(1 for k in keywords if _match(low, k))


def match_word(low: str, kw: str) -> bool:
    """单个关键词是否命中（``low`` 需先 ``lower()``）——``_match`` 的公开别名。

    查看器（``explorer``）要按同一条规则标出「这段命中了哪些词」。再写一份
    「英文词首匹配」的副本，迟早会和判档口径漂移；``MATCH_VERSION`` 只描述这一份实现。
    """
    return _match(low, kw)


# 「命中很多、信息为零」的两类段落。实测：港交所那句「遵守对发行人重大影响的法规……
# 涉及薪酬与解雇、招聘与晋升、工时、休息时间、平等机会、多元、反歧视」一句话能命中
# 4–5 个关键词，在「按命中数排序」里必然霸榜，把真正写事实的段落（往往只命中 1 个词）挤出去。
# GRI 内容索引行（403-2、2-19）同理：是指标编号目录，不是证据。
_NOISE = re.compile(
    r"significant impact on the issuer"
    r"|GRI\s*\d{3}"
    r"|\b\d{3}-\d\b"
    r"|GRI\s+(?:Content\s+)?Index|GRI\s*內容索引",
    re.I)
NOISE_PENALTY = 3          # 命中数越高罚得越多，但不至于清零（真没别的段落时仍可用）

# 同分时的第二排序键：带年份或比例的段落更像「本期已实施」，而不像制度表述。
# 判据里 100 分要的是执行证据，命中数一样时没理由让「只讲原则」的段落先占席位。
_FACT = re.compile(r"(?:19|20)\d{2}|[\d.]+\s*[%％]|\d[\d,]*\s*(?:人|人次|天|小時|小时|hours?)", re.I)


def recall(doc: Doc, rubric: Rubric, limit: int = RECALL_LIMIT) -> list[Passage]:
    """按关键词命中数取前 N 段；同分时「带年份/比例」的优先，再按原文顺序（保证可复现）。"""
    scored: list[tuple[int, int, int, int, str]] = []
    seen: set[str] = set()
    for s in sentences(doc):
        low = s.text.lower()
        n = _hits(low, rubric.keywords)
        if not n:
            continue
        key = re.sub(r"\W+", "", low)[:100]
        if key in seen:                      # 跨页重复的同一段（目录/摘要）只留一次
            continue
        seen.add(key)
        if _NOISE.search(s.text):            # 套话/索引行降权，别占满 12 个席位
            n = max(1, n - NOISE_PENALTY)
        fact = 1 if _FACT.search(s.text) else 0
        scored.append((-n, -fact, s.page, s.idx, s.text))
    scored.sort()
    return [Passage(idx=i, page=page, text=text, hits=-n)
            for i, (n, _fact, page, _idx, text) in enumerate(scored[:limit], start=1)]


# ---------------------------------------------------------------- 2 兜底
def evidence_levels(text: str) -> set[str]:
    """这一段里出现了哪几层证据（可能同时有「制度」和「执行结果」）。

    返回集合而不是单一层级：一句话里同时写「制定福利政策…参保率达到 100%」时，
    它既是制度证据也是结果证据；按最强层级归一化会把「制度」这一半丢掉，
    导致本来够 100 分的段落被判成 50。
    """
    low = text.lower()
    return {level for level in ("result", "system", "statement")
            if any(_match(low, w) for w in LEVEL_WORDS[level])}


def judge_keyword(rubric: Rubric, passages: list[Passage]) -> Verdict:
    """关键词兜底：证据层级 → 档位。

    口径取自细则「通用规则 1」：原则声明单独不足以证明制度或执行；制度＋执行结果才是
    100 分档的证据形态。这是**保守**判定——宁可按低档，也不凭一个词给满分。
    """
    levels: set[str] = set()
    for p in passages:
        levels |= evidence_levels(p.text)
    if not passages:
        tier, why = 0, "关键词未召回任何相关段落"
    elif "result" in levels and "system" in levels:
        tier, why = 100, "同时命中制度表述与执行/结果表述（已实施、覆盖率、检查结果等）"
    elif levels:
        tier, why = 50, "只命中制度或声明类表述，未见可核验的执行结果"
    else:
        tier, why = 0, "召回的段落里没有制度/执行/声明类证据"
    if tier not in rubric.tiers:
        tier = max(t for t in rubric.tiers if t <= tier)
    top = sorted(passages, key=lambda p: -p.hits)[:3]
    return Verdict(key=rubric.key, tier=tier, reason=f"关键词兜底：{why}",
                   evidence=[f"[p{p.page}] {p.text[:160]}" for p in top],
                   source="keyword", n_passages=len(passages))


# ---------------------------------------------------------------- 3 LLM 判档
def _header(rubric: Rubric) -> list[str]:
    lines = [f"【指标】{rubric.ind} {rubric.name}", "",
             "【评分标准】只能选其中一档："]
    for tier in sorted(rubric.tiers, reverse=True):
        lines.append(f"{tier} 分：{rubric.tiers[tier]}")
    if rubric.note:
        lines += ["", f"【判档提醒】{rubric.note}"]
    return lines + ["", "【报告段落】"]


def _footer(rubric: Rubric) -> list[str]:
    return ["", "【输出】严格按此 JSON 输出，tier 只能是 "
            + " / ".join(str(t) for t in sorted(rubric.tiers, reverse=True)) + "：",
            '{"tier": 100, "reason": "不超过80字，说明命中了哪一档的哪一条",'
            ' "evidence": [1, 5], "missing": "离更高一档还缺什么；已给最高档则留空"}']


def _render(passages: list[Passage],
            blinder: Blinder | None) -> tuple[list[str], int]:
    """段落渲染成提示词行；顺带统计脱敏次数（进产物，供审计「这段确实盲评过」）。"""
    lines: list[str] = []
    hits = 0
    for p in passages:
        if blinder:
            text, k = blinder.scrub(p.text)
            hits += k
        else:
            text = p.text
        lines.append(f"[{p.idx}] p{p.page} {text[:PASSAGE_CHARS]}")
    return lines, hits


def _prompt(rubric: Rubric, passages: list[Passage],
            blinder: Blinder | None = None) -> str:
    """拼提示词。段落先过脱敏器：模型只能看到 ``[COMPANY]``，看不到公司名。"""
    rendered, _ = _render(passages, blinder)
    return "\n".join(_header(rubric) + rendered + _footer(rubric))


def _coerce_tier(raw, rubric: Rubric) -> int:
    """把模型给的 tier 压成合法档位；给不出合法值就抛异常（→ 降级到兜底）。"""
    if isinstance(raw, bool):
        raise LLMError("tier 不是数字")
    if isinstance(raw, (int, float)):
        val = int(raw)
    else:
        m = re.search(r"\d{1,3}", str(raw))
        if not m:
            raise LLMError(f"tier 无法解析：{raw!r}")
        val = int(m.group(0))
    valid = set(rubric.tiers)
    if val in valid:
        return val
    raise LLMError(f"tier={val} 不在档位集合 {sorted(valid, reverse=True)} 内")


def judge_llm(rubric: Rubric, passages: list[Passage], llm: LLM,
              blinder: Blinder | None = None) -> Verdict:
    """让模型在三档里选一个，并回指依据段落。发送前做脱敏残留检查（失败即不发送）。"""
    if not passages:
        return judge_keyword(rubric, passages)
    rendered, alias_hits = _render(passages, blinder)
    prompt = "\n".join(_header(rubric) + rendered + _footer(rubric))
    if blinder is not None:
        left = blinder.leaks(prompt)
        if left:
            # fail closed：宁可这次退回关键词兜底，也不把带公司名的文本发出去
            raise LLMError(f"脱敏后仍残留公司名 {left[:3]}，本次不发送")
    obj = chat_json(llm, SYSTEM, prompt, max_tokens=1400)
    tier = _coerce_tier(obj.get("tier"), rubric)
    cited: list[str] = []
    for x in obj.get("evidence") or []:
        try:
            i = int(re.sub(r"\D", "", str(x)) or 0)
        except ValueError:
            continue
        if 1 <= i <= len(passages):
            p = passages[i - 1]
            cited.append(f"[p{p.page}] {p.text[:160]}")   # 证据存原文，便于回查
    if not cited:                            # 模型没给有效编号：退回命中数最高的三段
        cited = [f"[p{p.page}] {p.text[:160]}"
                 for p in sorted(passages, key=lambda p: -p.hits)[:2]]
    return Verdict(key=rubric.key, tier=tier, reason=str(obj.get("reason") or "")[:300],
                   evidence=cited, source="llm", missing=str(obj.get("missing") or "")[:200],
                   model=llm.model, n_passages=len(passages), alias_hits=alias_hits)


# ---------------------------------------------------------------- 对外入口
def judge(doc: Doc, rubric: Rubric, llm: LLM | None = None, *,
          blinder: Blinder | None = None, gate_hit: bool = False,
          mode: str = "auto") -> Verdict:
    """一次判档。

    ``mode``：``auto``（有 LLM 用 LLM，失败降级）/ ``llm`` / ``keyword``。
    ``blinder``：盲评脱敏器；给了它，送模型的段落里就不会有公司名。
    """
    if gate_hit:
        return Verdict(key=rubric.key, tier=0,
                       reason=f"触发 0 分前置条件：{rubric.gate}",
                       source="gate")
    passages = recall(doc, rubric)
    use_llm = mode != "keyword" and llm is not None and llm.ready
    if use_llm:
        try:
            return judge_llm(rubric, passages, llm, blinder)
        except LLMError as exc:
            fb = _judge_keyword_baseline(doc, rubric)
            fb.reason = f"{fb.reason}（LLM 降级：{exc}）"[:400]
            return fb
    return _judge_keyword_baseline(doc, rubric)


def _judge_keyword_baseline(doc: Doc, rubric: Rubric) -> Verdict:
    """兜底判档：用**冻结的 v1.4 词表**重新召回前 12 段再判。

    为什么不复用 ``recall(doc, rubric)`` 的结果：召回词表可以一直扩（GT 对账在加同义词），
    但兜底是回归基线，加词会改变排序与前 12 段的内容，档位就跟着漂。两条路各用一套词，
    才既能扩召回、又不动基线。见 ``rubric.BASELINE_EXCLUDED``。
    """
    from dataclasses import replace

    base = replace(rubric, groups=baseline_groups(rubric))
    return judge_keyword(base, recall(doc, base, limit=KEYWORD_RECALL_LIMIT))


__all__ = ["Passage", "Verdict", "recall", "judge", "judge_llm", "judge_keyword",
           "evidence_levels", "match_word", "RECALL_LIMIT", "KEYWORD_RECALL_LIMIT"]