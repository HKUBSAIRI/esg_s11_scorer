# -*- coding: utf-8 -*-
"""落地层：阶段产物（JSON）+ 表（parquet / JSONL），以及「这份产物还能不能用」的指纹。

为什么需要这一层
----------------
改造前 ``score_batch()`` 一次调用走完全程，中间的 Doc / Extraction / 判定全在内存：
单份 PDF 失败要整批重跑，改一个阈值也不知道该重算哪一段。落地之后管道多了两样东西：

1. **断点**：每个阶段写 ``build/FY{year}/{code}/{stage}.json``，分区键 ``(code, year)``
2. **依据**：每份产物记着自己的 ``key``（指纹），重跑时先比指纹，再决定是否重算

指纹怎么算（唯一的复用判据）
--------------------------
    key(parse)    = H( pdf 字节 + PARSE_VERSION )
    key(facts)    = H( key(parse) + EXTRACT_VERSION + 抽数依赖的字节（override 表） )
    key(elements) = H( key(parse) + RULESET_VERSION + JUDGE_VERSION + JUDGE_PLUS_VERSION )
    key(score)    = H( key(facts) + key(elements) + key(prior facts) + 对标快照 + MODEL_VERSION )

上游变了下游必变，规则变了自己变，无关的不会牵连——这就是把「整批重跑」换成
「只重算受影响的分区」的全部依据。指纹只由内容决定，不看时间戳，所以同一份输入
永远算出同一个 key（可复现的前提）。

为什么表要两种后端
------------------
gold 表很小（一年几百行），用 parquet 是为了「能用 SQL / pandas 直接读」，不是为了性能。
pyarrow 缺席时退回 JSONL：同样的行、同样的列，只是没了列类型与压缩。两种后端由
``TableStore.backend`` 统一选择，调用方看不到差别。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

ENCODING = "utf-8"
_CHUNK = 1 << 20
_BACKENDS = ("parquet", "jsonl")


class StoreError(Exception):
    """产物损坏或版本对不上。宁可炸，也不要静默读出一张空表。"""


# ---------------------------------------------------------------- 指纹
def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def digest_file(path: str | Path) -> str:
    """整份文件的分块 sha256（PDF 动辄几十 MB，不一次性读入内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def digest_obj(obj: Any) -> str:
    """结构化对象 → 指纹（键序无关，中文不转义，保证跨机跨进程一致）。"""
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return digest_bytes(text.encode(ENCODING))


def combine(*parts: Any) -> str:
    """把若干来源（指纹/版本号/常量）合成一个 key；``None`` 视为固定占位符。"""
    return digest_obj(["-" if p is None else p for p in parts])


_SOURCE_MEMO: dict[str, str] = {}


def source_digest(*modules: Any) -> str:
    """阶段实现源码的指纹（模块对象或路径均可）。

    为什么用它而不是手写 ``*_VERSION``
    ---------------------------------
    手写版本号是**人肉保险**：改了 ``extract.py`` 忘了 +0.1，旧产物就会被当成仍然有效
    而直接复用——测试跑得飞快，结论却是上一次的。用源码指纹后，「代码变了就重算」
    由机器保证，版本号只负责表达「格式/口径变了」这种给人看的意义。
    """
    parts = []
    for m in modules:
        path = Path(getattr(m, "__file__", m))
        key = str(path)
        if key not in _SOURCE_MEMO:
            _SOURCE_MEMO[key] = digest_file(path) if path.is_file() else "-"
        parts.append(f"{path.name}:{_SOURCE_MEMO[key]}")
    return digest_obj(parts)


def short(key: str | None, n: int = 12) -> str:
    return (key or "")[:n]


# ---------------------------------------------------------------- 原子写
def _atomic_write(path: Path, data: bytes) -> Path:
    """先写 ``.tmp`` 再 rename：中途崩了也不会留下半个 JSON/半张表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 阶段产物
@dataclass(frozen=True)
class ArtifactStore:
    """``build/FY{year}/`` 的读写。一个子目录 = 一家公司（分区键就是公司代码）。

    产物文件是**带信封**的 JSON::

        {"stage": "facts", "code": "02269", "year": 2025,
         "key": "9f2c...", "producer": "esg_s11_scorer 1.1",
         "produced_at": "2026-11-03T16:02:11+08:00",
         "payload": {...}}

    信封让「能不能复用」和「产出内容」分开放：查指纹不用解析 payload，出了问题时
    也能一眼看出这份文件是哪一版、什么时候、由谁写的。
    """

    root: Path
    year: int

    def unit_dir(self, code: str) -> Path:
        return self.root / code

    def path(self, code: str, name: str) -> Path:
        return self.unit_dir(code) / f"{name}.json"

    # -- 读 -------------------------------------------------------------
    def envelope(self, code: str, name: str) -> dict | None:
        p = self.path(code, name)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding=ENCODING))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"产物损坏，无法解析：{p}（{exc}）") from exc
        if not isinstance(data, dict) or "payload" not in data:
            raise StoreError(f"产物格式不对（缺 payload 信封）：{p}")
        return data

    def payload(self, code: str, name: str) -> dict | None:
        env = self.envelope(code, name)
        return None if env is None else env.get("payload")

    def key(self, code: str, name: str) -> str | None:
        env = self.envelope(code, name)
        return None if env is None else env.get("key")

    def fresh(self, code: str, name: str, key: str) -> bool:
        """指纹一致 = 这份产物就是当前输入/规则下的结果，可以直接复用。"""
        return self.key(code, name) == key

    # -- 写 -------------------------------------------------------------
    def write(self, code: str, name: str, key: str, payload: dict,
              producer: str = "") -> Path:
        env = {"stage": name, "code": code, "year": self.year, "key": key,
               "producer": producer, "produced_at": _now(), "payload": payload}
        blob = json.dumps(env, ensure_ascii=False, indent=1, default=str)
        return _atomic_write(self.path(code, name), blob.encode(ENCODING))

    # -- 枚举 -----------------------------------------------------------
    def units(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and any(p.glob("*.json")))

    def units_with(self, name: str) -> list[str]:
        return [c for c in self.units() if self.path(c, name).is_file()]

    def forget(self, code: str, name: str) -> bool:
        p = self.path(code, name)
        if p.is_file():
            p.unlink()
            return True
        return False

    # -- 年度级元数据 -----------------------------------------------------
    @property
    def meta_path(self) -> Path:
        return self.root / "_meta.json"

    def read_meta(self) -> dict:
        if not self.meta_path.is_file():
            return {}
        try:
            return json.loads(self.meta_path.read_text(encoding=ENCODING))
        except (OSError, json.JSONDecodeError):
            return {}

    def write_meta(self, **fields: Any) -> Path:
        meta = self.read_meta()
        meta.update({k: v for k, v in fields.items() if v is not None})
        meta["updated_at"] = _now()
        blob = json.dumps(meta, ensure_ascii=False, indent=1, default=str)
        return _atomic_write(self.meta_path, blob.encode(ENCODING))


# ---------------------------------------------------------------- 表
def _backend() -> str:
    """有 pyarrow 就用 parquet，没有就退回 JSONL（行/列完全一致）。"""
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
    except ImportError:
        return "jsonl"
    return "parquet"


def table_backend() -> str:
    return _backend()


def _pa_array(values: Sequence[Any]):
    """按列内非空值推断类型：bool → int → float → 其余一律按字符串。

    parquet 不接受「全 null 的列」（推断出 null 类型会写失败），所以空列统一落成
    字符串列；理由：这些列在真实数据里本来就有值，空列只出现在小样本测试里。
    """
    import pyarrow as pa

    present = [v for v in values if v is not None]
    if not present:
        return pa.array([None] * len(values), type=pa.string())
    if all(isinstance(v, bool) for v in present):
        return pa.array(values, type=pa.bool_())
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        if any(isinstance(v, float) for v in present):
            return pa.array([None if v is None else float(v) for v in values],
                            type=pa.float64())
        return pa.array(values, type=pa.int64())
    return pa.array([None if v is None else str(v) for v in values], type=pa.string())


@dataclass(frozen=True)
class TableStore:
    """``build/tables/FY{year}/{name}.<ext>``。一行一个 dict，列序稳定，整表覆盖写。"""

    root: Path
    year: int

    @property
    def backend(self) -> str:
        return _backend()

    def path(self, name: str) -> Path:
        """返回实际存在的那个后缀；都不存在时返回当前后端偏好的路径。"""
        for ext in _BACKENDS:
            p = self.root / f"{name}.{ext}"
            if p.is_file():
                return p
        return self.root / f"{name}.{self.backend}"

    def exists(self, name: str) -> bool:
        return any((self.root / f"{name}.{ext}").is_file() for ext in _BACKENDS)

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        out = set()
        for p in self.root.glob("*.*"):
            if p.suffix.lstrip(".") in _BACKENDS:
                out.add(p.stem)
        return sorted(out)

    def write(self, name: str, rows: Iterable[dict],
              columns: Sequence[str] | None = None) -> Path:
        """整表覆盖写（幂等：同样的 rows 写两次结果一样）。"""
        materialised = [{k: v for k, v in r.items()} for r in rows]
        cols = list(columns or (list(materialised[0].keys()) if materialised else []))
        if not cols:
            raise StoreError(f"表 {name} 既没有行也没有声明列；空表必须给 columns")
        normalised = [{c: r.get(c) for c in cols} for r in materialised]

        if self.backend == "parquet":
            import pyarrow as pa
            import pyarrow.parquet as pq
            table = pa.table({c: _pa_array([r[c] for r in normalised]) for c in cols})
            path = self.root / f"{name}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            pq.write_table(table, tmp)
            os.replace(tmp, path)
            return path

        lines = [json.dumps(r, ensure_ascii=False, default=str) for r in normalised]
        blob = ("\n".join(lines) + ("\n" if lines else "")).encode(ENCODING)
        return _atomic_write(self.root / f"{name}.jsonl", blob)

    def read(self, name: str) -> list[dict]:
        path = self.path(name)
        if not path.is_file():
            return []
        try:
            if path.suffix == ".parquet":
                import pyarrow.parquet as pq
                return pq.read_table(path).to_pylist()
            text = path.read_text(encoding=ENCODING)
        except Exception as exc:                     # 损坏的表宁可炸，也不要当空表
            raise StoreError(f"表 {name} 读取失败：{path}（{exc}）") from exc
        return [json.loads(line) for line in text.splitlines() if line.strip()]


__all__ = [
    "StoreError", "digest_bytes", "digest_file", "digest_obj", "combine", "short",
    "source_digest",
    "ArtifactStore", "TableStore", "table_backend",
]