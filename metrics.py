# -*- coding: utf-8 -*-
"""指标口径算子：全包只有一份实现。

重构前「改善率」有三份代码——``extract.improvement``、``bench._improve``、
``scoring`` 通过 ``from .bench import _improve`` 引用私有函数。公式本身简单，但它是
S1.1.1 的 B 项与 S1.1.2 的 B 项共用的核心算子，三份实现迟早漂移，且跨模块引用私有名
（下划线开头）本身就是坏味道。集中到本模块后：

    extract / bench / scoring  →  metrics.improvement

口径（S1.1.1 的 B 项与 S1.1.2 的 B 项共用，档位见 ``config``）：

    改善率 = -(本期 - 上期) / 上期 × 100

- 上期为 0 或缺失 → ``None``（表示"不可算"，由调用方决定 N/A 还是走 0/100 特例）；
- 不做四舍五入，保留原始精度，展示层再格式化。
"""
from __future__ import annotations


def improvement(cur: float | None, prior: float | None) -> float | None:
    """改善率（%）。上期缺失或为 0 时返回 None。"""
    if cur is None or prior is None or prior == 0:
        return None
    return -(cur - prior) / prior * 100.0


def fmt_pct(v: float | None, nd: int = 2) -> str:
    """统一的百分数展示（带符号），避免各报表各写一套格式。"""
    if v is None:
        return "N/A"
    return f"{v:+.{nd}f}%"