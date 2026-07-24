from waikiki import db, embeddings
from tests.conftest import FakeEmbedder


def test_seed_and_get_library(wiki):
    embeddings.seed_library()
    lib = embeddings.get_library()
    assert any(m["provider"] == "fastembed" for m in lib)


def test_add_model_unknown_provider(wiki):
    res = embeddings.add_model("nope", "some/model")
    assert res["ok"] is False and "provider" in res["error"]


def test_add_model_empty_slug(wiki):
    res = embeddings.add_model("fastembed", "  ")
    assert res["ok"] is False


def test_add_model_success(wiki, monkeypatch):
    # Avoid a real download: pretend the model loads.
    monkeypatch.setattr(embeddings, "_build", lambda p, m: FakeEmbedder())
    res = embeddings.add_model("fastembed", "some/new-model")
    assert res["ok"] is True and res["dim"] == 8
    lib = embeddings.get_library()
    assert any(m["model"] == "some/new-model" for m in lib)
    assert db.get_setting("embedder_provider") == "fastembed"
    assert db.get_setting("embedder_model") == "some/new-model"


def test_set_active(wiki):
    embeddings.set_active("voyage", "voyage-3.5")
    assert db.get_setting("embedder_provider") == "voyage"
    assert db.get_setting("embedder_model") == "voyage-3.5"
