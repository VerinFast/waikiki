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

import httpx
from fastmcp import FastMCP

from . import config, db, rag, store, wikis

WEB = config.WEB_URL
_ACTIVE_FILE = config.DATA_DIR / "mcp_active_wiki"

mcp = FastMCP(
    "🌺 Waikiki",
    instructions=(
        "Waikiki has multiple isolated wikis. You MUST call switch_wiki(slug) "
        "before reading or writing pages; content tools error otherwise. Wikis "
        "never share content — switching is the only way to cross between them. "
        "Every result includes the 'wiki' it acted on; check it to avoid mixing "
        "contexts."
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
    return {"wiki": wiki, "slug": page["slug"], "title": page["title"], "markdown": markdown}


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
def replace_page(slug: str, markdown: str) -> dict:
    """Replace a page's entire body **live** in the active wiki."""
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


def main() -> None:
    global _ACTIVE
    db.init_db()
    _ACTIVE = _load_active()
    mcp.run()


if __name__ == "__main__":
    main()
