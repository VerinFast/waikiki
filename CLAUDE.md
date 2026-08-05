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
- `docs/rfc/0001-multi-tenant-waikiki.md` — the port-to-multi-tenant RFC.
- `docs/repository-layer.md` — the layering spec (RFC 0001, Phase 0).

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
   MCP server's active wiki is independent of the browser's.
5. **One code path for Human and LLM.** REST, HTML views, and MCP tools all go
   through `store`/`rag` so both callers get identical render + version + index.

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
