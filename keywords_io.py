# -*- coding: utf-8 -*-
"""检索词表的外部文件层：CSV 读写与校验。

为什么词表要搬到文件里
---------------------
词表是**业务口径**，不是代码：评审会上说「加一个『年休假』」，不该需要改 .py、跑测试、
发版。所以把它挪到包内的 ``keywords.csv``（可直接用 Excel 打开），代码只负责读、校验、
算指纹。改词表仍然会让 ``rubric_hash()`` 变（词表在指纹里），缓存不会串。

文件形状（四列，一行一个词）
---------------------------
    指标,证据组,关键词,启用
    S1.1.3,T3 休息休假保障,年休假,是

- ``指标``   ：rubric 的 key（S1.1.1-C / S1.1.2-C / S1.1.3 / S1.1.4 / S1.1.5）
- ``证据组`` ：T1/T2… 或任意组名；空的归到「未分组」。组的先后 = 表里第一次出现的先后
- ``关键词`` ：匹配用的原文。中文按**子串**（「工时」命中「标准工时制」）；
               英文按**词首**（``wage`` 不命中 ``sewage``、``diversity`` 不命中 ``biodiversity``），
               但词尾不设限，单复数照旧命中
- ``启用``   ：``是`` 生效、``否`` 暂时停用（词留在文件里，方便回滚），留空视为「是」

改了没生效？词表是**进程启动时读一次**的：重启进程即可。想用另一份文件（比如按项目
分叉的词表），设 ``ESG_S11_KEYWORDS=<路径>``。
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Mapping

COL_IND, COL_GROUP, COL_WORD, COL_ON = "指标", "证据组", "关键词", "启用"
HEADER = (COL_IND, COL_GROUP, COL_WORD, COL_ON)
ENV_NAME = "ESG_S11_KEYWORDS"
ON_VALUES = {"", "是", "y", "yes", "1", "true", "on"}
OFF_VALUES = {"否", "n", "no", "0", "false", "off"}
UNGROUPED = "未分组"


class KeywordFileError(RuntimeError):
    """词表文件有问题：缺列、路径不存在、指标名不认识。消息里带行号与修法。"""


def default_path() -> Path:
    """生效的词表文件：``ESG_S11_KEYWORDS`` > 包内 ``keywords.csv``。"""
    raw = os.environ.get(ENV_NAME, "").strip()
    if raw:
        return Path(os.path.expandvars(raw)).expanduser()
    return Path(__file__).with_name("keywords.csv")


def describe_source(path: Path | None = None) -> str:
    """这个词表是包内默认还是外部指定的（给 ``--show-config`` 与日志看）。"""
    p = Path(path or default_path())
    return "环境变量 " + ENV_NAME if os.environ.get(ENV_NAME, "").strip() else f"包内默认（{p.name}）"


def load(path: str | Path | None = None,
         known: tuple[str, ...] | None = None) -> dict[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    """读词表 → ``{指标: ((证据组, (词, …)), …)}``，顺序即文件顺序。

    ``known`` 给了就校验指标名，写错名字立刻报错——否则那几行会**静默失效**，
    词表看着有、跑起来没命中，是最难查的一类问题。
    """
    p = Path(path or default_path())
    if not p.is_file():
        raise KeywordFileError(
            f"词表文件不存在：{p}\n"
            f"修法：把包内的 keywords.csv 复制过来，或把 {ENV_NAME} 指向正确的文件。")
    rows = _read_rows(p)
    groups: dict[str, list[list]] = {}
    seen: dict[tuple[str, str], set[str]] = {}
    for lineno, row in rows:
        ind = (row.get(COL_IND) or "").strip()
        word = (row.get(COL_WORD) or "").strip()
        if not ind and not word:
            continue                                     # 空行
        if ind.startswith("#"):
            continue                                     # 注释行
        if not ind or not word:
            raise KeywordFileError(_where(p, lineno, "指标与关键词都不能为空"))
        if known is not None and ind not in known:
            raise KeywordFileError(
                _where(p, lineno, f"指标 {ind!r} 不在 {list(known)} 里"))
        flag = (row.get(COL_ON) or "").strip().lower()
        if flag in OFF_VALUES:
            continue                                     # 停用的词保留在文件里，不参与匹配
        if flag not in ON_VALUES:
            raise KeywordFileError(
                _where(p, lineno, f"启用列只认「是/否」（收到 {flag!r}）"))
        group = (row.get(COL_GROUP) or "").strip() or UNGROUPED
        key = (ind, group)
        if key not in seen:
            seen[key] = set()
            groups.setdefault(ind, []).append([group, []])
        # 同一个词在一个组里写两遍没有意义（匹配只看「有没有命中」），去重但保留首次位置
        if word in seen[key]:
            continue
        seen[key].add(word)
        groups[ind][-1][1].append(word)
    return {ind: tuple((g, tuple(ws)) for g, ws in gs) for ind, gs in groups.items()}


def write(path: str | Path, rubrics: Mapping[str, object] | None = None) -> Path:
    """把当前生效的词表倒回 CSV（``rubrics`` 缺省取 ``rubric.RUBRICS``）。

    用途两个：① 首次生成随包发布的 keywords.csv；② 别人给的 Excel 词表迁进来前，
    先导一份标准格式做对照。``utf-8-sig`` 是为了 Excel 双击不乱码。
    """
    if rubrics is None:
        from .rubric import RUBRICS as rubrics  # 延迟导入，避免与 rubric 循环
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for key, rub in rubrics.items():
            for group, words in getattr(rub, "groups", ()):
                for word in words:
                    w.writerow((key, group, word, "是"))
    return out


def _read_rows(path: Path) -> list[tuple[int, dict]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = [c.strip() for c in (reader.fieldnames or [])]
        missing = [c for c in HEADER[:3] if c not in cols]
        if missing:
            raise KeywordFileError(
                f"词表文件缺列 {missing}：{path}\n"
                f"表头应为：{','.join(HEADER)}")
        reader.fieldnames = cols
        return [(i, row) for i, row in enumerate(reader, start=2)]


def _where(path: Path, lineno: int, msg: str) -> str:
    return f"{path.name} 第 {lineno} 行：{msg}"


__all__ = ["KeywordFileError", "HEADER", "ENV_NAME", "UNGROUPED",
           "default_path", "describe_source", "load", "write"]