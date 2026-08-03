"""Chunking, indexing, and hybrid (BM25 + vector) retrieval.

Search fuses two rankings over the chunk index with Reciprocal Rank Fusion:

    * BM25   via FTS5  (chunks_fts)          — always available
    * cosine via sqlite-vec (vec_chunks)     — when the extension loaded

If sqlite-vec isn't available the search silently falls back to BM25 only, so
the wiki keeps working everywhere.
"""
from __future__ import annotations

import re
import sys
from typing import List, Tuple

from . import config, db, embeddings


# --- Chunking -----------------------------------------------------------------

def chunk_text(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    size, overlap = config.CHUNK_CHARS, config.CHUNK_OVERLAP
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        # Prefer to break on a paragraph/sentence boundary near the window edge.
        if end < len(text):
            window = text[start:end]
            brk = max(window.rfind("\n\n"), window.rfind(". "))
            if brk > size * 0.5:
                end = start + brk + 1
        chunks.append(text[start:end].strip())
        start = max(end - overlap, end) if end == len(text) else end - overlap
    return [c for c in chunks if c]


# --- Indexing -----------------------------------------------------------------

def reindex_page(page_id: int, markdown: str) -> None:
    """Rebuild chunk + vector index for one page. Called on every save."""
    conn = db.get_conn()
    # Drop stale vectors for this page's old chunks, then the chunks themselves.
    old_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM chunks WHERE page_id=?", (page_id,)).fetchall()]
    if db.VEC_AVAILABLE and old_ids:
        for tbl in ("vec_chunks", "vec_chunks_sub"):
            conn.executemany(f"DELETE FROM {tbl} WHERE chunk_id=?",
                             [(i,) for i in old_ids])
    conn.execute("DELETE FROM chunks WHERE page_id=?", (page_id,))

    pieces = chunk_text(markdown)
    if not pieces:
        conn.commit()
        return

    chunk_ids: List[int] = []
    for ordinal, piece in enumerate(pieces):
        cur = conn.execute(
            "INSERT INTO chunks(page_id, ord, text) VALUES (?, ?, ?)",
            (page_id, ordinal, piece),
        )
        chunk_ids.append(cur.lastrowid)

    # Embed + store vectors. Child pages go into the partitioned sub-index so the
    # main index (used by RAG) stays small. (BM25 is still indexed via triggers.)
    if db.VEC_AVAILABLE:
        try:
            import sqlite_vec

            embedder = embeddings.get_embedder()
            db.ensure_vec_table(embedder.dim)
            vectors = embedder.embed(pieces)
            row = conn.execute("SELECT parent_id FROM pages WHERE id=?",
                               (page_id,)).fetchone()
            parent_id = row["parent_id"] if row else None
            if parent_id:
                conn.executemany(
                    "INSERT INTO vec_chunks_sub(chunk_id, parent_id, embedding) "
                    "VALUES (?, ?, ?)",
                    [(cid, parent_id, sqlite_vec.serialize_float32(v))
                     for cid, v in zip(chunk_ids, vectors)],
                )
            else:
                conn.executemany(
                    "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, ?)",
                    [(cid, sqlite_vec.serialize_float32(v))
                     for cid, v in zip(chunk_ids, vectors)],
                )
        except Exception as exc:
            print(f"[waikiki] embedding failed for page {page_id}: {exc}", file=sys.stderr)
    conn.commit()


def remove_page(page_id: int) -> None:
    """Drop a page's chunks + vectors from the index (used on soft-delete)."""
    conn = db.get_conn()
    old_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM chunks WHERE page_id=?", (page_id,)).fetchall()]
    if db.VEC_AVAILABLE and old_ids:
        for tbl in ("vec_chunks", "vec_chunks_sub"):
            conn.executemany(f"DELETE FROM {tbl} WHERE chunk_id=?",
                             [(i,) for i in old_ids])
    conn.execute("DELETE FROM chunks WHERE page_id=?", (page_id,))
    conn.commit()


def reindex_all() -> int:
    """Re-chunk and re-embed every active page (use after switching embedders)."""
    conn = db.get_conn()
    pages = conn.execute(
        "SELECT id, markdown FROM pages WHERE deleted_at IS NULL").fetchall()
    for p in pages:
        reindex_page(p["id"], p["markdown"])
    return len(pages)


# --- Retrieval ----------------------------------------------------------------

def _fts_query(text: str) -> str:
    tokens = re.findall(r"\w+", text.lower())
    return " OR ".join(f'"{t}"' for t in tokens) if tokens else '""'


def _bm25_chunks(query: str, k: int) -> List[int]:
    rows = db.get_conn().execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
        "ORDER BY bm25(chunks_fts) LIMIT ?",
        (_fts_query(query), k),
    ).fetchall()
    return [r["rowid"] for r in rows]


def _vector_chunks(query: str, k: int) -> List[int]:
    if not db.VEC_AVAILABLE:
        return []
    try:
        import sqlite_vec

        embedder = embeddings.get_embedder()
        db.ensure_vec_table(embedder.dim)
        qvec = embedder.embed([query])[0]
        rows = db.get_conn().execute(
            "SELECT chunk_id FROM vec_chunks "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (sqlite_vec.serialize_float32(qvec), k),
        ).fetchall()
        return [r["chunk_id"] for r in rows]
    except Exception as exc:
        print(f"[waikiki] vector search failed: {exc}", file=sys.stderr)
        return []


def search_chunks(query: str, k: int = config.RAG_TOP_K) -> List[dict]:
    """Hybrid retrieval over chunks. Returns dicts with page + snippet + score."""
    pool = max(k * 3, 20)
    bm25 = _bm25_chunks(query, pool)
    vec = _vector_chunks(query, pool)

    # Reciprocal Rank Fusion.
    scores: dict[int, float] = {}
    for ranking in (bm25, vec):
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (config.RRF_K + rank + 1)

    if not scores:
        return []
    top = sorted(scores, key=scores.get, reverse=True)[:k]

    placeholders = ",".join("?" * len(top))
    rows = db.get_conn().execute(
        f"SELECT c.id AS chunk_id, c.text, p.id AS page_id, p.slug, p.title "
        f"FROM chunks c JOIN pages p ON p.id = c.page_id "
        f"WHERE c.id IN ({placeholders}) AND p.deleted_at IS NULL "
        f"AND p.parent_id IS NULL",  # child pages are excluded from main RAG
        top,
    ).fetchall()
    by_id = {r["chunk_id"]: r for r in rows}
    return [
        {
            "chunk_id": cid,
            "page_id": by_id[cid]["page_id"],
            "slug": by_id[cid]["slug"],
            "title": by_id[cid]["title"],
            "text": by_id[cid]["text"],
            "score": round(scores[cid], 5),
        }
        for cid in top
        if cid in by_id
    ]


def search_subtree(query: str, parent_id: int, k: int = config.RAG_TOP_K) -> List[dict]:
    """Hybrid search restricted to one parent's child pages (its partition)."""
    conn = db.get_conn()
    pool = max(k * 3, 20)
    scores: dict[int, float] = {}

    if db.VEC_AVAILABLE:
        try:
            import sqlite_vec

            embedder = embeddings.get_embedder()
            db.ensure_vec_table(embedder.dim)
            qvec = embedder.embed([query])[0]
            rows = conn.execute(
                "SELECT chunk_id FROM vec_chunks_sub WHERE parent_id=? "
                "AND embedding MATCH ? ORDER BY distance LIMIT ?",
                (parent_id, sqlite_vec.serialize_float32(qvec), pool),
            ).fetchall()
            for rank, r in enumerate(rows):
                scores[r["chunk_id"]] = scores.get(r["chunk_id"], 0.0) + 1.0 / (config.RRF_K + rank + 1)
        except Exception as exc:
            print(f"[waikiki] subtree vector search failed: {exc}", file=sys.stderr)

    rows = conn.execute(
        "SELECT c.id AS cid FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
        "JOIN pages p ON p.id = c.page_id "
        "WHERE f.chunks_fts MATCH ? AND p.parent_id=? ORDER BY bm25(f.chunks_fts) LIMIT ?",
        (_fts_query(query), parent_id, pool),
    ).fetchall()
    for rank, r in enumerate(rows):
        scores[r["cid"]] = scores.get(r["cid"], 0.0) + 1.0 / (config.RRF_K + rank + 1)

    if not scores:
        return []
    top = sorted(scores, key=scores.get, reverse=True)[:k]
    placeholders = ",".join("?" * len(top))
    got = conn.execute(
        f"SELECT c.id AS chunk_id, c.text, p.slug, p.title FROM chunks c "
        f"JOIN pages p ON p.id = c.page_id WHERE c.id IN ({placeholders})", top,
    ).fetchall()
    by_id = {r["chunk_id"]: r for r in got}
    return [{"chunk_id": cid, "slug": by_id[cid]["slug"], "title": by_id[cid]["title"],
             "text": by_id[cid]["text"], "score": round(scores[cid], 5)}
            for cid in top if cid in by_id]


def search_pages(query: str, limit: int = 20) -> List[dict]:
    """BM25 page-level search for the search box (title + body)."""
    rows = db.get_conn().execute(
        "SELECT p.slug, p.title, snippet(pages_fts, 1, '<mark>', '</mark>', ' … ', 12) AS snip "
        "FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid "
        "WHERE pages_fts MATCH ? AND p.deleted_at IS NULL AND p.parent_id IS NULL "
        "ORDER BY bm25(pages_fts) LIMIT ?",
        (_fts_query(query), limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --- User-facing search: hybrid, index-scoped -------------------------------

MODES = ("hybrid", "keyword", "semantic")


def list_indices() -> List[dict]:
    """Searchable partitions (DB indices): all pages, plus one partition per
    parent page (its child pages, which live in their own vector sub-index)."""
    parents = db.get_conn().execute(
        "SELECT DISTINCT pa.id, pa.slug, pa.title FROM pages ch "
        "JOIN pages pa ON pa.id = ch.parent_id "
        "WHERE ch.deleted_at IS NULL AND pa.deleted_at IS NULL "
        "ORDER BY pa.title"
    ).fetchall()
    out = [{"key": "all", "label": "All pages"}]
    out += [{"key": f"parent:{p['slug']}", "label": f"Sub-pages of {p['title']}"}
            for p in parents]
    return out


def _resolve_parent(index: str):
    """parent_id for a 'parent:<slug>' index, else None (not a partition)."""
    if index and index.startswith("parent:"):
        row = db.get_conn().execute(
            "SELECT id FROM pages WHERE slug=?", (index.split(":", 1)[1],)).fetchone()
        return row["id"] if row else None
    return None


def _scope(index: str, exclude_children: bool):
    """('parent', id) for a partition; else ('main',) when excluding children
    (top-level only) or ('all',) for the whole wiki."""
    pid = _resolve_parent(index)
    if pid is not None:
        return ("parent", pid)
    return ("main",) if exclude_children else ("all",)


def _bm25(query: str, k: int, scope) -> List[int]:
    conn = db.get_conn()
    base = ("SELECT f.rowid AS cid FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
            "JOIN pages p ON p.id = c.page_id "
            "WHERE f.chunks_fts MATCH ? AND p.deleted_at IS NULL ")
    if scope[0] == "main":
        rows = conn.execute(base + "AND p.parent_id IS NULL ORDER BY bm25(f.chunks_fts) LIMIT ?",
                            (_fts_query(query), k)).fetchall()
    elif scope[0] == "parent":
        rows = conn.execute(base + "AND p.parent_id = ? ORDER BY bm25(f.chunks_fts) LIMIT ?",
                            (_fts_query(query), scope[1], k)).fetchall()
    else:  # all
        rows = conn.execute(base + "ORDER BY bm25(f.chunks_fts) LIMIT ?",
                            (_fts_query(query), k)).fetchall()
    return [r["cid"] for r in rows]


def _vec(query: str, k: int, scope) -> List[int]:
    if not db.VEC_AVAILABLE:
        return []
    try:
        import sqlite_vec

        embedder = embeddings.get_embedder()
        db.ensure_vec_table(embedder.dim)
        qvec = sqlite_vec.serialize_float32(embedder.embed([query])[0])
        conn = db.get_conn()
        if scope[0] == "parent":
            rows = conn.execute(
                "SELECT chunk_id FROM vec_chunks_sub WHERE parent_id=? "
                "AND embedding MATCH ? ORDER BY distance LIMIT ?",
                (scope[1], qvec, k)).fetchall()
            return [r["chunk_id"] for r in rows]
        if scope[0] == "main":
            rows = conn.execute(
                "SELECT chunk_id FROM vec_chunks WHERE embedding MATCH ? "
                "ORDER BY distance LIMIT ?", (qvec, k)).fetchall()
            return [r["chunk_id"] for r in rows]
        # all: merge the main index + every child partition, by distance
        a = conn.execute("SELECT chunk_id, distance FROM vec_chunks "
                         "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                         (qvec, k)).fetchall()
        b = conn.execute("SELECT chunk_id, distance FROM vec_chunks_sub "
                         "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                         (qvec, k)).fetchall()
        merged = sorted(list(a) + list(b), key=lambda r: r["distance"])
        out, seen = [], set()
        for r in merged:
            if r["chunk_id"] not in seen:
                seen.add(r["chunk_id"])
                out.append(r["chunk_id"])
            if len(out) >= k:
                break
        return out
    except Exception as exc:
        print(f"[waikiki] scoped vector search failed: {exc}", file=sys.stderr)
        return []


def _snippet(text: str, query: str, width: int = 220) -> str:
    import html

    text = " ".join((text or "").split())
    tokens = [t.lower() for t in re.findall(r"\w+", query) if t]
    low = text.lower()
    pos = min((low.find(t) for t in tokens if low.find(t) != -1), default=-1)
    if pos == -1:
        snip, lead, trail = text[:width], "", "…" if len(text) > width else ""
    else:
        start = max(0, pos - width // 3)
        snip = text[start:start + width]
        lead = "… " if start > 0 else ""
        trail = " …" if start + width < len(text) else ""
    out = html.escape(snip)
    for t in sorted(set(tokens), key=len, reverse=True):
        out = re.sub("(?i)(" + re.escape(t) + ")", r"<mark>\1</mark>", out)
    return lead + out + trail


def search(query: str, index: str = "all", mode: str = "hybrid",
           exclude_children: bool = False, limit: int = 20) -> List[dict]:
    """Page-level search over a chosen partition and mode. `index` is 'all' or
    'parent:<slug>'; `exclude_children` restricts an 'all' search to top-level
    pages. `mode` is hybrid (BM25 + embeddings), keyword (BM25), or semantic
    (embeddings). Falls back to BM25 if vectors are unavailable."""
    query = (query or "").strip()
    if not query:
        return []
    mode = mode if mode in MODES else "hybrid"
    scope = _scope(index, exclude_children)
    pool = max(limit * 4, 40)

    rankings: List[List[int]] = []
    if mode in ("hybrid", "keyword"):
        rankings.append(_bm25(query, pool, scope))
    if mode in ("hybrid", "semantic"):
        vec = _vec(query, pool, scope)
        if vec:
            rankings.append(vec)
    if not rankings:  # semantic requested but vectors off — fall back to BM25
        rankings.append(_bm25(query, pool, scope))

    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (config.RRF_K + rank + 1)
    if not scores:
        return []

    ids = list(scores)
    placeholders = ",".join("?" * len(ids))
    rows = db.get_conn().execute(
        f"SELECT c.id AS cid, c.text, p.slug, p.title FROM chunks c "
        f"JOIN pages p ON p.id = c.page_id WHERE c.id IN ({placeholders}) "
        f"AND p.deleted_at IS NULL", ids).fetchall()
    best: dict[str, dict] = {}
    for r in rows:
        s = scores[r["cid"]]
        cur = best.get(r["slug"])
        if cur is None or s > cur["_score"]:
            best[r["slug"]] = {"slug": r["slug"], "title": r["title"],
                               "_score": s, "_text": r["text"]}
    ranked = sorted(best.values(), key=lambda d: d["_score"], reverse=True)[:limit]
    return [{"slug": d["slug"], "title": d["title"],
             "snip": _snippet(d["_text"], query), "score": round(d["_score"], 5)}
            for d in ranked]
