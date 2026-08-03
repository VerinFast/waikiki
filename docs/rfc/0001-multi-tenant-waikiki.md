# RFC 0001 — Multi-tenant Waikiki on Postgres

| | |
|---|---|
| **Status** | Draft — for discussion |
| **Author** | Jason + Claude |
| **Created** | 2026-08-03 |
| **Target** | `good-place` cluster (Kwirker staging / Electric Sheep prod) |
| **Supersedes** | — |

## 1. Summary

Port Waikiki from a single-user macOS app backed by per-wiki SQLite files to a
multi-tenant service inside the `good-place` cluster, backed by Postgres. The
desktop app remains, but becomes a **client** that can pull a wiki down for local
work rather than being the only place a wiki exists. The local AI integrations
(`claude`, `agy`, local MCP over stdio) are dropped in favour of the platform's
own agents.

This document is written to be argued with. Section 9 is the list of things I
think we actually need to decide; the rest is context and a proposed shape.

## 2. Motivation

Waikiki today is good at exactly one thing: a single person plus their local
agent, on one machine. v0.9–0.10 added LAN sharing and temporary public links,
which proved the collaboration model works but also showed the ceiling — a shared
password, one owner, and availability tied to a laptop being awake.

The goals:

- **Multiple people, persistently.** Real accounts, real permissions, no laptop.
- **Agents as first-class collaborators**, run by the platform rather than
  shelling out to a CLI on someone's Mac.
- **Keep the local story.** Offline work on a plane is a feature, not a
  regression.

Non-goals for this RFC: public/anonymous wikis, federation between deployments,
and billing design beyond "it uses the existing `plan` limits."

## 3. What exists today

Waikiki is ~6,700 LOC across 21 modules. The parts that matter here:

| Area | Today | Notes |
|---|---|---|
| Storage | One SQLite file **per wiki** | Isolation is *physical* |
| Search | FTS5 (BM25) + `sqlite-vec`, fused with RRF | `db.py`, `rag.py` |
| Collab | `pycrdt` rooms in-process + 1 s snapshot flusher | `collab.py` |
| Agents | stdio MCP server, ~50 tools, active wiki in a file | `mcp_server.py` |
| Local-only | image gen (`agy`), chat (`claude`), Bonjour, tunnel | ~500 LOC, to drop |
| Web | `api.py`, 1,310 LOC, routes call `store` directly | Has SQL in routes |
| Schema | 15 tables per wiki | Includes 2 FTS + 2 vec virtual tables |

The DB-specific surface is **concentrated**: FTS5 and `sqlite-vec` barely appear
outside `db.py` and `rag.py`, across ~134 SQL statements total. That is the
single most encouraging fact about this port.

## 4. What good-place already gives us

Reading `api/ARCHITECTURE` and `docs/content/tenancy.md`, most of the hard
platform work is already done and we should not reinvent it:

- **Row-level tenancy.** `tenant_id` on every business table; unique constraints
  include it; hard-delete only on tenant cascade.
- **Hybrid retrieval precedent.** `tool.description_long` → pgvector, plus a
  generated `search_tsv` TSVECTOR for the BM25 half. *This is Waikiki's exact
  retrieval design, already running in Postgres.*
- **Identity.** Keycloak OIDC, `user_tenant` membership, active-tenant cookie.
- **Authorisation.** Casbin with `/{tenant_id}/…` policy paths.
- **Limits.** `plan` rows + `Depends(enforce_plan(...))`.
- **Migrations.** Alembic, hand-reviewed.

And a set of principles the port must obey, notably: **routes own routing and
pydantic only — no SQL, no direct infra clients**, and **chokepoints are hard**
(one module per infra dependency). Waikiki's current `api.py` violates both.

## 5. Proposed architecture

### 5.1 Data model

Waikiki's `wiki` stops being a file and becomes a row. Every content table gains
`tenant_id` **and** `wiki_id`:

```
tenant (existing)
  └── wiki            (id, tenant_id, slug, name, visibility, created_by)
        ├── page      (id, tenant_id, wiki_id, slug, title, markdown, html, …)
        ├── page_version, page_tag, comment, suggestion
        ├── template, custom_element, activity
        ├── image     → object storage, row keeps metadata only
        └── chunk     (id, tenant_id, wiki_id, page_id, text,
                       embedding vector(384), search_tsv tsvector)
```

Notes:

- `chunk` collapses today's `chunks` + `chunks_fts` + `vec_chunks` +
  `vec_chunks_sub` into **one table**, matching the `tool` precedent: a
  `pgvector` column and a generated `tsvector`, with a GIN index on the latter
  and HNSW/IVFFlat on the former. The parent/child "sub-index" partition becomes
  a `parent_page_id` predicate rather than a second physical table.
- Slug uniqueness becomes `UNIQUE (wiki_id, slug)`.
- `image` blobs move out of Postgres to GCS (the platform already mounts it).
  Keeping multi-MB PNGs in rows was fine for a local file; it is not here.

### 5.2 The isolation problem (most important section)

Waikiki's headline guarantee is that **wikis cannot see each other** — no
cross-wiki links, no cross-wiki search, no leakage into an agent's context. Today
that is enforced by physics: different files, one connection each.

In Postgres it becomes a `WHERE wiki_id = ?` that a human can forget. That is a
**genuine downgrade in guarantee strength**, and the port is not acceptable
unless we replace physics with something equally hard to bypass. Options:

1. **Postgres RLS** with a per-request `SET LOCAL app.wiki_id`. Enforcement moves
   into the database; a forgotten predicate fails closed. Costs: every connection
   must set the GUC, and RLS interacts awkwardly with connection pooling.
2. **A repository chokepoint** — all wiki content access goes through
   `services/wiki_store.py`, which takes a `WikiScope` object and refuses to
   build a query without one. Consistent with the platform's chokepoint
   principle; enforced by review and tests, not by the engine.
3. **Both.** Repository for ergonomics, RLS as the backstop.

My recommendation is **(3)**, and I think this deserves the most scrutiny of
anything in this RFC. It is the difference between "isolated" and "isolated
until someone writes a JOIN."

### 5.3 Search

Direct translation, following `tool`:

| Today | Postgres |
|---|---|
| `chunks_fts` MATCH + `bm25()` | `search_tsv @@ websearch_to_tsquery`, ranked by `ts_rank_cd` |
| `vec_chunks` MATCH + distance | `embedding <=> :q` (pgvector, HNSW) |
| RRF fusion in Python | unchanged — the fusion code is already DB-agnostic |

`rag.py`'s fusion and chunking logic ports essentially as-is; only the two
retrieval helpers are rewritten. Embeddings move server-side (see 9.5).

### 5.4 Real-time collaboration — the actual hard problem

This, not the database, is the riskiest part. Today `collab.py` keeps CRDT rooms
in process memory and flushes snapshots on a 1 s timer. That model assumes
**exactly one server process**. On K8s with >1 replica, two users on the same
page can land on different pods and silently diverge.

Options, roughly in increasing order of effort:

1. **Sticky sessions by wiki** — route `/collab/{wiki}/{page}` by consistent hash
   so one page always lands on one pod. Cheap, but rolling deploys drop rooms and
   it caps a hot wiki at one pod.
2. **Externalise room state to Valkey** — the platform already runs it for queues
   and shared state. Y updates are small and append-only; a Valkey stream per room
   with pods as subscribers is a well-trodden pattern.
3. **A dedicated collab service** — one deployment that owns all rooms, with the
   API as a client. Cleanest separation, most moving parts.

I lean **(2)**, with **(1)** as an acceptable v1 if we accept the deploy caveat.
Worth noting: whatever we choose here also underpins §5.6.

### 5.5 Agents replace the local CLIs

Delete `imagegen.py`, `chat.py`, `clirun.py`, `shellenv.py`, `bonjour.py`,
`tunnel.py` (~500 LOC) and the assumption that a wiki has a shell.

- **Chat-with-an-article** → a platform agent invocation, with the page and RAG
  excerpts as context.
- **Image generation** → an agent with an image tool; result lands in GCS.
- **MCP** → the stdio server becomes an HTTP surface (or a registered `tool`
  row), so Janet/Michael and tenant agents can read and write wikis under normal
  Casbin policy rather than a stdio pipe and a file holding "active wiki".

The `switch_wiki` model needs rethinking: an agent's "active wiki" is currently
process-global state in a file. In a multi-tenant service it must be a parameter
on every call, scoped by policy. This is a small API change with a large
correctness benefit — the entire class of "agent wrote to the wrong wiki" bugs
disappears.

### 5.6 Desktop client and local sync

The desktop app stays, and gains a mode: **sign in, pick a wiki, pull it local**.

The good news is we already have the right substrate. Waikiki has run Yjs CRDTs
since v0.0.x; CRDTs are designed for exactly this (offline edits, later merge,
no lost writes). The sketch:

- Local SQLite remains the on-device store — unchanged code path, which is why
  the desktop app keeps working offline.
- Sync is per-page Y updates exchanged with the server, not row diffs.
- Images sync lazily; metadata first, blobs on demand.
- Conflict handling is the CRDT's job for page bodies. **Non-CRDT state
  (settings, templates, elements, page deletion) has no merge story yet** and
  needs one — see 9.4.

## 6. Migration path

Deliberately incremental; each phase is shippable.

| Phase | Scope | Exit criteria |
|---|---|---|
| 0 | Restructure `api.py` → routes + `services/` with no SQL in routes; extract a repository layer behind `WikiScope` | Existing SQLite app still passes its 172 tests |
| 1 | Postgres backend behind the repository; Alembic migrations; RLS | Both backends pass the same suite |
| 2 | Tenancy + Keycloak + Casbin; drop the shared-password model | A second user can be invited to a wiki |
| 3 | Search on tsvector + pgvector | Retrieval parity vs SQLite on a fixture corpus |
| 4 | Collab externalised (5.4) | Two pods, one page, no divergence under load |
| 5 | Agent integration; delete local-CLI modules | Chat + image gen work with no local binaries |
| 6 | Desktop sync client | Round-trip an offline edit |

Phase 0 is worth doing **regardless of whether we proceed** — it improves the
local app and is the precondition for everything else.

## 7. What we lose

Honesty about regressions:

- **Physical isolation** becomes logical (§5.2).
- **Zero-config.** Today Waikiki is "download, open." Hosted means accounts,
  network, and an operator.
- **Local model support.** Ollama-backed generation only makes sense on device.
- **The MCP-over-stdio ergonomics** — Claude Desktop pointing at a local binary
  is genuinely nice and won't survive as-is.

## 8. Alternatives considered

- **Keep SQLite, sync via Litestream/rqlite.** Preserves physical isolation and
  most code, but doesn't give us shared auth, RBAC, or agent integration — we'd
  rebuild the platform we already have.
- **Waikiki as a good-place *skill* rather than a service.** Cheaper, but wikis
  are user-facing surfaces with their own UI, not an agent capability.
- **Separate deployable using good-place only for OIDC.** Faster to stand up,
  but forfeits Casbin, plan limits, and audit — and creates a second tenancy
  model to keep in sync. Rejected unless we want Waikiki sellable standalone
  (see 9.1).

## 9. Open questions

These are the decisions I think we should work through; I've given my lean but
none of these are settled.

1. **Service inside good-place, or standalone product using its identity?**
   Everything else depends on this. *Lean: inside*, given the RBAC/limits/audit
   reuse — but "sellable standalone" is a real product question, not a technical
   one.
2. **Is a wiki owned by a tenant, a user, or a team?** Casbin can express any of
   them, but it drives the sharing UI and the invite flow. *Lean: tenant-owned
   with per-wiki role grants.*
3. **Do we keep the hard no-cross-wiki-links rule?** It's a strong product
   promise that prevents contamination of agent context. It also frustrates users
   who want one link. *Lean: keep it, and make "move/copy page" good instead.*
4. **What is the merge story for non-page state?** Settings, templates, custom
   elements, deletions. CRDT covers page bodies only. *Lean: last-writer-wins
   with a visible conflict log, server authoritative.*
5. **Who owns embeddings, and who pays?** Server-side embedding is required for
   shared search, but it's per-tenant cost and a plan-limit lever. Does the
   desktop client embed locally when offline and re-embed on sync?
6. **How do agents authenticate per-wiki?** As the invoking user (simple, but
   audit gets murky), or as a distinct agent principal with its own grants
   (cleaner, more policy to manage)? *Lean: agent principal — matches "agent
   identity stays opaque."*
7. **Collab: sticky-hash v1, or Valkey from the start?** (§5.4)
8. **Does the desktop app remain a full wiki, or become a cache?** "Full wiki
   that can also sync" is more useful and more work — it means two authorities
   forever.

## 10. Rough effort

Not an estimate to plan against, but a sense of shape. Phases 0–3 are the
tractable core: mechanical restructuring plus a well-precedented search port.
Phase 4 (collab) and Phase 6 (sync) are where the genuine research risk lives,
and both are the same underlying problem — distributed CRDT state. If we want to
de-risk early, prototype Phase 4 before committing to the rest.
