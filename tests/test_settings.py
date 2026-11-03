# -*- coding: utf-8 -*-
"""配置层测试：.env 解析、优先级、相对路径解析、模板与变量名同步。

这层不值得「跑一遍看看」：它的失效方式是**静默指错目录**（跑出来的报告对了，
但读的是上一版 PDF），所以每条规则都钉死。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401
from esg_s11_scorer import settings as S


class TestParseEnvFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_env_fixture.env"

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def _write(self, text: str) -> dict[str, str]:
        self.tmp.write_text(text, encoding="utf-8")
        return S.parse_env_file(self.tmp)

    def test_basic_kv_and_comment(self):
        got = self._write("# 注释\n\nESG_YEAR=2024\n  ESG_BENCH = none  \n")
        self.assertEqual(got, {"ESG_YEAR": "2024", "ESG_BENCH": "none"})

    def test_quotes_and_inline_comment(self):
        got = self._write(
            'ESG_PDF_DIR="D:/报告 2025/FY2025"  # 带空格\n'
            "ESG_OUT_DIR='out dir'\n"
            "ESG_PROJECT_ROOT=C:/a/b   # 行内注释\n")
        self.assertEqual(got["ESG_PDF_DIR"], "D:/报告 2025/FY2025")
        self.assertEqual(got["ESG_OUT_DIR"], "out dir")
        self.assertEqual(got["ESG_PROJECT_ROOT"], "C:/a/b")

    def test_windows_backslashes_kept_literal(self):
        got = self._write(r"ESG_SAMPLE_PDF_DIR=C:\Users\Alpha\样本" + "\n")
        self.assertEqual(got["ESG_SAMPLE_PDF_DIR"], r"C:\Users\Alpha\样本")

    def test_export_prefix_and_bom(self):
        self.tmp.write_text("export ESG_YEAR=2023\n", encoding="utf-8-sig")
        self.assertEqual(S.parse_env_file(self.tmp), {"ESG_YEAR": "2023"})

    def test_garbage_lines_ignored(self):
        got = self._write("不是配置\n=无键\n#\nESG_YEAR=2025\n")
        self.assertEqual(got, {"ESG_YEAR": "2025"})


class TestToPath(unittest.TestCase):
    def test_relative_joins_base(self):
        base = Path("C:/proj")
        self.assertEqual(S.to_path("50-应用/a", base), Path("C:/proj/50-应用/a"))

    def test_absolute_untouched(self):
        self.assertEqual(S.to_path("D:/x/y", Path("C:/proj")), Path("D:/x/y"))

    def test_env_var_expanded(self):
        with mock.patch.dict(os.environ, {"MY_DIR": "D:/data"}):
            self.assertEqual(S.to_path("$MY_DIR/fy", Path("C:/p")), Path("D:/data/fy"))


class TestBuild(unittest.TestCase):
    """用「环境变量 + 临时 .env」直接驱动 build()，不碰真实文件系统结构。"""

    KEYS = ("ESG_YEAR", "ESG_PDF_DIR", "ESG_OUT_DIR", "ESG_PRIOR_DIR", "ESG_BENCH",
            "ESG_BENCH_CSV", "ESG_OVERRIDE_CSV", "ESG_PROJECT_ROOT", "ESG_SAMPLE_PDF_DIR",
            "ESG_VERDICTS", "ESG_REPORT_ROOT")

    def setUp(self):
        self.tmpdir = Path(__file__).resolve().parent / "_env_case"
        self.tmpdir.mkdir(exist_ok=True)
        self.env = self.tmpdir / ".env"
        patcher = mock.patch.dict(os.environ, {"ESG_PROJECT_ROOT": str(self.tmpdir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in self.KEYS:                       # 别被本机真实环境变量干扰
            if k != "ESG_PROJECT_ROOT":
                os.environ.pop(k, None)

    def tearDown(self):
        self.env.unlink(missing_ok=True)
        try:
            self.tmpdir.rmdir()
        except OSError:
            pass

    def _build(self, text: str | None = None, env_file: str | None = None):
        if text is not None:
            self.env.write_text(text, encoding="utf-8")
        return S.build(env_file or (str(self.env) if self.env.exists() else None))

    def test_defaults_follow_project_root_and_year(self):
        s = self._build("ESG_YEAR=2024\n")
        self.assertEqual(s.year, 2024)
        self.assertEqual(s.pdf_dir, self.tmpdir / "50-应用/good_sample/分析指标/ESG报告/FY2024")
        self.assertEqual(s.out_dir, self.tmpdir / "50-应用/good_sample/分析指标/打分结果")
        self.assertEqual(s.insight_app, self.tmpdir / "50-应用/ESG_insight/app")
        self.assertIsNone(s.prior_dir)            # 未配 = 交给 pipeline 自动找同级 FY-1

    def test_env_var_beats_dotenv(self):
        self.env.write_text("ESG_YEAR=2020\nESG_BENCH=none\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"ESG_YEAR": "2026"}):
            s = S.build(str(self.env))
        self.assertEqual(s.year, 2026)            # 进程环境变量 > .env
        self.assertEqual(s.bench_mode, "none")    # .env > 默认
        self.assertIn("环境变量", s.sources["ESG_YEAR"])
        self.assertIn(".env", s.sources["ESG_BENCH"])

    def test_relative_pdf_dir_resolves_against_project_root(self):
        s = self._build("ESG_PDF_DIR=data/FY2025\nESG_OUT_DIR=out\n")
        self.assertEqual(s.pdf_dir, self.tmpdir / "data/FY2025")
        self.assertEqual(s.out_dir, self.tmpdir / "out")

    def test_explicit_pdf_dir_wins_over_report_root(self):
        s = self._build("ESG_REPORT_ROOT=根\nESG_PDF_DIR=D:/别处\n")
        self.assertEqual(s.pdf_dir, Path("D:/别处"))
        self.assertEqual(s.report_root, self.tmpdir / "根")

    def test_bad_year_falls_back_with_warning(self):
        s = self._build("ESG_YEAR=明年\n")
        self.assertEqual(s.year, S.DEFAULT_YEAR)
        self.assertTrue(any("ESG_YEAR" in w for w in s.warnings))

    def test_bad_bench_falls_back_with_warning(self):
        s = self._build("ESG_BENCH=cs v\n")
        self.assertEqual(s.bench_mode, "auto")
        self.assertTrue(any("ESG_BENCH" in w for w in s.warnings))

    def test_problems_names_the_variable(self):
        s = self._build("ESG_PDF_DIR=不存在的目录\n")
        msgs = s.problems()
        self.assertTrue(msgs)
        self.assertIn("ESG_PDF_DIR", msgs[0])

    def test_rows_cover_every_variable(self):
        s = self._build("")
        self.assertEqual([r[0] for r in s.rows()], list(S.iter_env_names()))


class TestTemplateInSync(unittest.TestCase):
    """.env.example 漏一个变量，用户就永远不知道它能配。"""

    def test_every_variable_documented(self):
        text = (S.PKG_DIR / ".env.example").read_text(encoding="utf-8")
        missing = [n for n in S.iter_env_names() if n not in text]
        self.assertEqual(missing, [], msg=f".env.example 未提及：{missing}")


class TestPackageWiring(unittest.TestCase):
    def test_config_constants_come_from_settings(self):
        from esg_s11_scorer import config
        s = S.get_settings()
        self.assertEqual(config.REPORT_DIR, s.report_root)
        self.assertEqual(config.DEFAULT_OUT, s.out_dir)
        self.assertEqual(config.DEFAULT_PDF_DIR, s.pdf_dir)
        self.assertEqual(config.INSIGHT_APP, s.insight_app)
        self.assertEqual(config.DEFAULT_YEAR, s.year)

    def test_insight_app_on_sys_path(self):
        """config / pipeline 靠 sys.path 才 import 得到 s11_text。"""
        self.assertIn(str(S.get_settings().insight_app), sys.path)


if __name__ == "__main__":
    unittest.main(verbosity=2)