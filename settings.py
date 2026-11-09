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

# 外部资源（MySQL / GPU 服务器）的默认与可选值。打分链路**不消费**这两组配置，
# 它们只是把「同一份凭据」留在配置层：以后加一个入库阶段或拉语料的脚本时，
# 不必再到脚本里硬编码 host / 密码（20-脚本、21-探针 里已有几十处这样的硬编码）。
DEFAULT_DB_PORT = 3306
DEFAULT_DB_CHARSET = "utf8mb4"
DEFAULT_GPU_PORT = 22
GPU_ACCOUNT_NAMES = ("kyrie", "zixin")

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


def _mask(secret: str) -> str:
    """密钥掩码：只留前缀与长度，够判断「配的是哪把钥匙」，不足以复原。

    短口令（≤12 位）连前缀都不露——``45176_`` 这种 6 位前缀已经是大半个密码了。
    """
    if len(secret) <= 12:
        return f"…共 {len(secret)} 位"
    return f"{secret[:6]}…共 {len(secret)} 位"


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

    def path(self, name: str, default: Path, base: Path,
             year: int | None = None) -> tuple[Path, str]:
        raw, src = self.raw(name)
        if raw is None:
            return default, src
        if year is not None:
            raw = expand_year(raw, year)
        return to_path(raw, base), src


def to_path(raw: str, base: Path) -> Path:
    p = Path(os.path.expandvars(raw)).expanduser()
    if not p.is_absolute():
        p = base / p
    return Path(os.path.normpath(p))


def expand_year(raw: str, year: int) -> str:
    """把路径里的 ``{year}`` / ``{year-1}`` 换成实际年度。

    为什么值得一个函数：``.../overrides_FY{year}.csv`` 这样写，明年只改 ``ESG_YEAR``，
    兜底表、上期目录、输出子目录会自动跟着走。写死 ``FY2025`` 的后果不是报错，而是
    **静默串档**——今年的批次套用去年的人工值，报告看着正常、数是错的。
    """
    if "{" not in raw:
        return raw
    out = raw
    for delta in (0, -1, 1):
        token = "{year}" if delta == 0 else f"{{year{delta:+d}}}"
        out = out.replace(token, str(year + delta))
    return out


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
    keywords_csv: Path
    prior_dir: Path | None = None
    bench_mode: str = "auto"
    bench_csv: Path | None = None
    override_csv: Path | None = None
    sample_pdf_dir: Path | None = None
    verdicts: Path | None = None
    alias_csv: Path | None = None
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_thinking: bool = False
    # 外部资源：MySQL（备用，打分链路不读）与 GPU 服务器账号（同上）
    db_host: str = ""
    db_port: int = DEFAULT_DB_PORT
    db_user: str = ""
    db_password: str = ""
    db_name: str = ""
    db_charset: str = DEFAULT_DB_CHARSET
    gpu_host: str = ""
    gpu_port: int = DEFAULT_GPU_PORT
    gpu_default: str = ""
    gpu_kyrie_user: str = ""
    gpu_kyrie_password: str = ""
    gpu_kyrie_dir: str = ""
    gpu_zixin_user: str = ""
    gpu_zixin_password: str = ""
    gpu_zixin_dir: str = ""
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
        ("ESG_S11_KEYWORDS", "keywords_csv", "file"),
        ("ESG_BENCH", "bench_mode", "text"),
        ("ESG_BENCH_CSV", "bench_csv", "file"),
        ("ESG_OVERRIDE_CSV", "override_csv", "file?"),
        ("ESG_ALIAS_CSV", "alias_csv", "file?"),
        ("ESG_SAMPLE_PDF_DIR", "sample_pdf_dir", "dir"),
        ("ESG_VERDICTS", "verdicts", "file"),
        ("ESG_LLM_BASE_URL", "llm_base_url", "text"),
        ("ESG_LLM_MODEL", "llm_model", "text"),
        ("ESG_LLM_THINKING", "llm_thinking", "text"),
        ("ESG_LLM_API_KEY", "llm_api_key", "secret"),
        ("ESG_DB_HOST", "db_host", "text"),
        ("ESG_DB_PORT", "db_port", "text"),
        ("ESG_DB_USER", "db_user", "text"),
        ("ESG_DB_PASSWORD", "db_password", "secret"),
        ("ESG_DB_NAME", "db_name", "text"),
        ("ESG_DB_CHARSET", "db_charset", "text"),
        ("ESG_GPU_HOST", "gpu_host", "text"),
        ("ESG_GPU_PORT", "gpu_port", "text"),
        ("ESG_GPU_DEFAULT", "gpu_default", "text"),
        ("ESG_GPU_KYRIE_USER", "gpu_kyrie_user", "text"),
        ("ESG_GPU_KYRIE_PASSWORD", "gpu_kyrie_password", "secret"),
        ("ESG_GPU_KYRIE_DIR", "gpu_kyrie_dir", "text"),
        ("ESG_GPU_ZIXIN_USER", "gpu_zixin_user", "text"),
        ("ESG_GPU_ZIXIN_PASSWORD", "gpu_zixin_password", "secret"),
        ("ESG_GPU_ZIXIN_DIR", "gpu_zixin_dir", "text"),
    )

    def rows(self) -> list[tuple[str, str, str, str]]:
        """``[(变量名, 生效值, 来源, 状态)]``，供 ``--show-config`` 打印。状态 ∈ 存在/缺失/将创建/—。

        ``secret`` 类的值只显示掩码：``--show-config`` 的输出经常被贴进工单/聊天里，
        密钥一旦随它扩散就收不回来。
        """
        out = []
        for name, attr, kind in self._FIELDS:
            val = getattr(self, attr)
            if val is None:
                text, state = "（未设置，运行时自动推导）", "—"
            elif kind == "secret":
                text = "（未设置）" if not val else f"已配置（{_mask(str(val))}）"
                state = "—"
            elif kind == "text":
                text, state = str(val), "—"
            elif kind == "file?":
                # 「可选文件」：配了就该落在说好的位置，但今年没有是正常的，
                # 所以只报在不在，不当错误（兜底表、别名表都属于这种）。
                p = Path(val)
                text, state = str(p), ("存在" if p.exists() else "不存在（可选）")
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
            if kind in ("text", "secret", "file?") or attr in ("out_dir", "build_dir"):
                continue  # 文本项无路径可言；可选文件不存在属正常；输出/落地目录运行时创建
            val = getattr(self, attr)
            if val is not None and not Path(val).exists():
                msgs.append(f"{name} 指向的路径不存在：{val}")
        return msgs

    # -- 外部资源：MySQL / GPU 服务器 -------------------------------------
    @property
    def db_ready(self) -> bool:
        """四项（host/user/password/name）齐备才算可用；端口与字符集有默认值。"""
        return bool(self.db_host and self.db_user and self.db_password and self.db_name)

    def db_params(self) -> dict:
        """直接喂给 ``pymysql.connect(**params)``；没配齐就抛错，不猜默认库。"""
        if not self.db_ready:
            raise ValueError("MySQL 未配置齐：需要 ESG_DB_HOST / _USER / _PASSWORD / _NAME")
        return {"host": self.db_host, "port": self.db_port, "user": self.db_user,
                "password": self.db_password, "database": self.db_name,
                "charset": self.db_charset or DEFAULT_DB_CHARSET}

    def gpu_account_names(self) -> tuple[str, ...]:
        """``ESG_GPU_*_USER`` 里填了用户名的那几个账号（按固定顺序）。"""
        return tuple(n for n in GPU_ACCOUNT_NAMES if getattr(self, f"gpu_{n}_user"))

    def gpu_account(self, name: str | None = None) -> dict:
        """某台 GPU 账号的 ssh 参数；``name`` 缺省取 ``ESG_GPU_DEFAULT``。

        返回的键与 ``gpu_ssh.py`` 的用法对齐（host/port/username/password/dir），
        但本包只负责取值，不负责连接——连接库（paramiko）不是本包依赖。
        """
        who = (name or self.gpu_default or "").strip().lower()
        if who not in GPU_ACCOUNT_NAMES:
            raise ValueError(f"未知 GPU 账号 {who!r}，可选 {GPU_ACCOUNT_NAMES}")
        user = getattr(self, f"gpu_{who}_user")
        if not user:
            raise ValueError(f"ESG_GPU_{who.upper()}_USER 未配置")
        return {"host": self.gpu_host, "port": self.gpu_port, "username": user,
                "password": getattr(self, f"gpu_{who}_password"),
                "dir": getattr(self, f"gpu_{who}_dir")}


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
                                app_dir / "good_sample" / "分析指标" / "ESG报告", base=root,
                                year=year)
    sources["ESG_REPORT_ROOT"] = src

    # 不写 ESG_PDF_DIR 时：默认吃「报告根/FY{year}」，这样 ESG_YEAR 一改，输入跟着改
    pdf_dir, src = lay.path("ESG_PDF_DIR", report_root / f"FY{year}", base=root, year=year)
    sources["ESG_PDF_DIR"] = src if src != "默认" else f"默认（ESG_REPORT_ROOT/FY{year}）"

    # 不写 ESG_PRIOR_DIR 时留 None：pipeline 会自己找 ESG_PDF_DIR 同级的 FY{year-1}
    raw_prior, src_prior = lay.raw("ESG_PRIOR_DIR")
    prior_dir = to_path(expand_year(raw_prior, year), root) if raw_prior else None
    sources["ESG_PRIOR_DIR"] = src_prior if raw_prior else "自动（PDF 同级 FY{year-1}）"

    out_dir, src = lay.path("ESG_OUT_DIR",
                            app_dir / "good_sample" / "分析指标" / "打分结果", base=root,
                            year=year)
    sources["ESG_OUT_DIR"] = src

    # 管道中间态（阶段产物 + 表）。默认放包内 build/（.gitignore 已排除），
    # 与 out_dir（给人看的 xlsx）分开：一个是可再生的工作区，一个是交付物。
    build_dir, src = lay.path("ESG_BUILD_DIR", PKG_DIR / "build", base=root, year=year)
    sources["ESG_BUILD_DIR"] = src

    insight_app, src = lay.path("ESG_INSIGHT_APP", app_dir / "ESG_insight" / "app", base=root,
                                year=year)
    sources["ESG_INSIGHT_APP"] = src

    standard_doc, src = lay.path("ESG_STANDARD_DOC",
                                 app_dir / "sample" / "02-指标标准" / "S1.1_指标与判断标准.md",
                                 base=root, year=year)
    sources["ESG_STANDARD_DOC"] = src

    # 定性召回词表：默认用包内那份（可直接用 Excel 编辑）；换项目/换口径时指到另一个 CSV
    keywords_csv, src = lay.path("ESG_S11_KEYWORDS", PKG_DIR / "keywords.csv", base=root,
                                 year=year)
    sources["ESG_S11_KEYWORDS"] = src

    # 年度和目录名对不上，是最典型也最隐蔽的串档：改了 ESG_YEAR，路径还指着去年。
    # 这里只警告不拦（临时用去年的目录做对照是合法用法），但要让人看见。
    for _name, _path, _expect in (("ESG_PDF_DIR", pdf_dir, year),
                                  ("ESG_PRIOR_DIR", prior_dir, year - 1)):
        if _path is None:
            continue
        _m = re.search(r"FY(\d{4})", Path(_path).name)
        if _m and int(_m.group(1)) != _expect:
            warnings.append(f"{_name} 指向 {Path(_path).name}，按 ESG_YEAR={year} 应为 "
                            f"FY{_expect}：疑似串档（或用 {{year}} 占位符让它自动跟随）")

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
    bench_csv = to_path(expand_year(raw_csv, year), root) if raw_csv else None
    sources["ESG_BENCH_CSV"] = src if raw_csv else "默认（不读外部对标表）"

    # 兜底表 / 别名表带年度：不写就留 None（不读），写了就让 {year} 跟着年度走。
    # 这两个文件常「今年还没有」，所以状态里只报「不存在（可选）」，不当错误。
    raw_ovr, src = lay.raw("ESG_OVERRIDE_CSV")
    override_csv = to_path(expand_year(raw_ovr, year), root) if raw_ovr else None
    sources["ESG_OVERRIDE_CSV"] = src if raw_ovr else "默认（不读人工兜底表）"

    raw_alias, src = lay.raw("ESG_ALIAS_CSV")
    alias_csv = to_path(expand_year(raw_alias, year), root) if raw_alias else None
    sources["ESG_ALIAS_CSV"] = src if raw_alias else "默认（只用文件名与股票代码脱敏）"

    raw_sample, src = lay.raw("ESG_SAMPLE_PDF_DIR")
    sample_pdf_dir = (to_path(expand_year(raw_sample, year), root) if raw_sample
                      else app_dir / "sample" / "01-源PDF")
    sources["ESG_SAMPLE_PDF_DIR"] = src

    raw_verd, src = lay.raw("ESG_VERDICTS")
    verdicts = (to_path(expand_year(raw_verd, year), root) if raw_verd
                else app_dir / "sample" / "04-脚本与索引" / "verdicts.py")
    sources["ESG_VERDICTS"] = src

    # 定性判档用的 LLM：base_url / model 是普通文本，api_key 是密钥（只进 .env）
    llm_base_url, src = lay.raw("ESG_LLM_BASE_URL")
    sources["ESG_LLM_BASE_URL"] = src
    llm_model, src = lay.raw("ESG_LLM_MODEL")
    sources["ESG_LLM_MODEL"] = src
    llm_api_key, src = lay.raw("ESG_LLM_API_KEY")
    sources["ESG_LLM_API_KEY"] = src if llm_api_key else "默认（未配置）"
    # 思维链默认关闭：判档是「照标准选一档」，推理链只会吃 token 预算并让 JSON 被截断
    raw_think, src = lay.raw("ESG_LLM_THINKING")
    llm_thinking = str(raw_think or "").strip().lower() in ("1", "on", "true", "yes", "开")
    sources["ESG_LLM_THINKING"] = src if raw_think else "默认（关）"
    if (llm_base_url or llm_model) and not llm_api_key:
        warnings.append("配了 ESG_LLM_BASE_URL / ESG_LLM_MODEL 但缺 ESG_LLM_API_KEY，"
                        "定性判档将退回关键词兜底")

    # 外部资源：只做「取值 + 自检」，不连网。缺项不算错（打分链路用不到），
    # 但半配（比如给了 host 忘了库名）要出声——否则将来第一次连库才发现少一项。
    def text(name: str) -> str:
        val, src = lay.raw(name)
        sources[name] = src if val else "默认（未配置）"
        return val or ""

    def number(name: str, default: int) -> int:
        raw, src = lay.raw(name)
        if not raw:
            sources[name] = "默认"
            return default
        try:
            sources[name] = src
            return int(raw)
        except ValueError:
            warnings.append(f"{name}={raw!r} 不是整数，已回退 {default}")
            sources[name] = f"无效值，回退 {default}"
            return default

    db_host = text("ESG_DB_HOST")
    db_port = number("ESG_DB_PORT", DEFAULT_DB_PORT)
    db_user = text("ESG_DB_USER")
    db_password = text("ESG_DB_PASSWORD")
    db_name = text("ESG_DB_NAME")
    db_charset = text("ESG_DB_CHARSET") or DEFAULT_DB_CHARSET
    given = [v for v in (db_host, db_user, db_password, db_name) if v]
    if given and len(given) < 4:
        warnings.append("MySQL 配置不完整：ESG_DB_HOST / _USER / _PASSWORD / _NAME 需同时给，"
                        "否则 db_params() 会拒绝连接")

    gpu_host = text("ESG_GPU_HOST")
    gpu_port = number("ESG_GPU_PORT", DEFAULT_GPU_PORT)
    gpu_default = text("ESG_GPU_DEFAULT").strip().lower()
    if gpu_default and gpu_default not in GPU_ACCOUNT_NAMES:
        warnings.append(f"ESG_GPU_DEFAULT={gpu_default!r} 不在 {GPU_ACCOUNT_NAMES}，已忽略")
        gpu_default = ""
    gpu_kyrie_user = text("ESG_GPU_KYRIE_USER")
    gpu_kyrie_password = text("ESG_GPU_KYRIE_PASSWORD")
    gpu_kyrie_dir = text("ESG_GPU_KYRIE_DIR")
    gpu_zixin_user = text("ESG_GPU_ZIXIN_USER")
    gpu_zixin_password = text("ESG_GPU_ZIXIN_PASSWORD")
    gpu_zixin_dir = text("ESG_GPU_ZIXIN_DIR")
    if (gpu_kyrie_user or gpu_zixin_user) and not gpu_host:
        warnings.append("配了 GPU 账号但缺 ESG_GPU_HOST，gpu_account() 拿不到连接地址")

    return Settings(
        year=year, project_root=root, pdf_dir=pdf_dir, out_dir=out_dir,
        build_dir=build_dir,
        report_root=report_root, insight_app=insight_app, standard_doc=standard_doc,
        keywords_csv=keywords_csv,
        prior_dir=prior_dir, bench_mode=bench_mode, bench_csv=bench_csv,
        override_csv=override_csv, sample_pdf_dir=sample_pdf_dir, verdicts=verdicts,
        alias_csv=alias_csv,
        llm_base_url=llm_base_url, llm_model=llm_model, llm_api_key=llm_api_key,
        llm_thinking=llm_thinking,
        db_host=db_host, db_port=db_port, db_user=db_user, db_password=db_password,
        db_name=db_name, db_charset=db_charset,
        gpu_host=gpu_host, gpu_port=gpu_port, gpu_default=gpu_default,
        gpu_kyrie_user=gpu_kyrie_user, gpu_kyrie_password=gpu_kyrie_password,
        gpu_kyrie_dir=gpu_kyrie_dir, gpu_zixin_user=gpu_zixin_user,
        gpu_zixin_password=gpu_zixin_password, gpu_zixin_dir=gpu_zixin_dir,
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
    "DEFAULT_DB_PORT", "DEFAULT_DB_CHARSET", "DEFAULT_GPU_PORT", "GPU_ACCOUNT_NAMES",
    "Settings", "parse_env_file", "locate_env_file", "to_path", "expand_year",
    "build", "configure", "get_settings", "reload", "iter_env_names",
]