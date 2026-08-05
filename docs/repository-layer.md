# The repository chokepoint (RFC 0001, Phase 0)

Waikiki's data access is layered so that **routes own routing and validation
only** and **all SQL lives behind one repository chokepoint**. This is Phase 0 of
[RFC 0001](rfc/0001-multi-tenant-waikiki.md) — the mechanical restructure that is
worth doing on its own and is the precondition for the multi-tenant / Postgres
work that follows.

## The layers

```
HTTP routes / MCP tools        api.py, mcp_server.py
        │   parse + validate (pydantic), call the repository, shape the response
        ▼
Repository (domain data access)  store.py, elements.py, wikis.py, rag.py
        │   owns the SQL for pages, settings, tags, versions, search, …
        ▼
Infrastructure chokepoint        db.py
        │   SQLite connection, schema, FTS5 + sqlite-vec, backend selection
        ▼
SQLite file per wiki             data/wikis/<slug>.db
```

- **Routes never open a cursor and never contain SQL.** A handler in `api.py`
  parses the request, calls a repository function (`store.get_page`,
  `store.parent_of`, `store.get_setting`, …), and shapes the response. That
  invariant is enforced by `tests/test_repository_chokepoint.py`, which fails the
  build if a SQL literal or `get_conn()`/`.execute()`/`.fetch*()` reappears in
  `api.py`.
- **`store.py` is the repository.** Pages, images, versions, comments,
  suggestions, tags, activity, and the route-facing **settings** accessors live
  here. `elements.py`, `wikis.py`, and `rag.py` are sibling repositories for their
  own concerns (custom elements, the wiki registry, hybrid retrieval).
- **`db.py` is the infrastructure chokepoint** — the one module that knows about
  SQLite: the per-`(thread, wiki)` connection cache, the schema, FTS5, the
  `sqlite-vec` load, and the `apsw`/stdlib backend shim. It also holds the
  low-level `get_setting`/`set_setting`/`all_settings` accessors that the settings
  table needs; the repository's settings functions are thin seams over these, and
  infrastructure-internal callers (embeddings, imagegen, ai, the MCP server) call
  `db` directly because they already sit below the repository.

## Why the seam matters

Today isolation between wikis is **physical** — one SQLite file per wiki, one
connection each. RFC 0001 §5.2 turns that into a logical `WHERE wiki_id = ?` in
Postgres, which is a genuine downgrade in guarantee strength unless every read
and write is funnelled through a single place that cannot forget the predicate.

The repository chokepoint is that single place. Because every route already goes
through it, a later phase can attach a `WikiScope` object (and Postgres RLS as a
backstop) **at the repository boundary** without touching a single route handler.
Keeping routes free of ad-hoc SQL now is therefore an isolation property, not
just tidiness — it is the difference between "isolated" and "isolated until
someone writes a JOIN in a view function."

## What is explicitly *not* in Phase 0

Per RFC 0001's migration table, these come later and are **out of scope** here:

- Postgres, Alembic migrations, and RLS (Phase 1).
- Tenancy, Keycloak, Casbin, dropping the shared-password model (Phase 2).
- `tsvector` + `pgvector` search (Phase 3).
- Externalised CRDT collaboration (Phase 4).
- Agent integration and deleting the local-CLI modules — chat, imagegen, clirun,
  bonjour, tunnel (Phase 5).
- The desktop sync client (Phase 6).

Phase 0 changes **no** schema, storage, API surface, or auth behaviour. It is a
pure structural refactor; the existing test suite is the correctness anchor.
