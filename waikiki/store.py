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

import io
import json
import re
from typing import IO, List, Optional, Sequence

from . import db, edits, elements, metaschema, rag, render, structure, ydoc

_INCLUDE = re.compile(r"!\[\[([^\]]+?)\]\]")   # ![[Page]] / ![[Page#Section]] transclusion

# The frontmatter property that binds a page to the template whose metadata
# schema describes it. Like `tags`, it lives in the same block but is its own
# concept; see `apply_template` and `check_metadata`.
TEMPLATE_KEY = "template"


# --- Pages --------------------------------------------------------------------

_SORTS = {"updated": "p.updated_at DESC", "title": "p.title COLLATE NOCASE ASC",
          # Manual drag-and-drop order; unordered pages fall to the end by title.
          "custom": "p.sort_order IS NULL, p.sort_order ASC, p.title COLLATE NOCASE ASC"}


def list_pages(include_deleted: bool = False, sort: str = "updated",
               starred_only: bool = False,
               include_children: bool | str | Sequence[str] | None = False) -> List[dict]:
    """Pages in the active wiki, newest first by default.

    ``include_children`` decides how deep the listing reaches:

    * ``False`` (default) — top-level pages only. The sidebar rail depends on
      this, so it stays the default.
    * ``True`` — every page, sub-pages included.
    * a sequence of parent slugs (or a single slug string) — top-level pages
      plus the *direct* children of exactly those parents. One branch of a big
      wiki, without the other 200 pages.

    Every row carries both ``parent_id`` (the internal integer) and
    ``parent_slug``, so a returned child is navigable by callers — notably
    agents — that never see page ids (issue #45).
    """
    if isinstance(include_children, str):        # a bare slug, not a char sequence
        parents = [render.slugify(include_children)]
        all_children = False
    elif include_children is None or isinstance(include_children, bool):
        parents = []
        all_children = bool(include_children)
    else:
        parents = [render.slugify(s) for s in include_children if s]
        all_children = False
    clauses = [] if include_deleted else ["p.deleted_at IS NULL"]
    args: list = []
    if starred_only:
        clauses.append("p.starred = 1")
    if not all_children:
        if parents:
            marks = ",".join("?" for _ in parents)
            clauses.append(f"(p.parent_id IS NULL OR par.slug IN ({marks}))")
            args.extend(parents)
        else:
            clauses.append("p.parent_id IS NULL")  # children never show in the rail
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = _SORTS.get(sort, _SORTS["updated"])
    rows = db.get_conn().execute(
        f"SELECT p.slug, p.title, p.updated_at, p.deleted_at, p.starred, "
        f"p.parent_id, par.slug AS parent_slug "
        f"FROM pages p LEFT JOIN pages par ON par.id = p.parent_id "
        f"{where} ORDER BY {order}", args
    ).fetchall()
    return [dict(r) for r in rows]


def count_child_pages(include_deleted: bool = False) -> int:
    """How many pages have a parent — i.e. how many a top-level-only listing
    leaves out. Lets a caller report what it withheld instead of implying the
    wiki is smaller than it is (issue #45)."""
    where = "WHERE parent_id IS NOT NULL" + ("" if include_deleted else " AND deleted_at IS NULL")
    row = db.get_conn().execute(f"SELECT COUNT(*) AS n FROM pages {where}").fetchone()
    return int(dict(row)["n"]) if row else 0


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


def ancestors(page: Optional[dict], limit: int = 32) -> List[dict]:
    """The parent chain above `page`, outermost first, for breadcrumbs.

    `limit` and the seen-set are not paranoia: `set_parent` lets a page be
    reparented under its own descendant, and a cycle here would hang the request
    that renders it. A truncated trail is better than a hung page.
    """
    trail: List[dict] = []
    seen = {page.get("id")} if page else set()
    current = page
    while len(trail) < limit:
        parent_id = current.get("parent_id") if current else None
        if not parent_id or parent_id in seen:
            break
        seen.add(parent_id)
        row = db.get_conn().execute(
            "SELECT id, slug, title, parent_id FROM pages "
            "WHERE id=? AND deleted_at IS NULL", (parent_id,)
        ).fetchone()
        if not row:
            break
        current = dict(row)
        trail.append({"slug": current["slug"], "title": current["title"]})
    trail.reverse()
    return trail


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


def create_page(title: str, markdown: str = "", author: str = "human",
                slug: Optional[str] = None) -> dict:
    """Create a page, deriving the slug from the title unless one is given.

    ``slug`` exists for the interchange round-trip, where the slug is not ours to
    invent: a bundle's page hierarchy is expressed *by slug*, so a page that
    landed under a title-derived slug would leave every ``parent_slug`` pointing
    at nothing. A requested slug still goes through the uniqueness loop.
    """
    conn = db.get_conn()
    markdown = _normalize_newlines(markdown)
    base = render.slugify(slug or title) or "untitled"
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
    # Canonical state before the derived index — see the note in `_set_body`.
    _sync_ydoc(page_id, slug, title, markdown)
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


def _sync_ydoc(page_id: int, slug: str, title: str, markdown: str) -> None:
    """Advance the page's canonical Y.Doc to match a projection write.

    The Y.Doc is the source of truth (``page_ydoc``); ``pages.markdown``/``html``
    are the projection this repository writes for FTS/render/RAG. Every content
    write keeps the two in step via this call — see ``waikiki/ydoc.py``."""
    _meta, tags, _body = structure.parse_frontmatter(markdown)
    ydoc.sync(page_id, slug, title, markdown, list(tags))


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
    # The canonical Y.Doc is written *before* the search index, because the two
    # are not the same kind of thing: `page_ydoc` is the source of truth (rule 6)
    # while the RAG index is a derived cache that `reindex_page` can rebuild from
    # the markdown at any time. With the old order, anything that raised in
    # reindex — an embedder that isn't ready yet, a missing sqlite-vec, a model
    # download failing — committed the projection and then skipped the canonical
    # write, silently leaving the Y.Doc a revision behind. That is not
    # hypothetical: the Help wiki's About page in this developer's install still
    # carries version 0.18.0 in its Y.Doc and 0.21.0 in its markdown. See
    # `docs/data-safety.md` (question 1).
    _sync_ydoc(page["id"], slug, title, markdown)
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
    # The frontmatter table is NOT prepended here any more — it lives on the
    # page's Metadata tab. Custom elements authored in the body (infoboxes,
    # stat blocks) are untouched: those are deliberate content, this was an
    # automatic dump of every key.
    return html


def get_property(slug: str, key: str) -> Optional[str]:
    page = get_page(slug)
    if not page:
        return None
    meta, _t, _b = structure.parse_frontmatter(page["markdown"])
    return _lookup_prop(meta, key)


def set_property(slug: str, key: str, value: str) -> Optional[dict]:
    """Set a frontmatter property on a page (creating the frontmatter if absent)."""
    return set_properties(slug, {key: value})


def set_properties(slug: str, props: dict, author: str = "ai") -> Optional[dict]:
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
    return update_page(slug, page["title"], fm + body.lstrip("\n"), author=author)


def replace_properties(slug: str, props: dict,
                       author: str = "human") -> Optional[dict]:
    """Replace a page's frontmatter properties wholesale, in the order given.

    `set_properties` merges, which cannot express a removal or a rename from a
    form that submits the whole set. This writes exactly `props`, so the editor's
    view is the truth.

    Tags are untouched: they live in the same block but are their own concept,
    and a `tags` key here would produce two competing sources. The caller is
    expected to have filtered it out.
    """
    page = get_page(slug)
    if not page:
        return None
    _meta, tags, body = structure.parse_frontmatter(page["markdown"])
    lines = [f"{k}: {v}" for k, v in (props or {}).items()]
    if tags:
        lines.insert(0, "tags: " + ", ".join(tags))
    fm = ("---\n" + "\n".join(lines) + "\n---\n") if lines else ""
    return update_page(slug, page["title"], fm + body.lstrip("\n"), author=author)


def set_tags(slug: str, tags: List[str], author: str = "human") -> Optional[dict]:
    """Replace a page's tags by rewriting the frontmatter `tags:` line.

    The markdown is the source of truth: writing `page_tags` directly would leave
    the page's own text disagreeing with its tags, and the next save from the
    editor would silently revert it. Going through `update_page` re-parses,
    re-indexes and advances the canonical Y.Doc like any other content write.

    Tags are normalised the way `parse_frontmatter` reads them back — lowercased,
    de-duplicated, and with the `,`/`;` separators stripped out of each tag so a
    value cannot smuggle in an extra one.
    """
    page = get_page(slug)
    if not page:
        return None
    meta, _old, body = structure.parse_frontmatter(page["markdown"])
    clean: List[str] = []
    seen = set()
    for tag in tags or []:
        norm = " ".join(re.sub(r"[,;]", " ", str(tag)).split()).lower()
        if norm and norm not in seen:
            seen.add(norm)
            clean.append(norm)
    lines = [f"{k}: {v}" for k, v in meta.items()]
    if clean:
        lines.insert(0, "tags: " + ", ".join(clean))
    fm = ("---\n" + "\n".join(lines) + "\n---\n") if lines else ""
    return update_page(slug, page["title"], fm + body.lstrip("\n"), author=author)


def content_version(markdown: str) -> str:
    """Short stable fingerprint of a page's text.

    Cheaper to compare than `updated_at`, and strictly more accurate: timestamps
    have one-second granularity (two saves in the same second look identical) and
    don't move at all for unsaved live edits. Hashing the text catches both."""
    import hashlib
    return hashlib.sha256((markdown or "").encode("utf-8")).hexdigest()[:12]


def _unchecked(template: Optional[str] = None, found: bool = True) -> dict:
    """"Nothing constrains this page" — the answer for every page that predates
    a schema. Built fresh each call so no caller can mutate a shared default."""
    return {"template": template, "template_found": found, "ok": True,
            "errors": [], "fields": [], "values": {}}


def check_metadata(props: dict) -> dict:
    """Validate a page's properties against its template's declared schema.

    Reports, never blocks: the result says whether the page matches what its
    template expects, and the caller decides how loudly to say so. A page with no
    ``template:`` property, a template that no longer exists, or a template that
    declares nothing all come back ``ok`` with no fields — which is every page in
    every wiki that existed before this feature.
    """
    name = _lookup_prop(props or {}, TEMPLATE_KEY)
    if not name or not str(name).strip():
        return _unchecked()
    name = str(name).strip()
    tpl = template_by_name(name)
    schema_text = (tpl or {}).get("meta_schema") or ""
    if not tpl or not schema_text.strip():
        return _unchecked(name, bool(tpl))
    return {"template": name, "template_found": True,
            **metaschema.validate(props, schema_text)}


def metadata_schema(slug: str) -> Optional[dict]:
    """`check_metadata` for a stored page (None if there is no such page)."""
    page = get_page(slug)
    if not page:
        return None
    meta, _tags, _body = structure.parse_frontmatter(page["markdown"])
    return check_metadata(meta)


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
        # What the page's template expects of those properties, and whether they
        # match. `ok` with no fields when nothing declares a schema.
        "schema": check_metadata(meta),
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


def _page_titles() -> dict:
    """Map each active page's canonical slug to its title."""
    return {r["slug"]: r["title"] for r in db.get_conn().execute(
        "SELECT slug, title FROM pages WHERE deleted_at IS NULL").fetchall()}


def outbound_links(markdown: str) -> List[dict]:
    """The wikilinks in *this markdown*, resolved against the wiki — the
    'what does this page point at' half of `backlinks`.

    One row per distinct (target, label), in first-seen order:

        target   canonical slug to fetch (the slugified target if unresolved)
        title    the target page's title, or None when it doesn't exist
        label    the wording the reader sees. This is the point of the function:
                 in [[Edaphos|earth]] the visible word is "earth" but the page
                 is `edaphos`, so a caller that slugifies the label lands on
                 nothing.
        exists   False for a red link. Kept rather than filtered — a red link
                 says "this page is a stub worth writing", and it stops a caller
                 fetching a 404.
        count    how many times that link appears (nine [[Igni]]s → one row).

    Takes markdown rather than a slug so the caller can describe the text it is
    actually handing over — MCP `get_page` may return unsaved live edits that are
    newer than the stored copy. Same-page [[#section]] links are not outbound and
    never appear."""
    idx = link_index()
    titles = _page_titles()
    rows: dict[tuple, dict] = {}
    for ref in render.extract_wikilink_refs(markdown):
        slug = idx.get(ref["target"])
        key = (slug or ref["target"], ref["label"])
        if key in rows:
            rows[key]["count"] += 1
            continue
        rows[key] = {"target": key[0], "title": titles.get(slug) if slug else None,
                     "label": ref["label"], "exists": slug is not None, "count": 1}
    return list(rows.values())


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
        "SELECT id, name, markdown, meta_schema FROM templates "
        "ORDER BY name COLLATE NOCASE").fetchall()]


def template_get(tid: int) -> Optional[dict]:
    r = db.get_conn().execute(
        "SELECT id, name, markdown, meta_schema FROM templates WHERE id=?",
        (tid,)).fetchone()
    return dict(r) if r else None


def template_by_name(name: str) -> Optional[dict]:
    r = db.get_conn().execute(
        "SELECT id, name, markdown, meta_schema FROM templates "
        "WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return dict(r) if r else None


def template_save(name: str, markdown: str, tid: Optional[int] = None,
                  meta_schema: Optional[str] = None) -> None:
    """Create or update a template.

    ``meta_schema`` is the optional metadata declaration (``waikiki.metaschema``).
    ``None`` means *leave it as it is* — an agent updating a template's markdown
    must not silently drop the schema a human authored, and vice versa. Pass ``""``
    to clear it (which is what the template editor's empty textarea does).
    """
    conn = db.get_conn()
    if tid:
        if meta_schema is None:
            conn.execute("UPDATE templates SET name=?, markdown=? WHERE id=?",
                         (name, markdown, tid))
        else:
            conn.execute(
                "UPDATE templates SET name=?, markdown=?, meta_schema=? WHERE id=?",
                (name, markdown, meta_schema, tid))
    elif meta_schema is None:
        conn.execute(
            "INSERT INTO templates(name, markdown) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET markdown=excluded.markdown",
            (name, markdown))
    else:
        conn.execute(
            "INSERT INTO templates(name, markdown, meta_schema) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET markdown=excluded.markdown, "
            "meta_schema=excluded.meta_schema",
            (name, markdown, meta_schema))
    conn.commit()


def template_delete(tid: int) -> None:
    conn = db.get_conn()
    conn.execute("DELETE FROM templates WHERE id=?", (tid,))
    conn.commit()


def apply_template(tpl: dict, title: str = "") -> str:
    """Materialise a template's markdown for a new page.

    ``{{title}}`` is filled in, and — only when the template declares a metadata
    schema — a ``template:`` frontmatter property is stamped in so the page
    remembers what it is meant to be. The stamp lives in the markdown rather than
    in a column because the markdown is the projection of the canonical Y.Doc: it
    rides along with versions, exports and the Kahala interchange round-trip,
    where a side-table pointer would be lost. It is also visible and editable —
    a human can retype it to re-bind an old page to a template, or delete it to
    opt out.

    A template with no schema produces exactly the bytes it did before.
    """
    md = (tpl.get("markdown") or "").replace("{{title}}", title)
    if not (tpl.get("meta_schema") or "").strip():
        return md
    meta, tags, body = structure.parse_frontmatter(md)
    if _lookup_prop(meta, TEMPLATE_KEY) is not None:
        return md                            # the template says so itself
    lines = [f"{TEMPLATE_KEY}: {tpl['name']}"] + [f"{k}: {v}" for k, v in meta.items()]
    if tags:
        lines.insert(0, "tags: " + ", ".join(tags))
    return "---\n" + "\n".join(lines) + "\n---\n" + body.lstrip("\n")


def create_from_template(template_name: str, title: str) -> Optional[dict]:
    tpl = template_by_name(template_name)
    if not tpl:
        return None
    return create_page(title, apply_template(tpl, title), author="ai")


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


# --- Wiki-interchange: canonical Y.Doc snapshot + changelog round-trip --------
# The repository is the chokepoint for the Kahala <-> Waikiki round-trip too:
# routes/MCP call these, never the CRDT layer directly. "Produce" exports this
# wiki's content for import-up to Kahala; "consume" applies content sent
# export-down from Kahala. All payloads are content-only (no tenant_id/wiki_id/
# permissions — those are the server's to re-attach) and local embeddings are
# regenerated on import, never shipped. See ``waikiki/ydoc.py``.

def export_snapshot(slug: str) -> Optional[bytes]:
    """A page as a wiki-interchange snapshot (full Y.Doc + image sidecar)."""
    page = get_page(slug)
    return ydoc.export_snapshot(page) if page else None


def page_state_vector(slug: str) -> Optional[bytes]:
    """A page's Yjs state vector (what a peer sends to request a changelog)."""
    page = get_page(slug)
    return ydoc.state_vector(page) if page else None


def export_changelog(slug: str, since_state_vector: bytes) -> Optional[bytes]:
    """The updates a peer at ``since_state_vector`` is missing for a page."""
    page = get_page(slug)
    return ydoc.export_changelog(page, since_state_vector) if page else None


def import_snapshot(raw: bytes, author: str = "import") -> dict:
    """Create/update a page from a wiki-interchange snapshot (export-down).

    Runs the version gate (an incompatible envelope raises before any write),
    projects the decoded content into the store — which re-renders, re-indexes
    RAG locally and versions it — then persists the *decoded* Y.Doc as the
    canonical state, preserving the sender's CRDT lineage so later changelog
    sync stays incremental. Matching is by ``slug`` (falling back to title)."""
    imp = ydoc.decode_snapshot(raw)
    title = imp.title or (imp.slug or "Untitled")
    # A snapshot is the sender's whole state, so it is the later write for every
    # field. Project its tags into the frontmatter when they only live in the
    # doc's `tags` root, or the local tag index would silently disagree with the
    # canonical doc it was just handed.
    #
    # Both "before" values are empty because there is no prior local state to
    # compare against — everything here arrived in this envelope. Passing the
    # decoded root as `before_root` would compare it against itself, making
    # root_moved always false, so a disagreeing frontmatter would overwrite the
    # peer's canonical tags. Empty makes ties resolve to the `tags` root (which
    # the schema defines as canonical) and lets frontmatter win only when it is
    # the sole source of tags.
    content = ydoc.reconcile_tags(imp.doc, before_root=[], before_frontmatter=[])
    page = upsert_page(title, content, slug=imp.slug, author=author)
    ydoc.persist(page["id"], imp.doc)   # keep the sender's lineage authoritative
    return get_page(page["slug"])


def import_changelog(slug: str, raw: bytes, author: str = "import") -> Optional[dict]:
    """Merge an incoming changelog into a page and re-project it (export-down).

    Version-gated. Merges the update into the page's canonical Y.Doc, projects
    the merged content back into the store, then persists the merged state as
    canonical. Returns None if the page doesn't exist locally."""
    page = get_page(slug)
    if not page:
        return None
    # Snapshot both tag homes *before* the merge, so we can tell which one the
    # incoming update actually moved and let that side win (see reconcile_tags).
    before = ydoc.canonical_doc(page)
    before_root = ydoc.tags_of(before)
    _m, before_fm, _b = structure.parse_frontmatter(page["markdown"])

    doc = ydoc.apply_changelog(page, raw)   # version gate + CRDT merge
    content = ydoc.reconcile_tags(doc, before_root, list(before_fm))
    # Project the MERGED doc, not the local row: the doc is the source of truth,
    # and reading the old title here dropped remote renames on the floor.
    title = ydoc.title_of(doc) or page["title"]
    _set_body(slug, title, content, author=author)
    ydoc.persist(page["id"], doc)           # keep the merged lineage authoritative
    return get_page(slug)


# --- Wiki-interchange: the whole-wiki bundle (gather / apply) -----------------
# A snapshot is one page; a *bundle* is the wiki above it — what the RFC's D4
# actually asks for ("both produce/consume a snapshot (whole wiki) and a
# changelog (incremental)"). The repository gathers the rows and performs the
# writes; ``ydoc`` owns the format calls. Same content-only boundary as the
# per-page path: no tenant_id/wiki_id/permissions, and embeddings are regenerated
# locally rather than shipped.

def _export_page_rows() -> List[dict]:
    """Every live page with what the bundle needs, its parent named by slug.

    The join deliberately drops a parent that is itself in the trash: a bundle is
    self-contained, and a ``parent_slug`` naming a page the bundle does not carry
    is refused by the format layer — correctly, since it would orphan the child
    on import.
    """
    rows = db.get_conn().execute(
        "SELECT p.*, par.slug AS parent_slug FROM pages p "
        "LEFT JOIN pages par ON par.id = p.parent_id AND par.deleted_at IS NULL "
        "WHERE p.deleted_at IS NULL ORDER BY p.slug"
    ).fetchall()
    return [dict(r) for r in rows]


def _export_elements() -> List[dict]:
    out = []
    for e in elements.list_elements():
        try:
            fields = json.loads(e.get("fields") or "[]")
        except ValueError:
            fields = []
        out.append({"slug": e["slug"], "name": e["name"], "fields": fields,
                    "html": e["html"], "css": e["css"], "js": e["js"]})
    return out


def _export_templates() -> List[dict]:
    return [{"name": t["name"], "markdown": t["markdown"] or "",
             "meta_schema": t.get("meta_schema") or ""} for t in templates_list()]


def export_wiki_bundle(dest: Optional[IO[bytes]] = None,
                       label: Optional[str] = None) -> Optional[bytes]:
    """Gather the whole active wiki into a wiki-interchange bundle.

    Carries the pages (each as its canonical Y.Doc), their hierarchy **by slug**,
    manual order and starred flags, the custom elements, the templates with the
    metadata schemas they declare, and one copy of each distinct image blob.
    Trashed pages are left behind — the trash is local housekeeping, not content.

    Streams into ``dest`` when given: a real wiki here is 215 pages / ~57MB, and
    pages are gathered one at a time, so peak memory is one page and its images
    rather than the wiki. Called without ``dest`` it returns the whole bundle as
    ``bytes``, which is only reasonable for a small wiki (the tests, an MCP
    caller); prefer the file form for anything real.
    """
    from . import wikis

    if label is None:
        label = wikis.name_of(db.active_wiki())
    pages = _export_page_rows()
    els, tpls = _export_elements(), _export_templates()
    if dest is not None:
        ydoc.export_bundle(dest, pages, elements=els, templates=tpls, label=label)
        return None
    buf = io.BytesIO()
    ydoc.export_bundle(buf, pages, elements=els, templates=tpls, label=label)
    return buf.getvalue()


def _place_page(slug: str, parent_slug: Optional[str],
                sort_order: Optional[int], starred: bool) -> None:
    """Put an imported page where the bundle says it belongs."""
    conn = db.get_conn()
    conn.execute("UPDATE pages SET starred=?, sort_order=? WHERE slug=?",
                 (1 if starred else 0,
                  int(sort_order) if sort_order is not None else None, slug))
    conn.commit()
    set_parent(slug, parent_slug)      # None = top level; also re-routes vectors


def _upsert_by_slug(slug: str, title: str, markdown: str, author: str) -> dict:
    """Create or update the page with exactly this slug.

    Not ``upsert_page``: that derives a new page's slug from its title, which
    would land an imported page under a slug nothing in the bundle references.
    """
    if get_page(slug):
        return update_page(slug, title, markdown, author)
    return create_page(title, markdown, author, slug=slug)


def _read_bundle(reader) -> None:
    """Decode every part of a bundle without writing anything (the dry run).

    This is how the import gets its all-or-nothing property against a bad
    payload — the realistic failure mode, since the bundle arrives from a peer.
    Every page envelope is version-gated and re-validated content-only, every
    section is shape-checked, and every image blob is re-hashed against the
    digest it was stored under, *before* the first local write. Page 140 of 215
    failing therefore leaves the wiki untouched rather than half-imported.

    What this does not cover is a failure of the local writes themselves (a full
    disk, a lock). Making that atomic too would mean staging the whole wiki in a
    scratch database and swapping the file, which is a bigger change than this
    issue: see the note in ``docs/vendoring.md``.
    """
    reader.elements()
    reader.templates()
    for _img in reader.iter_images():          # re-hashes each blob
        pass
    for entry in reader.iter_pages(attach_blobs=False):
        ydoc.read_bundle_page(entry)           # version gate + content-only


def import_wiki_bundle(source, author: str = "import") -> dict:
    """Apply a whole-wiki bundle into the **active** wiki (export-down).

    ``source`` is a bundle's bytes or a readable, seekable binary file. Pages are
    merged by slug — an existing page is updated (and versioned) in place, a new
    one is created under the slug the bundle names, and nothing local is ever
    deleted. Definitions land first (templates, elements), then each distinct
    image blob is re-homed **once** and the ``/image/<id>`` references rewritten
    to match, then the pages, then their placement (parent, order, starred) once
    every page exists to be pointed at.

    Every write goes through the ordinary repository path, so imported content is
    rendered, versioned and re-indexed exactly like a human's edit — and the
    embeddings are regenerated here rather than shipped, per the round-trip's
    content-only rule. Raises before touching the wiki if the bundle is
    malformed, version-incompatible, or carries a server-only field.

    Returns a count of what landed.
    """
    with ydoc.open_bundle(source) as reader:       # gates run on open
        _read_bundle(reader)                       # dry run: nothing written yet

        tpls = reader.templates()
        for tpl in tpls:
            template_save(tpl.name, tpl.markdown, meta_schema=tpl.meta_schema)
        els = reader.elements()
        for el in els:
            elements.save_element(el.slug, el.name, el.fields, el.html, el.css, el.js)

        remap = ydoc.rehome_bundle_images(reader)
        landed: dict[str, str] = {}
        placements: list[tuple] = []
        for entry in reader.iter_pages(attach_blobs=False):
            imp = ydoc.read_bundle_page(entry, remap)
            title = imp.title or entry.title or entry.slug
            # The bundle is the sender's whole state, so it is the later write for
            # every field; both "before" values are empty because there is no
            # prior local state that this envelope did not bring (see the same
            # reasoning in ``import_snapshot``).
            content = ydoc.reconcile_tags(imp.doc, before_root=[],
                                          before_frontmatter=[])
            page = _upsert_by_slug(entry.slug, title, content, author)
            ydoc.persist(page["id"], imp.doc)      # keep the sender's lineage
            landed[entry.slug] = page["slug"]
            placements.append((entry.slug, entry.parent_slug,
                               entry.sort_order, entry.starred))

        for slug, parent, order, starred in placements:
            parent_slug = landed.get(parent) if parent else None
            _place_page(landed[slug], parent_slug, order, starred)

    return {"pages": len(landed), "elements": len(els),
            "templates": len(tpls), "images": len(set(remap.values()))}


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
