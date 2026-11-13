# -*- coding: utf-8 -*-
"""对标池快照：冻结 → 读回 → 分数不漂。

这条链路要证明三件事，少一件「先收集、后一起算」就不成立：
1. 池子能原样写下来、读回来（行、n、note、kind 一个不丢）；
2. 读回来的池子算出的指纹与冻结时一致（否则缓存全失效，等于每次都重算）；
3. 用快照算分与当初建池算分给出同一组数（否则「冻结」只是写了个文件）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401
from esg_s11_scorer.bench import Bench, from_rows, load_snapshot, save_snapshot
from esg_s11_scorer.config import REPORT_DIR
from esg_s11_scorer.models import PeerRow
from esg_s11_scorer.pipeline import Pipeline, run_pipeline

PDF = REPORT_DIR / "FY2025" / "02269_WUXI_BIO_ESG_FY2025.pdf"
OVERALL = ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5")


def _scores(batch) -> dict:
    return {v.code: {k: v.card.indicators[k].strict for k in OVERALL}
            for v in batch.views}


class TestSnapshotIO(unittest.TestCase):
    """纯读写：不碰 PDF、不碰管道。"""

    def _bench(self) -> Bench:
        return from_rows([
            PeerRow("01177", 2025, 11.2, 12.4, "医药", "a.pdf"),
            PeerRow("01513", 2025, 9.8, 10.1, "医药", "b.pdf"),
            PeerRow("01801", 2025, 14.0, 15.5, "医药", "c.pdf"),
        ], kind="auto", note="批内分位：n=3")

    def test_roundtrip_keeps_every_field(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snap.json"
            before = self._bench()
            save_snapshot(before, path, snapshot="abc123")
            after = load_snapshot(path)
            self.assertEqual(after.kind, before.kind)
            self.assertEqual(after.n, 3)
            self.assertEqual(after.note, before.note)
            self.assertEqual([(r.code, r.year, r.turnover, r.prior_turnover, r.industry)
                              for r in after.rows],
                             [(r.code, r.year, r.turnover, r.prior_turnover, r.industry)
                              for r in before.rows])
            for metric, higher in (("turnover", False), ("improvement", True)):
                for code in ("01177", "01513", "01801"):
                    self.assertEqual(after.percentile(code, metric, higher),
                                     before.percentile(code, metric, higher))

    def test_roundtrip_keeps_snapshot_id(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snap.json"
            pipe = Pipeline(2025, Path(td), bench_mode="auto")
            bench = self._bench()
            save_snapshot(bench, path, snapshot=pipe.snapshot_id(bench))
            self.assertEqual(pipe.snapshot_id(load_snapshot(path)),
                             pipe.snapshot_id(bench))

    def test_broken_file_is_loud(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "broken.json"
            path.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_snapshot(path)


class TestFreezeAndReplay(unittest.TestCase):
    """真跑一遍：冻结 → 用快照重算，分数必须一模一样。"""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.build = Path(cls.td.name)
        cls.kw = dict(build_dir=cls.build, bench_mode="auto", qualitative="keyword")
        cls.first = run_pipeline(PDF, 2025, None, **cls.kw)
        cls.snapshot = cls.first.snapshot
        cls.replayed = run_pipeline(PDF, 2025, None, bench_snapshot=cls.snapshot, **cls.kw)

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_freeze_file_on_disk(self):
        path = self.build / "bench" / "FY2025" / f"{self.snapshot}.json"
        self.assertTrue(path.is_file(), f"快照没落盘：{path}")
        bench = load_snapshot(path)
        self.assertEqual(bench.n, self.first.bench.n)
        self.assertEqual(bench.kind, "auto")

    def test_manifest_lists_every_input(self):
        path = self.build / "FY2025" / "manifest.csv"
        self.assertTrue(path.is_file(), "名单没写出来")
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn("02269", text)
        self.assertIn("在池", text)

    def test_replay_reuses_collection_and_keeps_scores(self):
        self.assertEqual(self.replayed.snapshot, self.snapshot, "读回后指纹变了，缓存会全失效")
        self.assertEqual(_scores(self.replayed.batch), _scores(self.first.batch))
        for name in ("parse", "facts", "qualitative"):
            self.assertEqual(self.replayed.stats[name].rebuilt, 0, f"{name} 不该重算")

    def test_freeze_only_does_not_publish(self):
        with tempfile.TemporaryDirectory() as td:
            result = run_pipeline(PDF, 2025, None, build_dir=Path(td),
                                  qualitative="keyword", freeze_bench=True)
            self.assertIsNone(result.batch, "冻结模式不该发布批次")
            self.assertTrue((Path(td) / "bench" / "FY2025" / f"{result.snapshot}.json").is_file())
            self.assertTrue((Path(td) / "FY2025" / "manifest.csv").is_file())


class TestSnapshotGuards(unittest.TestCase):
    def test_unknown_snapshot_is_loud(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                run_pipeline(PDF, 2025, None, build_dir=Path(td),
                             qualitative="keyword", bench_snapshot="0000000000000000")

    def test_snapshot_from_other_year_refused(self):
        with tempfile.TemporaryDirectory() as td:
            bench = from_rows([PeerRow("01177", 2024, 11.2, 12.4)],
                              kind="auto", note="上一年的池子")
            pipe = Pipeline(2025, Path(td), bench_mode="auto")
            snap = pipe.snapshot_id(bench)
            save_snapshot(bench, pipe.bench_snapshot_path(snap), snapshot=snap)
            with self.assertRaises(ValueError):
                run_pipeline(PDF, 2025, None, build_dir=Path(td),
                             qualitative="keyword", bench_snapshot=snap)

    def test_freeze_and_snapshot_are_exclusive(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                run_pipeline(PDF, 2025, None, build_dir=Path(td), qualitative="keyword",
                             bench_snapshot="x", freeze_bench=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)