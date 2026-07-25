"""MCP server for Waikiki — lets Claude edit the wiki, with strict wiki isolation.

Waikiki hosts several fully-isolated wikis (e.g. Beaconlight, Crosslake,
StartupOS). To prevent cross-wiki contamination, this server keeps its OWN
active-wiki pointer, separate from whatever the human is viewing in the browser.
Every content tool refuses to run until you pick a wiki with `switch_wiki`, and
every result echoes the wiki it acted on.

    Typical flow: list_wikis() → switch_wiki("beaconlight") → search()/get_page()/
    append_to_page() … then switch_wiki("crosslake") to move deliberately.

Run over stdio (Claude Desktop):  python -m waikiki.mcp_server
Live-edit tools POST to the running web app (which owns the CRDT rooms) with an
X-Waikiki-Wiki header; read/search tools open the wiki's SQLite file directly.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from mcp.types import Icon

from . import config, db, edits, rag, render, store, wikis

WEB = config.WEB_URL
_ACTIVE_FILE = config.DATA_DIR / "mcp_active_wiki"

# Connector-list logo: a 🌺 drawn as text inside a tiny SVG, inlined as a data
# URI so no asset needs hosting. Clients render it with the system emoji font,
# replacing the plain "W" initial fallback.
_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<text x="32" y="33" font-size="52" text-anchor="middle" '
    'dominant-baseline="central">\U0001f33a</text></svg>'
)
_ICON = Icon(
    src="data:image/svg+xml;utf8," + quote(_ICON_SVG, safe=""),
    mimeType="image/svg+xml",
    sizes=["any"],
)

mcp = FastMCP(
    "🌺 Waikiki",
    icons=[_ICON],
    instructions=(
        "Waikiki has multiple isolated wikis. You MUST call switch_wiki(slug) "
        "before reading or writing pages; content tools error otherwise. Wikis "
        "never share content — switching is the only way to cross between them. "
        "Every result includes the 'wiki' it acted on; check it to avoid mixing "
        "contexts.\n\n"
        "To CHANGE an existing page, prefer edit_page (a targeted find/replace) or "
        "append_to_page — these apply as surgical live edits that merge with a "
        "human editing the same page. Use replace_page ONLY to rewrite a page from "
        "scratch; it overwrites everything and discards concurrent human edits. "
        "Workflow: get_page to read exact current text, then edit_page with a "
        "unique snippet."
    ),
)


def _load_active() -> str | None:
    try:
        val = _ACTIVE_FILE.read_text().strip()
        return val if val and wikis.exists(val) else None
    except Exception:
        return None


def _save_active(slug: str) -> None:
    try:
        _ACTIVE_FILE.write_text(slug)
    except Exception:
        pass


_ACTIVE: str | None = None


def _require_wiki() -> str:
    if _ACTIVE is None:
        raise RuntimeError(
            "No active wiki. Call list_wikis() then switch_wiki(slug) before "
            "reading or writing pages."
        )
    db.current_wiki.set(_ACTIVE)  # scope direct DB access to this wiki
    return _ACTIVE


def _headers() -> dict:
    return {"X-Waikiki-Wiki": _ACTIVE} if _ACTIVE else {}


# --- Wiki selection -----------------------------------------------------------

@mcp.tool
def list_wikis() -> dict:
    """List the available isolated wikis and which one is currently active."""
    return {"active": _ACTIVE, "wikis": wikis.list_wikis()}


@mcp.tool
def current_wiki() -> dict:
    """Which wiki is active for you right now (None until you switch_wiki)."""
    return {"active": _ACTIVE}


@mcp.tool
def switch_wiki(slug: str) -> dict:
    """Switch your active wiki. Required before any page read/write. This does
    NOT change what the human sees in their browser."""
    global _ACTIVE
    if not wikis.exists(slug):
        return {"error": f"no wiki '{slug}'", "wikis": [w["slug"] for w in wikis.list_wikis()]}
    _ACTIVE = slug
    _save_active(slug)
    return {"active": _ACTIVE, "name": wikis.name_of(slug)}


@mcp.tool
def create_wiki(name: str) -> dict:
    """Create a new isolated wiki and switch to it."""
    global _ACTIVE
    slug = wikis.create_wiki(name)
    _ACTIVE = slug
    _save_active(slug)
    return {"active": slug, "name": name}


# --- Pages (all scoped to the active wiki) ------------------------------------

@mcp.tool
def list_pages() -> dict:
    """List pages in the active wiki."""
    wiki = _require_wiki()
    return {"wiki": wiki, "pages": store.list_pages()}


@mcp.tool
def get_page(slug: str) -> dict:
    """Get a page's title and current markdown (reflects unsaved live edits)."""
    wiki = _require_wiki()
    page = store.get_page(slug)
    if not page:
        return {"wiki": wiki, "error": f"no page '{slug}' in {wiki}"}
    try:
        r = httpx.get(f"{WEB}/api/collab/{slug}/live", headers=_headers(), timeout=10)
        markdown = r.json()["markdown"] if r.status_code == 200 else page["markdown"]
    except Exception:
        markdown = page["markdown"]
    return {"wiki": wiki, "slug": page["slug"], "title": page["title"],
            "markdown": markdown, "outline": render.extract_toc(markdown)}


@mcp.tool
def create_page(title: str, markdown: str = "") -> dict:
    """Create a new page in the active wiki."""
    wiki = _require_wiki()
    page = store.create_page(title, markdown, author="ai")
    return {"wiki": wiki, "slug": page["slug"], "title": page["title"]}


@mcp.tool
def append_to_page(slug: str, text: str) -> dict:
    """Append text to a page **live** in the active wiki — anyone viewing it sees
    it stream in. Call repeatedly to write incrementally."""
    wiki = _require_wiki()
    try:
        r = httpx.post(f"{WEB}/api/collab/{slug}/append", json={"text": text},
                       headers=_headers(), timeout=15)
        if r.status_code == 200:
            return {"wiki": wiki, "slug": slug, "live": True, "length": r.json().get("length")}
    except Exception:
        pass
    page = store.get_page(slug)  # headless fallback: no live sync
    if not page:
        return {"wiki": wiki, "error": f"no page '{slug}' in {wiki}"}
    store.update_page(slug, page["title"], page["markdown"] + text, author="ai")
    return {"wiki": wiki, "slug": slug, "live": False}


@mcp.tool
def edit_page(slug: str, old_text: str, new_text: str) -> dict:
    """PREFERRED way to modify a page: replace an exact snippet of its current
    markdown (`old_text`) with `new_text`. Only that region changes, so it applies
    as a live surgical edit that MERGES with a human editing the same page at the
    same time — unlike replace_page, which overwrites everything.

    `old_text` must match the page's current text exactly and occur exactly once;
    if it's ambiguous, include more surrounding lines. Call get_page first to copy
    the exact text. To add new content at the end, use append_to_page instead."""
    wiki = _require_wiki()
    try:
        r = httpx.post(f"{WEB}/api/collab/{slug}/edit",
                       json={"old": old_text, "new": new_text},
                       headers=_headers(), timeout=15)
        if r.status_code == 200:
            data = r.json()
            if not data.get("ok", True):
                return {"wiki": wiki, "slug": slug, "error": data.get("error")}
            return {"wiki": wiki, "slug": slug, "edited": True, "live": True,
                    "length": data.get("length")}
    except Exception:
        pass
    # headless fallback: direct str-replace in the DB
    page = store.get_page(slug)
    if not page:
        return {"wiki": wiki, "error": f"no page '{slug}' in {wiki}"}
    md = page["markdown"]
    n = md.count(old_text)
    if n == 0:
        return {"wiki": wiki, "error": "old_text was not found in the page"}
    if n > 1:
        return {"wiki": wiki, "error": "old_text is not unique — include more context"}
    store.update_page(slug, page["title"], md.replace(old_text, new_text, 1), author="ai")
    return {"wiki": wiki, "slug": slug, "edited": True, "live": False}


def _text_op(slug: str, payload: dict) -> dict:
    """Apply a structured text op live (with a headless DB fallback)."""
    wiki = _require_wiki()
    try:
        r = httpx.post(f"{WEB}/api/collab/{slug}/op", json=payload,
                       headers=_headers(), timeout=15)
        if r.status_code == 200:
            data = r.json()
            if not data.get("ok", True):
                return {"wiki": wiki, "slug": slug, "error": data.get("error")}
            return {"wiki": wiki, "slug": slug, "edited": True, "live": True,
                    "length": data.get("length")}
    except Exception:
        pass
    page = store.get_page(slug)
    if not page:
        return {"wiki": wiki, "error": f"no page '{slug}' in {wiki}"}
    try:
        new_md = edits.apply_to_string(page["markdown"], edits.make_planner(payload))
    except ValueError as exc:
        return {"wiki": wiki, "slug": slug, "error": str(exc)}
    store.update_page(slug, page["title"], new_md, author="ai")
    return {"wiki": wiki, "slug": slug, "edited": True, "live": False}


@mcp.tool
def replace_section(slug: str, heading: str, markdown: str) -> dict:
    """Replace one section — a heading and its content up to the next same-or-
    higher heading — with new markdown (include the heading line in `markdown`).
    `heading` is the exact heading text without the leading #s. This is the
    preferred way to rewrite a single section. Merge-safe live edit."""
    return _text_op(slug, {"op": "replace_section", "heading": heading,
                           "markdown": markdown})


@mcp.tool
def insert_after(slug: str, anchor: str, text: str) -> dict:
    """Insert `text` immediately after the unique snippet `anchor` (e.g. a heading
    line or a sentence). Merge-safe live edit."""
    return _text_op(slug, {"op": "insert", "after": anchor, "text": text})


@mcp.tool
def insert_before(slug: str, anchor: str, text: str) -> dict:
    """Insert `text` immediately before the unique snippet `anchor`."""
    return _text_op(slug, {"op": "insert", "before": anchor, "text": text})


@mcp.tool
def prepend_to_page(slug: str, text: str) -> dict:
    """Insert `text` at the very top of the page."""
    return _text_op(slug, {"op": "prepend", "text": text})


@mcp.tool
def remove_from_page(slug: str, text: str) -> dict:
    """Delete the unique snippet `text` from the page (surgical, merge-safe)."""
    return _text_op(slug, {"op": "remove", "text": text})


@mcp.tool
def replace_page(slug: str, markdown: str) -> dict:
    """Overwrite a page's ENTIRE body — use only to rewrite a page from scratch.
    For changes to an existing page, prefer edit_page (targeted, merge-safe) or
    append_to_page; replace_page discards any concurrent human edits."""
    wiki = _require_wiki()
    try:
        r = httpx.post(f"{WEB}/api/collab/{slug}/replace", json={"markdown": markdown},
                       headers=_headers(), timeout=15)
        if r.status_code == 200:
            return {"wiki": wiki, "slug": slug, "live": True, "length": r.json().get("length")}
    except Exception:
        pass
    page = store.get_page(slug)
    if not page:
        return {"wiki": wiki, "error": f"no page '{slug}' in {wiki}"}
    store.update_page(slug, page["title"], markdown, author="ai")
    return {"wiki": wiki, "slug": slug, "live": False}


@mcp.tool
def delete_page(slug: str) -> dict:
    """Move a page to the trash in the active wiki (restorable, not permanent)."""
    wiki = _require_wiki()
    return {"wiki": wiki, "trashed": store.soft_delete(slug), "slug": slug}


@mcp.tool
def list_trash() -> dict:
    """List trashed (soft-deleted) pages in the active wiki."""
    wiki = _require_wiki()
    return {"wiki": wiki, "trash": store.list_trash()}


@mcp.tool
def restore_page(slug: str) -> dict:
    """Restore a trashed page in the active wiki."""
    wiki = _require_wiki()
    return {"wiki": wiki, "restored": store.restore(slug), "slug": slug}


@mcp.tool
def search(query: str, k: int = 6) -> dict:
    """Hybrid BM25 + vector search over the active wiki only (RAG)."""
    wiki = _require_wiki()
    return {"wiki": wiki, "results": rag.search_chunks(query, k)}


@mcp.tool
def changes_since(since: str = "") -> dict:
    """Change feed for the active wiki: page edits newest-first, each with author
    and timestamp. Pass an ISO datetime ('YYYY-MM-DD HH:MM:SS') to get only
    changes after it — so you can catch up on the human's edits without re-reading
    everything. Omit `since` for the most recent changes."""
    wiki = _require_wiki()
    return {"wiki": wiki, "since": since or None,
            "changes": store.recent_changes(since or None)}


@mcp.tool
def backlinks(slug: str) -> dict:
    """List pages in the active wiki that link to `slug` ('what links here')."""
    wiki = _require_wiki()
    return {"wiki": wiki, "slug": slug, "backlinks": store.backlinks(slug)}


@mcp.tool
def broken_links() -> dict:
    """List wikilinks in the active wiki that point to non-existent pages."""
    wiki = _require_wiki()
    return {"wiki": wiki, "broken": store.broken_links()}


@mcp.tool
def list_templates() -> dict:
    """List page templates in the active wiki."""
    wiki = _require_wiki()
    return {"wiki": wiki, "templates": [t["name"] for t in store.templates_list()]}


@mcp.tool
def create_from_template(template_name: str, title: str) -> dict:
    """Create a new page from a named template ({{title}} is filled in)."""
    wiki = _require_wiki()
    page = store.create_from_template(template_name, title)
    if not page:
        return {"wiki": wiki, "error": f"no template '{template_name}'"}
    return {"wiki": wiki, "slug": page["slug"], "title": page["title"]}


@mcp.tool
def export_pdf(slug: str, dest_path: str) -> dict:
    """Render a page to a PDF file at `dest_path`."""
    import os

    from . import pdfgen

    wiki = _require_wiki()
    page = store.get_page(slug)
    if not page:
        return {"wiki": wiki, "error": f"no page '{slug}' in {wiki}"}
    data = pdfgen.page_pdf(page["title"], page["html"])
    out = os.path.expanduser(dest_path)
    with open(out, "wb") as f:
        f.write(data)
    return {"wiki": wiki, "slug": slug, "path": out, "bytes": len(data)}


@mcp.tool
def upload_asset(filename: str = "", path: str = "", base64_data: str = "") -> dict:
    """Upload an image / video / audio / file into the active wiki (stored in its
    SQLite DB). Provide either a local `path` (read from disk) or `base64_data`.
    Returns markdown to embed it — images render inline, video/audio get players."""
    import base64 as _b64
    import mimetypes
    import os

    wiki = _require_wiki()
    if path:
        try:
            with open(os.path.expanduser(path), "rb") as f:
                data = f.read()
        except OSError as exc:
            return {"wiki": wiki, "error": str(exc)}
        filename = filename or os.path.basename(path)
    elif base64_data:
        try:
            data = _b64.b64decode(base64_data)
        except Exception as exc:
            return {"wiki": wiki, "error": f"bad base64: {exc}"}
        filename = filename or "asset"
    else:
        return {"wiki": wiki, "error": "provide either path or base64_data"}
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    asset_id = store.save_image(filename, mime, data)
    url = f"/image/{asset_id}/{filename}"
    return {"wiki": wiki, "id": asset_id, "url": url, "markdown": f"![{filename}]({url})"}


@mcp.tool
def add_comment(slug: str, body: str) -> dict:
    """Leave a comment/note on a page (e.g. 'expand this section')."""
    wiki = _require_wiki()
    r = store.comment_add(slug, body, author="ai")
    return {"wiki": wiki, "comment": r} if r else {"wiki": wiki, "error": "no such page"}


@mcp.tool
def list_comments(slug: str) -> dict:
    """List comments on a page."""
    wiki = _require_wiki()
    return {"wiki": wiki, "slug": slug, "comments": store.comments_list(slug)}


@mcp.tool
def resolve_comment(comment_id: int) -> dict:
    """Mark a comment resolved."""
    wiki = _require_wiki()
    store.comment_resolve(comment_id)
    return {"wiki": wiki, "resolved": comment_id}


@mcp.tool
def propose_edit(slug: str, markdown: str, note: str = "") -> dict:
    """Propose a full-page rewrite for the human to review — it is NOT applied
    until they accept it. Use this for big/risky changes instead of replace_page."""
    wiki = _require_wiki()
    r = store.suggestion_add(slug, markdown, note, author="ai")
    return {"wiki": wiki, "suggestion": r} if r else {"wiki": wiki, "error": "no such page"}


@mcp.tool
def list_suggestions(slug: str = "") -> dict:
    """List pending proposed edits (for a page, or the whole active wiki)."""
    wiki = _require_wiki()
    return {"wiki": wiki, "suggestions": store.suggestions_list(slug or None)}


@mcp.tool
def export_markdown(dest_dir: str) -> dict:
    """Export every page of the active wiki to `dest_dir` as <slug>.md files
    (round-trip to a repo's docs/)."""
    import os

    wiki = _require_wiki()
    n = wikis.export_markdown(wiki, os.path.expanduser(dest_dir))
    return {"wiki": wiki, "written": n, "dir": os.path.expanduser(dest_dir)}


@mcp.tool
def list_tags() -> dict:
    """List tags in the active wiki with page counts. Tag a page by adding a
    frontmatter block: ---\\ntags: character, spirit\\n---"""
    wiki = _require_wiki()
    return {"wiki": wiki, "tags": store.all_tags()}


@mcp.tool
def pages_by_tag(tag: str) -> dict:
    """List pages in the active wiki tagged with `tag` (an auto-index)."""
    wiki = _require_wiki()
    return {"wiki": wiki, "tag": tag, "pages": store.pages_with_tag(tag)}


@mcp.tool
def clone_page(slug: str) -> dict:
    """Duplicate a page as a new top-level page ('<Title> (copy)')."""
    wiki = _require_wiki()
    page = store.clone_page(slug)
    if not page:
        return {"wiki": wiki, "error": f"no page '{slug}' in {wiki}"}
    return {"wiki": wiki, "slug": page["slug"], "title": page["title"]}


@mcp.tool
def set_parent(slug: str, parent_slug: str = "") -> dict:
    """Make `slug` a child of `parent_slug` (empty to make it top-level again).
    Child pages are hidden from the sidebar and excluded from the main search
    index (they live in the parent's own partition). Use search_subpages to
    search within a parent."""
    wiki = _require_wiki()
    page = store.set_parent(slug, parent_slug or None)
    if not page:
        return {"wiki": wiki, "error": f"could not set parent for '{slug}'"}
    return {"wiki": wiki, "slug": slug, "parent": parent_slug or None}


@mcp.tool
def list_children(parent_slug: str) -> dict:
    """List the child pages of `parent_slug` (they're hidden from the sidebar)."""
    wiki = _require_wiki()
    return {"wiki": wiki, "parent": parent_slug, "children": store.children(parent_slug)}


@mcp.tool
def search_subpages(parent_slug: str, query: str, k: int = 6) -> dict:
    """Hybrid search restricted to one parent's child pages (its own index
    partition) — for large sub-collections kept out of the main index."""
    wiki = _require_wiki()
    parent = store.get_page(parent_slug)
    if not parent:
        return {"wiki": wiki, "error": f"no page '{parent_slug}' in {wiki}"}
    return {"wiki": wiki, "parent": parent_slug,
            "results": rag.search_subtree(query, parent["id"], k)}


def main() -> None:
    global _ACTIVE
    db.init_db()
    _ACTIVE = _load_active()
    mcp.run()


if __name__ == "__main__":
    main()
