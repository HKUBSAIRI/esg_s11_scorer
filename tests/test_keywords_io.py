# -*- coding: utf-8 -*-
"""词表文件层测试：改词表不该改代码。

断言四件事：
1. 包内那份 keywords.csv 能完整还原五个 rubric 的词表（不然就是发布漏了词）；
2. 写回再读回来，组名、组序、词序都不变（Excel 编辑后不该悄悄重排）；
3. 手滑（缺列、指标名写错、启用列乱填）必须立刻报错，不能静默失效；
4. ``ESG_S11_KEYWORDS`` 能把整份词表换掉——这是「一个项目一套词表」的入口。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from esg_s11_scorer import keywords_io  # noqa: E402
from esg_s11_scorer.keywords_io import KeywordFileError  # noqa: E402
from esg_s11_scorer.rubric import KEYWORD_FILE, RUBRICS  # noqa: E402

HEADER = "指标,证据组,关键词,启用\n"


class TestShippedFile(unittest.TestCase):
    def test_default_file_exists(self):
        self.assertTrue(Path(KEYWORD_FILE).is_file(), f"词表文件不在：{KEYWORD_FILE}")

    def test_covers_every_rubric(self):
        loaded = keywords_io.load(KEYWORD_FILE, known=tuple(RUBRICS))
        self.assertEqual(sorted(loaded), sorted(RUBRICS))
        for key, groups in loaded.items():
            self.assertTrue(groups, f"{key} 没有词条")
            for group, words in groups:
                self.assertTrue(group.strip(), f"{key} 有空组名")
                self.assertTrue(words, f"{key} 的 {group} 没有词")

    def test_matches_loaded_rubrics(self):
        """文件 → RUBRICS 必须逐词一致（顺序也一致）。"""
        loaded = keywords_io.load(KEYWORD_FILE)
        for key, rub in RUBRICS.items():
            self.assertEqual(rub.groups, loaded[key], msg=f"{key} 词表与文件不一致")


class TestRoundTrip(unittest.TestCase):
    def test_write_then_load_keeps_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kw.csv"
            keywords_io.write(path, RUBRICS)
            back = keywords_io.load(path)
        for key, rub in RUBRICS.items():
            self.assertEqual(back[key], rub.groups, msg=f"{key} 往返后变了")

    def test_utf8_bom_for_excel(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kw.csv"
            keywords_io.write(path, RUBRICS)
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"),
                            "CSV 要带 BOM，否则 Excel 双击中文乱码")


class TestParsing(unittest.TestCase):
    def _load(self, body: str, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "k.csv"
        p.write_text(body, encoding="utf-8-sig")
        return keywords_io.load(p, **kw)

    def test_disabled_word_is_skipped(self):
        got = self._load(HEADER + "S1.1.3,T1,工时,是\nS1.1.3,T1,班表,否\n")
        self.assertEqual(got["S1.1.3"], (("T1", ("工时",)),))

    def test_blank_group_falls_back(self):
        got = self._load(HEADER + "S1.1.3,,工时,是\n")
        self.assertEqual(got["S1.1.3"], ((keywords_io.UNGROUPED, ("工时",)),))

    def test_blank_and_comment_rows_ignored(self):
        got = self._load(HEADER + "\n# 这一行是备注\nS1.1.3,T1,工时,是\n")
        self.assertEqual(got["S1.1.3"], (("T1", ("工时",)),))

    def test_duplicate_word_kept_once(self):
        got = self._load(HEADER + "S1.1.3,T1,工时,是\nS1.1.3,T1,工时,是\n")
        self.assertEqual(got["S1.1.3"], (("T1", ("工时",)),))

    def test_unknown_indicator_raises(self):
        with self.assertRaises(KeywordFileError) as ctx:
            self._load(HEADER + "S1.9,T1,工时,是\n", known=("S1.1.3",))
        self.assertIn("S1.9", str(ctx.exception))

    def test_bad_switch_raises(self):
        with self.assertRaises(KeywordFileError) as ctx:
            self._load(HEADER + "S1.1.3,T1,工时,也许\n")
        self.assertIn("启用", str(ctx.exception))

    def test_missing_column_raises(self):
        with self.assertRaises(KeywordFileError) as ctx:
            self._load("指标,关键词\nS1.1.3,工时\n")
        self.assertIn("缺列", str(ctx.exception))

    def test_missing_file_raises_with_hint(self):
        with self.assertRaises(KeywordFileError) as ctx:
            keywords_io.load(Path(tempfile.gettempdir()) / "不存在的词表.csv")
        self.assertIn(keywords_io.ENV_NAME, str(ctx.exception))


class TestEnvOverride(unittest.TestCase):
    """``ESG_S11_KEYWORDS`` 指到别的文件时，default_path() 必须跟着走。"""

    def setUp(self):
        self._bak = os.environ.get(keywords_io.ENV_NAME)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._bak is None:
            os.environ.pop(keywords_io.ENV_NAME, None)
        else:
            os.environ[keywords_io.ENV_NAME] = self._bak

    def test_env_wins(self):
        p = Path(self.tmp.name) / "项目A词表.csv"
        p.write_text(HEADER + "S1.1.3,T1,工时,是\n", encoding="utf-8-sig")
        os.environ[keywords_io.ENV_NAME] = str(p)
        self.assertEqual(keywords_io.default_path(), p)
        self.assertIn(keywords_io.ENV_NAME, keywords_io.describe_source())
        self.assertEqual(keywords_io.load()["S1.1.3"], (("T1", ("工时",)),))


if __name__ == "__main__":
    unittest.main(verbosity=2)