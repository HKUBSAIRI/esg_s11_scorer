# -*- coding: utf-8 -*-
"""定性判档：关键词召回段落 → 一次判档（LLM 主路径 / 关键词兜底）。

为什么是「召回 + 一次判档」而不是「拆成多个二元判据 + 组合规则」
-------------------------------------------------------
评分标准（``rubric.py``）本身就是按 100/50/0 三档写的自然语言判据。把它拆成几十个
二元判据再用 if/elif 拼回档位，等于把一个语义判断拆成几十个词法判断再手工合并——每多
拆一格就多一处误判面，而合并规则本身又是一份没人能维护的隐式口径。

现在的链路只有三步，每一步都能独立解释（2026-11 起判档内部多了「逐组判定」，
但它仍是一次调用、一次语义判断，不是把判据拆成词法规则）：

    1. 召回：跳过非计分组后，按关键词命中数取段落（只决定「给模型看哪几段」，
       不影响分数）；关键词兜底仍用完整词表，基线冻结
    2. 判档：段落 + 证据组表 + 100 分路径 + 三档判据 → 逐组 yes/partial/no、
       选一档、回指段落（LLM）；再用 ``rubric.path100`` 复核档位，缺组只降不升，
       降档或「分组够分但判低」都标 ``status=review``（待复核，不改分数）
    3. 兜底：LLM 不可用/不守格式时，按证据层级（声明 < 制度 < 执行结果）给档

「档位只能取 100/50/0」是硬约束：模型给别的值一律判为无效并降级，绝不四舍五入猜一个分。

2026-11 的 ``groups`` 不违背上面这条：它不是「几十个词表判断 + if/elif 拼档」，而是让模型
在同一次语义判断里逐组交代依据；``path100`` 是细则路径（如 P1∧P2∧(P3…P6)）的逐字转写，
合并规则只有一份、可被测试核对。这样做的原因是细则本身已经把 100 分写成路径，
提示词若只给三段自由文本，模型就只能自己隐式完成合取——判完无法复核。
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


def _as_list(raw) -> list:
    """模型输出偶尔把数组写成单个整数/字符串；统一按一项处理，别让整批判档炸掉。"""
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if raw is None or raw == "":
        return []
    return [raw]


SYSTEM = (
    "你是 ESG 报告核查员，按给定的评分标准给一个指标判档。\n"
    "只能依据<报告段落>里出现的内容判断，不得使用段落以外的知识，不得臆测报告没写的事。\n"
    "档位只能取给定集合中的一个值；证据不足时按更低档位判。\n"
    "判档时区分三层证据，层级越高越能支持高分：\n"
    "1) 原则声明（重视、关怀、零容忍、依法合规）——只说明立场，不能单独支撑 100 分；\n"
    "2) 制度与措施（政策、程序、负责部门、审批流程）——支持制度成立，不等于已经执行；\n"
    "3) 实际执行与结果（本年度已实施、覆盖率/使用率/人数、检查或监测结论、补发/整改/复核完成）"
    "——才是执行证据。\n"
    "逐组判断时，只有该组要求全部满足才记 yes，只满足一部分记 partial，未识别记 no；"
    "标注「不满足」的说法（如计划整改、仅设渠道、只倡导多元）一律记 no。\n"
    "档位 0 只用于两种情况：basis=none（报告确实未提及本项内容），"
    "或 basis=violation（存在已核实且未有效纠正的违规）。\n"
    "只要报告出现本项相关的原则声明、制度或措施——即使证据组全部未成立——"
    "档位记 50，不得因为「没有执行证据」直接记 0。\n"
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
    group: str = ""
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    score: float = 0.0


@dataclass
class Verdict:
    """一次判档的结论：档位 + 理由 + 依据段落 + 出处（llm/keyword/gate）。

    ``status`` 为 ``review`` 表示「档位可用但需人工复核」：模型的档位与证据组路径
    对不上、或它自己说证据冲突。它是**标记**，不改分数；0/50/100 仍是唯一档位集合。
    """
    key: str
    tier: int
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    source: str = "keyword"                 # llm / keyword / gate
    missing: str = ""
    model: str = ""
    n_passages: int = 0
    alias_hits: int = 0                     # 送模型前脱敏替换了多少处（0 = 文本本就没有公司名）
    status: str = "ok"                      # ok / review（待复核）
    groups: dict[str, str] = field(default_factory=dict)   # 组 id → yes/partial/no
    missing_groups: list[str] = field(default_factory=list)
    event: bool | None = None               # 是否披露已识别事件（仅 S1.1.5 使用）
    basis: str = ""                         # 模型自报的最高证据层：none/statement/system/result/violation

    def to_dict(self) -> dict:
        return {"key": self.key, "tier": self.tier, "reason": self.reason,
                "evidence": list(self.evidence), "source": self.source,
                "missing": self.missing, "model": self.model,
                "n_passages": self.n_passages, "alias_hits": self.alias_hits,
                "status": self.status, "groups": dict(self.groups),
                "missing_groups": list(self.missing_groups), "event": self.event,
                "basis": self.basis}

    @classmethod
    def from_dict(cls, d: dict) -> "Verdict":
        return cls(key=str(d.get("key") or ""), tier=int(d.get("tier") or 0),
                   reason=str(d.get("reason") or ""),
                   evidence=[str(x) for x in _as_list(d.get("evidence"))],
                   source=str(d.get("source") or "keyword"),
                   missing=str(d.get("missing") or ""),
                   model=str(d.get("model") or ""),
                   n_passages=int(d.get("n_passages") or 0),
                   alias_hits=int(d.get("alias_hits") or 0),
                   status=str(d.get("status") or "ok"),
                   groups={str(k): str(v) for k, v in (d.get("groups") or {}).items()},
                   missing_groups=[str(x) for x in _as_list(d.get("missing_groups"))],
                   event=(d.get("event") if isinstance(d.get("event"), bool) else None),
                   basis=str(d.get("basis") or ""))


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


def recall(doc: Doc, rubric: Rubric, limit: int = RECALL_LIMIT, *,
           llm_path: bool = True) -> list[Passage]:
    """按关键词命中数取前 N 段。

    ``llm_path=True``（主路径）只看 ``rubric.recall_groups``：实测 S1.1.3 有 53% 的
    席位被 ``T7 人权``（2 个词、全批 215 段）占掉，S1.1.5 有 14% 被 ``D0 人权政策``
    占掉——这两组不在 100 分路径里，命中再多也不能支撑判决。
    ``llm_path=False`` 用完整词表，供 keyword 兜底冻结基线使用。

    同分时「带年份/比例」的优先，再按原文顺序（保证可复现）。
    """
    words = rubric.recall_keywords if llm_path else rubric.keywords
    scored: list[tuple[int, int, int, int, str]] = []
    seen: set[str] = set()
    for s in sentences(doc):
        low = s.text.lower()
        n = _hits(low, words)
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
    lines = [f"【指标】{rubric.ind} {rubric.name}", ""]
    if rubric.specs:
        lines.append("【证据组】逐组判断；写着「不满足/不算/不能替代/不足」的情形出现时记 no：")
        for spec in rubric.specs:
            lines.append(f"{spec.id} {spec.label}：{spec.need}")
        lines.append("")
    if rubric.path100:
        rule = rubric.path100
        parts: list[str] = []
        if rule.require_all:
            parts.append("全部成立：" + "、".join(rule.require_all))
        if rule.require_any:
            parts.append("至少一项成立：" + "、".join(rule.require_any))
        lines.append("【100 分路径】" + "；".join(parts) + "。")
        if rule.note:
            lines.append(rule.note)
        if rubric.event_groups:
            lines.append("如报告披露了已识别事件，" + "、".join(rubric.event_groups)
                         + " 也须全部成立；没有事件时这些组记 no。")
        lines.append("")
    lines += ["【评分标准】只能选其中一档："]
    for tier in sorted(rubric.tiers, reverse=True):
        lines.append(f"{tier} 分：{rubric.tiers[tier]}")
    if rubric.note:
        lines += ["", f"【判档提醒】{rubric.note}"]
    return lines + ["", "【报告段落】"]


def _footer(rubric: Rubric) -> list[str]:
    tiers = " / ".join(str(t) for t in sorted(rubric.tiers, reverse=True))
    lines = ["", f"【输出】严格按此 JSON 输出，tier 只能是 {tiers}："]
    if rubric.specs:
        first = rubric.specs[0].id
        lines += [
            '{"groups": [{"id": "%s", "hit": "yes", "evidence": [1]},' % first,
            '            {"id": "其余每个组各一行", "hit": "yes/partial/no", "evidence": [2]}],',
            ' "tier": 100, "basis": "none/statement/system/result/violation",',
            ' "reason": "不超过80字，说明哪几组成立、落在哪一档",',
            ' "evidence": [1, 5], "missing": "离更高一档还缺哪些组；已给最高档则留空",',
        ]
        if rubric.event_groups:
            lines += [' "event": false, "review": false}',
                      'event=true 表示报告披露了已识别事件；hit 只能是 yes / partial / no；'
                      'review=true 表示证据冲突或评分表未覆盖、需人工复核。']
        else:
            lines += [' "review": false}',
                      'hit 只能是 yes / partial / no；'
                      'review=true 表示证据冲突或评分表未覆盖、需人工复核。']
    else:
        lines += ['{"tier": 100, "reason": "不超过80字",'
                  ' "evidence": [1, 5], "missing": "离更高一档还缺什么；已给最高档则留空"}']
    return lines


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


_HIT_NO = {"no", "n", "false", "否", "无", "不成立", "未识别", "不满足"}
_HIT_PARTIAL = {"partial", "partly", "部分", "部分满足", "部分成立"}
_HIT_YES = {"yes", "y", "true", "是", "有", "满足", "成立"}


def _norm_hit(raw) -> str:
    """把模型给的组状态压成 yes/partial/no；认不出的一律 no（宁低不高）。"""
    s = str(raw or "").strip().lower()
    if s in _HIT_NO:
        return "no"
    if s in _HIT_PARTIAL:
        return "partial"
    if s in _HIT_YES:
        return "yes"
    return "no"


def _parse_groups(obj: dict, rubric: Rubric) -> tuple[dict[str, str], list[int]]:
    """读 ``groups``：支持 ``[{id,hit,evidence}]`` 与 ``{id: hit}`` 两种写法。

    返回（组状态, 组内引用的段落编号）。不认识的组 id 直接丢掉——多出来的组只说明
    模型没照表填，不能拿它去改档。
    """
    valid = {s.id for s in rubric.specs}
    hits: dict[str, str] = {}
    ids: list[int] = []
    raw = obj.get("groups")
    if isinstance(raw, dict):
        items = [(k, v) for k, v in raw.items()]
    elif isinstance(raw, list):
        items = [(x.get("id"), x) for x in raw if isinstance(x, dict)]
    else:
        return {}, []
    for key, val in items:
        gid = str(key or "").strip().upper()
        if gid not in valid:
            continue
        if isinstance(val, dict):
            hits[gid] = _norm_hit(val.get("hit"))
            for x in _as_list(val.get("evidence")):
                i = _as_index(x)
                if i:
                    ids.append(i)
        else:
            hits[gid] = _norm_hit(val)
    return hits, ids


def _as_index(x) -> int:
    try:
        i = int(re.sub(r"\D", "", str(x)) or 0)
    except ValueError:
        return 0
    return i if i > 0 else 0


def _parse_event(obj: dict, rubric: Rubric) -> bool | None:
    """是否披露了已识别事件；``None`` = 模型没说。"""
    if not rubric.event_groups:
        return None
    raw = obj.get("event")
    if isinstance(raw, bool):
        return raw
    s = str(raw or "").strip().lower()
    if s in {"true", "yes", "y", "是", "有", "1"}:
        return True
    if s in {"false", "no", "n", "否", "无", "0"}:
        return False
    return None


_BASIS = {"none", "statement", "system", "result", "violation"}


def _parse_basis(obj: dict) -> str:
    """模型自报的最高证据层；认不出就返回空串（不据此改档）。"""
    s = str(obj.get("basis") or "").strip().lower()
    return s if s in _BASIS else ""


def check_path(rubric: Rubric, groups: dict[str, str],
               event: bool | None = None) -> tuple[bool, list[str]]:
    """100 分路径是否成立，以及缺哪些组。

    ``require_all`` 要求 yes；``partial`` 不能顶硬条件。``require_any`` 里 yes 或
    partial 都算「有一条路径」。事件条件只在 ``event=True`` 时追加。
    """
    rule = rubric.path100
    if rule is None:
        return True, []
    missing = [g for g in rule.require_all if groups.get(g) != "yes"]
    if rule.require_any and not any(groups.get(g) in ("yes", "partial")
                                    for g in rule.require_any):
        missing.append("/".join(rule.require_any))
    if event and rubric.event_groups:
        missing += [g for g in rubric.event_groups if groups.get(g) != "yes"]
    return (not missing), missing


def _tier_from_groups(rubric: Rubric, groups: dict[str, str],
                      event: bool | None) -> int:
    """模型的 tier 缺失/非法时，按分组反推：路径成立 100，有任一组成立 50，否则 0。"""
    ok, _ = check_path(rubric, groups, event)
    if ok:
        return 100
    return 50 if any(v in ("yes", "partial") for v in groups.values()) else 0


def judge_llm(rubric: Rubric, passages: list[Passage], llm: LLM,
              blinder: Blinder | None = None) -> Verdict:
    """让模型逐组判定、选档、回指依据段落；再用 100 分路径复核档位。

    复核只做两件事，方向都是**保守**：判 100 但路径缺组 → 降为 50 并标待复核；
    分组已够 100 却判了低档 → 保留原档并标待复核。它不会把低档抬成高档。
    """
    if not passages:
        return judge_keyword(rubric, passages)
    rendered, alias_hits = _render(passages, blinder)
    prompt = "\n".join(_header(rubric) + rendered + _footer(rubric))
    if blinder is not None:
        left = blinder.leaks(prompt)
        if left:
            # fail closed：宁可这次退回关键词兜底，也不把带公司名的文本发出去
            raise LLMError(f"脱敏后仍残留公司名 {left[:3]}，本次不发送")
    obj = chat_json(llm, SYSTEM, prompt, max_tokens=1600)
    groups, group_ids = _parse_groups(obj, rubric)
    event = _parse_event(obj, rubric)
    basis = _parse_basis(obj)
    derived = False
    try:
        tier = _coerce_tier(obj.get("tier"), rubric)
    except LLMError:
        if not groups:
            raise
        tier = _tier_from_groups(rubric, groups, event)
        derived = True

    status = "ok"
    notes: list[str] = []
    missing_groups: list[str] = []
    if derived:
        notes.append(f"模型未给合法档位，按证据组推为 {tier}")
        status = "review"
    if tier == 0 and basis in ("statement", "system", "result"):
        # 细则的 0 分档只覆盖「未识别」与「已核实违规」；有声明/制度却判 0 是模型把
        # 「证据组全不成立」误当成「没提到」。按 50 分档记，并留痕供复核。
        tier = 50
        notes.append(f"仅有 {basis} 层内容，按 50 分档（0 分仅限未识别或已核实违规）")
        status = "review"
    if tier < rubric.floor_tier:
        notes.append(f"本项 0 分档不适用（前置条件已通过），按 {rubric.floor_tier} 记")
        tier = rubric.floor_tier
        status = "review"
    if rubric.path100 and groups:
        ok, lack = check_path(rubric, groups, event)
        if tier == 100 and not ok:
            lower = max(t for t in rubric.tiers if t < 100)
            notes.append(f"判 100 但路径缺 {'、'.join(lack)}，按 {lower} 记")
            tier, missing_groups, status = lower, lack, "review"
        elif tier != 100 and ok:
            notes.append(f"证据组已满足 100 分路径但判了 {tier}，保留原档待复核")
            status = "review"
    elif rubric.path100:
        notes.append("模型未返回证据组判定，档位未经路径校验")
        status = "review"
    if event is None and rubric.event_groups and tier == 100:
        notes.append("未说明是否存在已识别事件")
        status = "review"
    if _truthy(obj.get("review")):
        notes.append("模型标记待复核")
        status = "review"

    cited: list[str] = []
    seen: set[int] = set()
    for x in _as_list(obj.get("evidence")) + group_ids:
        i = _as_index(x)
        if not i or i in seen:
            continue
        seen.add(i)
        if 1 <= i <= len(passages):
            p = passages[i - 1]
            cited.append(f"[p{p.page}] {p.text[:160]}")   # 证据存原文，便于回查
    if not cited:                            # 模型没给有效编号：退回命中数最高的三段
        cited = [f"[p{p.page}] {p.text[:160]}"
                 for p in sorted(passages, key=lambda p: -p.hits)[:2]]
    reason = str(obj.get("reason") or "").strip()
    if notes:
        reason = "档位校验：" + "；".join(notes) + ("｜" + reason if reason else "")
    missing = str(obj.get("missing") or "").strip()
    if missing_groups:
        lack = "、".join(missing_groups)
        missing = f"{missing}；缺 {lack}" if missing else lack
    return Verdict(key=rubric.key, tier=tier, reason=reason[:400],
                   evidence=cited, source="llm", missing=missing[:200],
                   model=llm.model, n_passages=len(passages), alias_hits=alias_hits,
                   status=status, groups=groups, missing_groups=missing_groups,
                   event=event, basis=basis)


def _truthy(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"true", "yes", "y", "是", "1"}


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
    return judge_keyword(base, recall(doc, base, limit=KEYWORD_RECALL_LIMIT,
                                     llm_path=False))


__all__ = ["Passage", "Verdict", "recall", "judge", "judge_llm", "judge_keyword",
           "evidence_levels", "match_word", "check_path",
           "RECALL_LIMIT", "KEYWORD_RECALL_LIMIT"]
