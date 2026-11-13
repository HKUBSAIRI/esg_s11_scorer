# -*- coding: utf-8 -*-
"""三路独立判档（``ensemble``）单测：**不联网**。

这个模块的价值全在「它不会改分数」和「分歧分类没错」两件事上，所以锁的是：

- 产物只落在 ``qualitative-votes.json``，绝不写 ``qualitative-llm`` / ``score``（旁路保证）
- 一致性标签与分歧类型：同段不同档 = 判据歧义，不同段 = 证据分歧，没引用 = 召回缺陷
- 一票失败不降级成关键词兜底，而是记 ``tier=None``（否则"失败"会被读成"一致地判了某档"）
- 投票人规格与温度进指纹：换模型/换温度必须重算
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402  （触发 sys.path 注入 ESG_insight/app）
from esg_s11_scorer import ensemble, qualitative  # noqa: E402
from esg_s11_scorer.llm import LLM, LLMError  # noqa: E402
from esg_s11_scorer.qualitative import Passage, Verdict  # noqa: E402
from esg_s11_scorer.rubric import RUBRICS  # noqa: E402
from esg_s11_scorer.store import ArtifactStore  # noqa: E402

BASE = LLM(base_url="https://example.invalid/v1", model="m-0", api_key="k")


def _v(tier: int, pages=(3,), source="llm") -> Verdict:
    return Verdict(key="k", tier=tier, source=source,
                   evidence=[f"[p{p}] 原文" for p in pages])


class TestVoters(unittest.TestCase):
    def test_default_is_primary_model_three_temperatures(self):
        vs = ensemble.parse_voters("", BASE)
        self.assertEqual(len(vs), 3)
        self.assertEqual([v.llm.temperature for v in vs], [0.0, 0.7, 0.9])
        self.assertEqual({v.llm.model for v in vs}, {"m-0"})

    def test_explicit_models(self):
        vs = ensemble.parse_voters("qwen-plus@0.7, qwen-max", BASE)
        self.assertEqual([v.llm.model for v in vs], ["qwen-plus", "qwen-max"])
        self.assertEqual([v.llm.temperature for v in vs], [0.7, 0.0])
        self.assertTrue(all(v.llm.ready for v in vs), "投票人应继承 base 的 base_url/key")

    def test_duplicate_names_are_disambiguated(self):
        vs = ensemble.parse_voters("a@0.7,a@0.7,a@0.7", BASE)
        self.assertEqual(len({v.name for v in vs}), 3)

    def test_bad_temperature_is_loud(self):
        with self.assertRaises(ValueError):
            ensemble.parse_voters("m@hot", BASE)

    def test_fingerprint_tracks_temperature(self):
        a = ensemble.voters_fingerprint(ensemble.parse_voters("m@0.0", BASE))
        b = ensemble.voters_fingerprint(ensemble.parse_voters("m@0.7", BASE))
        self.assertNotEqual(a, b)


class TestAgreement(unittest.TestCase):
    def test_unanimous(self):
        self.assertEqual(ensemble.agreement([50, 50, 50]), ("3/3", 50))

    def test_majority(self):
        self.assertEqual(ensemble.agreement([50, 50, 100]), ("2/1", 50))

    def test_no_majority(self):
        self.assertEqual(ensemble.agreement([0, 50, 100]), ("no-majority", None))

    def test_failed_vote_is_incomplete_not_a_tier(self):
        self.assertEqual(ensemble.agreement([50, None, 50]), ("incomplete", None))


class TestSplitType(unittest.TestCase):
    def _votes(self, *specs):
        """specs = (tier, 引用原文, source)；原文为空时退回页码表示。"""
        return [{"tier": t, "evidence": [e] if e else [], "pages": [1], "source": s}
                for t, e, s in specs]

    def test_same_sentence_different_tier_is_rubric(self):
        v = self._votes((50, "公司已实施工时监测，覆盖 98%。", "llm"),
                        (100, "公司已实施工时监测，覆盖 98%。", "llm"),
                        (50, "公司已实施工时监测，覆盖 98%。", "llm"))
        self.assertEqual(ensemble.split_type(v), "rubric")

    def test_disjoint_sentences_is_evidence(self):
        v = self._votes((50, "公司制定工时管理制度。", "llm"),
                        (100, "全年加班审批 1,204 次。", "llm"),
                        (50, "报告期内开展休假检查。", "llm"))
        self.assertEqual(ensemble.split_type(v), "evidence")

    def test_same_page_different_sentence_is_evidence(self):
        """同一页两段不同的话：按页码比会误判成「同一段证据」，按原文比才不会。"""
        v = self._votes((50, "公司制定工时管理制度。", "llm"),
                        (100, "全年加班审批 1,204 次。", "llm"),
                        (50, "公司制定工时管理制度。", "llm"))
        for x in v:
            x["pages"] = [7]
        self.assertEqual(ensemble.split_type(v), "evidence")

    def test_partial_overlap_is_mixed(self):
        a, b, c = "A 句原文。", "B 句原文。", "C 句原文。"
        v = [{"tier": 50, "evidence": [a, b], "source": "llm"},
             {"tier": 50, "evidence": [a, b], "source": "llm"},
             {"tier": 100, "evidence": [a, c], "source": "llm"}]
        self.assertEqual(ensemble.split_type(v), "mixed")

    def test_no_citation_is_empty(self):
        v = self._votes((50, "", "llm"), (100, "", "llm"), (50, "", "llm"))
        for x in v:
            x["pages"] = []
        self.assertEqual(ensemble.split_type(v), "empty")

    def test_unanimous_and_gate(self):
        self.assertEqual(ensemble.split_type(self._votes((50, "同一句。", "llm"),
                                                         (50, "同一句。", "llm"),
                                                         (50, "同一句。", "llm"))), "none")
        self.assertEqual(ensemble.split_type(self._votes((0, "", "gate"),
                                                         (0, "", "gate"),
                                                         (0, "", "gate"))), "gate")


class TestBallot(unittest.TestCase):
    """一票是怎么拿到的：成功存页码与理由，失败存错误但**不给档**。"""

    def setUp(self):
        self.passages = [Passage(idx=1, page=7, text="公司已实施工时监测。", hits=3)]

    def _with_chat(self, fn):
        orig = qualitative.chat_json
        qualitative.chat_json = fn
        self.addCleanup(setattr, qualitative, "chat_json", orig)

    def test_success_keeps_pages_and_tier(self):
        self._with_chat(lambda *a, **k: {"tier": 100, "reason": "已实施",
                                         "evidence": [1], "missing": ""})
        v = ensemble.ballot(None, RUBRICS["S1.1.3"], ensemble.Voter("v1", BASE),
                            self.passages, None, False)
        self.assertEqual(v["tier"], 100)
        self.assertEqual(v["pages"], [7])
        self.assertEqual(v["source"], "llm")
        self.assertEqual(v["error"], "")

    def test_failure_records_error_without_tier(self):
        def boom(*a, **k):
            raise LLMError("网络断了")
        self._with_chat(boom)
        v = ensemble.ballot(None, RUBRICS["S1.1.3"], ensemble.Voter("v1", BASE),
                            self.passages, None, False)
        self.assertIsNone(v["tier"], "失败不能降级成关键词兜底，否则会被读成'一致'")
        self.assertEqual(v["source"], "error")
        self.assertIn("网络断了", v["error"])

    def test_illegal_tier_is_a_failure(self):
        self._with_chat(lambda *a, **k: {"tier": 75, "reason": "?", "evidence": []})
        v = ensemble.ballot(None, RUBRICS["S1.1.3"], ensemble.Voter("v1", BASE),
                            self.passages, None, False)
        self.assertIsNone(v["tier"])

    def test_gate_short_circuits_without_model(self):
        def should_not_run(*a, **k):
            raise AssertionError("闸门命中时不该调模型")
        self._with_chat(should_not_run)
        v = ensemble.ballot(None, RUBRICS["S1.1.1-C"], ensemble.Voter("v1", BASE),
                            self.passages, None, True)
        self.assertEqual((v["tier"], v["source"]), (0, "gate"))


class TestSidePath(unittest.TestCase):
    """旁路保证：只写 qualitative-votes，不改任何进分数的产物。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = ArtifactStore(root / "FY2025", 2025)
        self.store.write("00001", "parse", "pk",
                         {"path": "00001_X_ESG_FY2025.pdf",
                          "pages": [{"no": 1, "text": "公司建立工时管理制度并已实施监测。"}]})
        self.store.write("00001", "facts", "fk",
                         {"report_year": 2025,
                          "facts": [{"field": "turnover", "value": 12.3, "year": 2025,
                                     "page": 1, "method": "sentence", "quote": "q",
                                     "confidence": "高"}],
                          "best": {"turnover": {"2025": 0}}})
        self.tiers = {0.0: 50, 0.7: 50, 0.9: 100}
        orig = qualitative.judge_llm

        def fake(rub, passages, llm, blinder=None):
            return Verdict(key=rub.key, tier=self.tiers.get(llm.temperature, 50),
                           source="llm",
                           reason="测试", evidence=["[p1] 原文"], model=llm.model,
                           n_passages=len(passages))
        qualitative.judge_llm = fake
        self.addCleanup(setattr, qualitative, "judge_llm", orig)

    def test_collect_writes_only_the_votes_artifact(self):
        voters = ensemble.parse_voters("m@0.0,m@0.7,m@0.9", BASE)
        got = ensemble.collect(year=2025, build_dir=self.store.root.parent, voters=voters,
                               workers=1)
        files = sorted(p.name for p in self.store.unit_dir("00001").glob("*.json"))
        self.assertEqual(files[:2], ["facts.json", "parse.json"])
        self.assertEqual(len(files), 3)
        self.assertTrue(files[2].startswith("qualitative-votes"),
                        f"只该多出一份投票产物，实际 {files}")
        self.assertEqual(got["ballots"], 15, "5 项 × 3 票（本测试替换了判档，不走网络）")
        row = next(r for r in got["rows"] if r["key"] == "S1.1.3")
        self.assertEqual((row["agreement"], row["majority"], row["split_type"]),
                         ("2/1", 50, "rubric"))

    def test_second_run_reuses_cache_and_voter_change_invalidates(self):
        voters = ensemble.parse_voters("m@0.0,m@0.7,m@0.9", BASE)
        ensemble.collect(year=2025, build_dir=self.store.root.parent, voters=voters, workers=1)
        again = ensemble.collect(year=2025, build_dir=self.store.root.parent, voters=voters,
                                 workers=1)
        self.assertEqual(again["reused"], 1)
        other = ensemble.collect(year=2025, build_dir=self.store.root.parent,
                                 voters=ensemble.parse_voters("m@0.0,m@0.7,m@0.5", BASE),
                                 workers=1)
        self.assertEqual(other["reused"], 0, "换温度必须重算")

    def test_split_type_change_does_not_spend_new_calls(self):
        """票没变、只改了派生算法：复用旧票重算标签，不重调模型（这里靠假判档计数）。"""
        voters = ensemble.parse_voters("m@0.0,m@0.7,m@0.9", BASE)
        ensemble.collect(year=2025, build_dir=self.store.root.parent, voters=voters,
                         workers=1)
        art = ensemble.artifact_name(voters)
        env = self.store.envelope("00001", art)
        payload = dict(env["payload"])
        for item in payload["items"].values():          # 把派生字段改坏，模拟旧算法
            item["agreement"] = "3/3"
            item["split_type"] = "none"
        self.store.write("00001", art, env["key"], payload)   # 同一把票、同一个 key
        again = ensemble.collect(year=2025, build_dir=self.store.root.parent,
                                 voters=voters, workers=1)
        self.assertEqual(again["reused"], 1)
        row = next(r for r in again["rows"] if r["key"] == "S1.1.3")
        self.assertEqual(row["agreement"], "2/1", "派生字段应已按当前算法重算")

    def test_artifact_name_is_not_a_scoring_input(self):
        self.assertEqual(ensemble.ARTIFACT, "qualitative-votes")
        for name in ("qualitative-llm", "qualitative-keyword", "score"):
            self.assertNotEqual(ensemble.ARTIFACT, name)

    def test_two_voter_sets_do_not_overwrite_each_other(self):
        a = ensemble.parse_voters("m@0.0,m@0.7,m@0.9", BASE)
        b = ensemble.parse_voters("m@0.0,m@0.0,m@0.0", BASE)
        self.assertNotEqual(ensemble.artifact_name(a), ensemble.artifact_name(b),
                            "换投票人必须换产物名，否则上一组结论被顶掉")

    def test_gate_company_is_excluded_from_split_stats(self):
        voters = ensemble.parse_voters("m@0.0,m@0.7,m@0.9", BASE)
        self.store.write("00001", "facts", "fk2", {"report_year": 2025})
        got = ensemble.collect(year=2025, build_dir=self.store.root.parent, voters=voters,
                               workers=1, force=True)
        sumry = ensemble.summarize(got["rows"], {})
        self.assertEqual(sumry["gate"], 1, "S1.1.1-C 无流失率 → 闸门，不该算分歧")
        self.assertEqual(sumry["judgeable"], len(got["rows"]) - 1)


class TestKappa(unittest.TestCase):
    def _rows(self, tiers):
        return [{"code": str(i), "key": "S1.1.3", "split_type": "none",
                 "t1": a, "t2": b, "t3": c} for i, (a, b, c) in enumerate(tiers)]

    def test_perfect_agreement_is_one(self):
        rows = self._rows([(50, 50, 50), (100, 100, 100), (0, 0, 0)])
        self.assertAlmostEqual(ensemble.fleiss_kappa(rows), 1.0, places=6)

    def test_disagreement_is_lower(self):
        rows = self._rows([(50, 50, 50), (100, 100, 100), (0, 50, 100)])
        self.assertLess(ensemble.fleiss_kappa(rows), 1.0)

    def test_gate_items_are_excluded(self):
        rows = self._rows([(50, 50, 50), (100, 100, 100)]) + [
            {"code": "9", "key": "S1.1.1-C", "split_type": "gate", "t1": 0, "t2": 0, "t3": 0}]
        self.assertAlmostEqual(ensemble.fleiss_kappa(rows), 1.0, places=6)

    def test_single_item_is_undefined_not_a_number(self):
        """只有一项时 κ 在数学上无定义（p_e=1）：返回 None，不能编一个 1.0 出来。"""
        self.assertIsNone(ensemble.fleiss_kappa(self._rows([(50, 50, 50)])))


class TestCompare(unittest.TestCase):
    def test_overlap_between_two_runs(self):
        base = [{"code": "1", "key": "S1.1.3", "agreement": "2/1", "split_type": "rubric"},
                {"code": "1", "key": "S1.1.4", "agreement": "2/1", "split_type": "rubric"},
                {"code": "2", "key": "S1.1.5", "agreement": "3/3", "split_type": "none"}]
        other = [{"code": "1", "key": "S1.1.3", "agreement": "2/1", "split_type": "mixed"},
                 {"code": "1", "key": "S1.1.4", "agreement": "3/3", "split_type": "none"},
                 {"code": "2", "key": "S1.1.5", "agreement": "2/1", "split_type": "evidence"}]
        c = ensemble.compare("本次", base, [("另一套", other)])[0]
        self.assertEqual(c["split"], 2)
        self.assertEqual((c["overlap"], c["only_base"], c["only_other"]), (1, 1, 1))
        self.assertAlmostEqual(c["jaccard"], 1 / 3, places=3)


class TestImpact(unittest.TestCase):
    """复核优先级 = 分歧 × 改档影响。权重来自 config.SPECS，不是手写数字。"""

    def test_factors_follow_specs_weights(self):
        f = ensemble.FACTORS
        self.assertAlmostEqual(f["S1.1.1-C"], 0.25 * 0.3, places=6)   # 加权项里的 30%
        self.assertAlmostEqual(f["S1.1.2-C"], 0.30 * 0.3, places=6)
        self.assertAlmostEqual(f["S1.1.3"], 0.15, places=6)           # tiered 整项
        self.assertAlmostEqual(f["S1.1.4"], 0.15, places=6)
        self.assertAlmostEqual(f["S1.1.5"], 0.15, places=6)

    def test_half_range_flip_costs_3_75_or_7_5(self):
        self.assertEqual(ensemble.total_delta("S1.1.1-C", 50), 3.75)
        self.assertEqual(ensemble.total_delta("S1.1.2-C", 50), 4.5)
        self.assertEqual(ensemble.total_delta("S1.1.5", 50), 7.5)

    def test_span_ignores_failed_votes(self):
        v = [{"tier": 50}, {"tier": 100}, {"tier": None}]
        self.assertEqual(ensemble.tier_span(v), 50)

    def test_expected_impact_ranks_by_rate_times_delta(self):
        # 同样 7 项分裂：S1.1.5 每项 7.5 分，S1.1.1-C 每项 3.75 分 → 前者优先
        rows = []
        for i in range(7):
            rows.append({"code": f"c{i}", "key": "S1.1.5", "agreement": "2/1",
                         "split_type": "rubric", "majority": 100, "n_passages": 24,
                         "votes": 3, "t1": 100, "t2": 50, "t3": 100,
                         "factor": ensemble.FACTORS["S1.1.5"], "delta": 7.5})
        for i in range(7):
            rows.append({"code": f"c{i}", "key": "S1.1.1-C", "agreement": "2/1",
                         "split_type": "rubric", "majority": 100, "n_passages": 24,
                         "votes": 3, "t1": 100, "t2": 50, "t3": 100,
                         "factor": ensemble.FACTORS["S1.1.1-C"], "delta": 3.75})
        s = ensemble.summarize(rows, {})
        self.assertGreater(s["per_key"]["S1.1.5"]["exp"],
                           s["per_key"]["S1.1.1-C"]["exp"])
        self.assertEqual(s["delta_total"], 7 * 7.5 + 7 * 3.75)
        self.assertEqual(s["first80"], 10,
                         "7×7.5 先吃掉 67%，再补 3 个 3.75 到 81% → 前 10 项")


class TestMinoritySignals(unittest.TestCase):
    """两个"少数派"不能混：第一票 vs 现行交付档位。混了会把旧口径的漂移读成分歧。"""

    def _row(self, t1, t2, t3, majority, agreement="2/1", key="S1.1.5"):
        return {"code": "c", "key": key, "agreement": agreement, "split_type": "mixed",
                "majority": majority, "n_passages": 24, "votes": 3,
                "t1": t1, "t2": t2, "t3": t3, "factor": 0.15, "delta": 7.5}

    def test_t1_and_delivery_minority_are_counted_separately(self):
        # 第一票 100 与多数派 50 相反；交付档位也是 100（=第一票）→ 只有 t1_minority
        rows = [self._row(100, 50, 50, 50)]
        s = ensemble.summarize(rows, {("c", "S1.1.5"): 100})
        self.assertEqual(s["t1_minority"], 1)
        self.assertEqual(s["minority"], 1)
        self.assertEqual(s["drift"], 0, "交付档位与第一票一致，不算漂移")

    def test_drift_when_delivery_differs_from_today(self):
        # 交付 50，今天第一票 100（多数派也是 100）→ 红框不该画，但漂移要报
        rows = [self._row(100, 100, 50, 100)]
        s = ensemble.summarize(rows, {("c", "S1.1.5"): 50})
        self.assertEqual(s["t1_minority"], 0, "第一票在多数派里 → 不画红框")
        self.assertEqual(s["minority"], 1, "交付档位 50 与多数派 100 相反")
        self.assertEqual(s["drift"], 1)


class TestReport(unittest.TestCase):
    def test_report_has_acceptance_table_and_split_list(self):
        rows = [{"code": "00001", "key": "S1.1.3", "agreement": "2/1", "split_type": "rubric",
                 "majority": 50, "n_passages": 24, "votes": 3,
                 "t1": 50, "t2": 50, "t3": 100, "p1": "3", "p2": "3", "p3": "4",
                 "r1": "", "r2": "", "r3": ""},
                {"code": "00002", "key": "S1.1.4", "agreement": "3/3", "split_type": "none",
                 "majority": 100, "n_passages": 24, "votes": 3,
                 "t1": 100, "t2": 100, "t3": 100, "p1": "5", "p2": "5", "p3": "5",
                 "r1": "", "r2": "", "r3": ""}]
        bundle = {"year": 2025, "voters": [{"name": "m@0.0"}, {"name": "m@0.7"},
                                           {"name": "m@0.9"}],
                  "companies": ["00001", "00002"], "ballots": 15, "reused": 0,
                  "rows": rows, "primary": {("00001", "S1.1.3"): 100},
                  "summary": ensemble.summarize(rows, {("00001", "S1.1.3"): 100})}
        with tempfile.TemporaryDirectory() as td:
            md, csv_path, js = ensemble.write_report(bundle, out_dir=td)
            text = md.read_text(encoding="utf-8")
            self.assertIn("验收表", text)
            self.assertIn("分裂率", text)
            self.assertIn("S1.1.3", text.split("待复核清单")[1],
                          "分裂项要出现在复核清单里")
            lines = csv_path.read_text(encoding="utf-8-sig").strip().splitlines()
            self.assertEqual(len(lines), 3)          # 表头 + 两行
            payload = json.loads(js.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["split"], 1)
            self.assertEqual(payload["summary"]["minority"], 1,
                             "主模型 100 与多数派 50 不同 → 少数派")


if __name__ == "__main__":
    unittest.main(verbosity=2)