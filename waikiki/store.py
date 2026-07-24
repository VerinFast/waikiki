"""Page + image CRUD. Shared by the REST API, the HTML views, and the MCP server
so Human and LLM callers go through identical logic (render + version + index).
"""
from __future__ import annotations

from typing import List, Optional

from . import db, rag, render


# --- Pages --------------------------------------------------------------------

def list_pages() -> List[dict]:
    rows = db.get_conn().execute(
        "SELECT slug, title, updated_at FROM pages ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_page(slug: str) -> Optional[dict]:
    row = db.get_conn().execute(
        "SELECT * FROM pages WHERE slug=?", (slug,)
    ).fetchone()
    return dict(row) if row else None


def _snapshot(page_id: int, title: str, markdown: str, author: str) -> None:
    db.get_conn().execute(
        "INSERT INTO page_versions(page_id, title, markdown, author) VALUES (?,?,?,?)",
        (page_id, title, markdown, author),
    )


def create_page(title: str, markdown: str = "", author: str = "human") -> dict:
    conn = db.get_conn()
    base = render.slugify(title) or "untitled"
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM pages WHERE slug=?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    html = render.render_markdown(markdown)
    cur = conn.execute(
        "INSERT INTO pages(slug, title, markdown, html) VALUES (?,?,?,?)",
        (slug, title, markdown, html),
    )
    page_id = cur.lastrowid
    _snapshot(page_id, title, markdown, author)
    conn.commit()
    rag.reindex_page(page_id, markdown)
    return get_page(slug)


def update_page(slug: str, title: str, markdown: str, author: str = "human") -> Optional[dict]:
    conn = db.get_conn()
    page = get_page(slug)
    if not page:
        return None
    html = render.render_markdown(markdown)
    conn.execute(
        "UPDATE pages SET title=?, markdown=?, html=?, updated_at=datetime('now') WHERE slug=?",
        (title, markdown, html, slug),
    )
    _snapshot(page["id"], title, markdown, author)
    conn.commit()
    rag.reindex_page(page["id"], markdown)
    return get_page(slug)


def upsert_page(title: str, markdown: str, slug: Optional[str] = None,
                author: str = "human") -> dict:
    """Create if absent, otherwise update. Convenient for LLM callers."""
    if slug:
        existing = get_page(slug)
        if existing:
            return update_page(slug, title, markdown, author)
    # Also match by generated slug so repeat "create" calls update in place.
    guessed = render.slugify(title)
    if get_page(guessed):
        return update_page(guessed, title, markdown, author)
    return create_page(title, markdown, author)


def delete_page(slug: str) -> bool:
    conn = db.get_conn()
    page = get_page(slug)
    if not page:
        return False
    conn.execute("DELETE FROM pages WHERE slug=?", (slug,))  # cascades chunks/versions
    conn.commit()
    return True


def page_versions(slug: str) -> List[dict]:
    page = get_page(slug)
    if not page:
        return []
    rows = db.get_conn().execute(
        "SELECT id, title, author, created_at FROM page_versions "
        "WHERE page_id=? ORDER BY created_at DESC",
        (page["id"],),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Images -------------------------------------------------------------------

def save_image(filename: str, mimetype: str, data: bytes) -> int:
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO images(filename, mimetype, data) VALUES (?,?,?)",
        (filename, mimetype, data),
    )
    conn.commit()
    return cur.lastrowid


def get_image(image_id: int) -> Optional[dict]:
    row = db.get_conn().execute(
        "SELECT * FROM images WHERE id=?", (image_id,)
    ).fetchone()
    return dict(row) if row else None
