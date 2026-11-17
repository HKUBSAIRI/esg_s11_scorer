# -*- coding: utf-8 -*-
"""OpenAI-compatible Embedding 客户端，用于细则级混合检索。

默认复用 ``ESG_LLM_*`` 的 DashScope 兼容地址与密钥，只单独配置
``ESG_EMBEDDING_MODEL``。客户端只依赖标准库，并缓存同一进程内的文本向量。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import urllib.error
import urllib.request
from array import array
from dataclasses import dataclass, field
from pathlib import Path

EMBEDDING_VERSION = "embedding-client-1.0"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class EmbeddingError(RuntimeError):
    """Embedding 调用失败；调用方应退回关键词检索。"""


@dataclass(frozen=True)
class Embedding:
    base_url: str = ""
    model: str = "text-embedding-v3"
    api_key: str = ""
    timeout: int = 90
    retries: int = 2
    batch_size: int = 10
    cache_path: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip() and self.api_key.strip())

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/embeddings"

    def fingerprint(self) -> str:
        raw = f"{self.base_url.rstrip('/')}|{self.model}|{EMBEDDING_VERSION}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def __repr__(self) -> str:
        return (f"Embedding(model={self.model!r}, base_url={self.base_url!r}, "
                f"batch_size={self.batch_size}, "
                f"key={'已配置' if self.api_key else '未配置'})")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return _CACHE.embed(self, texts)


@dataclass
class _EmbeddingCache:
    vectors: dict[tuple[str, str], list[float]] = field(default_factory=dict)

    def embed(self, embedding: Embedding, texts: list[str]) -> list[list[float]]:
        if not embedding.ready:
            raise EmbeddingError(
                "Embedding 未配置（缺 ESG_EMBEDDING_BASE_URL / "
                "ESG_EMBEDDING_MODEL / ESG_EMBEDDING_API_KEY）")
        if not texts:
            return []

        memory_missing = [
            text for text in texts if (embedding.model, text) not in self.vectors
        ]
        disk = _load_disk(embedding, memory_missing)
        self.vectors.update({
            (embedding.model, text): vector for text, vector in disk.items()
        })
        missing = list(dict.fromkeys(
            text for text in texts if (embedding.model, text) not in self.vectors))
        batch = max(1, int(embedding.batch_size))
        for start in range(0, len(missing), batch):
            chunk = missing[start:start + batch]
            payload = {
                "model": embedding.model,
                "input": chunk,
                "encoding_format": "float",
            }
            data = _post(embedding, payload)
            rows = data.get("data")
            if not isinstance(rows, list) or len(rows) != len(chunk):
                raise EmbeddingError("Embedding 返回条数与输入不一致")
            try:
                ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
                vectors = [
                    _normalize([float(value) for value in row["embedding"]])
                    for row in ordered
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise EmbeddingError(f"Embedding 返回结构异常：{exc}") from exc
            for text, vector in zip(chunk, vectors):
                self.vectors[(embedding.model, text)] = vector
            _store_disk(embedding, dict(zip(chunk, vectors)))
        return [self.vectors[(embedding.model, text)] for text in texts]


_CACHE = _EmbeddingCache()


def _cache_db(embedding: Embedding) -> Path | None:
    return Path(embedding.cache_path) if embedding.cache_path else None


def _load_disk(embedding: Embedding, texts: list[str]) -> dict[str, list[float]]:
    path = _cache_db(embedding)
    if path is None:
        return {}
    out: dict[str, list[float]] = {}
    wanted = {
        hashlib.sha256(text.encode("utf-8")).hexdigest(): text
        for text in texts
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS vectors ("
                "model TEXT NOT NULL, digest TEXT NOT NULL, vector TEXT NOT NULL, "
                "PRIMARY KEY (model, digest))")
            digests = list(wanted)
            for start in range(0, len(digests), 500):
                part = digests[start:start + 500]
                marks = ",".join("?" for _ in part)
                rows = conn.execute(
                    f"SELECT digest, vector FROM vectors "
                    f"WHERE model=? AND digest IN ({marks})",
                    (embedding.model, *part),
                ).fetchall()
                for digest, vector in rows:
                    if isinstance(vector, bytes):
                        values = array("f")
                        values.frombytes(vector)
                        out[wanted[digest]] = [float(value) for value in values]
                    else:
                        out[wanted[digest]] = [
                            float(value) for value in json.loads(vector)
                        ]
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return out
    return out


def _store_disk(embedding: Embedding, vectors: dict[str, list[float]]) -> None:
    path = _cache_db(embedding)
    if path is None or not vectors:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS vectors ("
                "model TEXT NOT NULL, digest TEXT NOT NULL, vector TEXT NOT NULL, "
                "PRIMARY KEY (model, digest))")
            conn.executemany(
                "INSERT OR REPLACE INTO vectors(model, digest, vector) VALUES (?, ?, ?)",
                [(
                    embedding.model,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    array("f", vector).tobytes(),
                ) for text, vector in vectors.items()],
            )
            conn.commit()
        finally:
            conn.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _post(embedding: Embedding, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = ""
    for attempt in range(max(0, embedding.retries) + 1):
        request = urllib.request.Request(
            embedding.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {embedding.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=embedding.timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {exc.code}: {detail}"
            if exc.code not in RETRY_STATUS:
                raise EmbeddingError(last) from None
        except Exception as exc:  # noqa: BLE001 - 网络层异常种类很多
            last = f"{type(exc).__name__}: {exc}"
        if attempt < embedding.retries:
            time.sleep(1.5 * (attempt + 1))
    raise EmbeddingError(
        f"Embedding 调用失败（已重试 {embedding.retries} 次）：{last}")


def from_settings(settings=None) -> Embedding:
    if settings is None:
        from .settings import get_settings
        settings = get_settings()
    return Embedding(
        base_url=settings.embedding_base_url or settings.llm_base_url or "",
        model=settings.embedding_model or "text-embedding-v3",
        api_key=settings.embedding_api_key or settings.llm_api_key or "",
        timeout=int(settings.embedding_timeout or 90),
        retries=int(settings.embedding_retries),
        batch_size=int(settings.embedding_batch_size or 10),
        cache_path=str(Path(settings.build_dir) / "embedding-cache" / "vectors.sqlite3"),
    )


__all__ = ["Embedding", "EmbeddingError", "EMBEDDING_VERSION", "from_settings"]
