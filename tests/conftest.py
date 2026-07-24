"""Shared test fixtures.

Each test gets an isolated temp SQLite DB and a fast, deterministic fake embedder
so nothing downloads a model or hits the network.
"""
from __future__ import annotations

import hashlib
import threading

import pytest

from waikiki import config, db, embeddings


class FakeEmbedder:
    provider = "fake"
    model = "fake"
    dim = 8

    def embed(self, texts):
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([b / 255.0 for b in h[: self.dim]])
        return out


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    """Fresh DB + fake embedder for a single test."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(embeddings, "get_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(embeddings, "active", lambda: ("fake", "fake"))
    db.init_db()
    yield
