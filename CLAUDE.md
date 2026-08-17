# Waikiki — Claude notes

Small local wiki for Human ↔ LLM collaboration: FastAPI + one SQLite file per
wiki, hybrid RAG (FTS5 BM25 + sqlite-vec, fused with RRF), a `pycrdt` room per
`(wiki, page)` for live co-editing, and an MCP server so Claude edits the same
document you do. This file is the agent-facing companion to `README.md`; keep the
two in parity (same substance, different voice) whenever you change either.

## Layout

- `waikiki/api.py` — FastAPI app: HTML views, JSON REST, image serving, AI SSE,
  the `/collab` CRDT websocket. **Routes only.**
- `waikiki/store.py` — the **repository chokepoint**: page/image/version/comment/
  suggestion/tag/activity CRUD **and** the route-facing settings accessors.
- `waikiki/elements.py`, `wikis.py`, `rag.py` — sibling repositories (custom
  elements, the wiki registry, hybrid retrieval).
- `waikiki/db.py` — infrastructure chokepoint: SQLite connection cache, schema,
  FTS5, sqlite-vec load, apsw/stdlib backend shim, low-level settings SQL.
- `waikiki/mcp_server.py` — FastMCP tool surface; shares `store`/`rag` with the API.
- `waikiki/collab.py` — CRDT rooms + the debounced snapshot flusher.
- `waikiki/ydoc.py` — **canonical Y.Doc persistence** + the wiki-interchange
  produce/consume round-trip. Sits below `store`, like `rag`.
- `waikiki/vendor/wiki_interchange/` — vendored, version-pinned interchange
  format (see `docs/vendoring.md`).
- `waikiki/doorman.py` — **optional** integration with the sibling Doorman app:
  detection plus nicer speech. Must never be required, started or installed —
  see `docs/doorman.md`.
- `waikiki/deeplink.py` — `waikiki://` deep links: the allow-list that turns an
  external URL into an in-app destination (see `docs/deep-links.md`).
- `waikiki/updater.py` — self-update for the packaged `.app`: signature-verified
  download, staging, and the detached bundle swap (see `docs/updates.md`).
- `docs/rfc/0001-multi-tenant-waikiki.md` — the port-to-multi-tenant RFC.
- `docs/repository-layer.md` — the layering spec (RFC 0001, Phase 0).
- `docs/vendoring.md` — vendored packages, pins, and re-sync steps.
- `docs/updates.md` — the auto-update trust model and release procedure.
- `docs/deep-links.md` — the `waikiki://` scheme and why it is an allow-list.
- `docs/doorman.md` — the optional Doorman integration and why it stays optional.

## Architectural rules (load-bearing)

1. **Routes own routing + pydantic validation only.** A handler parses/validates
   the request, calls a repository function, and shapes the response. **No raw
   SQL and no cursor use (`get_conn()`/`.execute()`/`.fetch*()`) in `api.py`** —
   `tests/test_repository_chokepoint.py` fails the build if either reappears.
2. **All SQL lives behind the repository.** `store.py` (and `elements`/`wikis`/
   `rag`) own the domain SQL; `db.py` is the only module that knows SQLite. Route
   settings access goes through `store.get_setting/set_setting/all_settings`, not
   `db.*`. Infrastructure-internal callers (embeddings, imagegen, ai, mcp_server)
   may call `db` directly — they already sit below the repository.
3. **The repository seam is where per-wiki / tenant scoping attaches later.**
   Keep every read/write behind it so a future `WikiScope` + Postgres RLS lands
   without touching a route. Isolation is the reason, not tidiness.
4. **Wikis are fully isolated.** Pages, search, and `[[links]]` never cross
   wikis. The active wiki is a per-request contextvar (`db.current_wiki`); the
   MCP server's active wiki is independent of the browser's **and of every other
   agent's** — it is keyed per MCP session (`mcp_server._ACTIVE_BY_SESSION`) and
   deliberately **not persisted**. Never store it in a module global or on disk:
   a shared pointer meant a respawned agent inherited another agent's wiki and
   wrote pages into it (issue #11). A fresh session has no active wiki and must
   call `switch_wiki`; refusing is correct, silently inheriting is not.
   `tests/test_cross_wiki_isolation.py` guards this end to end.
5. **One code path for Human and LLM.** REST, HTML views, and MCP tools all go
   through `store`/`rag` so both callers get identical render + version + index.
6. **The Y.Doc is the canonical persisted state; markdown is a projection.** Each
   page's full encoded `pycrdt` Y.Doc lives in `page_ydoc` and is the source of
   truth; `pages.markdown`/`html` are maintained *alongside* it for FTS, render
   and RAG. Every content write goes through `store` and advances the canonical
   Y.Doc (`store._sync_ydoc` → `ydoc.sync`) — never write markdown as truth and
   discard the doc. The live `collab.py` room is still just an editing buffer; its
   flush lands through `store` like any other write, so the canonical doc stays
   authoritative. See `waikiki/ydoc.py`.
7. **The Kahala ⟷ Waikiki round-trip is content-only and version-gated.** Export
   (`store.export_snapshot` / `export_changelog`) and import
   (`store.import_snapshot` / `import_changelog`) go through the repository, using
   the **vendored** `wiki_interchange` encoders. Payloads carry content only —
   never invent or require `tenant_id`/`wiki_id`/permissions (Kahala re-attaches
   those server-side); local embeddings are regenerated on import, never shipped.
   An incompatible spec/Yjs version is **rejected**, never merged. Re-sync the
   vendored lib per `docs/vendoring.md` and keep its pin in lockstep.
8. **The updater fails closed, and its trust root is pinned at build time.**
   `updater.py` downloads a bundle and then *executes* it, so it is the highest-
   privilege path in the app. Every release zip must Ed25519-verify against
   `PUBLIC_KEY_HEX` **before** it is expanded; no pinned key means updating is
   disabled, never "trust the download". Never source that key from the network,
   `app_config.json`, or any other writable place — a replaceable pinned key is
   not a trust root. The bundle's ad-hoc `codesign --sign -` proves nothing about
   origin; it is not a substitute. See `docs/updates.md`.

## Before committing

- `python -m pytest` — the suite is the correctness anchor. Don't weaken, skip,
  or `xfail` a test to make a refactor pass; fix the refactor.
- Keep files under ~500 lines where practical and keep `README.md` ⇄ `CLAUDE.md`
  ⇄ `docs/` in parity.

## Scope discipline (RFC 0001)

Phase 0 (this structure) changes **no** schema, storage, API surface, or auth.
Postgres/RLS (P1), tenancy/Keycloak/Casbin (P2), tsvector+pgvector search (P3),
externalised collab (P4), agents replacing the local CLIs — chat/imagegen/clirun/
bonjour/tunnel (P5), and the desktop sync client (P6) are **later phases**. Don't
pull them forward.

Note: canonical Y.Doc persistence + the `wiki_interchange` round-trip (rules 6–7,
`waikiki/ydoc.py`) belong to the newer **atd-v3 / Kahala** track (issue #3391),
not to RFC 0001's phases. They stack on the Phase 0 chokepoint but are their own
work: the interchange *format* and *canonical state*, not the P4/P6 transport.
The local desktop surface is deliberately left intact (the monorepo fold is a
separate ticket).
