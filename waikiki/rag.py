"""Chunking, indexing, and hybrid (BM25 + vector) retrieval.

Search fuses two rankings over the chunk index with Reciprocal Rank Fusion:

    * BM25   via FTS5  (chunks_fts)          — always available
    * cosine via sqlite-vec (vec_chunks)     — when the extension loaded

If sqlite-vec isn't available the search silently falls back to BM25 only, so
the wiki keeps working everywhere.
"""
from __future__ import annotations

import re
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
        conn.executemany("DELETE FROM vec_chunks WHERE chunk_id=?",
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

    # Embed + store vectors (best-effort; BM25 still indexed via triggers).
    if db.VEC_AVAILABLE:
        try:
            import sqlite_vec

            embedder = embeddings.get_embedder()
            db.ensure_vec_table(embedder.dim)
            vectors = embedder.embed(pieces)
            conn.executemany(
                "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, ?)",
                [(cid, sqlite_vec.serialize_float32(v))
                 for cid, v in zip(chunk_ids, vectors)],
            )
        except Exception as exc:
            print(f"[waikiki] embedding failed for page {page_id}: {exc}")
    conn.commit()


def reindex_all() -> int:
    """Re-chunk and re-embed every page (use after switching embedders)."""
    conn = db.get_conn()
    pages = conn.execute("SELECT id, markdown FROM pages").fetchall()
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
        print(f"[waikiki] vector search failed: {exc}")
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
        f"WHERE c.id IN ({placeholders})",
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


def search_pages(query: str, limit: int = 20) -> List[dict]:
    """BM25 page-level search for the search box (title + body)."""
    rows = db.get_conn().execute(
        "SELECT p.slug, p.title, snippet(pages_fts, 1, '<mark>', '</mark>', ' … ', 12) AS snip "
        "FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid "
        "WHERE pages_fts MATCH ? ORDER BY bm25(pages_fts) LIMIT ?",
        (_fts_query(query), limit),
    ).fetchall()
    return [dict(r) for r in rows]
