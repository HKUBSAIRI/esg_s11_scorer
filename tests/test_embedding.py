# -*- coding: utf-8 -*-
"""Embedding 客户端单测：配置、分批、缓存和密钥隐藏，不联网。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import esg_s11_scorer  # noqa: F401,E402
from esg_s11_scorer import embedding as E  # noqa: E402


class TestEmbeddingClient(unittest.TestCase):
    def setUp(self):
        E._CACHE.vectors.clear()

    def test_ready_endpoint_and_fingerprint(self):
        emb = E.Embedding(base_url="https://example.test/v1/", model="m", api_key="k")
        self.assertTrue(emb.ready)
        self.assertEqual(emb.endpoint, "https://example.test/v1/embeddings")
        self.assertEqual(len(emb.fingerprint()), 12)

    def test_repr_hides_key(self):
        emb = E.Embedding(base_url="https://example.test/v1", model="m", api_key="secret")
        self.assertNotIn("secret", repr(emb))
        self.assertIn("已配置", repr(emb))

    def test_batches_and_reuses_cache(self):
        calls = []

        def fake_post(_embedding, payload):
            calls.append(list(payload["input"]))
            return {"data": [
                {"index": i, "embedding": [float(len(text)), 1.0]}
                for i, text in enumerate(payload["input"])
            ]}

        emb = E.Embedding(
            base_url="https://example.test/v1",
            model="m",
            api_key="k",
            batch_size=2,
        )
        with mock.patch.object(E, "_post", side_effect=fake_post):
            first = emb.embed(["alpha", "beta", "gamma"])
            second = emb.embed(["alpha", "beta", "gamma"])
        self.assertEqual(first, second)
        self.assertEqual(calls, [["alpha", "beta"], ["gamma"]])
        self.assertAlmostEqual(sum(x * x for x in first[0]), 1.0)

    def test_empty_input_does_not_call_api(self):
        emb = E.Embedding(base_url="https://example.test/v1", model="m", api_key="k")
        with mock.patch.object(E, "_post") as post:
            self.assertEqual(emb.embed([]), [])
        post.assert_not_called()

    def test_disk_cache_roundtrip_uses_model_text_key(self):
        def fake_post(_embedding, payload):
            return {"data": [
                {"index": i, "embedding": [float(i + 1), 1.0]}
                for i, _ in enumerate(payload["input"])
            ]}

        with tempfile.TemporaryDirectory() as tmp:
            emb = E.Embedding(
                base_url="https://example.test/v1",
                model="disk-model",
                api_key="k",
                cache_path=str(Path(tmp) / "vectors.sqlite3"),
            )
            with mock.patch.object(E, "_post", side_effect=fake_post) as post:
                first = emb.embed(["disk alpha"])
                E._CACHE.vectors.clear()
                second = emb.embed(["disk alpha"])
            for left, right in zip(first[0], second[0]):
                self.assertAlmostEqual(left, right, places=6)
            self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
