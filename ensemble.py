# -*- coding: utf-8 -*-
"""三路独立判档 · 只采集、不出分：把「判档分歧」变成人工复核的信号。

为什么是旁路，不是新的 ``--qualitative`` 模式
-------------------------------------------
判档结论直接就是分（100/50/0），所以任何改动聚合方式的做法都在改口径。本模块刻意
**不参与打分**：

* 只写 ``build/FY{year}/{code}/qualitative-votes.json``，**不写** ``qualitative-llm`` /
  ``score`` / gold 表 / ``ESG_OUT_DIR`` 下任何交付物；
* 分数仍由 ``--qualitative llm`` 出（主模型单次判档），一个字都不变；
* 产物里同时记下三个投票人的档位、引用页码与理由，供复核回查。

要回答的问题只有一个：**这批数据里，判档到底有多稳？** 而不是「投票能不能把分改准」。
``3/3 一致`` 是**稳定性**证据，不是准确性证据——三家一致地判错（例如 S1.1.4 一律读成
100 分）时本模块会安静地打上「一致」，所以它必须和 ``calibrate.py`` 的 GT 校准一起读。

为什么需要 3 个投票人而不是 1 个
--------------------------------
单次判档的抖动（同一份报告重跑换档）无法自查：缓存命中让重跑看起来"稳定"，实际是
没有重算。三路独立判档给出的是**同一批证据下的分布**：全同 = 稳；2/1 与 1-1-1 = 该
项落在判据边界上，送人看。分歧本身有信息量，没必要用众数把它抹平。

分歧类型（决定复核动作，不额外花钱：页码取自 ``Verdict.evidence``）
-----------------------------------------------------------------
    判据歧义（rubric）  三票引用**同一段**、档位不同 → 改 rubric 的 note/判据措辞
    证据分歧（evidence）三票引用**不同段**               → 看原文，多半是召回漏了决定句
    部分重叠（mixed）   有的票重合、有的不重合           → 先按证据分歧处理
    无引用（empty）     都没给出有效引用                 → 最高优先级：召回缺陷
    前置闸门（gate）    未披露离职率 → 三票都不调模型，恒为 0 档（不参与分歧统计）
    调用失败（incomplete）至少一票没拿到结论            → 看错误，别当成"分歧"

口径版本
--------
``ENSEMBLE_VERSION`` 与投票人列表（模型名 + temperature + 指纹）一起进产物指纹：
换一家模型、改一个温度，旧产物就不能复用。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from . import blind as blind_mod
from . import qualitative as qual_mod
from . import stages
from .llm import LLM, LLMError, from_settings
from .models import Extraction
from .config import SPECS
from .rubric import QUALITATIVE_PARTS, RUBRICS, rubric_hash
from .settings import get_settings
from .store import ArtifactStore, combine, short, source_digest

ENSEMBLE_VERSION = "ensemble-1.0"
ARTIFACT = "qualitative-votes"           # 与 qualitative-llm / qualitative-keyword 并列存放
DEFAULT_TEMPS = (0.0, 0.7, 0.9)          # 未配置投票人时：同一模型三个温度
PAGES = re.compile(r"\[p(\d+)\]")


def artifact_name(voters: list[Voter]) -> str:
    """产物名带投票人指纹：两套投票人各存一份，便于"同温度 / 跨模型"两组直接对比。

    不能共用一个文件名：``qualitative-votes.json`` 只能留住最后一次跑的结论，
    换一组投票人就把上一组顶掉——这正是 ``qualitative-llm`` / ``qualitative-keyword``
    当初分家的同一个理由。
    """
    return f"{ARTIFACT}-{short(voters_fingerprint(voters), 6)}"


# ---------------------------------------------------------------- 投票人
@dataclass(frozen=True)
class Voter:
    """一路判档人：展示名 + 一份 LLM 配置（温度已定）。"""

    name: str
    llm: LLM

    def to_dict(self) -> dict:
        return {"name": self.name, "model": self.llm.model,
                "temperature": self.llm.temperature,
                "fingerprint": self.llm.fingerprint()}

    @property
    def fingerprint(self) -> str:
        return combine(self.name, self.llm.fingerprint(), self.llm.temperature)


def parse_voters(raw: str | None, base: LLM) -> list[Voter]:
    """``"qwen-plus@0.7,qwen-max@0.7"`` → 投票人列表；空串 = 主模型三个温度。

    规格里只写模型名（``qwen-max``）时沿用 ``ESG_LLM_BASE_URL`` / ``ESG_LLM_API_KEY``，
    温度默认 0.0。换厂商要在这条链路上接第二套 base_url/key，那是下一步的事——本模块
    先回答"同源模型族内的稳定性下界"。同名重复时自动加 ``#2``，保证产物里的名字唯一。
    """
    specs = [s.strip() for s in (raw or "").replace("\n", ",").split(",") if s.strip()]
    if not specs:
        specs = [f"{base.model}@{t}" for t in DEFAULT_TEMPS]
    voters: list[Voter] = []
    seen: Counter[str] = Counter()
    for spec in specs:
        model, temp = spec, 0.0
        if "@" in spec:
            model, _, raw_t = spec.partition("@")
            try:
                temp = float(raw_t)
            except ValueError:
                raise ValueError(f"投票人规格 {spec!r} 的温度不是数字") from None
        model = model.strip()
        if not model:
            raise ValueError(f"投票人规格 {spec!r} 没有模型名")
        seen[spec] += 1
        name = spec if seen[spec] == 1 else f"{spec}#{seen[spec]}"
        voters.append(Voter(name=name, llm=replace(base, model=model, temperature=temp)))
    return voters


def voters_fingerprint(voters: list[Voter]) -> str:
    return combine([v.to_dict() for v in voters])


# ---------------------------------------------------------------- 一票一档
def ballot(doc, rubric, voter: Voter, passages: list, blinder,
           gate_hit: bool) -> dict:
    """一路判档。失败**不降级**到关键词兜底：本模块要的是"这一票有没有拿到结论"。"""
    if gate_hit:
        return {"voter": voter.name, "model": voter.llm.model,
                "temperature": voter.llm.temperature, "tier": 0, "source": "gate",
                "reason": f"触发 0 分前置条件：{rubric.gate}", "missing": "",
                "pages": [], "alias_hits": 0, "n_passages": len(passages), "error": ""}
    t0 = time.perf_counter()
    try:
        v = qual_mod.judge_llm(rubric, passages, voter.llm, blinder)
    except LLMError as exc:
        return {"voter": voter.name, "model": voter.llm.model,
                "temperature": voter.llm.temperature, "tier": None, "source": "error",
                "reason": "", "missing": "", "pages": [], "alias_hits": 0,
                "n_passages": len(passages), "error": str(exc)[:300],
                "seconds": round(time.perf_counter() - t0, 1)}
    return {"voter": voter.name, "model": voter.llm.model,
            "temperature": voter.llm.temperature, "tier": v.tier, "source": v.source,
            "reason": v.reason, "missing": v.missing,
            "pages": sorted({int(m) for m in PAGES.findall(" ".join(v.evidence))}),
            # 引用文本进产物：判"同证据不同档"必须按句子比，按页码比会把同一页里
            # 两段不同的话当成同一段证据（一页里往往有 2–3 段被召回）。
            "evidence": [_norm(e) for e in v.evidence],
            "alias_hits": v.alias_hits, "n_passages": v.n_passages, "error": "",
            "seconds": round(time.perf_counter() - t0, 1)}


def agreement(tiers: list[int | None]) -> tuple[str, int | None]:
    """``("3/3", 50)`` / ``("2/1", 50)`` / ``("no-majority", None)``，任一票失败记 incomplete。

    注意这里**只做描述，不做聚合**：众数只用来标"多数派是谁"，不写回分数。
    """
    ok = [t for t in tiers if t is not None]
    if not ok or len(ok) < len(tiers):
        return "incomplete", None
    counts = Counter(ok)
    top, n = counts.most_common(1)[0]
    if n == len(ok):
        return f"{n}/{len(ok)}", top
    if n * 2 > len(ok):
        return f"{n}/{len(ok) - n}", top
    return "no-majority", None


def _norm(evidence: str) -> str:
    """证据行归一化：去掉 ``[p12]`` 前缀与空白，只留句子本身（用于跨票比对）。"""
    body = PAGES.sub(" ", str(evidence))
    return re.sub(r"\s+", " ", body).strip().lower()[:120]


def split_type(votes: list[dict]) -> str:
    """分歧的成因：同一句证据不同档 → 判据歧义；引用了不同句子 → 召回/证据分歧。

    比的是**引用原文**（``evidence``），不是页码：同一页常有 2–3 段被召回，按页比会把
    "各看各的段落"误判成"看的是同一段"。老产物没有 ``evidence`` 时退回按页比。
    """
    tiers = [v.get("tier") for v in votes]
    if any(t is None for t in tiers):
        return "incomplete"
    if all(v.get("source") == "gate" for v in votes):
        return "gate"
    if len(set(tiers)) == 1:
        return "none"

    sets = [_cited(v) for v in votes]
    if not any(sets):
        return "empty"
    counts = Counter(tiers)
    top = counts.most_common(1)[0][0]
    maj = [s for s, v in zip(sets, votes) if v.get("tier") == top and s]
    dis = [s for s, v in zip(sets, votes) if v.get("tier") != top and s]
    if not maj or not dis:
        return "empty" if not any(sets) else "mixed"
    common = set.intersection(*maj) if len(maj) > 1 else set().union(*maj)
    union = set().union(*maj)
    share = sum(len(d & common) for d in dis) / max(1, sum(len(d) for d in dis))
    if share >= 0.6:
        return "rubric"
    if not any(d & union for d in dis):
        return "evidence"
    return "mixed"


def _cited(vote: dict) -> set[str]:
    """一票引用的句子集合；老产物（只有 ``pages``）退回 ``p{页码}`` 这种粗粒度表示。"""
    ev = vote.get("evidence")
    if ev:
        return {e for e in ev if e}
    return {f"p{p}" for p in (vote.get("pages") or ())}


# ---------------------------------------------------------------- 采集
def _unit(store: ArtifactStore, code: str, *, voters: list[Voter], aliases,
          has_turnover: bool, force: bool) -> tuple[dict, bool]:
    """一家公司：五项指标 × N 路判档。返回（产物 payload, 是否复用）。"""
    parse_payload = store.payload(code, "parse") or {}
    filename = Path(str(parse_payload.get("path") or "")).name
    blinder = blind_mod.build(code, filename=filename, extra=aliases)
    art = artifact_name(voters)          # 不能叫 name：下面 for name, rub 会把它覆盖掉
    # 两层指纹：``votes_key`` 只描述「谁、看着哪份报告、按什么判据」投的票；
    # 外层 key 再叠上「算出这些票的代码」。两者分开是为了：改一致性/分歧类型的算法时
    # 只重算派生字段（免费），不必把 288 次模型调用再买一遍。
    votes_key = combine(store.key(code, "parse") or "-",
                        "gate:0" if has_turnover else "gate:1",
                        rubric_hash(), voters_fingerprint(voters), blinder.digest(),
                        ENSEMBLE_VERSION)
    key = combine(votes_key, source_digest(qual_mod, blind_mod))
    if not force:
        if store.fresh(code, art, key):
            return store.payload(code, art) or {}, True
        old = store.payload(code, art) or {}
        if old.get("votes_key") == votes_key and old.get("items"):
            payload = _derive(old, voters=voters, votes_key=votes_key,
                              filename=filename, gate=not has_turnover)
            store.write(code, art, key, payload, producer=f"ensemble {ENSEMBLE_VERSION}")
            return payload, True

    doc = stages.doc_from_payload(parse_payload)
    items: dict[str, dict] = {}
    for name, rub in RUBRICS.items():
        passages = qual_mod.recall(doc, rub)
        gate_hit = bool(rub.gate) and not has_turnover
        votes = [ballot(doc, rub, v, passages, blinder, gate_hit) for v in voters]
        items[name] = {"key": name, "votes": votes, "n_passages": len(passages)}
    payload = _derive({"items": items}, voters=voters, votes_key=votes_key,
                      filename=filename, gate=not has_turnover)
    store.write(code, art, key, payload, producer=f"ensemble {ENSEMBLE_VERSION}")
    return payload, False


def _derive(payload: dict, *, voters: list[Voter], votes_key: str, filename: str,
            gate: bool) -> dict:
    """票 → 派生字段（一致性、多数派、分歧类型）。派生随时可重算，模型结论不重算。"""
    items: dict[str, dict] = {}
    for name, item in (payload.get("items") or {}).items():
        votes = item.get("votes") or []
        label, majority = agreement([v.get("tier") for v in votes])
        items[name] = {**item, "key": name, "agreement": label, "majority": majority,
                       "split_type": split_type(votes), "votes": votes}
    return {"voters": [v.to_dict() for v in voters], "votes_key": votes_key,
            "rubric_hash": rubric_hash(), "gate": gate, "filename": filename,
            "items": items}


def collect(*, year: int, build_dir: str | Path, voters: list[Voter],
            codes: list[str] | None = None, limit: int | None = None,
            force: bool = False, workers: int = 4, progress=None) -> dict:
    """跑遍已有 parse 产物的公司（**不解析 PDF、不算分、不写交付物**）。"""
    settings = get_settings()
    store = ArtifactStore(Path(build_dir) / f"FY{year}", year)
    aliases = blind_mod.load_alias_csv(settings.alias_csv)
    todo = [c for c in store.units_with("parse") if not codes or c in codes]
    if limit:
        todo = todo[:limit]
    if not todo:
        raise SystemExit(f"{store.root} 下没有 parse 产物：先跑一遍主流程（--dry-run 即可）")

    def one(code: str) -> tuple[str, dict, bool, str]:
        t0 = time.perf_counter()
        ex = Extraction.from_dict(store.payload(code, "facts") or {})
        ry = ex.report_year or year
        has_t = ex.value("turnover", ry) is not None
        payload, reused = _unit(store, code, voters=voters, aliases=aliases,
                                has_turnover=has_t, force=force)
        return code, payload, reused, f"{time.perf_counter() - t0:.1f}s"

    done: list[tuple[str, dict, bool]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for code, payload, reused, dt in pool.map(one, todo):
            done.append((code, payload, reused))
            if progress:
                progress(f"[判档] {code} {'复用' if reused else dt}")
    done.sort(key=lambda x: x[0])

    ballots = sum(1 for _, p, _ in done for it in (p.get("items") or {}).values()
                  for v in it.get("votes") or [] if v.get("source") in ("llm", "error"))
    return {"store": store, "rows": _rows(done), "voters": [v.to_dict() for v in voters],
            "companies": [c for c, _, _ in done],
            "reused": sum(1 for _, _, r in done if r),
            "ballots": ballots, "year": year}


# ---------------------------------------------------------------- 影响折算
# 复核优先级不能只看"分裂率"：一个 50 分的档位翻转，落在 S1.1.5 上动总分 7.5 分，
# 落在 S1.1.1-C（加权项里 30% 的分项）上只动 3.75 分。人力有限时，先看"改得起分"的。
def impact_factors() -> dict[str, float]:
    """每个定性判档 → 「档位每差 1 分，总分差多少」（0–1 之间的系数）。

    权重唯一信源是 ``config.SPECS``：tiered 指标整项取档位，weighted 指标再乘分项权重
    （S1.1.1/S1.1.2 的 C 项都是 0.3）。系数 × 50 = 一次 50↔100 翻转的总分变化。
    """
    specs = {s.ind: s for s in SPECS}
    out: dict[str, float] = {}
    for ind, keys in QUALITATIVE_PARTS.items():
        spec = specs[ind]
        for key in keys:
            part = RUBRICS[key].part
            share = (spec.weights.get(part, 0.0)
                     if spec.kind == "weighted" else 1.0)
            out[key] = spec.weight / 100 * share
    return out


FACTORS: dict[str, float] = impact_factors()


def tier_span(votes: list[dict]) -> int:
    """这一项上三票的档位跨度（100→50 记 50 分），用来算"复核能改多少总分"。"""
    tiers = [v.get("tier") for v in votes if v.get("tier") is not None]
    return (max(tiers) - min(tiers)) if len(tiers) >= 2 else 0


def total_delta(key: str, span: int) -> float:
    """档位跨度折算到总分变动（保留两位）。"""
    return round(span * FACTORS.get(key, 0.0), 2)


# ---------------------------------------------------------------- 汇总
def _rows(done: list[tuple[str, dict, bool]]) -> list[dict]:
    """一票一行还不够用：报告按 (公司, 指标) 一行，档位与页码分列，便于直接拉透视。"""
    rows: list[dict] = []
    for code, payload, _ in done:
        for name in RUBRICS:
            item = (payload.get("items") or {}).get(name) or {}
            votes = item.get("votes") or []
            if votes:                      # 派生字段一律从票重算：产物里的旧标签不作数
                label, majority = agreement([v.get("tier") for v in votes])
                stype = split_type(votes)
            else:
                label, majority = item.get("agreement"), item.get("majority")
                stype = item.get("split_type")
            row = {"code": code, "key": name,
                   "agreement": label or "-",
                   "split_type": stype or "-",
                   "majority": majority,
                   "n_passages": item.get("n_passages"),
                   "votes": len(votes),
                   "factor": FACTORS.get(name, 0.0),
                   "delta": total_delta(name, tier_span(votes))}
            for i, v in enumerate(votes, start=1):
                row[f"t{i}"] = v.get("tier")
                row[f"p{i}"] = "/".join(str(p) for p in (v.get("pages") or []))
                row[f"e{i}"] = (v.get("evidence") or [""])[0][:80]
                row[f"r{i}"] = (v.get("reason") or v.get("error") or "")[:120]
            rows.append(row)
    return rows


def _primary_tiers(store: ArtifactStore, codes: list[str]) -> dict[tuple[str, str], int]:
    """现行主路径（``qualitative-llm``）的档位：用来回答"主模型是不是少数派"。"""
    out: dict[tuple[str, str], int] = {}
    for code in codes:
        payload = store.payload(code, "qualitative-llm") or {}
        for name, v in payload.items():
            if isinstance(v, dict) and v.get("tier") is not None:
                out[(code, name)] = int(v["tier"])
    return out


def fleiss_kappa(rows: list[dict]) -> float | None:
    """三票之间的一致性系数（Fleiss' κ）：>0.8 = 三票近乎复制，机制没在起作用。

    只算三票都给出档位的项：``gate``（不调模型）与 ``incomplete``（有票失败）不进分母。
    """
    items: list[list[int]] = []
    for r in rows:
        if r.get("split_type") in ("gate", "incomplete"):
            continue
        tiers = [int(r[f"t{i}"]) for i in (1, 2, 3) if r.get(f"t{i}") is not None]
        if len(tiers) >= 2:
            items.append(tiers)
    if not items:
        return None
    m = len(items[0])
    if any(len(it) != m for it in items):
        return None
    per_item = []
    for it in items:
        c = Counter(it)
        per_item.append((sum(v * v for v in c.values()) - m) / (m * (m - 1)))
    p_bar = sum(per_item) / len(items)
    total = Counter(t for it in items for t in it)
    n_total = sum(total.values())
    p_e = sum((v / n_total) ** 2 for v in total.values())
    if p_e >= 1:
        return None
    return (p_bar - p_e) / (1 - p_e)


def summarize(rows: list[dict], primary: dict[tuple[str, str], int]) -> dict:
    """验收表要的几个数：分裂率、类型分布、少数派、调用失败率。

    两个"少数派"必须分开数，混起来会得出错误结论：

    * ``t1_minority``：**今天**第一票（deepseek@0.0）与多数派不同 —— 图上的红框，看图就能验证；
    * ``minority``：**现行交付**的档位（``qualitative-llm`` 产物）与多数派不同 —— 只有它等于
      第一票时才是"同一把尺子"的比较。

    差在哪：交付产物的 key 是按当时的 rubric/提示词算的。判据改过之后 key 就失效，旧档位
    与今天同模型同温度的输出也会不同（实测 20 家里 10 项不一致，且全部是往上抬）。所以
    ``drift`` 单独报：非零说明**交付分数不是当前代码算出来的**，得先重跑再谈复核。

    **不含准确率**：那要 GT（``calibrate.py`` 的专家分），本模块不自己编一个。
    """
    judgeable = [r for r in rows if r["split_type"] not in ("gate", "incomplete")]
    split = [r for r in judgeable if r["agreement"] != "3/3"]
    types = Counter(r["split_type"] for r in judgeable)
    errors = [r for r in rows if r["split_type"] == "incomplete"]
    minor = [r for r in split
             if primary.get((r["code"], r["key"])) not in (None, r["majority"])]
    t1_minor = [r for r in split
                if r.get("t1") is not None and r["t1"] != r["majority"]]
    drift = [r for r in judgeable
             if r.get("t1") is not None
             and primary.get((r["code"], r["key"])) not in (None, r["t1"])]
    per_key: dict[str, dict] = {}
    for name in RUBRICS:
        mine = [r for r in judgeable if r["key"] == name]
        bad = [r for r in mine if r["agreement"] != "3/3"]
        dist = Counter()
        for r in mine:
            for v in (r.get("t1"), r.get("t2"), r.get("t3")):
                if v is not None:
                    dist[v] += 1
        rate = (len(bad) / len(mine)) if mine else 0.0
        factor = FACTORS.get(name, 0.0)
        # 一次 50 分档位翻转（100↔50，最常见的情形）折算到总分
        flip = round(50 * factor, 2)
        mean_delta = (round(sum(r.get("delta") or 0 for r in bad) / len(bad), 2)
                      if bad else 0.0)
        per_key[name] = {"n": len(mine), "split": len(bad), "rate": rate,
                         "factor": factor, "flip": flip,
                         "exp": round(rate * mean_delta, 2),
                         "mean_delta": mean_delta,
                         "delta_sum": round(sum(r.get("delta") or 0 for r in bad), 2),
                         "dist": dict(sorted(dist.items()))}

    # 复核预算有限时的覆盖曲线：按单项改档影响从大到小排，前 N 项覆盖多少总分变动
    deltas = sorted((r.get("delta") or 0.0) for r in split if (r.get("delta") or 0) > 0)
    deltas.reverse()
    total_d = sum(deltas)
    cover = []
    acc = 0.0
    for i, d in enumerate(deltas, start=1):
        acc += d
        cover.append({"n": i, "covered": round(acc, 2),
                      "share": round(acc / total_d, 3) if total_d else 0.0})
    first80 = next((c["n"] for c in cover if c["share"] >= 0.8), len(cover))
    return {"rows": len(rows), "judgeable": len(judgeable),
            "split": len(split), "rate": (len(split) / len(judgeable)) if judgeable else 0.0,
            "types": dict(types), "errors": len(errors),
            "minority": len(minor), "t1_minority": len(t1_minor), "drift": len(drift),
            "per_key": per_key, "kappa": fleiss_kappa(rows),
            "delta_total": round(total_d, 2), "coverage": cover, "first80": first80,
            "gate": sum(1 for r in rows if r["split_type"] == "gate")}


# ---------------------------------------------------------------- 报告
def _split_keys(rows: list[dict]) -> set[tuple[str, str]]:
    return {(r["code"], r["key"]) for r in rows
            if r.get("split_type") not in ("gate", "incomplete")
            and r.get("agreement") != "3/3"}


def compare(base_label: str, base_rows: list[dict],
            others: list[tuple[str, list[dict]]]) -> list[dict]:
    """两套投票人各自的分裂清单比一比：重合多少、各自独有多少。

    这是「分歧是模型偏好还是采样噪声」的判据：若 A（三模型 @0.7）与 B（三模型 @0.0）
    的分裂集合几乎不重合，说明主要来源是采样噪声，复核清单每跑一次都会换一张；
    若高度重合，说明分歧是稳定的模型间差异，可以作为固定口径使用。
    """
    base = _split_keys(base_rows)
    nbase = len([r for r in base_rows
                 if r.get("split_type") not in ("gate", "incomplete")])
    out = []
    for label, rows in others:
        other = _split_keys(rows)
        n = len([r for r in rows if r.get("split_type") not in ("gate", "incomplete")])
        both = base & other
        out.append({"label": label, "base": base_label, "split": len(other),
                    "rate": (len(other) / n) if n else 0.0,
                    "overlap": len(both),
                    "jaccard": (len(both) / len(base | other)) if (base | other) else 0.0,
                    "only_base": len(base - other), "only_other": len(other - base),
                    "n": n})
    return out


def _kappa_txt(k: float | None) -> tuple[str, str]:
    """κ 的展示与判读：太低说明判据太松，太高说明三票其实是同一个判档人在说话。"""
    if k is None:
        return "—", "样本不足"
    if k > 0.8:
        return f"{k:.2f}", "过高（三票近乎复制，机制没起作用）"
    if k < 0.4:
        return f"{k:.2f}", "过低（判据边界太松，需改 rubric）"
    return f"{k:.2f}", "通过"


def write_report(bundle: dict, *, out_dir: str | Path,
                 thresholds: dict | None = None) -> tuple[Path, Path, Path]:
    """写 ``ensemble.md`` / ``ensemble.csv`` / ``ensemble.json``（人读 + 机器读）。"""
    th = {"split_rate": 0.25, "error_rate": 0.02}
    th.update(thresholds or {})
    rows, sumry = bundle["rows"], bundle["summary"]
    primary = bundle["primary"]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    csv_path = out / "ensemble.csv"
    cols = ["code", "key", "agreement", "split_type", "majority", "factor", "delta",
            "n_passages", "votes", "t1", "t2", "t3", "p1", "p2", "p3",
            "e1", "e2", "e3", "r1", "r2", "r3"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    names = [v["name"] for v in bundle["voters"]]
    error_rate = (sumry["errors"] / sumry["judgeable"]) if sumry["judgeable"] else 0.0
    md: list[str] = [
        "# 三路独立判档 · 稳定性体检（只采集、不出分）", "",
        f"> 投票人：{'、'.join(names)}　|　年度：FY{bundle['year']}　|　"
        f"公司 {len(bundle['companies'])} 家　|　判档项 {sumry['rows']}"
        f"（可判 {sumry['judgeable']}，闸门 {sumry['gate']}）　|　"
        f"判档票数 {bundle['ballots']}（含失败）　|　复用 {bundle['reused']} 家",
        "", "**这份表测的是稳定性，不是准确性。** 三家一致地判错时它照样打「3/3」。"
        "准确率要拿 `calibrate.py` 的专家分单独量。", "",
        f"**出分模型只有一支：`{bundle.get('prod_model') or '（未配置）'}`（`ESG_LLM_MODEL`）。**"
        f"第一票就是它今天的复算；「交付档位」是它 2026-11-09 那次调用的结果；"
        f"其余投票人只做体检，不写 `qualitative-llm`、不进 `打分结果/`。", "",
        "## 一、验收表", "",
        "| 项 | 实测 | 阈值 | 结论 |", "|---|---|---|---|",
        f"| 判档分裂率 | {sumry['rate']:.1%}（{sumry['split']}/{sumry['judgeable']}） "
        f"| ≤{th['split_rate']:.0%} | {'通过' if sumry['rate'] <= th['split_rate'] else '不通过'} |",
        f"| 调用失败率 | {error_rate:.1%}（{sumry['errors']}） | ≤{th['error_rate']:.0%} "
        f"| {'通过' if error_rate <= th['error_rate'] else '不通过'} |",
        f"| 第一票（今日模型）是少数派 | {sumry['t1_minority']} 项 | 描述性 | — |",
        f"| 现行交付档位是少数派（旧口径） | {sumry['minority']} 项 | 描述性 | — |",
        f"| 交付档位与今日复现不一致 | {sumry['drift']} 项 | 0 "
        f"| {'通过' if not sumry['drift'] else '不通过：交付不是当前代码算的，先重跑'} |",
        "| 3/3 层准确率 − 全体准确率 | 待补 | ≥ +10pp | **未验**（需 GT） |",
        f"| Fleiss' κ（三票一致性） | {_kappa_txt(sumry.get('kappa'))[0]} | 0.4–0.8 "
        f"| {_kappa_txt(sumry.get('kappa'))[1]} |", "",
        "## 二、分指标（按「期望影响」排序）",
        "",
        "「单次翻转」= 一次 100↔50 改档对**总分**的影响（= 指标权重 × 分项权重 × 50）；"
        "「期望影响」= 分裂率 × 实测平均改档影响。前者说「改一档有多贵」，后者说「这个指标"
        "平均每项能捞回多少分」，人力排序看后者。", "",
        "| 指标 | 可判 | 分裂 | 分裂率 | 单次翻转(总分) | 期望影响(分/项) | 三票档位分布 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, d in sorted(sumry["per_key"].items(), key=lambda kv: -kv[1]["exp"]):
        dist = "、".join(f"{k}×{v}" for k, v in d["dist"].items()) or "—"
        md.append(f"| {name} | {d['n']} | {d['split']} | {d['rate']:.0%} "
                  f"| {d['flip']} | {d['exp']} | {dist} |")

    md += ["", "## 三、分歧类型", "", "| 类型 | 项数 | 说明 | 该做什么 |",
           "|---|---|---|---|",
           f"| 判据歧义 | {sumry['types'].get('rubric', 0)} | 三票引用同一段、档位不同 "
           "| 改 rubric 的 note/判据措辞，升 RUBRIC_VERSION |",
           f"| 证据分歧 | {sumry['types'].get('evidence', 0)} | 三票引用不同段 "
           "| 看原文，多半要补 keywords.csv |",
           f"| 部分重叠 | {sumry['types'].get('mixed', 0)} | 部分票重合 | 先按证据分歧处理 |",
           f"| 无引用 | {sumry['types'].get('empty', 0)} | 都没给出有效引用 "
           "| 召回缺陷，最高优先级 |", "",
           "## 四、待复核清单（按改档影响排序）", ""]
    todo = [r for r in rows if r["split_type"] not in ("none", "gate")]
    if not todo:
        md.append("（本批没有需要复核的判档项）")
    else:
        cov = sumry.get("coverage") or []
        first80 = sumry.get("first80") or 0
        md.append(f"共 {len(todo)} 项；这些项若全部按少数派改档，合计影响 **{sumry.get('delta_total', 0)} "
                  f"个总分点**（每项各属一家公司，不是同一家的总分相加）。"
                  f"按影响从大到小，**前 {first80} 项覆盖 80% 的分数变动**——人力有限就先看这些。")
        md += ["", "| # | 公司 | 指标 | 一致性 | 类型 | 交付(11-09) | 三票 | 改档影响(总分) | 累计覆盖 |",
               "|---|---|---|---|---|---|---|---|---|"]
        acc = 0.0
        for i, r in enumerate(sorted(todo, key=lambda x: -(x.get("delta") or 0)), start=1):
            tiers = " / ".join("失败" if r.get(f"t{i}") is None else str(r.get(f"t{i}"))
                               for i in (1, 2, 3) if r.get(f"t{i}") is not None
                               or f"t{i}" in r)
            prim = primary.get((r["code"], r["key"]))
            acc += r.get("delta") or 0
            share = (acc / sumry["delta_total"]) if sumry.get("delta_total") else 0.0
            md.append(f"| {i} | {r['code']} | {r['key']} | {r['agreement']} "
                      f"| {r['split_type']} | {'—' if prim is None else prim} | {tiers} "
                      f"| {r.get('delta', 0)} | {share:.0%} |")

    md += ["", "## 五、怎么用这份结果", "",
           "1. 复核顺序看第四节：先做「改档影响」大的（`S1.1.3/4/5` 整项权重，动一档 7.5 分；"
           "`S1.1.1-C`/`S1.1.2-C` 只是加权项里的 30% 分项，动一档 3.75/4.5 分）。",
           "2. 分裂项按类型分流：`rubric` 改判据、`evidence` 补词表、`empty` 查召回。",
           "3. **不要**用多数票改分：本批分裂项仍按主模型的分执行，复核结论走人工裁定。",
           "4. 分裂率低不等于判得对：`S1.1.4` 这类「一致地高」的项分裂率只有 5%，"
           "但专家判 50——稳定性报告看不见它，必须靠 GT 校准。",
           f"5. 体检结果里 `交付档位与今日复现不一致 = {sumry['drift']} 项` 时，先别忙复核："
           "说明交付的分数不是当前代码算的（判据/脱敏改过，产物 key 已失效），"
           "要拿当前代码重跑一遍交付再谈。",
           "5. 下一步：拿 10 家专家分跑 `calibrate.py`，按一致性分层算准确率，再决定要不要"
           "把三路判档写进交付流程。", ""]

    cmp_rows = bundle.get("compare") or []
    if cmp_rows:
        md += ["## 六、投票人配置对比", "",
               f"基准：{cmp_rows[0]['base']}。**看重合度**：两套配置的分裂清单若几乎不重合，"
               "说明分歧主要来自采样噪声（复核清单每次都会换一张）；若高度重合，说明是稳定的"
               "模型间差异，可以作为固定口径。", "",
               "| 配置 | 分裂 | 分裂率 | 与基准重合 | Jaccard | 仅基准分裂 | 仅该配置分裂 |",
               "|---|---|---|---|---|---|---|"]
        for c in cmp_rows:
            md.append(f"| {c['label']} | {c['split']} | {c['rate']:.1%} | {c['overlap']} "
                      f"| {c['jaccard']:.0%} | {c['only_base']} | {c['only_other']} |")
        md.append("")
    md_path = out / "ensemble.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    json_path = out / "ensemble.json"
    # 主模型档位的键是 (code, 指标) 元组，JSON 里存成 "code|指标"
    primary_json = {f"{c}|{k}": v for (c, k), v in (primary or {}).items()}
    json_path.write_text(json.dumps({**{k: v for k, v in bundle.items() if k != "store"},
                                     "primary": primary_json},
                                    ensure_ascii=False, indent=1, default=str),
                         encoding="utf-8")
    return md_path, csv_path, json_path


# ---------------------------------------------------------------- 入口
def read_rows(path: str | Path) -> list[dict]:
    """读回另一套配置写出的 ``ensemble.csv``（对比用）。"""
    rows: list[dict] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            for k in ("t1", "t2", "t3", "majority"):
                r[k] = int(r[k]) if str(r.get(k) or "").strip() not in ("", "None") else None
            rows.append(r)
    return rows


def run(*, year: int, build_dir: str | Path, voters: list[Voter],
        codes: list[str] | None = None, limit: int | None = None,
        force: bool = False, workers: int = 4, out_dir: str | Path | None = None,
        compare_with: list[str | Path] | None = None, progress=None) -> dict:
    got = collect(year=year, build_dir=build_dir, voters=voters, codes=codes,
                  limit=limit, force=force, workers=workers, progress=progress)
    primary = _primary_tiers(got["store"], got["companies"])
    sumry = summarize(got["rows"], primary)
    bundle = {"year": year, "voters": got["voters"], "companies": got["companies"],
              "ballots": got["ballots"], "reused": got["reused"], "rows": got["rows"],
              "primary": primary, "summary": sumry,
              # 出分模型单独记一笔：它只有一支（ESG_LLM_MODEL）；投票人 2/3 永远不出分。
              # 不写这一行，"第一票"和"交付档位"就容易被读成两个模型。
              "prod_model": get_settings().llm_model}
    if compare_with:
        others = []
        for p in compare_with:
            path = Path(p)
            others.append((f"{path.parent.name}/{path.stem}", read_rows(path)))
        bundle["compare"] = compare("本次运行", got["rows"], others)
    md, csv_path, js = write_report(bundle, out_dir=out_dir or Path("reports"))
    bundle["files"] = {"md": str(md), "csv": str(csv_path), "json": str(js)}
    return bundle


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    settings = get_settings()
    p = argparse.ArgumentParser(
        prog="python -m esg_s11_scorer.ensemble",
        description="三路独立判档 · 只采集不出分：量判档稳定性（不改任何交付物）")
    p.add_argument("--year", type=int, default=settings.year,
                   help=f"报告年度（默认 {settings.year}）")
    p.add_argument("--build-dir", default=str(settings.build_dir),
                   help="阶段产物目录（默认 ESG_BUILD_DIR）；只读其中的 parse/facts")
    p.add_argument("--voters", default=getattr(settings, "ensemble_voters", ""),
                   help="投票人，逗号分隔，形如 qwen-plus@0.7,qwen-max@0.7；"
                        "空 = 主模型三个温度")
    p.add_argument("--codes", default="", help="只跑这几家（逗号分隔）")
    p.add_argument("--compare-with", action="append", default=[],
                   metavar="CSV",
                   help="另一套投票人的 ensemble.csv，附加一段「分裂清单重合度」对比；可给多次")
    p.add_argument("--limit", type=int, default=None, help="只跑前 N 家（试跑用）")
    p.add_argument("--workers", type=int, default=4, help="并发公司数（默认 4）")
    p.add_argument("--force", action="store_true", help="忽略已有产物重算")
    p.add_argument("--out", default=str(Path(__file__).resolve().parent / "reports"),
                   help="报告目录（默认包内 reports/；不写 ESG_OUT_DIR）")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    base = from_settings(settings)
    if not base.ready:
        print("[错误] LLM 未配置（ESG_LLM_BASE_URL / MODEL / API_KEY）：无法判档",
              file=sys.stderr)
        return 2
    try:
        voters = parse_voters(args.voters, base)
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    codes = [c.strip() for c in args.codes.replace("，", ",").split(",") if c.strip()]
    progress = None if args.quiet else (lambda s: print(s, file=sys.stderr))

    t0 = time.perf_counter()
    bundle = run(year=args.year, build_dir=args.build_dir, voters=voters,
                 codes=codes or None, limit=args.limit, force=args.force,
                 workers=args.workers, out_dir=args.out,
                 compare_with=args.compare_with or None, progress=progress)
    s = bundle["summary"]
    print(f"\n投票人：{'、'.join(v['name'] for v in bundle['voters'])}")
    print(f"公司 {len(bundle['companies'])} 家 · 判档项 {s['rows']}"
          f"（可判 {s['judgeable']}，闸门 {s['gate']}）· 判档 {bundle['ballots']} 票"
          f" · 复用 {bundle['reused']} 家 · {time.perf_counter() - t0:.1f}s")
    print(f"分裂率 {s['rate']:.1%}（{s['split']}/{s['judgeable']}）· "
          f"类型 {s['types'] or '—'} · 主模型少数派 {s['minority']} 项"
          f" · κ {_kappa_txt(s.get('kappa'))[0]}")
    print(f"复核预算：合计可动 {s.get('delta_total', 0)} 个总分点；"
          f"按影响排序，前 {s.get('first80', 0)} 项覆盖 80%")
    for name, d in sorted(s["per_key"].items(), key=lambda kv: -kv[1]["exp"]):
        print(f"  {name:<9} 分裂 {d['split']}/{d['n']}（{d['rate']:.0%}）"
              f"　单次翻转 {d['flip']} 分　期望影响 {d['exp']} 分/项")
    print(f"\n[写出] {bundle['files']['md']}\n         {bundle['files']['csv']}"
          f"\n         {bundle['files']['json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())