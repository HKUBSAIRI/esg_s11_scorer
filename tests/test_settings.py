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


class TestExternalResources(unittest.TestCase):
    """MySQL / GPU 是「备用」配置：没配不算错，半配要出声，取值接口要能直接喂给客户端。"""

    def setUp(self):
        self.tmpdir = Path(__file__).resolve().parent / "_env_case_ext"
        self.tmpdir.mkdir(exist_ok=True)
        self.env = self.tmpdir / ".env"
        patcher = mock.patch.dict(os.environ, {"ESG_PROJECT_ROOT": str(self.tmpdir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in S.iter_env_names():              # 别被本机真实环境变量干扰
            if k != "ESG_PROJECT_ROOT":
                os.environ.pop(k, None)

    def tearDown(self):
        self.env.unlink(missing_ok=True)
        try:
            self.tmpdir.rmdir()
        except OSError:
            pass

    def _build(self, text: str = ""):
        self.env.write_text(text, encoding="utf-8")
        return S.build(str(self.env))

    def test_unset_is_not_an_error(self):
        """打分链路不读库，所以「一项都没配」必须是干净状态，不能刷警告。"""
        s = self._build("")
        self.assertFalse(s.db_ready)
        self.assertEqual(s.gpu_account_names(), ())
        self.assertEqual(s.warnings, ())

    def test_db_params_feeds_pymysql(self):
        s = self._build("ESG_DB_HOST=h\nESG_DB_USER=u\nESG_DB_PASSWORD=p\nESG_DB_NAME=d\n")
        self.assertTrue(s.db_ready)
        self.assertEqual(s.db_params(), {
            "host": "h", "port": 3306, "user": "u",
            "password": "p", "database": "d", "charset": "utf8mb4"})

    def test_partial_db_warns_and_refuses(self):
        s = self._build("ESG_DB_HOST=h\n")
        self.assertFalse(s.db_ready)
        with self.assertRaises(ValueError):
            s.db_params()
        self.assertTrue(any("MySQL" in w for w in s.warnings))

    def test_bad_db_port_falls_back(self):
        s = self._build("ESG_DB_PORT=三千\n")
        self.assertEqual(s.db_port, S.DEFAULT_DB_PORT)
        self.assertTrue(any("ESG_DB_PORT" in w for w in s.warnings))

    def test_gpu_account_default_and_named(self):
        s = self._build(
            "ESG_GPU_HOST=10.0.0.1\nESG_GPU_DEFAULT=zixin\n"
            "ESG_GPU_ZIXIN_USER=z\nESG_GPU_ZIXIN_PASSWORD=p\nESG_GPU_ZIXIN_DIR=/w\n"
            "ESG_GPU_KYRIE_USER=k\nESG_GPU_KYRIE_PASSWORD=q\nESG_GPU_KYRIE_DIR=/e\n")
        self.assertEqual(s.gpu_account_names(), ("kyrie", "zixin"))
        self.assertEqual(s.gpu_account(), {"host": "10.0.0.1", "port": 22,
                                           "username": "z", "password": "p", "dir": "/w"})
        self.assertEqual(s.gpu_account("kyrie")["dir"], "/e")
        with self.assertRaises(ValueError):
            s.gpu_account("nobody")

    def test_bad_gpu_default_warns(self):
        s = self._build("ESG_GPU_DEFAULT=nobody\n")
        self.assertEqual(s.gpu_default, "")
        self.assertTrue(any("ESG_GPU_DEFAULT" in w for w in s.warnings))

    def test_short_password_not_even_partly_printed(self):
        """``45176_`` 这种前缀已经是大半个口令了，--show-config 不该露出来。"""
        pwd = "45176_CKnfP"
        s = self._build(f"ESG_DB_PASSWORD={pwd}\n")
        shown = {r[0]: r[1] for r in s.rows()}["ESG_DB_PASSWORD"]
        self.assertNotIn("45176", shown)
        self.assertIn(f"共 {len(pwd)} 位", shown)


class TestYearAwarePaths(unittest.TestCase):
    """``{year}`` 占位符 + 「年度 vs 目录名」自检 —— 换年度时不许静默串档。"""

    def setUp(self):
        self.tmpdir = Path(__file__).resolve().parent / "_env_case_year"
        self.tmpdir.mkdir(exist_ok=True)
        self.env = self.tmpdir / ".env"
        patcher = mock.patch.dict(os.environ, {"ESG_PROJECT_ROOT": str(self.tmpdir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in S.iter_env_names():
            if k != "ESG_PROJECT_ROOT":
                os.environ.pop(k, None)

    def tearDown(self):
        self.env.unlink(missing_ok=True)
        try:
            self.tmpdir.rmdir()
        except OSError:
            pass

    def _build(self, text: str = ""):
        self.env.write_text(text, encoding="utf-8")
        return S.build(str(self.env))

    def test_expand_year_tokens(self):
        self.assertEqual(S.expand_year("a/FY{year}", 2026), "a/FY2026")
        self.assertEqual(S.expand_year("a/FY{year-1}", 2026), "a/FY2025")
        self.assertEqual(S.expand_year("{year}-{year-1}-{year+1}", 2026), "2026-2025-2027")
        self.assertEqual(S.expand_year("无占位符", 2026), "无占位符")

    def test_token_makes_every_io_path_follow_the_year(self):
        s = self._build(
            "ESG_YEAR=2026\n"
            "ESG_REPORT_ROOT=数据/报告\n"
            "ESG_PDF_DIR=数据/报告/FY{year}\n"
            "ESG_PRIOR_DIR=数据/报告/FY{year-1}\n"
            "ESG_OVERRIDE_CSV=结果/overrides_FY{year}.csv\n")
        self.assertEqual(s.pdf_dir, self.tmpdir / "数据/报告/FY2026")
        self.assertEqual(s.prior_dir, self.tmpdir / "数据/报告/FY2025")
        self.assertEqual(s.override_csv, self.tmpdir / "结果/overrides_FY2026.csv")
        self.assertEqual(s.warnings, ())          # 年度与目录名一致，不该有警告

    def test_year_mismatch_warns(self):
        """最常见的串档写法：ESG_YEAR 改了，路径还写着 FY2025。"""
        s = self._build("ESG_YEAR=2026\n"
                        "ESG_PDF_DIR=数据/FY2025\n"
                        "ESG_PRIOR_DIR=数据/FY2024\n")
        self.assertTrue(any("ESG_PDF_DIR" in w and "FY2026" in w for w in s.warnings))
        self.assertTrue(any("ESG_PRIOR_DIR" in w and "FY2025" in w for w in s.warnings))

    def test_optional_file_absent_is_not_an_error(self):
        """兜底表/别名表今年可能还没有：要显示，但不能算错误。"""
        s = self._build("ESG_OVERRIDE_CSV=结果/overrides_FY{year}.csv\n")
        states = {r[0]: r[3] for r in s.rows()}
        self.assertEqual(states["ESG_OVERRIDE_CSV"], "不存在（可选）")
        self.assertFalse(any("ESG_OVERRIDE_CSV" in m for m in s.problems()))

    def test_output_and_build_are_not_path_problems(self):
        """输出/中间态目录不存在属正常（运行时创建），不该报错。"""
        s = self._build("ESG_OUT_DIR=还没建的目录\nESG_BUILD_DIR=也没建\n")
        msgs = s.problems()
        self.assertFalse([m for m in msgs if "ESG_OUT_DIR" in m or "ESG_BUILD_DIR" in m])


if __name__ == "__main__":
    unittest.main(verbosity=2)