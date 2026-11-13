# -*- coding: utf-8 -*-
"""架构约束：把「分层」钉成可执行的断言，而不是文档里的一句话。

这个包 26 个模块放在同一个目录里，分层靠的是**依赖方向**而不是目录深度。
依赖方向一旦被破坏，报错往往很晚：比如渲染层顺手 import 了 pipeline，报告里的数
就可能与自己重算的数不一致；纯计算层顺手 import 了 store，函数不再纯，指纹缓存失效。

三条规则都对应真实退化：
1. 渲染层不得 import 编排层 —— 「只渲染、不计算」是这个包的核心承诺。
2. 纯计算层不得 import 存储/编排层 —— 否则不能单测、不能进指纹缓存。
3. 十六进制颜色与表头/标题/行高 helper 只许出现在 ``xlstyle`` —— 否则
   “改一次品牌色要改四处”会重演（历史上真的发生过：三本册子各写一份样式）。
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]

RENDER = {"report", "scoreviz", "qualviz", "keyword_table"}       # 只出 xlsx
ORCHESTRATION = {"pipeline", "cli", "calibrate", "ensemble", "disputes",
                 "__main__"}  # 调度/入口
PURE = {"extract", "bench", "scoring", "metrics", "models", "stages"}
STORAGE = {"store", "tables"}                                       # 落盘/gold 表
STYLE = "xlstyle"

HEX_COLOR = re.compile(r"['\"](?:FF|ff)?[0-9A-Fa-f]{6}['\"]")
HELPER_DEFS = {"title", "head", "note", "text_units", "lines_for", "row_height"}


def _parse(mod: str) -> ast.Module:
    return ast.parse((PKG / f"{mod}.py").read_text(encoding="utf-8"))


def _modules() -> list[str]:
    return sorted(p.stem for p in PKG.glob("*.py"))


def _module_level_imports(mod: str) -> set[str]:
    """只取**模块级** import（函数内的延迟导入是有意为之，不算依赖边）。"""
    deps: set[str] = set()
    for node in _parse(mod).body:
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                deps.add(node.module.split(".")[0])
            elif node.level:
                deps.update(a.name.split(".")[0] for a in node.names)
            elif node.module and node.module.split(".")[0] == PKG.name:
                rest = node.module.split(".")
                if len(rest) > 1:
                    deps.add(rest[1])
    return {d for d in deps if (PKG / f"{d}.py").exists()} - {mod}


class TestDependencyDirection(unittest.TestCase):
    def test_render_layer_does_not_import_orchestration(self):
        for mod in RENDER:
            bad = _module_level_imports(mod) & ORCHESTRATION
            self.assertFalse(bad, f"{mod} 是渲染层，不该 import {sorted(bad)}")

    def test_pure_calc_does_not_touch_storage(self):
        for mod in PURE:
            bad = _module_level_imports(mod) & (STORAGE | {"pipeline"})
            self.assertFalse(bad, f"{mod} 是纯计算层，不该 import {sorted(bad)}")

    def test_style_layer_is_leaf(self):
        self.assertEqual(_module_level_imports(STYLE), set(),
                         "xlstyle 必须谁都不依赖（它是被大家引用的基元）")

    def test_no_import_cycles(self):
        graph = {m: _module_level_imports(m) for m in _modules()}
        state: dict[str, int] = {}
        cycles: list[list[str]] = []

        def dfs(u: str, stack: list[str]) -> None:
            state[u] = 1
            stack.append(u)
            for v in sorted(graph.get(u, ())):
                if state.get(v) == 1:
                    cycles.append(stack[stack.index(v):] + [v])
                elif state.get(v, 0) == 0:
                    dfs(v, stack)
            stack.pop()
            state[u] = 2

        for m in graph:
            if state.get(m, 0) == 0:
                dfs(m, [])
        self.assertEqual(cycles, [], f"模块级循环依赖：{cycles}")


class TestStyleIsSingleSource(unittest.TestCase):
    def test_hex_colors_only_in_xlstyle(self):
        offenders = {}
        for m in _modules():
            if m == STYLE:
                continue
            hits = HEX_COLOR.findall((PKG / f"{m}.py").read_text(encoding="utf-8"))
            if hits:
                offenders[m] = hits[:3]
        self.assertEqual(offenders, {}, f"颜色应只定义在 {STYLE}.py：{offenders}")

    def test_table_helpers_only_in_xlstyle(self):
        offenders = {}
        for m in _modules():
            if m == STYLE:
                continue
            names = {n.name for n in _parse(m).body if isinstance(n, ast.FunctionDef)}
            dup = names & HELPER_DEFS
            if dup:
                offenders[m] = sorted(dup)
        self.assertEqual(offenders, {}, f"表头/标题/行高 helper 应只在 {STYLE}.py：{offenders}")

    def test_renderers_share_the_palette(self):
        """四个册子都从 xlstyle 取色，不各自 import openpyxl.styles 里的配色类。"""
        for mod in RENDER:
            src = (PKG / f"{mod}.py").read_text(encoding="utf-8")
            self.assertIn("xlstyle", src, f"{mod} 应当从 xlstyle 取样式")


if __name__ == "__main__":
    unittest.main(verbosity=2)