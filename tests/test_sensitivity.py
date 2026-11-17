# -*- coding: utf-8 -*-
"""细则影响分析（只读）单测：角色、反事实、旧产物跳过。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from esg_s11_scorer import sensitivity as sen  # noqa: E402
from esg_s11_scorer.rubric import RUBRICS  # noqa: E402


class TestRoles(unittest.TestCase):
    def test_required_any_and_support(self):
        role = sen.roles(RUBRICS["S1.1.2-C"])
        self.assertEqual(role["P1"], sen.ROLE_ALL)
        self.assertEqual(role["P2"], sen.ROLE_ALL)
        self.assertEqual(role["P4"], sen.ROLE_ANY)

    def test_event_groups(self):
        role = sen.roles(RUBRICS["S1.1.5"])
        self.assertEqual(role["D1"], sen.ROLE_ALL)
        self.assertEqual(role["D7"], sen.ROLE_ANY)
        self.assertEqual(role["D10"], sen.ROLE_EVENT)

    def test_optional_groups_are_support(self):
        role = sen.roles(RUBRICS["S1.1.4"])
        self.assertEqual(role["W4"], sen.ROLE_ALL)
        self.assertEqual(role["W1"], sen.ROLE_ANY)
        self.assertEqual(role["W5"], sen.ROLE_SUPPORT)
        self.assertEqual(role["W6"], sen.ROLE_SUPPORT)


class TestFlips(unittest.TestCase):
    def _run(self, key, groups, event=None):
        return sen.analyse(RUBRICS[key], groups, event)

    def test_alternative_path_drops_when_only_one(self):
        res = self._run("S1.1.2-C",
                        {"P1": "yes", "P2": "yes", "P3": "no", "P4": "yes",
                         "P5": "no", "P6": "no"})
        self.assertTrue(res.base_ok)
        self.assertIn("P4", res.drop)
        self.assertIn("P1", res.drop)

    def test_missing_any_group_lists_all_candidates(self):
        res = self._run("S1.1.2-C", {"P1": "yes", "P2": "yes"})
        self.assertFalse(res.base_ok)
        self.assertEqual(sorted(res.unlock), ["P3", "P4", "P5", "P6"])

    def test_support_group_never_drops(self):
        res = self._run("S1.1.4", {"W4": "yes", "W1": "yes", "W5": "no"})
        self.assertTrue(res.base_ok)
        self.assertEqual(res.drop, ["W1", "W4"])
        self.assertNotIn("W5", res.drop)

    def test_event_needs_last_group(self):
        groups = {"D1": "yes", "D2": "yes", "D5": "yes", "D7": "yes",
                  "D8": "yes", "D9": "yes", "D10": "no"}
        res = self._run("S1.1.5", groups, event=True)
        self.assertFalse(res.base_ok)
        self.assertEqual(res.unlock, ["D10"])
        self.assertIn("D10", res.gap)

    def test_no_event_ignores_event_groups(self):
        res = self._run("S1.1.5", {"D1": "yes", "D2": "yes", "D5": "yes"},
                        event=False)
        self.assertTrue(res.base_ok)


class TestImpactPoints(unittest.TestCase):
    def test_weighted_part_is_15(self):
        self.assertEqual(sen.impact_points("S1.1.1-C"), 15)
        self.assertEqual(sen.impact_points("S1.1.2-C"), 15)

    def test_tiered_is_50(self):
        for key in ("S1.1.3", "S1.1.4", "S1.1.5"):
            self.assertEqual(sen.impact_points(key), 50)


class TestLoad(unittest.TestCase):
    def test_skips_old_artifact_without_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "FY2025" / "00001"
            d.mkdir(parents=True)
            (d / "qualitative-llm.json").write_text(json.dumps({
                "payload": {"verdicts": {
                    "S1.1.2-C": {"key": "S1.1.2-C", "tier": 50,
                                 "groups": {"P1": "yes"}},
                    "S1.1.3": {"key": "S1.1.3", "tier": 50},
                    "S1.1.1-C": {"key": "S1.1.1-C", "tier": 0, "source": "gate"},
                }}}), encoding="utf-8")
            data, stale = sen.load([tmp], 2025, "llm")
            self.assertEqual(list(data["00001"]), ["S1.1.2-C"])
            self.assertEqual(stale, ["00001/S1.1.3"])

    def test_markdown_has_two_sections(self):
        data = {"00001": {"S1.1.2-C": {
            "key": "S1.1.2-C", "tier": 50,
            "groups": {"P1": "yes", "P2": "yes"}}}}
        text = sen.to_markdown(sen.summarize(data, [], "llm"))
        self.assertIn("## 一、每条细则的作用", text)
        self.assertIn("## 二、卡在 100 分以下的公司", text)
        self.assertIn("P3", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
