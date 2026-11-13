# -*- coding: utf-8 -*-
"""判档分歧页（``disputes``）单测：**不联网、不真解析 PDF**。

锁四件事：

- 文件名 → 公司代码/简称的映射（页头、总汇表都靠它）
- 一份报告 → 五个指标 × N 票的结构，且闸门（未披露离职率）在页面上是「判 0」而不是「分歧」
- 任务队列：状态从 queued 走到 done，错误落到 error（前端只显示一句话）
- 总汇：读 ``reports/ensemble.json``，缺文件时给提示而不是抛错
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from esg_s11_scorer import disputes, ensemble  # noqa: E402
from esg_s11_scorer.llm import LLM  # noqa: E402
from esg_s11_scorer.models import Extraction, NumFact  # noqa: E402
from s11_text import Doc, Page  # noqa: E402

BASE = LLM(base_url="https://example.invalid/v1", model="m-0", api_key="k")


def _doc() -> Doc:
    return Doc(path="t.pdf", pages=[Page(no=1, text="公司建立工时管理制度并已实施监测。", cjk=0)])


def _ex(turnover: float | None) -> Extraction:
    facts = [] if turnover is None else [
        NumFact(field="turnover", value=turnover, year=2025, page=1, method="sentence",
                quote="q", confidence="高")]
    return Extraction(report_year=2025, facts=facts,
                      best=({"turnover": {2025: facts[0]}} if facts else {}))


class _Fake:
    """替换「解析 / 抽数 / 投票」三处 IO，让整条链路离线可跑。"""

    def __init__(self, tiers: dict[float, int] | None = None,
                 turnover: float | None = 12.3, ballot_fail=False):
        self.tiers = tiers or {0.0: 50, 0.7: 50, 0.9: 100}
        self.turnover = turnover
        self.ballot_fail = ballot_fail

    def __enter__(self):
        self._orig = (disputes.load_pdf, disputes.extract, ensemble.ballot)
        disputes.load_pdf = lambda payload, name="": _doc()
        disputes.extract = lambda doc, filename="": _ex(self.turnover)

        def ballot(doc, rub, voter, passages, blinder, gate_hit):
            if self.ballot_fail:
                raise RuntimeError("模拟崩了")
            tier = 0 if gate_hit else self.tiers.get(voter.llm.temperature, 50)
            return {"voter": voter.name, "model": voter.llm.model,
                    "temperature": voter.llm.temperature, "tier": tier,
                    "source": "gate" if gate_hit else "llm", "reason": "测试",
                    "missing": "", "pages": [1], "evidence": ["[p1] 原文"], "error": ""}

        ensemble.ballot = ballot
        return self

    def __exit__(self, *exc):
        disputes.load_pdf, disputes.extract, ensemble.ballot = self._orig
        return False


class TestIdentity(unittest.TestCase):
    def test_code_and_chinese_name(self):
        code, name = disputes._identity("06821_ASYMCHEM_ESG_FY2025.pdf")
        self.assertEqual(code, "06821")
        self.assertEqual(name, "凯莱英")

    def test_unknown_code_falls_back_to_filename(self):
        code, name = disputes._identity("MY_REPORT.pdf")
        self.assertEqual(code, "")
        self.assertIn("MY REPORT", name)


class TestJudgeDocument(unittest.TestCase):
    voters = ensemble.parse_voters("m@0.0,m@0.7,m@0.9", BASE)

    def test_five_indicators_with_three_votes(self):
        with _Fake():
            out = disputes.judge_document(b"pdf", "06821_X_ESG_FY2025.pdf",
                                          voters=self.voters, llm=BASE)
        self.assertEqual(len(out["indicators"]), 5)
        self.assertEqual({i["key"] for i in out["indicators"]},
                         {"S1.1.1-C", "S1.1.2-C", "S1.1.3", "S1.1.4", "S1.1.5"})
        for it in out["indicators"]:
            self.assertEqual(len(it["votes"]), 3)
            self.assertIn(it["agreement"], {"3/3", "2/1", "no-majority"})
        self.assertEqual(out["company"]["code"], "06821")
        self.assertEqual(out["summary"]["split"], 5, "50/50/100 → 五项全是 2/1")
        self.assertEqual(out["summary"]["judgeable"], 5)

    def test_gate_is_not_a_disagreement(self):
        with _Fake(turnover=None):
            out = disputes.judge_document(b"pdf", "00013_X_ESG_FY2025.pdf",
                                          voters=self.voters, llm=BASE)
        gate = next(i for i in out["indicators"] if i["key"] == "S1.1.1-C")
        self.assertEqual(gate["split_type"], "gate")
        self.assertFalse(gate["split"])
        self.assertEqual(gate["votes"][0]["tier"], 0)
        self.assertEqual(out["summary"]["gate"], 1)
        self.assertEqual(out["summary"]["judgeable"], 4)
        self.assertFalse(out["file"]["has_turnover"])

    def test_progress_is_reported(self):
        seen: list[int] = []
        with _Fake():
            disputes.judge_document(b"pdf", "06821_X.pdf", voters=self.voters,
                                    llm=BASE, progress=lambda p, s: seen.append(p))
        self.assertEqual(seen[-1], 100)
        self.assertTrue(all(a <= b for a, b in zip(seen, seen[1:])), "进度不能倒退")


class TestJobs(unittest.TestCase):
    voters = ensemble.parse_voters("m@0.0,m@0.7,m@0.9", BASE)

    def _wait(self, jid: str) -> dict:
        for _ in range(200):
            st = disputes.job_state(jid)
            if st.get("status") in ("done", "error"):
                return st
            time.sleep(0.05)
        raise AssertionError("任务没有结束")

    def test_job_runs_and_returns_result(self):
        with _Fake():
            jid = disputes.start_job(b"pdf", "06821_X.pdf")
            st = self._wait(jid)
        self.assertEqual(st["status"], "done")
        self.assertEqual(len(st["result"]["indicators"]), 5)

    def test_job_error_is_reported_once(self):
        with _Fake(ballot_fail=True):
            jid = disputes.start_job(b"pdf", "06821_X.pdf")
            st = self._wait(jid)
        self.assertEqual(st["status"], "error")
        self.assertIn("出错了", st["error"])
        self.assertNotIn("result", st)

    def test_unknown_job(self):
        self.assertIn("没有这个任务", disputes.job_state("nope")["error"])


class TestSummary(unittest.TestCase):
    def _write(self, tmp: Path, rows: list[dict]) -> Path:
        p = tmp / "ensemble.json"
        p.write_text(json.dumps({"year": 2025, "voters": [{"name": "m@0.0"}],
                                 "summary": {"judgeable": len(rows),
                                             "split": sum(1 for r in rows
                                                          if r["agreement"] != "3/3")},
                                 "rows": rows}, ensure_ascii=False), encoding="utf-8")
        return p

    def test_reads_rows_and_counts_per_company(self):
        rows = [{"code": "00013", "key": "S1.1.5", "agreement": "2/1",
                 "split_type": "mixed", "majority": 50, "t1": 100, "t2": 50, "t3": 50},
                {"code": "00013", "key": "S1.1.3", "agreement": "3/3",
                 "split_type": "none", "majority": 50, "t1": 50, "t2": 50, "t3": 50},
                {"code": "01177", "key": "S1.1.4", "agreement": "3/3",
                 "split_type": "none", "majority": 100, "t1": 100, "t2": 100, "t3": 100}]
        with tempfile.TemporaryDirectory() as td:
            out = disputes.load_summary(path=self._write(Path(td), rows))
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["tables"]["rows"]), 3)
        by = {c["code"]: c for c in out["tables"]["companies"]}
        self.assertEqual(by["00013"]["split"], 1)
        self.assertEqual(by["01177"]["split"], 0)
        self.assertEqual(out["tables"]["indicators"]["S1.1.5"]["ind"], "S1.1.5")
        self.assertEqual(out["tables"]["names"]["00013"], "和黄医药")
        # 矩阵格子点开要显示三家理由与引用：这两列必须留在总汇里，别被裁剪掉
        row = out["tables"]["rows"][0]
        for k in ("t1", "t2", "t3", "r1", "r2", "r3", "e1", "e2", "e3"):
            self.assertIn(k, row, f"总汇缺少 {k}，矩阵详情会变空")

    def test_missing_file_gives_hint(self):
        with tempfile.TemporaryDirectory() as td:
            out = disputes.load_summary(path=Path(td) / "nope.json")
        self.assertFalse(out["ok"])
        self.assertIn("esg_s11_scorer.ensemble", out["hint"])


if __name__ == "__main__":
    unittest.main(verbosity=2)