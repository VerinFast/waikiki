"""Configuration for Waikiki.

Env-level knobs live here; per-install preferences that the UI can change
(active theme, which embedder to use) live in the `settings` table in the DB
so they can be edited from the browser without touching code.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import __version__ as VERSION  # single source of truth for the release

# Project root and data location -------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
if os.environ.get("WAIKIKI_DATA"):
    _default_data = Path(os.environ["WAIKIKI_DATA"])
elif getattr(sys, "frozen", False):
    # Packaged .app: never write inside the (possibly read-only) bundle.
    _default_data = Path.home() / "Library" / "Application Support" / "Waikiki"
else:
    _default_data = ROOT / "data"
DATA_DIR = _default_data
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Legacy single-DB path (pre multi-wiki). Migrated into wikis/main.db on first run.
DB_PATH = Path(os.environ.get("WAIKIKI_DB", DATA_DIR / "waikiki.db"))
# Each wiki is an isolated SQLite file under here — no cross-wiki links possible.
WIKIS_DIR = DATA_DIR / "wikis"
# Wikis created automatically on first run (besides migrated "main").
SEED_WIKIS = ["Beaconlight", "Crosslake", "StartupOS"]
# Slug of the built-in Help wiki (seeded with help text + the AI system prompts).
HELP_WIKI = "help"

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
    # Active embedder: provider in {fastembed, local, voyage} + a model slug.
    # fastembed (ONNX, no torch) is the default so the packaged app stays small.
    "embedder_provider": "fastembed",
    "embedder_model": "BAAI/bge-small-en-v1.5",  # dim 384
    "model_library": '[{"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"}]',
    # Retention (per wiki): keep last N versions per page; purge trashed pages
    # after N days. 0 = unlimited / never.
    "retention_versions": "50",
    "retention_trash_days": "30",
    # Allow raw HTML in markdown (per wiki). Local/trusted use; off by default.
    "allow_html": "0",
    # AI text generation (the editor's "Generate" button). Provider is
    # {anthropic (cloud), ollama (local, e.g. phi3)}.
    "gen_provider": "anthropic",
    "gen_model": ANTHROPIC_MODEL,        # used when gen_provider == anthropic
    "gen_model_local": "phi3",           # an Ollama model tag, when == ollama
    "ollama_url": "http://localhost:11434",
    # Chat-with-an-article, powered by a local CLI {claude, gemini}. An empty
    # model means "use the CLI's own default".
    "chat_provider": "claude",
    "chat_model": "",
    # Image generation: `claude` writes the prompt, then this CLI (agy/gemini)
    # renders the PNG into the article's own folder. Empty model = CLI default.
    "image_cli": "agy",
    "image_model": "",
}

# Built-in page templates seeded into each wiki (editable/removable in the UI).
DEFAULT_TEMPLATES = [
    ("Meeting notes",
     "# {{title}}\n\n**Date:** \n**Attendees:** \n\n## Agenda\n- \n\n"
     "## Notes\n\n## Action items\n- [ ] \n"),
    ("How-to",
     "# {{title}}\n\n> One-line summary.\n\n## Prerequisites\n- \n\n"
     "## Steps\n1. \n2. \n\n## See also\n- [[ ]]\n"),
    ("Person",
     "# {{title}}\n\n| | |\n|---|---|\n| Role | |\n| Team | |\n| Contact | |\n\n"
     "## About\n\n## Notes\n"),
]

# Retrieval / chunking -----------------------------------------------------------
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150
RAG_TOP_K = 6
RRF_K = 60  # reciprocal-rank-fusion constant
