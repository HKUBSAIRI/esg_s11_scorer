# -*- coding: utf-8 -*-
"""盲评脱敏：送进模型的段落里不能出现公司名称。

为什么要这一层
--------------
判档只看「写了什么」，不该看「是谁写的」。带着公司名，模型会受品牌先验影响
（同一条制度表述，知名企业更容易被判「制度完备」），同一批公司之间的横向比较也就不成立了。
所以模型看到的是脱敏后的段落，结论才只由文本本身支撑。

三层替换（依次执行，前一层没有的才轮到后一层）
--------------------------------------------
1. **显式别名**：文件名里的公司名（``02269_WUXI_BIO_ESG_FY2025`` → ``WUXI BIO``）、
   股票代码、以及 ``ESG_ALIAS_CSV`` 里人工补的品牌/子公司名。
   别名按长度降序替换，且容忍空白差异（``WuXi  Biologics`` 也能命中）。
2. **通用公司名网**：``某某生物制药有限公司`` / ``ABC Biologics`` 这类「专名 + 公司后缀」，
   用来兜住别名表没登记的全资子公司、并购标的。
3. **兜底断言**：替换完再扫一遍，仍有残留就抛错——**宁可这一次不判档**（退回关键词兜底），
   也不把带公司名的文本发出去。

占位符用 ``[COMPANY]``：语言无关、一眼看得出是被隐去的专名，且提示词里会说明它不参与判断。
"""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER = "[COMPANY]"

# 公司后缀：用于第 2 层通用网。只认这些，避免把普通名词当公司名。
SUFFIXES = (
    "Biologics", "Biopharmaceuticals", "Biopharmaceutical", "Pharmaceuticals",
    "Pharmaceutical", "Pharma", "Biosciences", "Bioscience", "Biotech", "Bio",
    "Holdings", "Holding", "Group", "Limited", "Ltd", "PLC", "Corp", "Corporation",
    "Inc", "Co", "AG", "SA", "GmbH",
    "生物制药", "生物科技", "生物医药", "药业", "制药", "医药", "集团", "控股",
    "股份有限公司", "有限公司", "股份",
)
# 通用词开头的不当作专名（否则 "The Group"、"Our Company" 会被误伤）
STOP = {
    "the", "this", "that", "our", "its", "their", "we", "you", "your", "all",
    "each", "every", "any", "such", "these", "those", "group", "company",
    "board", "management", "committee", "esg", "gri", "hkex", "ifrs", "iso",
    "annual", "report", "sustainability", "global", "china", "hong", "kong",
    "本公司", "本集团", "公司", "集团", "该公司", "该集团", "我公司", "本企业",
}
# 行业通名：出现在公司名里但本身不指某家公司（否则「生物制药行业」会被整段打掉）
SECTOR = {"生物", "医药", "制药", "医疗", "健康", "科技", "生物医药", "生物科技"}
# 文件名里这些词不是公司名（批次/年份/文档类型）
NOISE = {"ESG", "FY", "AR", "SR", "REPORT", "SUSTAINABILITY", "PDF"}

MIN_ALIAS_LEN = 4                    # 短于这个长度的别名不够特异，替换会误伤正文


@dataclass(frozen=True)
class Blinder:
    """一次运行的脱敏器：别名集合 + 占位符。"""

    aliases: tuple[str, ...] = ()
    placeholder: str = PLACEHOLDER
    generic: bool = True

    # -- 内部：把别名编成「容忍空白、吃掉词尾」的正则
    def _patterns(self) -> list[re.Pattern]:
        """``WUXI BIO`` 会连同 ``WuXi Biologics`` 的 ``logics`` 一起吃掉。

        只匹配到 ``WUXI BIO`` 会留下 ``[COMPANY]logics`` 这种残词，既难看又给模型递了半个线索。
        """
        pats = []
        for a in sorted(self.aliases, key=len, reverse=True):
            parts = [re.escape(p) for p in a.split()]
            pats.append(re.compile(r"\s+".join(parts) + r"[A-Za-z]*", re.I))
        return pats

    def scrub(self, text: str) -> tuple[str, int]:
        """返回（脱敏后的文本, 替换次数）。"""
        n = 0
        for pat in self._patterns():
            text, k = pat.subn(self.placeholder, text)
            n += k
        if self.generic:
            text, k = _generic_net(text, self.placeholder)
            n += k
        return _tidy(text, self.placeholder), n

    def leaks(self, text: str) -> list[str]:
        """仍然残留的别名（用于发送前的兜底断言）。"""
        return [a for a in self.aliases if a and re.search(
            r"\s+".join(re.escape(p) for p in a.split()), text, re.I)]

    def digest(self) -> str:
        """别名集合的指纹：别名表一改，判档缓存就该失效（否则脱敏规则变了还在用旧结论）。"""
        raw = "|".join(sorted(self.aliases)) + f"|{self.placeholder}|{int(self.generic)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


_GENERIC = re.compile(
    r"(?<![\w\[])"                                   # 不接在已替换的占位符/单词中间
    r"[A-Z][A-Za-z&.\-]*(?:\s+[A-Z][A-Za-z&.\-]*){0,3}\s+"
    r"(?:" + "|".join(SUFFIXES[:15]) + r")\.?"
    r"(?![\w])")
_GENERIC_CN = re.compile(
    r"[\u4e00-\u9fff]{3,8}(?:" + "|".join(SUFFIXES[15:]) + r")")


def _generic_net(text: str, placeholder: str) -> tuple[str, int]:
    """兜住别名表没登记的「专名 + 公司后缀」。"""
    n = 0

    def sub_en(m: re.Match) -> str:
        nonlocal n
        head = m.group(0).split()[0].lower().strip(".,")
        if head in STOP:
            return m.group(0)                        # the Group / our Company 不动
        n += 1
        return placeholder

    def sub_cn(m: re.Match) -> str:
        nonlocal n
        s = m.group(0)
        if s in STOP or any(s.startswith(w) for w in STOP):
            return s                                 # 本公司 / 该集团 不动
        if any(s.startswith(w) for w in SECTOR):
            return s                                 # 生物制药 / 医药集团 等行业通名不动
        n += 1
        return placeholder

    text = _GENERIC.sub(sub_en, text)
    text = _GENERIC_CN.sub(sub_cn, text)
    return text, n


def _tidy(text: str, placeholder: str) -> str:
    """连续占位符合并、去掉多余空白（``[COMPANY] [COMPANY] 有限公司`` 只留一个）。"""
    text = re.sub(rf"(?:{re.escape(placeholder)}\s*){{2,}}", placeholder + " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# ---------------------------------------------------------------- 别名来源
def aliases_from_filename(filename: str) -> list[str]:
    """``02269_WUXI_BIO_ESG_FY2025.pdf`` → ``["WUXI BIO", "WUXI", "02269", "2269"]``。

    只取「ESG / FY 之前」的部分当公司名；再补上前几个足够特异的单词（``WUXI`` / ``HANSOH``）。
    过短或纯通名（``BIO`` / ``SINO`` / ``PHARMA``）不单独成别名——它们会打掉 ``biologics``、
    ``biosimilar`` 这类正文词，把证据本身弄残。
    """
    stem = Path(filename).stem
    tokens = [t for t in re.split(r"[_\-.\s]+", stem) if t]
    code = tokens[0] if tokens and tokens[0].isdigit() else ""
    body = [t for t in (tokens[1:] if code else tokens) if not t.isdigit()]

    head: list[str] = []
    for t in body:
        if t.upper() in NOISE:
            break
        head.append(t)

    out: list[str] = []
    if head:
        joined = " ".join(head)
        if len(joined) >= MIN_ALIAS_LEN:
            out.append(joined)
        for t in head:
            if len(t) >= MIN_ALIAS_LEN and t.upper() not in NOISE:
                out.append(t)
    if code:
        out += [code, code.lstrip("0")]
    return [a for a in dict.fromkeys(out) if len(a) >= MIN_ALIAS_LEN]


def load_alias_csv(path: str | Path | None) -> dict[str, tuple[str, ...]]:
    """读人工别名表：``code,alias``（一行一个别名，同 code 多行即多个）。

    只登记文件名里看不出来的东西：中文名、品牌、产品线、子公司、曾被并购的旧名。
    """
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    out: dict[str, list[str]] = {}
    with p.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = (row.get("code") or row.get("代码") or "").strip()
            alias = (row.get("alias") or row.get("别名") or "").strip()
            if code and len(alias) >= 2:
                out.setdefault(code, []).append(alias)
    return {k: tuple(v) for k, v in out.items()}


def build(code: str, *, name: str = "", filename: str = "",
          extra: dict[str, tuple[str, ...]] | None = None) -> Blinder:
    """按「文件名 + 代码 + 人工别名」装配一台脱敏器。"""
    aliases = aliases_from_filename(filename or name)
    if name:
        aliases.append(name)
    aliases += list((extra or {}).get(code, ()))
    uniq = tuple(dict.fromkeys(a.strip() for a in aliases
                               if a and len(a.strip()) >= 2))
    return Blinder(aliases=uniq)


__all__ = ["Blinder", "PLACEHOLDER", "build", "aliases_from_filename",
           "load_alias_csv", "MIN_ALIAS_LEN"]