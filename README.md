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
| Extensible | Layered `routes → store (repository) → db (SQLite)`; open-source deps |
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
- **Each agent** gets its own active wiki, scoped to its MCP session. Two agents
  can work in two different wikis at once without moving each other, and the
  choice is never written to disk — so a restarted server leaves an agent with no
  active wiki rather than inheriting whichever one another agent last picked.

These are all independent by design — the human browsing Crosslake doesn't move
Claude, one agent doesn't move another, and vice-versa. To co-edit, ask Claude to
`switch_wiki` to the one you're in.

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

## Architecture — layers

Data access is layered so routes stay thin and all SQL lives behind one seam:

```
HTTP routes / MCP tools   api.py, mcp_server.py   parse + validate, call the repository
        ▼
Repository (data access)  store.py, elements.py, wikis.py, rag.py   owns the SQL
        ▼
Infrastructure            db.py   SQLite connection, schema, FTS5 + sqlite-vec
        ▼
SQLite file per wiki      data/wikis/<slug>.db
```

Route handlers **never open a cursor and never contain SQL** — they parse the
request, call a repository function (`store.get_page`, `store.parent_of`,
`store.get_setting`, …), and shape the response. That invariant is guarded by
`tests/test_repository_chokepoint.py`. This is Phase 0 of
[RFC 0001](docs/rfc/0001-multi-tenant-waikiki.md); see
[docs/repository-layer.md](docs/repository-layer.md) for the full rationale and
the seam where multi-tenant scoping attaches later.

### Typed page metadata (optional)

Frontmatter properties are plain strings, and stay plain strings. What a
**template** can declare is what those strings are supposed to *mean* — a few
lines of `name[*]: type` in the template editor (`str`, `int`, `float`, `bool`,
`date`, `list[str]`, or a choice like `player | npc`), compiled to `pydantic`
checks in [`waikiki/metaschema.py`](waikiki/metaschema.py).

Pages made from such a template carry a `template:` property and are checked
against it on their Metadata tab and through MCP's `get_metadata`/`set_metadata`.
Checking **warns; it never blocks**. A wiki is a place for half-finished notes:
the write always lands, the value on disk is never rewritten (`20 / 100` stays
`20 / 100`), and the mismatch is reported instead. A template that declares
nothing behaves exactly as it always did, and pages written before a schema
existed are untouched by it.

### The Y.Doc is canonical

A page's content is a **CRDT, not a string**. The full encoded `pycrdt` Y.Doc for
every page is persisted in `page_ydoc` and is the **source of truth**; the
`markdown`/`html` columns are a *projection* kept alongside it for full-text
search, rendering, and RAG. Every content write goes through the repository and
advances the canonical Y.Doc, so the CRDT accumulates real history across saves
and restarts (the live co-editing room in `collab.py` is just an editing buffer
whose flush lands through the same seam). See [`waikiki/ydoc.py`](waikiki/ydoc.py).

Because the persisted state is a genuine CRDT, Waikiki can **export** a page as a
snapshot (full Y.Doc + a content-addressed image sidecar) or a changelog (updates
since a peer's state vector), and **import** the same from a peer — the local half
of the round-trip with the hosted platform. The format is the vendored,
version-pinned [`wiki_interchange`](docs/vendoring.md) library; it carries content
only (no tenancy or permissions — those are the server's), regenerates embeddings
locally on import, and **rejects** an incompatible spec/protocol version rather
than merging bad bytes.

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

### Updates

**Settings → Updates** checks GitHub for a newer release and can install it: the
app backs up every wiki, downloads the release, verifies it, then quits and
relaunches itself to finish. Your content lives outside the bundle, so an update
replaces code only.

Because the app carries no Apple identity, it can't rely on macOS to tell a real
release from a tampered one — so every release zip is **Ed25519-signed** and the
app verifies it against a public key pinned at build time, *before* unpacking.
Anything unsigned, wrongly signed, or modified is refused, and a build with no
pinned key disables updating rather than trusting the download.

Checks are check-only and hourly at most; installing is always an explicit click,
since the swap restarts the app. Cutting a signed release is
`./scripts/release.sh v0.14.0` — see **[docs/updates.md](docs/updates.md)** for
the trust model, key handling, and failure modes.

### Doorman (optional)

If you also run [Doorman](https://github.com/VerinFast/doorman), Waikiki will
notice and use its much better speech for **Listen** and *"Say this word"*.

It is optional in every direction: Waikiki never starts or installs Doorman,
everything works identically without it, and you can decline the integration even
while Doorman is running (**Settings → Doorman**). See
**[docs/doorman.md](docs/doorman.md)**.

### Deep links

`waikiki://` URLs open the app at a specific place, and survive the app picking a
different port (which an `http://127.0.0.1:8787` link doesn't):

```
waikiki://beaconlight/meru            # a page
waikiki://beaconlight/meru#abilities  # a section
waikiki://beaconlight                 # a wiki's front page
waikiki://beaconlight?q=clockwork     # search, inside that wiki
waikiki://                            # the front page
```

**Page options → Copy link** copies one, and MCP `get_page` returns the same
thing as `link` — so an agent can hand you something you can actually open.

The wiki is the authority, so there are no reserved verbs to shadow a wiki named
`search` or `home`. A URL scheme is an input anything on the machine can fire, and
the app window has owner rights, so parsing is a strict **allow-list**: every slug
validated, paths constructed rather than echoed, search always scoped to the
validated wiki. See
**[docs/deep-links.md](docs/deep-links.md)**. Deep links work in the packaged
`.app` only — a source run has no `Info.plist` for macOS to route through.

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
selection — required first), then `list_pages` (top-level by default, like the
sidebar; `children=true` for the whole wiki, `children=["a-parent"]` for one
branch — and it always reports how many sub-pages it withheld, so an agent never
reads silence as absence), `get_page` (returns a heading outline plus its
resolved outbound `links` — target, title, the label the reader sees, and
whether the page exists, plus a one-line `hint` asking the agent to read the
linked pages it is about to rely on), `read_pages` (the same payload for up to 10
slugs in one call, so following those links costs one round-trip instead of ten —
slugs that don't exist come back in `missing` rather than failing the batch, and
anything past the cap in `dropped`), `create_page`, and a family of **merge-safe
live edit** tools:
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
