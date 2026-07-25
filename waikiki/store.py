"""Page + image CRUD. Shared by the REST API, the HTML views, and the MCP server
so Human and LLM callers go through identical logic (render + version + index).
"""
from __future__ import annotations

from typing import List, Optional

from . import db, rag, render


# --- Pages --------------------------------------------------------------------

def list_pages(include_deleted: bool = False) -> List[dict]:
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    rows = db.get_conn().execute(
        f"SELECT slug, title, updated_at, deleted_at FROM pages {where} "
        "ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def list_trash() -> List[dict]:
    rows = db.get_conn().execute(
        "SELECT slug, title, updated_at, deleted_at FROM pages "
        "WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_page(slug: str) -> Optional[dict]:
    row = db.get_conn().execute(
        "SELECT * FROM pages WHERE slug=?", (slug,)
    ).fetchone()
    return dict(row) if row else None


def _snapshot(page_id: int, title: str, markdown: str, author: str) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO page_versions(page_id, title, markdown, author) VALUES (?,?,?,?)",
        (page_id, title, markdown, author),
    )
    _prune_versions(page_id)


def _prune_versions(page_id: int) -> None:
    """Keep only the most recent `retention_versions` snapshots for a page."""
    keep = int(db.get_setting("retention_versions", "50") or "0")
    if keep <= 0:
        return
    db.get_conn().execute(
        "DELETE FROM page_versions WHERE page_id=? AND id NOT IN "
        "(SELECT id FROM page_versions WHERE page_id=? "
        " ORDER BY created_at DESC, id DESC LIMIT ?)",
        (page_id, page_id, keep),
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
        "UPDATE pages SET title=?, markdown=?, html=?, updated_at=datetime('now'), "
        "deleted_at=NULL WHERE slug=?",
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


def soft_delete(slug: str) -> bool:
    """Move a page to the trash: hide from lists/search but keep it (restorable).
    Its search index is dropped so it stops surfacing in RAG."""
    conn = db.get_conn()
    page = get_page(slug)
    if not page or page.get("deleted_at"):
        return False
    conn.execute("UPDATE pages SET deleted_at=datetime('now') WHERE slug=?", (slug,))
    conn.commit()
    rag.remove_page(page["id"])
    return True


def restore(slug: str) -> bool:
    """Bring a page back from the trash and re-index it."""
    conn = db.get_conn()
    page = get_page(slug)
    if not page or not page.get("deleted_at"):
        return False
    conn.execute(
        "UPDATE pages SET deleted_at=NULL, updated_at=datetime('now') WHERE slug=?",
        (slug,),
    )
    conn.commit()
    rag.reindex_page(page["id"], page["markdown"])
    return True


def hard_delete(slug: str) -> bool:
    """Permanently delete a page (and its versions/chunks via cascade)."""
    conn = db.get_conn()
    if not get_page(slug):
        return False
    conn.execute("DELETE FROM pages WHERE slug=?", (slug,))
    conn.commit()
    return True


# Default page deletion is soft (safe for humans and the AI).
delete_page = soft_delete


def sweep_trash() -> int:
    """Hard-delete trashed pages older than `retention_trash_days` (active wiki)."""
    days = int(db.get_setting("retention_trash_days", "30") or "0")
    if days <= 0:
        return 0
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT slug FROM pages WHERE deleted_at IS NOT NULL "
        "AND deleted_at < datetime('now', ?)",
        (f"-{days} days",),
    ).fetchall()
    for r in rows:
        conn.execute("DELETE FROM pages WHERE slug=?", (r["slug"],))
    conn.commit()
    return len(rows)


def page_versions(slug: str) -> List[dict]:
    page = get_page(slug)
    if not page:
        return []
    rows = db.get_conn().execute(
        "SELECT id, title, author, created_at FROM page_versions "
        "WHERE page_id=? ORDER BY created_at DESC, id DESC",
        (page["id"],),
    ).fetchall()
    return [dict(r) for r in rows]


def get_version(version_id: int) -> Optional[dict]:
    row = db.get_conn().execute(
        "SELECT * FROM page_versions WHERE id=?", (version_id,)
    ).fetchone()
    return dict(row) if row else None


def restore_version(slug: str, version_id: int) -> Optional[dict]:
    """Roll a page back to an earlier version (recorded as a new version)."""
    page = get_page(slug)
    v = get_version(version_id)
    if not page or not v or v["page_id"] != page["id"]:
        return None
    return update_page(slug, v["title"], v["markdown"], author="restore")


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
