"""Pluggable embeddings.

Two providers, selectable from the UI (stored in the settings table):

  * "local"  — sentence-transformers, any model on the HuggingFace Hub.
               Fully offline, no API key. Default: all-MiniLM-L6-v2 (dim 384).
  * "voyage" — Voyage AI, the embeddings provider Anthropic recommends
               (the Claude API has no native embeddings endpoint). Needs
               VOYAGE_API_KEY. Default: voyage-3.5 (dim 1024).

Both implement the same tiny interface, so adding OpenAI/Cohere/etc. later is
one more subclass. The active provider + model live in `settings`.
"""
from __future__ import annotations

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


class LocalEmbedder(Embedder):
    provider = "local"

    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer  # lazy: heavy import

        self.model = model
        self._st = SentenceTransformer(model)
        self.dim = self._st.get_sentence_embedding_dimension()

    def embed(self, texts: List[str]) -> List[List[float]]:
        vecs = self._st.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True
        )
        return [v.tolist() for v in vecs]


class VoyageEmbedder(Embedder):
    provider = "voyage"
    # Known output dimensions for the common Voyage models.
    _DIMS = {"voyage-3.5": 1024, "voyage-3.5-lite": 1024, "voyage-3-large": 1024,
             "voyage-code-3": 1024}

    def __init__(self, model: str):
        self.model = model
        self.dim = self._DIMS.get(model, 1024)
        self._key = os.environ.get(config.VOYAGE_API_KEY_ENV)
        if not self._key:
            raise RuntimeError(
                f"{config.VOYAGE_API_KEY_ENV} is not set — required for the Voyage "
                "embedder. Set it, or switch the embedder to 'local' in Settings."
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


@lru_cache(maxsize=4)
def _build(provider: str, model: str) -> Embedder:
    if provider == "voyage":
        return VoyageEmbedder(model)
    return LocalEmbedder(model)


def get_embedder() -> Embedder:
    """Instantiate the embedder chosen in Settings (cached per provider+model)."""
    provider = db.get_setting("embedder_provider", "local")
    if provider == "voyage":
        model = db.get_setting("embedder_voyage_model", "voyage-3.5")
    else:
        model = db.get_setting("embedder_local_model",
                               "sentence-transformers/all-MiniLM-L6-v2")
    return _build(provider, model)
