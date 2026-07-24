"""Configuration for Waikiki.

Env-level knobs live here; per-install preferences that the UI can change
(active theme, which embedder to use) live in the `settings` table in the DB
so they can be edited from the browser without touching code.
"""
from __future__ import annotations

import os
from pathlib import Path

# Project root and data location -------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("WAIKIKI_DATA", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("WAIKIKI_DB", DATA_DIR / "waikiki.db"))

# Server -------------------------------------------------------------------------
HOST = os.environ.get("WAIKIKI_HOST", "127.0.0.1")
PORT = int(os.environ.get("WAIKIKI_PORT", "8787"))

# Where the MCP server reaches the running web app to inject live edits.
WEB_URL = os.environ.get("WAIKIKI_WEB_URL", f"http://{HOST}:{PORT}")

# AI (Claude) --------------------------------------------------------------------
# Resolves ANTHROPIC_API_KEY / ant-login profile via the SDK automatically.
ANTHROPIC_MODEL = os.environ.get("WAIKIKI_MODEL", "claude-opus-4-8")

# Voyage (Anthropic-recommended cloud embeddings). Key read at call time.
VOYAGE_API_KEY_ENV = "VOYAGE_API_KEY"

# Defaults for the settings table (seeded on first run; editable in the UI) -------
DEFAULT_SETTINGS = {
    "theme": "default",
    # embedder: "local" (sentence-transformers / any HF model) or "voyage" (cloud)
    "embedder_provider": "local",
    "embedder_local_model": "sentence-transformers/all-MiniLM-L6-v2",  # dim 384
    "embedder_voyage_model": "voyage-3.5",  # dim 1024
}

# Retrieval / chunking -----------------------------------------------------------
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150
RAG_TOP_K = 6
RRF_K = 60  # reciprocal-rank-fusion constant
