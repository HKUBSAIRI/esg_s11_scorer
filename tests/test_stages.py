# -*- coding: utf-8 -*-
"""阶段与编排测试：产物能原样重建、缓存只在该重算时才重算。

这里补的是改造后新出现的失效方式：
- 序列化丢字段（``best`` 没了、证据句少了页号）→ 分数悄悄变
- 指纹永远不匹配 → 每次都全量重算，缓存形同虚设
- 指纹匹配过头 → 改了输入还在用旧结果（最坏的一种：看着很快，结论是上一次的）
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401
from esg_s11_scorer import stages, tables
from esg_s11_scorer.config import REPORT_DIR
from esg_s11_scorer.models import Extraction, NumFact
from esg_s11_scorer.pipeline import Pipeline, qualitative_artifact, qualitative_unit, run_pipeline
from esg_s11_scorer.store import ArtifactStore, TableStore

PDF = REPORT_DIR / "FY2025" / "02269_WUXI_BIO_ESG_FY2025.pdf"
OVERALL = ("S1.1.1", "S1.1.2", "S1.1.3", "S1.1.4", "S1.1.5")


def _scores(batch) -> dict:
    return {v.code: {k: v.card.indicators[k].strict for k in OVERALL} for v in batch.views}


class TestSerialisation(unittest.TestCase):
    """产物必须能原样还原：少一个字段，下游就是一个静默的 None。"""

    def _ex(self) -> Extraction:
        ex = Extraction(report_year=2025, denom_note="流失率＝离职人数/员工总数")
        ex.facts = [
            NumFact("turnover", 10.0, 2025, 12, "table:year-align", "…原文…", "高"),
            NumFact("turnover", 11.0, 2024, 12, "table:year-align", "…原文…", "高"),
            NumFact("turnover", 11.5, 2024, 0, "override", "人工填入：附表转置", "人工"),
        ]
        ex.best = {"turnover": {2025: ex.facts[0], 2024: ex.facts[2]}}
        ex.warnings = ["交叉校验通过"]
        return ex

    def test_extraction_roundtrip(self):
        ex = self._ex()
        back = Extraction.from_dict(ex.to_dict())
        self.assertEqual(back.report_year, 2025)
        self.assertEqual(len(back.facts), 3)
        self.assertEqual(back.denom_note, ex.denom_note)
        self.assertEqual(back.value("turnover", 2025), 10.0)
        # best 必须指回「人工填入」那一条，而不是同年另一条
        self.assertEqual(back.best["turnover"][2024].method, "override")
        self.assertEqual(back.best["turnover"][2024].value, 11.5)
        self.assertEqual(back.warnings, ["交叉校验通过"])

    def test_extraction_legacy_without_best(self):
        """老产物没有 best 字段：要能兜出来，不能整条崩掉。"""
        d = self._ex().to_dict()
        d.pop("best")
        back = Extraction.from_dict(d)
        self.assertEqual(back.value("turnover", 2025), 10.0)

    def test_doc_payload_roundtrip(self):
        payload = {"path": "x.pdf", "meta": {"sha256": "abc"},
                   "pages": [{"no": 1, "text": "第一页\n离职率 10%", "cjk": 0},
                             {"no": 2, "text": "第二页", "cjk": 0}]}
        doc = stages.doc_from_payload(payload)
        self.assertEqual(len(doc.pages), 2)
        self.assertEqual(doc.pages[0].no, 1)
        self.assertIn("离职率", doc.text)
        again = stages.doc_to_payload(doc)
        self.assertEqual(again["pages"], payload["pages"])

    def test_element_path_removed(self):
        """元素路径已删除：stages 里不该再留判定入口。"""
        self.assertFalse(hasattr(stages, "run_judge"))
        self.assertFalse(hasattr(stages, "has_voluntary"))


class TestPriorResolution(unittest.TestCase):
    """上期值三级回退：本报告附录 → 上一年报告 → N/A，且出处要说得清。"""

    def _facts(self, cur=10.0, prev=None):
        ex = Extraction(report_year=2025)
        ex.facts.append(NumFact("turnover", cur, 2025, 3, "table:year-align", "q", "高"))
        ex.best = {"turnover": {2025: ex.facts[0]}}
        if prev is not None:
            f = NumFact("turnover", prev, 2024, 3, "table:year-align", "q", "高")
            ex.facts.append(f)
            ex.best["turnover"][2024] = f
        return ex

    def test_from_own_appendix(self):
        facts = self._facts(prev=12.0)
        got = stages.resolve_prior(facts, None, 2025)
        self.assertEqual(got["prior_source"], "本报告附录")
        self.assertEqual(stages.prior_values(facts, 2025, got), (12.0, None))

    def test_from_prior_year(self):
        facts = self._facts()
        prior = Extraction(report_year=2024)
        f = NumFact("turnover", 13.0, 2024, 2, "table:year-align", "q", "高")
        prior.facts.append(f)
        prior.best = {"turnover": {2024: f}}
        got = stages.resolve_prior(facts, prior, 2025, prior_label="FY2024")
        self.assertEqual(got["prior_turnover"], 13.0)
        self.assertIn("上一年报告", got["prior_source"])
        self.assertEqual(stages.prior_values(facts, 2025, got)[0], 13.0)

    def test_missing_stays_missing(self):
        facts = self._facts()
        got = stages.resolve_prior(facts, None, 2025)
        self.assertIsNone(got["prior_turnover"])
        self.assertEqual(got["prior_source"], "缺失")

    def test_override_wins_the_label(self):
        facts = self._facts()
        f = NumFact("turnover", 11.5, 2024, 0, "override", "人工", "人工")
        facts.facts.append(f)
        facts.best["turnover"][2024] = f
        label = stages.prior_source_label(facts, "本报告附录", 2025)
        self.assertIn("人工填入", label)


class TestQualitativeArtifactPerMode(unittest.TestCase):
    """两种判档模式各存各的，不能互相顶掉。

    曾经共用一个 ``qualitative.json``：跑过 keyword（测试套件就是 keyword）之后，
    llm 的产物被覆盖，下一次真跑得重新调 25 次模型——缓存看起来在工作，实际每次都在花钱。
    """

    def test_modes_keep_separate_artifacts(self):
        from esg_s11_scorer import blind as blind_mod

        with tempfile.TemporaryDirectory() as td:
            store = ArtifactStore(root=Path(td), year=2025)
            bl = blind_mod.build("01177", filename="01177_X.pdf", extra=None)
            for mode in ("keyword", "llm"):
                qualitative_unit(store, "01177", "01177_X.pdf", mode=mode,
                                 blinder=bl, llm=None, has_turnover=False)
            for mode in ("keyword", "llm"):
                name = qualitative_artifact(mode)
                self.assertIsNotNone(store.key("01177", name), f"{name} 应存在")
            self.assertNotEqual(store.key("01177", qualitative_artifact("llm")),
                                store.key("01177", qualitative_artifact("keyword")),
                                "两种口径的指纹必须不同")
            # 再跑 keyword：应复用（键不变），且 llm 的产物没被顶掉
            llm_key = store.key("01177", qualitative_artifact("llm"))
            qualitative_unit(store, "01177", "01177_X.pdf", mode="keyword",
                             blinder=bl, llm=None, has_turnover=False)
            self.assertEqual(store.key("01177", qualitative_artifact("llm")), llm_key)


class TestPipelineCache(unittest.TestCase):
    """真跑一遍：复用要真的复用，重算要真的重算。"""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.build = Path(cls.td.name)
        cls.kw = dict(build_dir=cls.build, bench_mode="none", qualitative="keyword")

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_second_run_reuses_everything(self):
        first = run_pipeline(PDF, 2025, None, **self.kw)
        second = run_pipeline(PDF, 2025, None, **self.kw)
        for name in ("parse", "facts", "qualitative", "score"):
            self.assertEqual(second.stats[name].rebuilt, 0, f"{name} 不该重算")
            self.assertEqual(second.stats[name].reused, 1, f"{name} 应复用")
        self.assertEqual(_scores(first.batch), _scores(second.batch))

    def test_force_recomputes_and_agrees(self):
        cached = run_pipeline(PDF, 2025, None, **self.kw)
        forced = run_pipeline(PDF, 2025, None, force=True, **self.kw)
        self.assertEqual(forced.stats["parse"].rebuilt, 1)
        self.assertEqual(_scores(cached.batch), _scores(forced.batch))

    def test_override_change_only_redoes_facts(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "ovr.csv"
            csv_path.write_text("code,field,year,value,reason\n", encoding="utf-8")
            base = run_pipeline(PDF, 2025, None, override_csv=csv_path, **self.kw)
            # 产物文件会被下一次运行覆盖，先把它读出来留证
            before = [f for f in (base.artifacts.payload("02269", "facts") or {})["facts"]
                      if f["method"] == "override"]
            csv_path.write_text(
                "code,field,year,value,reason\n"
                "02269,prior_turnover,2024,11.5,测试用\n", encoding="utf-8")
            after = run_pipeline(PDF, 2025, None, override_csv=csv_path, **self.kw)
            self.assertEqual(after.stats["parse"].reused, 1, "override 改动不该重解析 PDF")
            self.assertEqual(after.stats["qualitative"].reused, 1, "override 改动不该重跑判档")
            self.assertEqual(after.stats["facts"].rebuilt, 1, "override 改动必须重抽数")
            data = after.batch.views[0].card.data
            self.assertEqual(data.prior_turnover, 11.5)
            self.assertIn("人工填入", data.prior_source)
            # 分数不一定变（B 项只看改善方向时 11.0→11.5 都是「改善」），
            # 但事实必须变：这正是「产物落地」要能证明的东西
            self.assertEqual(before, [])
            self.assertEqual(len([f for f in after.artifacts.payload("02269", "facts")["facts"]
                                  if f["method"] == "override"]), 1)

    def test_artifacts_and_tables_exist(self):
        run_pipeline(PDF, 2025, None, **self.kw)
        unit = self.build / "FY2025" / "02269"
        for stage in ("parse", "facts", "score", "prior", "qualitative-keyword"):
            self.assertTrue((unit / f"{stage}.json").is_file(), stage)
        tables_dir = TableStore(root=self.build / "tables" / "FY2025", year=2025)
        for name in ("facts", "companies", "indicators",
                     "components", "bench_rows", "bench_meta"):
            self.assertTrue(tables_dir.exists(name), name)
        for gone in ("elements", "patches"):
            self.assertFalse(tables_dir.exists(gone), f"{gone} 表不该再产出")


class TestPublishedSchema(unittest.TestCase):
    """表里的分必须等于记分卡的分：两张皮就是事故源头。"""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.build = Path(cls.td.name)
        cls.result = run_pipeline(PDF, 2025, REPORT_DIR / "FY2024",
                                  build_dir=cls.build, bench_mode="auto")
        cls.tables = TableStore(root=cls.build / "tables" / "FY2025", year=2025)

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_indicator_rows_match_card(self):
        rows = self.tables.read("indicators")
        self.assertEqual(len(rows), 5)
        card = self.result.batch.views[0].card
        for row in rows:
            ind = card.indicators[row["ind"]]
            self.assertAlmostEqual(float(row["strict"]), ind.strict, places=6)
            self.assertEqual(row["band"], ind.band)
            self.assertAlmostEqual(float(row["weight"]), ind.weight, places=6)

    def test_components_cover_every_component(self):
        rows = self.tables.read("components")
        card = self.result.batch.views[0].card
        want = sum(len(i.components) for i in card.indicators.values())
        self.assertEqual(len(rows), want)

    def test_card_rebuilt_from_rows_is_identical(self):
        payload = self.result.artifacts.payload("02269", "score")
        rebuilt = tables.card_from_payload(payload)
        card = self.result.batch.views[0].card
        self.assertEqual(rebuilt.total_strict, card.total_strict)
        self.assertEqual(list(rebuilt.indicators), list(card.indicators))
        for k in OVERALL:
            self.assertEqual([c.score for c in rebuilt.indicators[k].components],
                             [c.score for c in card.indicators[k].components])

    def test_bench_snapshot_recorded(self):
        meta = self.tables.read("bench_meta")[0]
        self.assertTrue(meta["snapshot"])
        self.assertEqual(meta["kind"], "auto")
        self.assertEqual(meta["peer_n"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)