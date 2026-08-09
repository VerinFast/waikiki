"""Shared test fixtures.

Each test gets an isolated temp data dir (its own wiki registry + per-wiki DBs)
and a fast, deterministic fake embedder so nothing downloads a model or hits the
network. The active wiki defaults to "main".
"""
from __future__ import annotations

import hashlib
import threading

import pytest

from waikiki import config, db, embeddings, updater, wikis


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


@pytest.fixture(autouse=True)
def _no_update_checks(monkeypatch):
    """Never let the test suite reach GitHub.

    The app's maintenance loop checks for updates, and it starts with every
    TestClient lifespan. It skips its first pass so a normal run doesn't fire a
    request during startup, but that is a timing property -- this makes it
    structural, so a future change to that loop can't quietly put a network call
    back into the suite.
    """
    monkeypatch.setattr(
        updater, "check",
        lambda: {"ok": False, "current": config.VERSION, "available": False,
                 "error": "network disabled in tests"})


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    """Isolated temp data dir + fake embedder; active wiki = 'main'."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "WIKIS_DIR", tmp_path / "wikis")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "waikiki.db")  # legacy, absent
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(db, "_schema_ready", set())
    monkeypatch.setattr(embeddings, "get_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(embeddings, "active", lambda: ("fake", "fake"))

    wikis.ensure_initialized()  # creates main + Beaconlight/Crosslake/StartupOS
    token = db.current_wiki.set("main")
    db.init_db()
    try:
        yield
    finally:
        db.current_wiki.reset(token)
