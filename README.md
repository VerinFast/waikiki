# 🌺 Waikiki

A small, local, SQLite-backed wiki built for **Human ↔ LLM collaboration** — where
Claude (via Claude Desktop over MCP) and you edit the **same document at the same
time**, seeing each other's changes live.

## Requirements → how

| Requirement | How |
|---|---|
| Python + SQLite-backed | FastAPI app; one SQLite file per wiki (`data/wikis/<slug>.db`) holds its pages, images, FTS index, and vectors |
| Multiple isolated wikis | Separate DB per wiki; pages, search, and `[[links]]` never cross |
| Themes | Swappable CSS in `waikiki/static/themes/` (default / dark / sepia) |
| Images | Upload / paste / drag → stored in SQLite, served at `/image/{id}` |
| Extensible | Clean `store` + `rag` + `collab` service layers; open-source deps |
| MCP + REST | `waikiki.mcp_server` (FastMCP) and `/api/*` share one code path |
| Markdown → HTML | `markdown-it-py` (GFM), `[[wiki links]]`, Pygments code |
| Tables | GFM tables via the `gfm-like` preset |
| Human editing | EasyMDE editor with live preview |
| Versioning | Every save snapshots to `page_versions` (human / ai / collab authored) |
| BM25 / RAG | SQLite **FTS5 `bm25()`** + **sqlite-vec** vectors, fused with RRF |
| **Real-time co-editing (CRDT)** | **Yjs / `pycrdt` room per (wiki, page); browser + Claude edit live with presence** |
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

### Save / Open wikis to files

Because each wiki is a single self-contained SQLite file, you can **Save** one to
a location you choose and **Open** an external wiki file back in — from the
**Wikis** page (⚙ in the header):

- **Save to file…** writes a consistent `.wiki` snapshot (safe even while the wiki
  is in use). In the desktop app this is a native Save dialog; in a browser it
  downloads the file.
- **Open wiki file…** validates the file and brings it in as a new isolated wiki.
  Native Open dialog in the app; a file upload in a browser.

`.wiki` files are just SQLite, so they're easy to back up, move between machines,
or share.

### History, trash & retention

- **Version history:** every save snapshots the page. Open a page → **History** to
  view any version (with a diff vs current) and **Restore** it. Retention keeps
  the last *N* versions per page (default 50; set per wiki in Settings, 0 = all).
- **Trash (soft delete):** deleting a page moves it to the **Trash** (header link),
  hidden from lists and search but restorable. **Restore** brings it back;
  **Delete forever** removes it permanently. Trashed pages are auto-purged after
  *N* days (default 30; per wiki in Settings, 0 = never). Claude's `delete_page`
  is also soft; it has `list_trash` / `restore_page` tools.
- **Wiki stats:** the **Wikis** page shows per-wiki article count, internal-link
  count (with **broken** — red-link — count), size on disk, and trash count.

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

The packaged app stores its data in **`~/Library/Application Support/Waikiki`**
(not inside the bundle), so it survives moving/replacing the app. Override with
`WAIKIKI_DATA`.

## Connect Claude Desktop (MCP)

**Easiest:** open Waikiki and click **Connect Claude** in the header (or visit
`/help`). That page shows a config **pre-filled with this install's real paths**
and a copy button — paste it into Claude Desktop's config and you're done.

The config file lives at
`~/Library/Application Support/Claude/claude_desktop_config.json` (create it if
it doesn't exist). Keep Waikiki running, paste the config, then **fully quit and
reopen Claude Desktop** (⌘Q). Two forms:

**A) Packaged app** — Claude Desktop launches the `.app` itself in MCP mode
(no source checkout needed). Replace the path with where your app lives:

```json
{
  "mcpServers": {
    "waikiki": {
      "command": "/Applications/Waikiki.app/Contents/MacOS/Waikiki",
      "env": {
        "WAIKIKI_MCP": "1",
        "WAIKIKI_DATA": "/Users/YOU/Library/Application Support/Waikiki",
        "WAIKIKI_WEB_URL": "http://127.0.0.1:8787"
      }
    }
  }
}
```

**B) Running from source** — launch the venv Python:

```json
{
  "mcpServers": {
    "waikiki": {
      "command": "/Users/YOU/localdev/waikiki/.venv/bin/python",
      "args": ["-m", "waikiki.mcp_server"],
      "env": {
        "PYTHONPATH": "/Users/YOU/localdev/waikiki",
        "WAIKIKI_DATA": "/Users/YOU/localdev/waikiki/data",
        "WAIKIKI_WEB_URL": "http://127.0.0.1:8787"
      }
    }
  }
}
```

> `WAIKIKI_DATA` **must match** the data directory the running app uses, so the
> MCP server and the window share the same wikis. The in-app Help page fills this
> in for you.

Then, in Claude: *"list the waikiki wikis and switch to Beaconlight."* Claude
must `switch_wiki` before it can read or write — this is what keeps wikis from
mixing. Open a page's editor and ask Claude to add a section; watch it type in
beside you.

MCP tools: `list_wikis`, `current_wiki`, `switch_wiki`, `create_wiki` (wiki
selection — required first), then `list_pages`, `get_page` (returns a heading
outline), `create_page`, and a family of **merge-safe live edit** tools:
`edit_page` (find/replace — preferred), `replace_section`, `insert_after` /
`insert_before`, `prepend_to_page`, `remove_from_page`, `append_to_page`, and
`replace_page` (full rewrite, last resort). Plus `changes_since` (change feed),
`backlinks`, `broken_links`, `delete_page` (to trash), `list_trash`,
`restore_page`, and `search` (hybrid RAG) — all scoped to the active wiki.

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

Interactive docs at `/docs`. Websocket sync at `ws://host/collab/{wiki}/{slug}`.
REST/collab requests select the wiki via the `X-Waikiki-Wiki` header (default:
the registry's default wiki).

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
