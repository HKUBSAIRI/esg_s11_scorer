# -*- coding: utf-8 -*-
"""落地层测试：产物与表能写、能读、能判新旧，坏了会炸。

这一层最容易出「静默错误」：读出一个空表、指纹永远不匹配（等于没有缓存）、
或者 parquet 把 bool 列写成了字符串——都不报错，只是结果悄悄不对。所以这里逐条钉死。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401
from esg_s11_scorer import store
from esg_s11_scorer.store import (ArtifactStore, StoreError, TableStore, combine,
                                  digest_bytes, digest_file, digest_obj, source_digest)


class TestDigest(unittest.TestCase):
    def test_bytes_and_file_agree(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.bin"
            p.write_bytes(b"hello")
            self.assertEqual(digest_bytes(b"hello"), digest_file(p))

    def test_obj_ignores_key_order(self):
        self.assertEqual(digest_obj({"a": 1, "b": 2}), digest_obj({"b": 2, "a": 1}))
        self.assertNotEqual(digest_obj({"a": 1}), digest_obj({"a": 2}))

    def test_combine_distinguishes_position(self):
        self.assertNotEqual(combine("a", "b"), combine("b", "a"))
        self.assertEqual(combine("a", None), combine("a", None))

    def test_source_digest_changes_with_content(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.py"
            p.write_text("x = 1\n", encoding="utf-8")
            first = source_digest(p)
            p.write_text("x = 2\n", encoding="utf-8")
            # 同一路径改内容必须换指纹：这是「代码变了就重算」的依据
            from esg_s11_scorer import store as S
            S._SOURCE_MEMO.clear()
            self.assertNotEqual(first, source_digest(p))


class TestArtifactStore(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = ArtifactStore(root=Path(self.td.name) / "FY2025", year=2025)

    def tearDown(self):
        self.td.cleanup()

    def test_write_read_fresh(self):
        self.assertIsNone(self.store.payload("02269", "facts"))
        self.store.write("02269", "facts", "k1", {"a": 1})
        self.assertEqual(self.store.payload("02269", "facts"), {"a": 1})
        self.assertEqual(self.store.key("02269", "facts"), "k1")
        self.assertTrue(self.store.fresh("02269", "facts", "k1"))
        self.assertFalse(self.store.fresh("02269", "facts", "k2"))

    def test_units_and_envelope(self):
        self.store.write("02269", "facts", "k", {"a": 1})
        self.store.write("01513", "facts", "k", {"a": 2})
        self.assertEqual(self.store.units(), ["01513", "02269"])
        env = self.store.envelope("02269", "facts")
        self.assertEqual(env["stage"], "facts")
        self.assertEqual(env["year"], 2025)
        self.assertIn("produced_at", env)

    def test_forget(self):
        self.store.write("02269", "facts", "k", {})
        self.assertTrue(self.store.forget("02269", "facts"))
        self.assertFalse(self.store.forget("02269", "facts"))

    def test_corrupted_raises(self):
        p = self.store.path("02269", "facts")
        p.parent.mkdir(parents=True)
        p.write_text("{ 不是 json", encoding="utf-8")
        with self.assertRaises(StoreError):
            self.store.payload("02269", "facts")

    def test_meta_roundtrip(self):
        self.store.write_meta(snapshot="abc")
        self.store.write_meta(bench_kind="auto")
        meta = self.store.read_meta()
        self.assertEqual(meta["snapshot"], "abc")
        self.assertEqual(meta["bench_kind"], "auto")


class TestTableStore(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tables = TableStore(root=Path(self.td.name), year=2025)

    def tearDown(self):
        self.td.cleanup()

    def test_roundtrip_types(self):
        rows = [{"code": "02269", "n": 1, "score": 67.5, "ok": True, "note": None},
                {"code": "01513", "n": 2, "score": None, "ok": False, "note": "缺上期"}]
        self.tables.write("t", rows, ("code", "n", "score", "ok", "note"))
        back = self.tables.read("t")
        self.assertEqual(len(back), 2)
        self.assertEqual(back[0]["code"], "02269")
        self.assertEqual(back[0]["n"], 1)
        self.assertEqual(back[0]["score"], 67.5)
        self.assertIs(back[0]["ok"], True)
        self.assertIsNone(back[1]["score"])
        self.assertEqual(back[1]["note"], "缺上期")

    def test_empty_table_keeps_columns(self):
        self.tables.write("t", [], ("a", "b"))
        self.assertTrue(self.tables.exists("t"))
        self.assertEqual(self.tables.read("t"), [])

    def test_missing_table_is_empty(self):
        self.assertEqual(self.tables.read("不存在"), [])

    def test_overwrite_is_idempotent(self):
        rows = [{"a": 1}]
        self.tables.write("t", rows, ("a",))
        first = self.tables.path("t").read_bytes()
        self.tables.write("t", rows, ("a",))
        self.assertEqual(first, self.tables.path("t").read_bytes())
        self.assertEqual(len(self.tables.read("t")), 1)

    def test_chinese_text_survives(self):
        self.tables.write("t", [{"名称": "药明生物", "note": "已核对"}], ("名称", "note"))
        self.assertEqual(self.tables.read("t")[0]["名称"], "药明生物")


class TestJsonlFallback(unittest.TestCase):
    """没装 pyarrow 的机器上，表必须一样能用（否则「可选依赖」就是空话）。"""

    def test_roundtrip_without_pyarrow(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(store, "_backend", return_value="jsonl"):
            tables = TableStore(root=Path(td), year=2025)
            rows = [{"code": "02269", "score": 67.5, "ok": True, "note": None}]
            path = tables.write("t", rows, ("code", "score", "ok", "note"))
            self.assertEqual(path.suffix, ".jsonl")
            back = tables.read("t")
            self.assertEqual(back[0]["code"], "02269")
            self.assertEqual(back[0]["score"], 67.5)
            self.assertTrue(tables.exists("t"))


if __name__ == "__main__":
    unittest.main(verbosity=2)