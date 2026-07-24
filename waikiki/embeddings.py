"""Pluggable embeddings with an in-UI model library.

Providers (selectable + addable from Settings):

  * "fastembed" — ONNX runtime, no PyTorch. The packaged-app default; small and
                  fast. Any model fastembed supports (BGE, E5, GTE, nomic, …) or
                  a compatible HF ONNX model, added by slug.
  * "local"     — sentence-transformers; any HF sentence-transformers slug.
                  Needs PyTorch (installed via the optional "local" extra), so
                  it's a dev/power-user path, not bundled in the binary.
  * "voyage"    — Voyage AI cloud (Anthropic-recommended). Needs VOYAGE_API_KEY.

The active model is `embedder_provider` + `embedder_model` in the settings table.
`model_library` (JSON in settings) is the list the Settings page shows, and
`add_model()` appends to it after a successful test-load.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import List

import httpx

from . import config, db


class Embedder:
    provider: str
    model: str
    dim: int

    def embed(self, texts: List[str]) -> List[List[float]]:  # pragma: no cover
        raise NotImplementedError


class FastEmbedEmbedder(Embedder):
    provider = "fastembed"

    def __init__(self, model: str):
        from fastembed import TextEmbedding  # lazy import

        self.model = model
        self._m = TextEmbedding(model_name=model)
        self.dim = len(next(iter(self._m.embed(["probe"]))))

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [list(map(float, v)) for v in self._m.embed(list(texts))]


class LocalEmbedder(Embedder):
    provider = "local"

    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer  # lazy, heavy

        self.model = model
        self._st = SentenceTransformer(model)
        self.dim = self._st.get_sentence_embedding_dimension()

    def embed(self, texts: List[str]) -> List[List[float]]:
        vecs = self._st.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        return [v.tolist() for v in vecs]


class VoyageEmbedder(Embedder):
    provider = "voyage"
    _DIMS = {"voyage-3.5": 1024, "voyage-3.5-lite": 1024, "voyage-3-large": 1024,
             "voyage-code-3": 1024}

    def __init__(self, model: str):
        self.model = model
        self.dim = self._DIMS.get(model, 1024)
        self._key = os.environ.get(config.VOYAGE_API_KEY_ENV)
        if not self._key:
            raise RuntimeError(
                f"{config.VOYAGE_API_KEY_ENV} is not set — required for the Voyage "
                "embedder. Set it, or pick a local/fastembed model in Settings."
            )

    def embed(self, texts: List[str]) -> List[List[float]]:
        resp = httpx.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {self._key}"},
            json={"input": texts, "model": self.model},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]


_CLASSES = {
    "fastembed": FastEmbedEmbedder,
    "local": LocalEmbedder,
    "voyage": VoyageEmbedder,
}


@lru_cache(maxsize=8)
def _build(provider: str, model: str) -> Embedder:
    cls = _CLASSES.get(provider)
    if cls is None:
        raise ValueError(f"unknown embedder provider '{provider}'")
    return cls(model)


def active() -> tuple[str, str]:
    provider = db.get_setting("embedder_provider", "fastembed")
    model = db.get_setting("embedder_model") or _DEFAULT_MODEL.get(provider, "")
    return provider, model


def get_embedder() -> Embedder:
    """Instantiate the active embedder (cached per provider+model)."""
    provider, model = active()
    return _build(provider, model)


_DEFAULT_MODEL = {
    "fastembed": "BAAI/bge-small-en-v1.5",
    "local": "sentence-transformers/all-MiniLM-L6-v2",
    "voyage": "voyage-3.5",
}


# --- Model library (what the Settings page manages) ---------------------------

def get_library() -> list[dict]:
    raw = db.get_setting("model_library")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _save_library(lib: list[dict]) -> None:
    db.set_setting("model_library", json.dumps(lib))


def set_active(provider: str, model: str) -> None:
    db.set_setting("embedder_provider", provider)
    db.set_setting("embedder_model", model)


def add_model(provider: str, model: str) -> dict:
    """Test-load a model (downloads it), and on success add it to the library and
    make it active. Returns {'ok': bool, 'error': str, 'dim': int}."""
    provider = (provider or "").strip()
    model = (model or "").strip()
    if provider not in _CLASSES:
        return {"ok": False, "error": f"unknown provider '{provider}'"}
    if not model:
        return {"ok": False, "error": "model slug is empty"}
    try:
        emb = _build(provider, model)  # may download weights
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    lib = get_library()
    if not any(m["provider"] == provider and m["model"] == model for m in lib):
        lib.append({"provider": provider, "model": model})
        _save_library(lib)
    set_active(provider, model)
    return {"ok": True, "dim": emb.dim}


def fastembed_catalog() -> list[str]:
    """Slugs fastembed can load out of the box (for the UI's suggestions)."""
    try:
        from fastembed import TextEmbedding
        return sorted(m["model"] for m in TextEmbedding.list_supported_models())
    except Exception:
        return []


def seed_library() -> None:
    """Ensure the library has at least the default fastembed model."""
    if not get_library():
        _save_library([{"provider": "fastembed", "model": _DEFAULT_MODEL["fastembed"]}])
