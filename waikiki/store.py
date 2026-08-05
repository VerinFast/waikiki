"""Repository chokepoint: page + image CRUD and settings access.

This module is the data-access **chokepoint** for the domain. Route handlers
(``api.py``), the HTML views, and the MCP server all go through it so Human and
LLM callers share identical logic (render + version + index) and no route owns
raw SQL. Routes parse/validate the request (pydantic), call a function here, and
shape the response — they never open a cursor themselves. All page/settings SQL
lives here or in the ``db`` infrastructure module it delegates to; see
``docs/repository-layer.md`` (RFC 0001, Phase 0).

The layering — ``routes → store (repository) → db (SQLite infra)`` — is the seam
where multi-tenant / per-wiki scoping (``WikiScope`` + Postgres RLS) attaches in
a later phase; keeping every read/write behind it now is what makes that possible
without touching a single route.
"""
from __future__ import annotations

import re
from typing import List, Optional

from . import db, edits, elements, rag, render, structure

_INCLUDE = re.compile(r"!\[\[([^\]]+?)\]\]")   # ![[Page]] / ![[Page#Section]] transclusion


# --- Pages --------------------------------------------------------------------

_SORTS = {"updated": "updated_at DESC", "title": "title COLLATE NOCASE ASC",
          # Manual drag-and-drop order; unordered pages fall to the end by title.
          "custom": "sort_order IS NULL, sort_order ASC, title COLLATE NOCASE ASC"}


def list_pages(include_deleted: bool = False, sort: str = "updated",
               starred_only: bool = False, include_children: bool = False) -> List[dict]:
    clauses = [] if include_deleted else ["deleted_at IS NULL"]
    if starred_only:
        clauses.append("starred = 1")
    if not include_children:
        clauses.append("parent_id IS NULL")  # children never show in the rail
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = _SORTS.get(sort, _SORTS["updated"])
    rows = db.get_conn().execute(
        f"SELECT slug, title, updated_at, deleted_at, starred, parent_id "
        f"FROM pages {where} ORDER BY {order}"
    ).fetchall()
    return [dict(r) for r in rows]


def children(parent_slug: str) -> List[dict]:
    """Direct child pages of a parent (not shown in the rail)."""
    parent = get_page(parent_slug)
    if not parent:
        return []
    rows = db.get_conn().execute(
        "SELECT slug, title FROM pages WHERE parent_id=? AND deleted_at IS NULL "
        "ORDER BY title COLLATE NOCASE", (parent["id"],)
    ).fetchall()
    return [dict(r) for r in rows]


def parent_of(page: Optional[dict]) -> Optional[dict]:
    """The parent page (slug + title) of `page`, or None if it is top-level.

    Repository home for the parent lookup that route handlers used to run as raw
    SQL against a cursor (RFC 0001, Phase 0 — no SQL in routes)."""
    parent_id = page.get("parent_id") if page else None
    if not parent_id:
        return None
    row = db.get_conn().execute(
        "SELECT slug, title FROM pages WHERE id=?", (parent_id,)
    ).fetchone()
    return dict(row) if row else None


def set_parent(slug: str, parent_slug: Optional[str]) -> Optional[dict]:
    """Make `slug` a child of `parent_slug` (or top-level if None). Re-indexes so
    its vectors move between the main and partitioned (child) indices."""
    conn = db.get_conn()
    page = get_page(slug)
    if not page:
        return None
    parent_id = None
    if parent_slug:
        parent = get_page(parent_slug)
        if not parent or parent["id"] == page["id"]:
            return None
        parent_id = parent["id"]
    conn.execute("UPDATE pages SET parent_id=? WHERE slug=?", (parent_id, slug))
    conn.commit()
    rag.reindex_page(page["id"], page["markdown"])  # route vectors correctly
    return get_page(slug)


def clone_page(slug: str) -> Optional[dict]:
    """Duplicate a page as a new top-level page ('<Title> (copy)')."""
    page = get_page(slug)
    if not page:
        return None
    return create_page(f"{page['title']} (copy)", page["markdown"], author="clone")


def toggle_star(slug: str) -> Optional[bool]:
    """Flip a page's starred state. Returns the new state, or None if missing."""
    conn = db.get_conn()
    page = get_page(slug)
    if not page:
        return None
    new = 0 if page.get("starred") else 1
    conn.execute("UPDATE pages SET starred=? WHERE slug=?", (new, slug))
    conn.commit()
    return bool(new)


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


def _normalize_newlines(markdown: str) -> str:
    """Store LF only. Browsers send CRLF from <textarea> on form submit; CRLF
    breaks the frontmatter fence match (dropping tags) and dirties diffs."""
    return (markdown or "").replace("\r\n", "\n").replace("\r", "\n")


def create_page(title: str, markdown: str = "", author: str = "human") -> dict:
    conn = db.get_conn()
    markdown = _normalize_newlines(markdown)
    base = render.slugify(title) or "untitled"
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM pages WHERE slug=?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    html = render_html(markdown)
    cur = conn.execute(
        "INSERT INTO pages(slug, title, markdown, html) VALUES (?,?,?,?)",
        (slug, title, markdown, html),
    )
    page_id = cur.lastrowid
    _snapshot(page_id, title, markdown, author)
    _index_meta(page_id, markdown)
    conn.commit()
    rag.reindex_page(page_id, markdown)
    if (b := _activity_bucket(author)):
        log_activity(b, "write")
    return get_page(slug)


def set_page_order(slugs: list[str]) -> None:
    """Persist the manual (Custom) sidebar order: sort_order = position."""
    conn = db.get_conn()
    for i, slug in enumerate(slugs):
        conn.execute("UPDATE pages SET sort_order=? WHERE slug=?", (i, slug))
    conn.commit()


def custom_order() -> List[str]:
    """Top-level page slugs in the current Custom (manual) order."""
    return [p["slug"] for p in list_pages(sort="custom")]


def move_page_order(slug: str, position: int) -> Optional[List[str]]:
    """Move `slug` to `position` in the Custom order (0-indexed): remove it, then
    insert at `position` — items between old and new shift by one, the rest stay.
    Returns the new order, or None if the slug isn't a top-level page."""
    order = custom_order()
    if slug not in order:
        return None
    order.remove(slug)
    position = max(0, min(int(position), len(order)))
    order.insert(position, slug)
    set_page_order(order)
    return order


# --- Activity (reads/writes for the wiki-info graph) --------------------------

def _activity_bucket(author: str) -> Optional[str]:
    if author == "human":
        return "human"
    if author in ("ai", "api", "collab"):   # collab = live CRDT (mostly AI-driven)
        return "ai"
    return None                             # system/seed — not counted


def log_activity(actor: str, action: str) -> None:
    if actor not in ("human", "ai"):
        return
    conn = db.get_conn()
    conn.execute("INSERT INTO activity(actor, action) VALUES (?, ?)", (actor, action))
    conn.commit()


def sweep_activity(days: int = 30) -> None:
    conn = db.get_conn()
    conn.execute("DELETE FROM activity WHERE ts < datetime('now', ?)",
                 (f"-{int(days)} days",))
    conn.commit()


def activity_last_7_days() -> List[dict]:
    """Per-day read/write counts by actor for the last 7 days (oldest first)."""
    import datetime

    rows = db.get_conn().execute(
        "SELECT date(ts) d, actor, action, COUNT(*) c FROM activity "
        "WHERE ts >= date('now','-6 days') GROUP BY d, actor, action").fetchall()
    agg = {(r["d"], r["actor"], r["action"]): r["c"] for r in rows}
    today = datetime.datetime.now(datetime.timezone.utc).date()
    out = []
    for i in range(6, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        out.append({
            "date": d,
            "human_read": agg.get((d, "human", "read"), 0),
            "ai_read": agg.get((d, "ai", "read"), 0),
            "human_write": agg.get((d, "human", "write"), 0),
            "ai_write": agg.get((d, "ai", "write"), 0),
        })
    return out


def _set_body(slug: str, title: str, markdown: str, author: str) -> None:
    """Core page write: render (link-by-title), save, snapshot, reindex."""
    conn = db.get_conn()
    markdown = _normalize_newlines(markdown)
    page = get_page(slug)
    html = render_html(markdown)
    conn.execute(
        "UPDATE pages SET title=?, markdown=?, html=?, updated_at=datetime('now'), "
        "deleted_at=NULL WHERE slug=?",
        (title, markdown, html, slug),
    )
    _snapshot(page["id"], title, markdown, author)
    _index_meta(page["id"], markdown)
    conn.commit()
    rag.reindex_page(page["id"], markdown)
    if (b := _activity_bucket(author)):
        log_activity(b, "write")


def update_page(slug: str, title: str, markdown: str, author: str = "human") -> Optional[dict]:
    page = get_page(slug)
    if not page:
        return None
    _set_body(slug, title, markdown, author)
    # If the title changed, rewrite [[old title]] links elsewhere so they follow
    # the rename (slugs stay stable, so existing links keep working regardless).
    if title.strip() and title.strip() != page["title"].strip():
        _rewrite_backlinks(slug, page["title"], title)
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


# --- Links & change feed ------------------------------------------------------

def _expand_includes(markdown: str, depth: int = 3) -> str:
    """Expand ![[Page]] / ![[Page#Section]] transclusions inline (bounded depth)."""
    if depth <= 0 or "![[" not in markdown:
        return markdown
    idx = link_index()

    def repl(m: re.Match) -> str:
        page_part, _, section = m.group(1).strip().partition("#")
        slug = idx.get(render.slugify(page_part))
        page = get_page(slug) if slug else None
        if not page:
            return m.group(0)
        _, _, body = structure.parse_frontmatter(page["markdown"])
        if section.strip():
            span = edits.section_span(body, section.strip())
            if span:
                body = body[span[0]:span[1]]
        return _expand_includes(body, depth - 1)

    return _INCLUDE.sub(repl, markdown)


_PROP = re.compile(r"\{\{\s*([A-Za-z0-9 _.\-]+?)\s*\}\}")


def _lookup_prop(meta: dict, key: str) -> Optional[str]:
    if key in meta:
        return meta[key]
    norm = key.replace(" ", "").lower()
    for k, v in meta.items():
        if k.replace(" ", "").lower() == norm:
            return v
    return None


def _interpolate_props(text: str, local_meta: dict) -> str:
    """Replace {{Key}} (this page's property) and {{Slug.Key}} (another page's)."""
    if "{{" not in text:
        return text

    def repl(m: re.Match) -> str:
        ref = m.group(1).strip()
        if "." in ref:
            slug_part, key = ref.split(".", 1)
            slug = link_index().get(render.slugify(slug_part))
            page = get_page(slug) if slug else None
            if not page:
                return m.group(0)
            meta, _t, _b = structure.parse_frontmatter(page["markdown"])
            val = _lookup_prop(meta, key.strip())
        else:
            val = _lookup_prop(local_meta, ref)
        return str(val) if val is not None else m.group(0)

    return _PROP.sub(repl, text)


def render_html(markdown: str) -> str:
    """Render for the active wiki: frontmatter infobox + transclusions +
    property interpolation + link-by-title + per-wiki HTML setting."""
    allow_html = db.get_setting("allow_html", "1") == "1"
    meta, _tags, body = structure.parse_frontmatter(markdown)
    body = _expand_includes(body)
    body = _interpolate_props(body, meta)
    # Custom elements: swap ```<slug> fenced blocks for Web Components (only bother
    # touching the DB registry when the page actually contains a fenced block).
    slots = []
    if "```" in body:
        reg = elements.registry()
        if reg:
            # Same resolver the prose uses, so element fields get link-by-title
            # resolution (and survive renames) rather than a client-side guess.
            body, slots = elements.expand(body, reg, link_index().get)
    html = render.render_markdown(body, link_index().get, allow_html=allow_html)
    if slots:
        html, used = elements.fill(html, slots)
        html += elements.defs_script(used)
    return render.infobox(meta) + html


def get_property(slug: str, key: str) -> Optional[str]:
    page = get_page(slug)
    if not page:
        return None
    meta, _t, _b = structure.parse_frontmatter(page["markdown"])
    return _lookup_prop(meta, key)


def set_property(slug: str, key: str, value: str) -> Optional[dict]:
    """Set a frontmatter property on a page (creating the frontmatter if absent)."""
    return set_properties(slug, {key: value})


def set_properties(slug: str, props: dict) -> Optional[dict]:
    """Set several frontmatter properties in ONE rewrite.

    Setting them one at a time re-parses, re-renders and re-indexes the page per
    key, and creates a version per key; batching keeps history readable and is
    much cheaper. Keys match existing ones case/space-insensitively. A value of
    None removes the property."""
    page = get_page(slug)
    if not page:
        return None
    meta, tags, body = structure.parse_frontmatter(page["markdown"])
    for key, value in (props or {}).items():
        target, norm = key, key.replace(" ", "").lower()
        for k in meta:
            if k.replace(" ", "").lower() == norm:
                target = k
                break
        if value is None:
            meta.pop(target, None)
        else:
            meta[target] = str(value)
    lines = [f"{k}: {v}" for k, v in meta.items()]
    if tags:
        lines.insert(0, "tags: " + ", ".join(tags))
    fm = ("---\n" + "\n".join(lines) + "\n---\n") if lines else ""
    return update_page(slug, page["title"], fm + body.lstrip("\n"), author="ai")


def content_version(markdown: str) -> str:
    """Short stable fingerprint of a page's text.

    Cheaper to compare than `updated_at`, and strictly more accurate: timestamps
    have one-second granularity (two saves in the same second look identical) and
    don't move at all for unsaved live edits. Hashing the text catches both."""
    import hashlib
    return hashlib.sha256((markdown or "").encode("utf-8")).hexdigest()[:12]


def page_metadata(slug: str) -> Optional[dict]:
    """Everything *about* a page without its body: properties, tags, lineage and
    timestamps. Agents use this to discover what a page records and to tell
    whether their copy is stale."""
    page = get_page(slug)
    if not page:
        return None
    meta, tags, _body = structure.parse_frontmatter(page["markdown"])
    parent = None
    if page.get("parent_id"):
        row = db.get_conn().execute(
            "SELECT slug, title FROM pages WHERE id=?", (page["parent_id"],)).fetchone()
        parent = dict(row) if row else None
    return {
        "slug": page["slug"],
        "title": page["title"],
        "properties": meta,
        "tags": tags or tags_of(slug),
        "parent": parent,
        "children": [c["slug"] for c in children(slug)],
        "starred": bool(page.get("starred")),
        "created_at": page.get("created_at"),
        "updated_at": page.get("updated_at"),
        "deleted_at": page.get("deleted_at"),
        "trashed": bool(page.get("deleted_at")),
        # Same token check_pages compares against (saved text; get_page's version
        # reflects live edits when a room is open).
        "version": content_version(page["markdown"]),
    }


def _index_meta(page_id: int, markdown: str) -> None:
    """Refresh a page's tags from its frontmatter."""
    _meta, tags, _body = structure.parse_frontmatter(markdown)
    conn = db.get_conn()
    conn.execute("DELETE FROM page_tags WHERE page_id=?", (page_id,))
    for t in dict.fromkeys(tags):  # dedupe, keep order
        conn.execute("INSERT OR IGNORE INTO page_tags(page_id, tag) VALUES (?, ?)",
                     (page_id, t))


def all_tags() -> List[dict]:
    return [dict(r) for r in db.get_conn().execute(
        "SELECT t.tag, COUNT(*) AS count FROM page_tags t JOIN pages p ON p.id=t.page_id "
        "WHERE p.deleted_at IS NULL GROUP BY t.tag ORDER BY t.tag").fetchall()]


def pages_with_tag(tag: str) -> List[dict]:
    return [dict(r) for r in db.get_conn().execute(
        "SELECT p.slug, p.title FROM page_tags t JOIN pages p ON p.id=t.page_id "
        "WHERE t.tag=? AND p.deleted_at IS NULL ORDER BY p.title COLLATE NOCASE",
        (tag.lower(),)).fetchall()]


def tags_of(slug: str) -> List[str]:
    page = get_page(slug)
    if not page:
        return []
    return [r["tag"] for r in db.get_conn().execute(
        "SELECT tag FROM page_tags WHERE page_id=? ORDER BY tag", (page["id"],)).fetchall()]


def rerender_all() -> int:
    """Re-render every page's HTML (after toggling allow_html). No new versions."""
    conn = db.get_conn()
    rows = conn.execute("SELECT slug, markdown FROM pages").fetchall()
    for r in rows:
        conn.execute("UPDATE pages SET html=? WHERE slug=?",
                     (render_html(r["markdown"]), r["slug"]))
    conn.commit()
    return len(rows)


def link_index() -> dict:
    """Map every active page's slug AND slugified-title to its canonical slug,
    so [[Title]] and [[slug]] both resolve (link-by-title)."""
    idx = {}
    for r in db.get_conn().execute(
            "SELECT slug, title FROM pages WHERE deleted_at IS NULL").fetchall():
        idx[r["slug"]] = r["slug"]
        idx[render.slugify(r["title"])] = r["slug"]
    return idx


def backlinks(slug: str) -> List[dict]:
    """Pages that link to `slug` ('what links here') — including sub-pages."""
    idx = link_index()
    out = []
    for p in list_pages(include_children=True):
        if p["slug"] == slug:
            continue
        page = get_page(p["slug"])
        targets = {idx.get(k, k) for k in render.extract_wikilinks(page["markdown"])}
        if slug in targets:
            out.append({"slug": p["slug"], "title": p["title"]})
    return out


def broken_links() -> List[dict]:
    """Every wikilink whose target page doesn't exist (red links) — scans all
    pages, sub-pages included."""
    idx = link_index()
    out = []
    for p in list_pages(include_children=True):
        page = get_page(p["slug"])
        for key in render.extract_wikilinks(page["markdown"]):
            if key not in idx:
                out.append({"from_slug": p["slug"], "from_title": p["title"],
                            "target": key})
    return out


def recent_changes(since: Optional[str] = None, limit: int = 50) -> List[dict]:
    """Version events across active pages, newest first — the change feed.
    `since` is an ISO datetime string ('YYYY-MM-DD HH:MM:SS')."""
    sql = ("SELECT p.slug, v.title, v.author, v.created_at "
           "FROM page_versions v JOIN pages p ON p.id = v.page_id "
           "WHERE p.deleted_at IS NULL ")
    args: list = []
    if since:
        sql += "AND v.created_at > ? "
        args.append(since)
    sql += "ORDER BY v.created_at DESC, v.id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in db.get_conn().execute(sql, args).fetchall()]


def _rewrite_backlinks(exclude_slug: str, old_title: str, new_title: str) -> None:
    old_key = render.slugify(old_title)

    def repl(m):
        target, label = m.group(1).strip(), m.group(2)
        page_part, sep, section = target.partition("#")
        if render.slugify(page_part) != old_key:
            return m.group(0)
        new_target = new_title + (("#" + section) if sep else "")
        return f"[[{new_target}{('|' + label) if label else ''}]]"

    for p in list_pages():
        if p["slug"] == exclude_slug:
            continue
        page = get_page(p["slug"])
        new_md = render._WIKILINK.sub(repl, page["markdown"])
        if new_md != page["markdown"]:
            _set_body(p["slug"], page["title"], new_md, author="rename")


# --- Comments & suggestions (review) ------------------------------------------

def comment_add(slug: str, body: str, author: str = "human") -> Optional[dict]:
    page = get_page(slug)
    if not page or not body.strip():
        return None
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO comments(page_id, author, body) VALUES (?,?,?)",
        (page["id"], author, body.strip()))
    conn.commit()
    return {"id": cur.lastrowid, "slug": slug}


def comments_list(slug: str, include_resolved: bool = True) -> List[dict]:
    page = get_page(slug)
    if not page:
        return []
    where = "" if include_resolved else "AND resolved=0"
    rows = db.get_conn().execute(
        f"SELECT id, author, body, resolved, created_at FROM comments "
        f"WHERE page_id=? {where} ORDER BY created_at", (page["id"],)).fetchall()
    return [dict(r) for r in rows]


def comment_resolve(comment_id: int) -> bool:
    conn = db.get_conn()
    conn.execute("UPDATE comments SET resolved=1 WHERE id=?", (comment_id,))
    conn.commit()
    return True


def suggestion_add(slug: str, markdown: str, note: str = "",
                   author: str = "ai") -> Optional[dict]:
    page = get_page(slug)
    if not page:
        return None
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO suggestions(page_id, author, note, markdown) VALUES (?,?,?,?)",
        (page["id"], author, note, markdown))
    conn.commit()
    return {"id": cur.lastrowid, "slug": slug}


def suggestions_list(slug: Optional[str] = None, status: str = "pending") -> List[dict]:
    conn = db.get_conn()
    if slug:
        page = get_page(slug)
        if not page:
            return []
        rows = conn.execute(
            "SELECT s.id, s.author, s.note, s.status, s.created_at, p.slug, p.title "
            "FROM suggestions s JOIN pages p ON p.id=s.page_id "
            "WHERE s.page_id=? AND s.status=? ORDER BY s.created_at DESC",
            (page["id"], status)).fetchall()
    else:
        rows = conn.execute(
            "SELECT s.id, s.author, s.note, s.status, s.created_at, p.slug, p.title "
            "FROM suggestions s JOIN pages p ON p.id=s.page_id "
            "WHERE s.status=? AND p.deleted_at IS NULL ORDER BY s.created_at DESC",
            (status,)).fetchall()
    return [dict(r) for r in rows]


def suggestion_get(sid: int) -> Optional[dict]:
    r = db.get_conn().execute(
        "SELECT s.*, p.slug, p.title FROM suggestions s JOIN pages p ON p.id=s.page_id "
        "WHERE s.id=?", (sid,)).fetchone()
    return dict(r) if r else None


def suggestion_apply(sid: int) -> Optional[dict]:
    s = suggestion_get(sid)
    if not s or s["status"] != "pending":
        return None
    page = update_page(s["slug"], s["title"], s["markdown"], author="suggestion")
    db.get_conn().execute("UPDATE suggestions SET status='applied' WHERE id=?", (sid,))
    db.get_conn().commit()
    return page


def suggestion_reject(sid: int) -> bool:
    conn = db.get_conn()
    conn.execute("UPDATE suggestions SET status='rejected' WHERE id=?", (sid,))
    conn.commit()
    return True


# --- Templates ----------------------------------------------------------------

def templates_list() -> List[dict]:
    return [dict(r) for r in db.get_conn().execute(
        "SELECT id, name, markdown FROM templates ORDER BY name COLLATE NOCASE").fetchall()]


def template_get(tid: int) -> Optional[dict]:
    r = db.get_conn().execute(
        "SELECT id, name, markdown FROM templates WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def template_by_name(name: str) -> Optional[dict]:
    r = db.get_conn().execute(
        "SELECT id, name, markdown FROM templates WHERE name=? COLLATE NOCASE",
        (name,)).fetchone()
    return dict(r) if r else None


def template_save(name: str, markdown: str, tid: Optional[int] = None) -> None:
    conn = db.get_conn()
    if tid:
        conn.execute("UPDATE templates SET name=?, markdown=? WHERE id=?",
                     (name, markdown, tid))
    else:
        conn.execute(
            "INSERT INTO templates(name, markdown) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET markdown=excluded.markdown",
            (name, markdown))
    conn.commit()


def template_delete(tid: int) -> None:
    conn = db.get_conn()
    conn.execute("DELETE FROM templates WHERE id=?", (tid,))
    conn.commit()


def create_from_template(template_name: str, title: str) -> Optional[dict]:
    tpl = template_by_name(template_name)
    if not tpl:
        return None
    md = tpl["markdown"].replace("{{title}}", title)
    return create_page(title, md, author="ai")


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


# --- Settings -----------------------------------------------------------------
# Route-facing settings access. The SQL itself lives in the ``db`` infrastructure
# chokepoint (the settings table is created and seeded there); these thin
# delegations are what routes call, so no handler reaches into the connection
# module directly. Non-route infrastructure (embeddings, imagegen, ai, the MCP
# server) keeps calling ``db`` directly — those are already below the repository.

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a wiki setting for the active wiki (repository seam over ``db``)."""
    return db.get_setting(key, default)


def set_setting(key: str, value: str) -> None:
    """Write a wiki setting for the active wiki (repository seam over ``db``)."""
    db.set_setting(key, value)


def all_settings() -> dict[str, str]:
    """Every setting for the active wiki (repository seam over ``db``)."""
    return db.all_settings()
