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

# True when running inside the packaged desktop shell (set by waikiki_app before
# the server starts). The page uses it to choose the native dictation route,
# because WKWebView has no SpeechRecognition.
def is_desktop() -> bool:
    return os.environ.get("WAIKIKI_DESKTOP") == "1"


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
    # Allow raw HTML in markdown (per wiki). On by default: all content is local
    # and only editable by the user and their local agent, so HTML is safe here.
    "allow_html": "1",
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
    # A per-wiki house style applied to every generated image (e.g. "clockwork
    # punk pixel art RPG in the style of Dragon Warrior 7").
    "image_style_prompt": "",
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

# Built-in custom element(s) seeded into each wiki (editable/removable in the UI).
# Each: (slug, name, fields-json, shadow-DOM html, scoped css, encapsulated js).
# fields: [{"name","required","label"}]. Required fields must be present in a
# block or it renders an error. Extra fields flow through as rows.
DEFAULT_ELEMENTS = [
    (
        "infobox",
        "Infobox",
        '[{"name": "title", "required": true, "label": "Title"}]',
        '<div class="ib">\n'
        '  <div class="ib-title"></div>\n'
        '  <div class="ib-img"></div>\n'
        '  <table class="ib-rows"></table>\n'
        '</div>',
        ".ib{float:right;max-width:300px;margin:0 0 1rem 1.2rem;border:1px solid "
        "var(--border,#ccc);border-radius:10px;overflow:hidden;background:var(--surface,#fff);"
        "font-family:var(--font,sans-serif);font-size:.9rem;color:var(--text,#111)}\n"
        ".ib-title{background:var(--accent,#0969da);color:var(--accent-contrast,#fff);"
        "font-weight:700;padding:.5rem .7rem;text-align:center}\n"
        ".ib-img img{width:100%;display:block}\n"
        ".ib-rows{width:100%;border-collapse:collapse}\n"
        ".ib-rows th,.ib-rows td{border-top:1px solid var(--border,#eee);padding:.35rem .6rem;"
        "text-align:left;vertical-align:top}\n"
        ".ib-rows th{width:40%;color:var(--muted,#666);font-weight:600}",
        "// `html` holds each field already rendered by the server, so [[wiki\n"
        "// links]] in a value become real links — no link parsing needed here.\n"
        "root.querySelector('.ib-title').textContent = props.title || '';\n"
        "var img = props.image || props.Image;\n"
        "if (img) { var im = document.createElement('img'); im.src = img; "
        "im.alt = props.title || ''; root.querySelector('.ib-img').appendChild(im); }\n"
        "var tb = root.querySelector('.ib-rows');\n"
        "Object.keys(props).forEach(function (k) {\n"
        "  if (k.toLowerCase() === 'title' || k.toLowerCase() === 'image') return;\n"
        "  var tr = document.createElement('tr');\n"
        "  var th = document.createElement('th'); th.textContent = k;\n"
        "  var td = document.createElement('td');\n"
        "  td.innerHTML = (html && html[k] != null) ? html[k] : '';\n"
        "  tr.appendChild(th); tr.appendChild(td); tb.appendChild(tr);\n"
        "});",
    ),
]

# Retrieval / chunking -----------------------------------------------------------
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150
RAG_TOP_K = 6
RRF_K = 60  # reciprocal-rank-fusion constant


def mcp_server_config(web_url: str | None = None) -> dict:
    """The MCP server definition for *this* install, ready to hand to a client.

    Shared by the Connect-Claude page (copy-paste into Claude Desktop) and by the
    chat feature, which passes it to the CLI with --mcp-config so the agent can
    read the wiki it is answering about instead of only seeing what we pasted
    into its prompt.

    Handles both shapes of install: the packaged .app relaunches its own binary
    in MCP mode, while a source checkout runs the module.
    """
    import sys

    env = {"WAIKIKI_DATA": str(DATA_DIR), "WAIKIKI_WEB_URL": web_url or WEB_URL}
    if getattr(sys, "frozen", False):
        server = {"command": sys.executable, "env": {**env, "WAIKIKI_MCP": "1"}}
    else:
        server = {"command": sys.executable, "args": ["-m", "waikiki.mcp_server"],
                  "env": {**env, "PYTHONPATH": str(ROOT)}}
    return {"mcpServers": {"waikiki": server}}
