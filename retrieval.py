# -*- coding: utf-8 -*-
"""S1.1 细则级混合检索：关键词 + Embedding + 组配额。

这是独立于正式判分链路的 PoC 入口。关键词负责精确术语，Embedding 负责
不同措辞；每组先保留最低配额，再按全局分数补足，避免一个高频证据组占满上下文。
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from s11_text import Doc, sentences

try:
    import numpy as np
except ImportError:  # pragma: no cover - 纯 Python 慢速 fallback
    np = None

from .embedding import Embedding, EmbeddingError
from .llm import LLM, LLMError
from .qualitative import Passage, judge_llm, match_word
from .qualitative import recall as keyword_recall
from .rubric import GroupSpec, Rubric

_NOISE = re.compile(
    r"significant impact on the issuer"
    r"|GRI\s*\d{3}"
    r"|\b\d{3}-\d\b"
    r"|GRI\s+(?:Content\s+)?Index|GRI\s*內容索引",
    re.I,
)
_FACT = re.compile(
    r"(?:19|20)\d{2}|[\d.]+\s*[%％]|\d[\d,]*\s*(?:人|人次|天|小時|小时|hours?)",
    re.I,
)


@dataclass(frozen=True)
class RetrievalConfig:
    top_k: int = 24
    group_quota: int = 2
    keyword_reserve: int = 16
    lexical_weight: float = 0.70
    semantic_weight: float = 0.30
    semantic_floor: float = 0.42
    noise_penalty: float = 0.20
    fact_bonus: float = 0.02

    def normalized_weights(self) -> tuple[float, float]:
        total = self.lexical_weight + self.semantic_weight
        if total <= 0:
            return 0.5, 0.5
        return self.lexical_weight / total, self.semantic_weight / total


@dataclass(frozen=True)
class _Chunk:
    order: int
    page: int
    text: str
    low: str
    fact: bool


def _chunks(doc: Doc) -> list[_Chunk]:
    out: list[_Chunk] = []
    seen: set[str] = set()
    for idx, sent in enumerate(sentences(doc)):
        text = sent.text.strip()
        key = re.sub(r"\W+", "", text.lower())[:100]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(_Chunk(
            order=idx,
            page=sent.page,
            text=text,
            low=text.lower(),
            fact=bool(_FACT.search(text)),
        ))
    return out


def _group_keywords(rubric: Rubric) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for group, words in rubric.recall_groups:
        out[group] = tuple(dict.fromkeys(out.get(group, ()) + words))
    return out


def _group_queries(rubric: Rubric, keywords: dict[str, tuple[str, ...]]) -> list[tuple[GroupSpec, str]]:
    """每个计分证据组一条 query；无 specs 的旧 rubric 退回关键词组。"""
    out: list[tuple[GroupSpec, str]] = []
    for spec in rubric.specs:
        words = keywords.get(spec.id, ())
        query = "；".join(part for part in (
            f"指标：{rubric.name}",
            f"证据组：{spec.id} {spec.label}",
            f"成立条件：{spec.need}",
            "相关表述：" + "、".join(words[:18]),
        ) if part)
        out.append((spec, query))
    if out:
        return out
    return [
        (GroupSpec(group, group, "、".join(words[:18])), 
         f"{rubric.name}；{group}；" + "、".join(words[:18]))
        for group, words in keywords.items()
    ]


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def retrieve_hybrid_with_stats(
    doc: Doc,
    rubric: Rubric,
    embedding: Embedding,
    *,
    config: RetrievalConfig | None = None,
) -> tuple[list[Passage], dict]:
    """返回候选段落及诊断信息；Embedding 不可用时由调用方决定降级。"""
    cfg = config or RetrievalConfig()
    chunks = _chunks(doc)
    if not chunks:
        return [], {"chunks": 0, "groups": 0, "selected_groups": {},
                    "embedding_model": embedding.model}

    keywords = _group_keywords(rubric)
    groups = _group_queries(rubric, keywords)
    texts = [chunk.text for chunk in chunks] + [query for _, query in groups]
    vectors = embedding.embed(texts)
    chunk_vectors = vectors[:len(chunks)]
    group_vectors = vectors[len(chunks):]
    lexical_w, semantic_w = cfg.normalized_weights()
    matrix = np.asarray(chunk_vectors, dtype=float) if np is not None else None

    scored: dict[int, dict] = {}
    quota_indices: list[int] = []
    for (spec, _), query_vector in zip(groups, group_vectors):
        words = keywords.get(spec.id, ())
        if matrix is not None:
            semantic_values = np.maximum(0.0, matrix @ np.asarray(
                query_vector, dtype=float)).tolist()
        else:
            semantic_values = [
                max(0.0, _dot(query_vector, vector)) for vector in chunk_vectors
            ]
        group_rows: list[tuple[float, int]] = []
        for index, chunk in enumerate(chunks):
            hits = sum(1 for word in words if match_word(chunk.low, word))
            lexical = min(1.0, hits / max(1, min(len(words), 5)))
            if _NOISE.search(chunk.text):
                lexical = max(0.0, lexical - cfg.noise_penalty)
            semantic = semantic_values[index]
            if not hits and semantic < cfg.semantic_floor:
                continue
            score = (lexical_w * lexical + semantic_w * semantic
                     + (cfg.fact_bonus if chunk.fact else 0.0))
            group_rows.append((score, index))
            previous = scored.get(index)
            row = {
                "score": score,
                "group": spec.id,
                "keyword_hits": hits,
                "lexical_score": lexical,
                "semantic_score": semantic,
            }
            if previous is None or score > previous["score"]:
                scored[index] = row
        group_rows.sort(key=lambda item: (-item[0], chunks[item[1]].page,
                                          chunks[item[1]].order))
        for _, index in group_rows[:max(0, cfg.group_quota)]:
            quota_indices.append(index)

    # 先保留当前关键词路径的高分结果，再补语义/组配额候选。这样混合检索是
    # 增量扩展，不会因为通用语义相似度把已验证的精确命中挤出去。
    key_to_index = {
        re.sub(r"\W+", "", chunk.text.lower())[:100]: index
        for index, chunk in enumerate(chunks)
    }
    selected: set[int] = set()
    page_counts: dict[int, int] = defaultdict(int)

    def add(index: int, *, per_page: int, quota_group: str = "") -> bool:
        if index in selected or len(selected) >= cfg.top_k:
            return False
        page = chunks[index].page
        if page_counts[page] >= per_page:
            return False
        selected.add(index)
        page_counts[page] += 1
        if quota_group and index in scored:
            scored[index]["quota_group"] = quota_group
        return True

    keyword_reserved = 0
    for keyword_rank, passage in enumerate(
            keyword_recall(doc, rubric, limit=cfg.top_k, llm_path=True), start=1):
        index = key_to_index.get(re.sub(r"\W+", "", passage.text.lower())[:100])
        if index is not None and index not in scored:
            hits = sum(1 for word in rubric.recall_keywords
                       if match_word(chunks[index].low, word))
            lexical = min(1.0, hits / max(1, min(len(rubric.recall_keywords), 5)))
            scored[index] = {
                "score": lexical_w * lexical
                + (cfg.fact_bonus if chunks[index].fact else 0.0),
                "group": "",
                "keyword_hits": hits,
                "lexical_score": lexical,
                "semantic_score": 0.0,
            }
        if index is not None and add(index, per_page=1):
            scored[index]["keyword_rank"] = keyword_rank
            keyword_reserved += 1
        if keyword_reserved >= cfg.keyword_reserve:
            break

    for index in quota_indices:
        add(index, per_page=1,
            quota_group=str(scored.get(index, {}).get("group") or ""))

    remaining = sorted(
        (index for index in scored),
        key=lambda index: (-scored[index]["score"], chunks[index].page,
                           chunks[index].order),
    )
    for index in remaining:
        if len(selected) >= cfg.top_k:
            break
        add(index, per_page=1)

    # 一页最多留 2 段：第一轮优先覆盖不同页面，剩余席位再补同页的补充证据。
    for index in remaining:
        if len(selected) >= cfg.top_k:
            break
        add(index, per_page=2)
    for index in remaining:
        if len(selected) >= cfg.top_k:
            break
        add(index, per_page=10_000)

    ordered = sorted(
        selected,
        key=lambda index: (scored[index].get("keyword_rank", 10_000),
                           -scored[index]["score"], chunks[index].page,
                           chunks[index].order),
    )[:cfg.top_k]
    passages: list[Passage] = []
    for idx, index in enumerate(ordered, start=1):
        chunk = chunks[index]
        row = scored[index]
        passages.append(Passage(
            idx=idx,
            page=chunk.page,
            text=chunk.text,
            hits=int(row["keyword_hits"]),
            group=str(row["group"]),
            lexical_score=float(row["lexical_score"]),
            semantic_score=float(row["semantic_score"]),
            score=float(row["score"]),
        ))
    selected_groups: dict[str, int] = {}
    for passage in passages:
        group = passage.group or "keyword"
        selected_groups[group] = selected_groups.get(group, 0) + 1
    return passages, {
        "chunks": len(chunks),
        "groups": len(groups),
        "selected_groups": selected_groups,
        "embedding_model": embedding.model,
        "embedding_fingerprint": embedding.fingerprint(),
        "top_k": cfg.top_k,
        "group_quota": cfg.group_quota,
        "keyword_reserve": cfg.keyword_reserve,
        "keyword_reserved": keyword_reserved,
        "unique_pages": len({passage.page for passage in passages}),
    }


def retrieve_hybrid(
    doc: Doc,
    rubric: Rubric,
    embedding: Embedding,
    *,
    config: RetrievalConfig | None = None,
) -> list[Passage]:
    passages, _ = retrieve_hybrid_with_stats(doc, rubric, embedding, config=config)
    return passages


def judge_hybrid(
    doc: Doc,
    rubric: Rubric,
    llm: LLM | None = None,
    embedding: Embedding | None = None,
    *,
    blinder=None,
    gate_hit: bool = False,
    mode: str = "auto",
    config: RetrievalConfig | None = None,
):
    """混合检索后调用现有 LLM 判档；任何检索/模型失败均退回关键词基线。"""
    if gate_hit or mode == "keyword" or llm is None or not llm.ready:
        return _keyword_fallback(doc, rubric, gate_hit=gate_hit)
    if embedding is None or not embedding.ready:
        verdict = _keyword_fallback(doc, rubric)
        verdict.reason = f"{verdict.reason}（混合检索降级：Embedding 未配置）"[:400]
        return verdict
    try:
        passages = retrieve_hybrid(doc, rubric, embedding, config=config)
    except EmbeddingError as exc:
        verdict = _keyword_fallback(doc, rubric)
        verdict.reason = f"{verdict.reason}（混合检索降级：{exc}）"[:400]
        return verdict
    try:
        return judge_llm(rubric, passages, llm, blinder)
    except LLMError as exc:
        verdict = _keyword_fallback(doc, rubric)
        verdict.reason = f"{verdict.reason}（LLM 降级：{exc}）"[:400]
        return verdict


def _keyword_fallback(doc: Doc, rubric: Rubric, *, gate_hit: bool = False):
    from . import qualitative
    return qualitative.judge(doc, rubric, None, gate_hit=gate_hit, mode="keyword")


__all__ = [
    "RetrievalConfig", "retrieve_hybrid", "retrieve_hybrid_with_stats", "judge_hybrid",
]
