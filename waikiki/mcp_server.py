"""MCP server for Waikiki — lets Claude (Claude Desktop / Code) edit the wiki,
including writing into a page **live** so a human watching in the browser sees
it appear in real time.

Run over stdio (for Claude Desktop):

    python -m waikiki.mcp_server

Live-edit tools (`append_to_page`, `replace_page`) POST to the running web app,
which applies the change to the shared CRDT room and broadcasts it to every open
browser. Read/search tools work directly against the SQLite file, so they still
function even if the web app isn't running (in that case live edits fall back to
a plain DB write with no real-time sync).
"""
from __future__ import annotations

import sys

import httpx
from fastmcp import FastMCP

from . import config, db, rag, store

mcp = FastMCP("waikiki")
WEB = config.WEB_URL


def _post_live(path: str, payload: dict) -> dict | None:
    """POST to the running web app; return None if it's unreachable."""
    try:
        r = httpx.post(f"{WEB}{path}", json=payload, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


@mcp.tool
def list_pages() -> list[dict]:
    """List all wiki pages (slug, title, last-updated)."""
    return store.list_pages()


@mcp.tool
def get_page(slug: str) -> dict:
    """Get a page's title and current markdown (reflects unsaved live edits)."""
    page = store.get_page(slug)
    if not page:
        return {"error": f"no page with slug '{slug}'"}
    # Prefer the live (possibly unsaved) text from the running web app.
    try:
        r = httpx.get(f"{WEB}/api/collab/{slug}/live", timeout=10)
        markdown = r.json()["markdown"] if r.status_code == 200 else page["markdown"]
    except Exception:
        markdown = page["markdown"]
    return {"slug": page["slug"], "title": page["title"], "markdown": markdown}


@mcp.tool
def create_page(title: str, markdown: str = "") -> dict:
    """Create a new wiki page. Returns its slug (then edit it live with the
    append_to_page / replace_page tools)."""
    page = store.create_page(title, markdown, author="ai")
    return {"slug": page["slug"], "title": page["title"]}


@mcp.tool
def append_to_page(slug: str, text: str) -> dict:
    """Append text to a page **live** — anyone viewing it in the editor sees it
    stream in immediately. Call repeatedly to write incrementally, as if typing
    into the document beside the human. Falls back to a plain save if the web app
    isn't running."""
    result = _post_live(f"/api/collab/{slug}/append", {"text": text})
    if result is not None:
        return {"slug": slug, "live": True, "length": result.get("length")}
    page = store.get_page(slug)  # headless fallback: no live sync
    if not page:
        return {"error": f"no page with slug '{slug}'"}
    store.update_page(slug, page["title"], page["markdown"] + text, author="ai")
    return {"slug": slug, "live": False}


@mcp.tool
def replace_page(slug: str, markdown: str) -> dict:
    """Replace a page's entire body **live**. Falls back to a plain save if the
    web app isn't running."""
    result = _post_live(f"/api/collab/{slug}/replace", {"markdown": markdown})
    if result is not None:
        return {"slug": slug, "live": True, "length": result.get("length")}
    page = store.get_page(slug)
    if not page:
        return {"error": f"no page with slug '{slug}'"}
    store.update_page(slug, page["title"], markdown, author="ai")
    return {"slug": slug, "live": False}


@mcp.tool
def delete_page(slug: str) -> dict:
    """Delete a wiki page by slug."""
    return {"deleted": store.delete_page(slug), "slug": slug}


@mcp.tool
def search(query: str, k: int = 6) -> list[dict]:
    """Hybrid BM25 + vector search over the wiki (RAG). Returns ranked snippets
    with their source page, for grounding answers or finding what to edit."""
    return rag.search_chunks(query, k)


def main() -> None:
    db.init_db()
    if "--http" in sys.argv:
        mcp.run(transport="http", host="127.0.0.1", port=8788)
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
