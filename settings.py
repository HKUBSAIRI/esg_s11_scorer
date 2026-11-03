# -*- coding: utf-8 -*-
"""本机配置层：PDF/输出/词表仓库等路径、报告年度、对标来源，全部可外部注入。

为什么单独一层
--------------
原来 ``config.py`` 里写的是::

    REPORT_DIR = PROJECT_ROOT / "50-应用" / "good_sample" / "分析指标" / "ESG报告"

换一批 PDF、换一个输出目录、换一台机器，就得改代码（或者在命令行每次敲一长串
``--pdf`` / ``--out``）。改代码的代价不是麻烦，而是**不可复现**：同一份代码跑出不同
结果，说不清是模型变了还是路径变了。所以把两类事实拆开存放：

    本机事实（文件在哪、哪一年）  → 环境变量 / .env（本模块）  —— 改了不影响分数
    模型事实（权重、阈值、词表）  → config.py 的 SPECS / BANDS —— 一改必须 +0.1 版本号

优先级（高 → 低）
-----------------
1. **命令行参数**（``--pdf`` / ``--out`` / ``--year`` …，见 ``cli.py``；传了就赢）
2. **进程环境变量**（``$env:ESG_PDF_DIR = "D:/报告/FY2025"`` 或临时 ``set``）
3. **``.env`` 文件**（查找顺序：``ESG_ENV_FILE`` 指定 → 包目录 → 当前目录 → 项目根）
4. **默认值**（按本仓库的目录结构推出，见 ``build()`` 里的 ``report_root`` / ``out_dir`` 等）

相对路径一律相对**项目根**（``ESG_PROJECT_ROOT``，默认是本包的上两级目录）解析，
所以 ``.env`` 里写 ``50-应用/good_sample/...`` 这类路径，从任何工作目录启动都指向同一处。
``~``、``$VAR``、``%VAR%`` 会被展开。

安全约定
--------
``.env`` 只放路径，**不放密钥、不准入版本库**（已在 ``.gitignore``）；模板见 ``.env.example``。
本模块解析 ``.env`` 不依赖 python-dotenv：几十行的 KEY=VALUE 不值得多一个依赖，
也就不存在「装了没装行为不一样」。
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping

ENV_PREFIX = "ESG_"
DEFAULT_YEAR = 2025
BENCH_MODES = ("auto", "csv", "none")

PKG_DIR = Path(__file__).resolve().parent
# config.py 原先的算法：esg_s11_scorer/config.py → parents[1]（50-应用）→ .parent
DEFAULT_PROJECT_ROOT = PKG_DIR.parents[1]

_ASSIGN_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


# ---------------------------------------------------------------- .env 解析
def parse_env_file(path: str | Path) -> dict[str, str]:
    """读 ``KEY=VALUE``；支持注释、空行、``export`` 前缀、成对引号（引号内保留原样）。

    刻意**不做**转义处理：Windows 路径里全是反斜杠，把 ``"C:\\dir"`` 解释成转义是负收益。
    想要字面反斜杠就写成 ``"C:/dir"``（正斜杠一样能被 pathlib 接受）。
    """
    vals: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8-sig")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ASSIGN_RE.match(line)
        if m:
            vals[m.group(1)] = _clean_value(m.group(2))
    return vals


def _clean_value(raw: str) -> str:
    v = raw.strip()
    if v[:1] in ("'", '"'):
        q = v[0]
        end = v.find(q, 1)
        if end > 0:
            return v[1:end]
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()   # 去掉行内注释


def locate_env_file(explicit: str | Path | None = None) -> Path | None:
    """按优先级找 ``.env``：显式 > ``ESG_ENV_FILE`` > 包目录 > 当前目录 > 项目根。"""
    if explicit:
        p = Path(str(explicit)).expanduser()
        return p if p.is_file() else None
    from_env = os.environ.get(f"{ENV_PREFIX}ENV_FILE", "").strip()
    if from_env:
        p = Path(from_env).expanduser()
        if p.is_file():
            return p
    for cand in (PKG_DIR / ".env", Path.cwd() / ".env", DEFAULT_PROJECT_ROOT / ".env"):
        if cand.is_file():
            return cand
    return None


# ---------------------------------------------------------------- 取值与来源
class _Layers:
    """三层取值（环境变量 / .env / 默认），并记下「这个值从哪来」。"""

    def __init__(self, file_vals: Mapping[str, str], env_file: Path | None) -> None:
        self.file_vals = dict(file_vals)
        self.env_file = env_file

    def raw(self, name: str) -> tuple[str | None, str]:
        live = os.environ.get(name, "").strip()
        if live:
            return live, f"环境变量 {name}"
        if self.file_vals.get(name, "").strip():
            where = self.env_file.name if self.env_file else ".env"
            return self.file_vals[name].strip(), f"{where} → {name}"
        return None, "默认"

    def path(self, name: str, default: Path, base: Path) -> tuple[Path, str]:
        raw, src = self.raw(name)
        if raw is None:
            return default, src
        return to_path(raw, base), src


def to_path(raw: str, base: Path) -> Path:
    p = Path(os.path.expandvars(raw)).expanduser()
    if not p.is_absolute():
        p = base / p
    return Path(os.path.normpath(p))


# ---------------------------------------------------------------- 配置对象
@dataclass(frozen=True)
class Settings:
    """一次导出的生效配置。字段即 ``.env.example`` 里的变量名（小写）。"""

    year: int
    project_root: Path
    pdf_dir: Path
    out_dir: Path
    build_dir: Path
    report_root: Path
    insight_app: Path
    standard_doc: Path
    prior_dir: Path | None = None
    bench_mode: str = "auto"
    bench_csv: Path | None = None
    override_csv: Path | None = None
    sample_pdf_dir: Path | None = None
    verdicts: Path | None = None
    env_file: Path | None = None
    sources: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    # -- 给用户看的表格 ---------------------------------------------------
    _FIELDS = (
        ("ESG_PROJECT_ROOT", "project_root", "dir"),
        ("ESG_YEAR", "year", "text"),
        ("ESG_PDF_DIR", "pdf_dir", "dir"),
        ("ESG_PRIOR_DIR", "prior_dir", "dir"),
        ("ESG_OUT_DIR", "out_dir", "dir"),
        ("ESG_BUILD_DIR", "build_dir", "dir"),
        ("ESG_REPORT_ROOT", "report_root", "dir"),
        ("ESG_INSIGHT_APP", "insight_app", "dir"),
        ("ESG_STANDARD_DOC", "standard_doc", "file"),
        ("ESG_BENCH", "bench_mode", "text"),
        ("ESG_BENCH_CSV", "bench_csv", "file"),
        ("ESG_OVERRIDE_CSV", "override_csv", "file"),
        ("ESG_SAMPLE_PDF_DIR", "sample_pdf_dir", "dir"),
        ("ESG_VERDICTS", "verdicts", "file"),
    )

    def rows(self) -> list[tuple[str, str, str, str]]:
        """``[(变量名, 生效值, 来源, 状态)]``，供 ``--show-config`` 打印。状态 ∈ 存在/缺失/将创建/—。"""
        out = []
        for name, attr, kind in self._FIELDS:
            val = getattr(self, attr)
            if val is None:
                text, state = "（未设置，运行时自动推导）", "—"
            elif kind == "text":
                text, state = str(val), "—"
            else:
                p = Path(val)
                text = str(p)
                if p.exists():
                    state = "存在"
                elif attr in ("out_dir", "build_dir"):
                    state = "将创建"
                else:
                    state = "缺失"
            out.append((name, text, self.sources.get(name, "默认"), state))
        return out

    def problems(self) -> list[str]:
        """「配了但路径不存在」的清单 —— 直接点名该修哪个变量。"""
        msgs: list[str] = []
        for name, attr, kind in self._FIELDS:
            if kind == "text" or attr in ("out_dir", "build_dir"):  # 输出/落地目录不存在属正常，运行时创建
                continue
            val = getattr(self, attr)
            if val is not None and not Path(val).exists():
                msgs.append(f"{name} 指向的路径不存在：{val}")
        return msgs


# ---------------------------------------------------------------- 装配
def build(env_file: str | Path | None = None) -> Settings:
    """读三层配置，算出全部生效路径（纯函数，便于测试）。"""
    env_path = locate_env_file(env_file)
    file_vals = parse_env_file(env_path) if env_path else {}
    lay = _Layers(file_vals, env_path)
    warnings: list[str] = []
    sources: dict[str, str] = {}

    root, src = lay.path("ESG_PROJECT_ROOT", DEFAULT_PROJECT_ROOT, base=Path.cwd())
    sources["ESG_PROJECT_ROOT"] = src

    year = DEFAULT_YEAR
    raw_year, src = lay.raw("ESG_YEAR")
    sources["ESG_YEAR"] = src
    if raw_year is not None:
        try:
            year = int(raw_year)
        except ValueError:
            warnings.append(f"ESG_YEAR={raw_year!r} 不是整数，已回退 {DEFAULT_YEAR}")
            sources["ESG_YEAR"] = f"无效值，回退 {DEFAULT_YEAR}"

    app_dir = root / "50-应用"
    report_root, src = lay.path("ESG_REPORT_ROOT",
                                app_dir / "good_sample" / "分析指标" / "ESG报告", base=root)
    sources["ESG_REPORT_ROOT"] = src

    # 不写 ESG_PDF_DIR 时：默认吃「报告根/FY{year}」，这样 ESG_YEAR 一改，输入跟着改
    pdf_dir, src = lay.path("ESG_PDF_DIR", report_root / f"FY{year}", base=root)
    sources["ESG_PDF_DIR"] = src if src != "默认" else f"默认（ESG_REPORT_ROOT/FY{year}）"

    # 不写 ESG_PRIOR_DIR 时留 None：pipeline 会自己找 ESG_PDF_DIR 同级的 FY{year-1}
    raw_prior, src_prior = lay.raw("ESG_PRIOR_DIR")
    prior_dir = to_path(raw_prior, root) if raw_prior else None
    sources["ESG_PRIOR_DIR"] = src_prior if raw_prior else "自动（PDF 同级 FY{year-1}）"

    out_dir, src = lay.path("ESG_OUT_DIR",
                            app_dir / "good_sample" / "分析指标" / "打分结果", base=root)
    sources["ESG_OUT_DIR"] = src

    # 管道中间态（阶段产物 + 表）。默认放包内 build/（.gitignore 已排除），
    # 与 out_dir（给人看的 xlsx）分开：一个是可再生的工作区，一个是交付物。
    build_dir, src = lay.path("ESG_BUILD_DIR", PKG_DIR / "build", base=root)
    sources["ESG_BUILD_DIR"] = src

    insight_app, src = lay.path("ESG_INSIGHT_APP", app_dir / "ESG_insight" / "app", base=root)
    sources["ESG_INSIGHT_APP"] = src

    standard_doc, src = lay.path("ESG_STANDARD_DOC",
                                 app_dir / "sample" / "02-指标标准" / "S1.1_指标与判断标准.md",
                                 base=root)
    sources["ESG_STANDARD_DOC"] = src

    bench_mode = "auto"
    raw_bench, src = lay.raw("ESG_BENCH")
    sources["ESG_BENCH"] = src
    if raw_bench:
        if raw_bench in BENCH_MODES:
            bench_mode = raw_bench
        else:
            warnings.append(f"ESG_BENCH={raw_bench!r} 不在 {BENCH_MODES}，已回退 auto")
            sources["ESG_BENCH"] = "无效值，回退 auto"

    raw_csv, src = lay.raw("ESG_BENCH_CSV")
    bench_csv = to_path(raw_csv, root) if raw_csv else None
    sources["ESG_BENCH_CSV"] = src if raw_csv else "默认（不读外部对标表）"

    raw_ovr, src = lay.raw("ESG_OVERRIDE_CSV")
    override_csv = to_path(raw_ovr, root) if raw_ovr else None
    sources["ESG_OVERRIDE_CSV"] = src if raw_ovr else "默认（不读人工兜底表）"

    raw_sample, src = lay.raw("ESG_SAMPLE_PDF_DIR")
    sample_pdf_dir = (to_path(raw_sample, root) if raw_sample
                      else app_dir / "sample" / "01-源PDF")
    sources["ESG_SAMPLE_PDF_DIR"] = src

    raw_verd, src = lay.raw("ESG_VERDICTS")
    verdicts = (to_path(raw_verd, root) if raw_verd
                else app_dir / "sample" / "04-脚本与索引" / "verdicts.py")
    sources["ESG_VERDICTS"] = src

    return Settings(
        year=year, project_root=root, pdf_dir=pdf_dir, out_dir=out_dir,
        build_dir=build_dir,
        report_root=report_root, insight_app=insight_app, standard_doc=standard_doc,
        prior_dir=prior_dir, bench_mode=bench_mode, bench_csv=bench_csv,
        override_csv=override_csv, sample_pdf_dir=sample_pdf_dir, verdicts=verdicts,
        env_file=env_path, sources=sources, warnings=tuple(warnings))


# ---------------------------------------------------------------- 进程内单例
_SETTINGS: Settings | None = None


def configure(env_file: str | Path | None = None, *, force: bool = False) -> Settings:
    """装配并缓存；同时把词表仓库注入 ``sys.path``（``s11_text`` 等靠它才 import 得到）。

    注意：``config.py`` 里的路径常量是**导入时快照**。需要跟随 ``--env-file`` 的代码，
    请直接拿本函数返回的对象，别二次读 ``config.REPORT_DIR``。
    """
    global _SETTINGS
    if _SETTINGS is not None and not force and env_file is None:
        return _SETTINGS
    s = build(env_file)
    _SETTINGS = s
    _ensure_import_path(s.insight_app)
    return s


def get_settings() -> Settings:
    """当前生效配置（未配置过就先配置一次）。"""
    return configure()


def reload(env_file: str | Path | None = None) -> Settings:
    """强制重读（命令行指定 ``--env-file`` 时用）。"""
    return configure(env_file, force=True)


def _ensure_import_path(insight_app: Path) -> None:
    if not insight_app.exists():
        return
    text = str(insight_app)
    if text not in sys.path:
        sys.path.insert(0, text)


def iter_env_names() -> Iterator[str]:
    """全部可配置变量名（``.env.example`` 与文档据此保持同步）。"""
    yield from (name for name, _, _ in Settings._FIELDS)


__all__ = [
    "ENV_PREFIX", "DEFAULT_YEAR", "BENCH_MODES", "PKG_DIR", "DEFAULT_PROJECT_ROOT",
    "Settings", "parse_env_file", "locate_env_file", "to_path",
    "build", "configure", "get_settings", "reload", "iter_env_names",
]