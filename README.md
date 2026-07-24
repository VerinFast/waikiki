# 🌺 Waikiki

A small, local, SQLite-backed wiki built for **Human ↔ LLM collaboration** — where
Claude (via Claude Desktop over MCP) and you edit the **same document at the same
time**, seeing each other's changes live.

## Requirements → how

| Requirement | How |
|---|---|
| Python + SQLite-backed | FastAPI app; one `data/waikiki.db` holds pages, images, FTS index, and vectors |
| Themes | Swappable CSS in `waikiki/static/themes/` (default / dark / sepia) |
| Images | Upload / paste / drag → stored in SQLite, served at `/image/{id}` |
| Extensible | Clean `store` + `rag` + `collab` service layers; open-source deps |
| MCP + REST | `waikiki.mcp_server` (FastMCP) and `/api/*` share one code path |
| Markdown → HTML | `markdown-it-py` (GFM), `[[wiki links]]`, Pygments code |
| Tables | GFM tables via the `gfm-like` preset |
| Human editing | EasyMDE editor with live preview |
| Versioning | Every save snapshots to `page_versions` (human / ai / collab authored) |
| BM25 / RAG | SQLite **FTS5 `bm25()`** + **sqlite-vec** vectors, fused with RRF |
| **Real-time co-editing (CRDT)** | **Yjs / `pycrdt` room per page; browser + Claude edit live with presence** |
| AI streaming | Claude writes into the live doc via MCP; plus an optional pull-model "Generate" button |

## Multiple isolated wikis

Waikiki hosts several **fully isolated** wikis (e.g. Beaconlight, Crosslake,
StartupOS) — each is its own SQLite file under `data/wikis/`. Pages, search, and
`[[wiki links]]` **never cross between wikis**: a link resolves only within the
active wiki's own database, so contamination is structurally impossible.

- **Humans** switch with the wiki dropdown in the header (a per-browser cookie).
- **Claude** has a *separate* active wiki, changed only with the MCP
  `switch_wiki` tool. Every content tool refuses to run until a wiki is chosen
  and **echoes the wiki it acted on**, so the AI can never silently cross wikis.

The two are independent by design — the human browsing Crosslake doesn't move
Claude, and vice-versa. To co-edit, ask Claude to `switch_wiki` to the one you're
in.

## How the collaboration works

```
   your browser  <--y-websocket-->  [ CRDT room (pycrdt) ]  <--HTTP inject--  MCP server
   (EasyMDE + Yjs, presence)               |                                  (Claude Desktop)
                                      (debounced)
                                           v
                          render HTML + snapshot to SQLite + RAG reindex
```

- Open a page's editor and you join a **CRDT room**; a colored presence chip shows
  who else is there.
- When Claude (through the MCP `append_to_page` / `replace_page` tools) writes,
  it lands in the **same room** and streams into your open editor live — and a
  **"Claude" presence chip** appears while it writes.
- Concurrent edits **merge** (CRDT), so you don't overwrite each other.
- Edits are debounced-persisted to SQLite, re-rendered to HTML, and re-embedded
  for search.

The old in-editor **✦ Generate** button is a *separate* convenience: it pulls a
one-off draft from the Anthropic API (needs `ANTHROPIC_API_KEY`). The real
collaboration path above uses no API key — the text comes from Claude over MCP.

## Setup

```bash
cd ~/localdev/waikiki
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

First run downloads the default embedding model (`fastembed` BGE-small, ONNX,
~130 MB) and warms it at startup. No PyTorch required.

**Embedding models are managed in the UI** — Settings → *Embedding model* → paste
a HuggingFace slug and click **Add model**. It downloads, becomes active, and all
pages are re-embedded (vector-dimension changes handled automatically). The
`fastembed` provider (ONNX, default) needs no PyTorch; the `local` provider
(any sentence-transformers slug) needs the optional `requirements-local.txt`.

## Run the web app

```bash
python run.py
# → http://127.0.0.1:8787
```

## Desktop app (macOS)

Waikiki ships as a native `.app` (a WKWebView window over the local server) — see
[Releases](https://github.com/VerinFast/waikiki/releases). Or build it yourself:

```bash
bash scripts/build_macos.sh
# → dist/Waikiki.app  (+ dist/Waikiki-macos.zip)
```

The app is currently **unsigned**, so on first launch macOS Gatekeeper will warn:
right-click the app → **Open** → **Open** to allow it (once). Signing/notarization
is a later step.

## Connect Claude Desktop (MCP)

Add Waikiki to `~/Library/Application Support/Claude/claude_desktop_config.json`
(keep the web app running so live edits reach your browser):

```json
{
  "mcpServers": {
    "waikiki": {
      "command": "/Users/jason/localdev/waikiki/.venv/bin/python",
      "args": ["-m", "waikiki.mcp_server"],
      "env": {
        "PYTHONPATH": "/Users/jason/localdev/waikiki",
        "WAIKIKI_WEB_URL": "http://127.0.0.1:8787"
      }
    }
  }
}
```

Restart Claude Desktop. Then, with a page's editor open in your browser, ask
Claude to "add a section on X to the kayaking page" and watch it type in beside
you.

MCP tools: `list_wikis`, `current_wiki`, `switch_wiki`, `create_wiki` (wiki
selection — required first), then `list_pages`, `get_page`, `create_page`,
`append_to_page` (live), `replace_page` (live), `delete_page`, `search` (hybrid
RAG) — all scoped to the active wiki.

## REST API

| Method | Path | |
|---|---|---|
| GET | `/api/pages` | list |
| POST | `/api/pages` | `{title, markdown}` |
| GET/PUT/DELETE | `/api/pages/{slug}` | get / update / delete |
| GET | `/api/search?q=&k=` | hybrid BM25+vector RAG |
| POST | `/api/images` | multipart upload |
| POST | `/api/collab/{slug}/append` · `/replace` | inject a live edit (used by MCP) |
| GET | `/api/collab/{slug}/live` | current live (unsaved) markdown |
| POST | `/api/ai/stream` | SSE token stream (pull-model Generate button) |

Interactive docs at `/docs`. Websocket sync at `ws://host/collab/{slug}`.

## Notes

- **sqlite-vec** loads via **apsw** (bundles a SQLite with loadable-extension
  support, which stock CPython often lacks). If unavailable, search degrades to
  **BM25-only** and everything else still works.
- Switching the embedder in Settings changes the vector dimension, so the vector
  index is rebuilt and all pages re-embedded automatically.
- Collaborative editing loads Yjs (`yjs`, `y-websocket`, `y-codemirror`) from
  esm.sh and EasyMDE from unpkg — an internet connection is needed for the
  editor libs (they can be vendored locally later). The read view, REST, and MCP
  work fully offline.
