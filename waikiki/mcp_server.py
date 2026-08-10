"""MCP server for Waikiki — lets Claude edit the wiki, with strict wiki isolation.

Waikiki hosts several fully-isolated wikis (e.g. Beaconlight, Crosslake,
StartupOS). To prevent cross-wiki contamination, the active wiki is scoped to a
single agent's **session** — separate from whatever the human is viewing in the
browser, and separate from every other agent connected to the same wikis. Every
content tool refuses to run until you pick a wiki with `switch_wiki`, and every
result echoes the wiki it acted on.

It is held in memory and never persisted, so a restart of this server leaves you
with no active wiki rather than inheriting one. That is deliberate: see
`_ACTIVE_BY_SESSION` below and issue #11.

    Typical flow: list_wikis() → switch_wiki("beaconlight") → search()/get_page()/
    append_to_page() … then switch_wiki("crosslake") to move deliberately.

Run over stdio (Claude Desktop):  python -m waikiki.mcp_server
Live-edit tools POST to the running web app (which owns the CRDT rooms) with an
X-Waikiki-Wiki header; read/search tools open the wiki's SQLite file directly.
"""
from __future__ import annotations

import contextvars
import os
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from mcp.types import Icon

from . import (accesslog, config, db, deeplink, edits, elements, imagegen, rag,
               render, store, structure, wikis)

WEB = config.WEB_URL
# NOTE: there is deliberately no on-disk active-wiki file. One existed here and
# it was shared by every MCP process on the machine, which is how two agents
# ended up writing into each other's wikis (issue #11). A stale
# DATA_DIR/mcp_active_wiki from an older build is unused and safe to delete.

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
        "Your active wiki is yours alone — no other agent can move you, and you "
        "cannot move them. It is NOT remembered if this server restarts. So if a "
        "content tool suddenly reports 'No active wiki', that is what happened: "
        "call switch_wiki again rather than assuming you are still where you "
        "were, and check the 'wiki' in each result against the wiki you meant.\n\n"
        "To CHANGE an existing page, prefer edit_page (a targeted find/replace) or "
        "append_to_page — these apply as surgical live edits that merge with a "
        "human editing the same page. Use replace_page ONLY to rewrite a page from "
        "scratch; it overwrites everything and discards concurrent human edits. "
        "Workflow: get_page to read exact current text, then edit_page with a "
        "unique snippet.\n\n"
        "This wiki changes underneath you. A human or another agent may edit a "
        "page at any time, including while you are working — but your earlier "
        "tool results stay in the conversation unchanged, so old text will look "
        "current. Do not trust a page you read earlier in this conversation. "
        "Keep the `version` each read returns, and before you rely on, quote, or "
        "edit that page again, call check_pages({slug: version, ...}) — it is "
        "cheap, batches every page you are holding, and returns no page bodies. "
        "Re-read with get_page only what it reports as stale. `trashed` means the "
        "page is in the bin: do not edit it without asking."
    ),
)


def _install_access_log() -> None:
    """Log every MCP tool call (name + duration + errors) to the shared access
    log — the key signal for diagnosing Claude Cowork/Desktop timeouts."""
    import time

    from fastmcp.server.middleware import Middleware

    class _AccessLog(Middleware):
        async def on_call_tool(self, context, call_next):
            t0 = time.monotonic()
            msg = getattr(context, "message", None)
            name = getattr(msg, "name", None) or "tool"
            args = getattr(msg, "arguments", None) or {}
            detail = " ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:4])
            try:
                result = await call_next(context)
            except Exception as exc:
                accesslog.mcp(name, False, time.monotonic() - t0,
                              f"{type(exc).__name__}: {exc} | {detail}")
                raise
            accesslog.mcp(name, True, time.monotonic() - t0, detail)
            return result

    try:
        mcp.add_middleware(_AccessLog())
    except Exception as exc:  # never block the server on logging setup
        print(f"[waikiki] MCP access-log middleware skipped: {exc}", file=sys.stderr)


# --- Active wiki: per session, never shared -----------------------------------
#
# The active wiki belongs to ONE agent's session and nothing else. It used to be
# a module global persisted to a single file in DATA_DIR, which meant every MCP
# process on the machine shared it: a respawned server silently adopted whichever
# wiki some other agent had last chosen, and kept writing there while its own
# context still believed it was somewhere else. Two agents in two wikis
# overwrote each other and filed pages into the wrong wiki. See issue #11 and
# tests/test_cross_wiki_isolation.py.
#
# So: keyed by session, held in memory, and deliberately NOT persisted. There is
# nothing to inherit across a restart. An agent whose server respawned gets a
# clear "no active wiki" error instead of silently writing into someone else's —
# an error the agent can see and recover from is strictly better than corruption
# it cannot.
_ACTIVE_BY_SESSION: dict[str, str] = {}

# Fallback for callers with no session identity: the test suite, and anything
# embedding these functions directly. With stdio one process serves one client,
# so a process-local value is already per-agent there.
_ACTIVE: str | None = None

# The session identity, overridable so tests (and embedders) can act as distinct
# agents without a live MCP connection.
_SESSION_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "waikiki_mcp_session", default=None)

# Sessions are few; this only bounds a pathological reconnect loop. Evicting
# costs an agent one switch_wiki call, never a wrong-wiki write.
MAX_TRACKED_SESSIONS = 64


def _session_key() -> str | None:
    """Identity of the calling agent's session, or None if there isn't one."""
    override = _SESSION_OVERRIDE.get()
    if override:
        return override
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
    except Exception:
        return None                     # no live request: fall back
    for attr in ("session_id", "client_id"):
        val = getattr(ctx, attr, None)
        if val:
            return str(val)
    return None


def _active_wiki() -> str | None:
    key = _session_key()
    return _ACTIVE if key is None else _ACTIVE_BY_SESSION.get(key)


def _set_active_wiki(slug: str) -> None:
    global _ACTIVE
    key = _session_key()
    if key is None:
        _ACTIVE = slug
        return
    if key not in _ACTIVE_BY_SESSION and len(_ACTIVE_BY_SESSION) >= MAX_TRACKED_SESSIONS:
        del _ACTIVE_BY_SESSION[next(iter(_ACTIVE_BY_SESSION))]   # oldest first
    _ACTIVE_BY_SESSION[key] = slug


def _require_wiki() -> str:
    active = _active_wiki()
    if active is None:
        raise RuntimeError(
            "No active wiki. Call list_wikis() then switch_wiki(slug) before "
            "reading or writing pages. Your choice is per-session and is not "
            "restored automatically, so if this server restarted mid-task you "
            "must switch again — do not assume you are where you started."
        )
    db.current_wiki.set(active)  # scope direct DB access to this wiki
    return active


def _headers() -> dict:
    active = _active_wiki()
    return {"X-Waikiki-Wiki": active} if active else {}


# --- Wiki selection -----------------------------------------------------------

@mcp.tool
def list_wikis() -> dict:
    """List the available isolated wikis and which one is currently active."""
    return {"active": _active_wiki(), "wikis": wikis.list_wikis()}


@mcp.tool
def current_wiki() -> dict:
    """Which wiki is active for you right now (None until you switch_wiki).

    This is yours alone: another agent switching wikis cannot move you, and you
    cannot move them. It is also not remembered across a restart of this server —
    if this returns None mid-task, that is why. Switch again rather than assuming
    you are still where you were."""
    return {"active": _active_wiki()}


@mcp.tool
def switch_wiki(slug: str) -> dict:
    """Switch your active wiki. Required before any page read/write.

    Scoped to your session: it does NOT change what the human sees in their
    browser, and it does NOT affect any other agent connected to this wiki. It is
    not persisted, so a restart of this server leaves you with no active wiki
    rather than inheriting one."""
    if not wikis.exists(slug):
        return {"error": f"no wiki '{slug}'", "wikis": [w["slug"] for w in wikis.list_wikis()]}
    _set_active_wiki(slug)
    return {"active": slug, "name": wikis.name_of(slug)}


@mcp.tool
def create_wiki(name: str) -> dict:
    """Create a new isolated wiki and switch to it (for you only)."""
    slug = wikis.create_wiki(name)
    _set_active_wiki(slug)
    return {"active": slug, "name": name}


# --- Pages (all scoped to the active wiki) ------------------------------------

@mcp.tool
def list_pages() -> dict:
    """List pages in the active wiki."""
    wiki = _require_wiki()
    return {"wiki": wiki, "pages": store.list_pages()}


@mcp.tool
def get_page(slug: str) -> dict:
    """Get a page's title, current markdown, properties and freshness.

    `markdown` reflects unsaved live edits when someone has the page open, so it
    can be NEWER than `updated_at` (which is the last saved revision) — `live`
    tells you which you got. Check `trashed` before writing: a trashed page is in
    the bin and edits to it are probably unwanted. Compare `updated_at` against
    what you saw last to tell whether your copy is stale.

    `link` is a `waikiki://<wiki>/<page>` deep link that opens this page in the
    desktop app. Give that to a human rather than an http:// URL — the app picks a
    free port at startup, so its http address can change between runs. Append
    `#section` (an entry's slug from `outline`) to land on a heading."""
    wiki = _require_wiki()
    page = store.get_page(slug)
    if not page:
        return {"wiki": wiki, "error": f"no page '{slug}' in {wiki}"}
    markdown, live = page["markdown"], False
    try:
        r = httpx.get(f"{WEB}/api/collab/{slug}/live", headers=_headers(), timeout=10)
        if r.status_code == 200:
            fresh = r.json()["markdown"]
            live = fresh != page["markdown"]
            markdown = fresh
    except Exception:
        pass
    store.log_activity("ai", "read")
    meta, tags, _ = structure.parse_frontmatter(markdown)
    return {"wiki": wiki, "slug": page["slug"], "title": page["title"],
            "link": deeplink.for_page(wiki, page["slug"]),
            "markdown": markdown, "outline": render.extract_toc(markdown),
            "properties": meta, "tags": tags,
            "updated_at": page.get("updated_at"),
            "deleted_at": page.get("deleted_at"),
            "trashed": bool(page.get("deleted_at")),
            "live": live,
            "version": store.content_version(markdown)}


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
def create_template(name: str, markdown: str) -> dict:
    """Create (or update) a page template in the active wiki. Put {{title}} where
    the page title should go — new pages made from it fill that in. Frontmatter is
    fine (e.g. a Character template with HitPoints / Class properties), and the
    body may contain arbitrary HTML in addition to Markdown — it renders (raw HTML
    is on by default; can be turned off per wiki in Settings → Content)."""
    wiki = _require_wiki()
    store.template_save(name, markdown)
    return {"wiki": wiki, "template": name}


@mcp.tool
def create_from_template(template_name: str, title: str) -> dict:
    """Create a new page from a named template ({{title}} is filled in)."""
    wiki = _require_wiki()
    page = store.create_from_template(template_name, title)
    if not page:
        return {"wiki": wiki, "error": f"no template '{template_name}'"}
    return {"wiki": wiki, "slug": page["slug"], "title": page["title"]}


@mcp.tool
def list_elements() -> dict:
    """List the wiki's custom elements — reusable structured components (Web
    Components with scoped CSS/JS) invoked from a page via a ```<slug> fenced
    block. Returns each element's slug, name, and field schema."""
    wiki = _require_wiki()
    import json as _json
    out = []
    for e in elements.list_elements():
        try:
            fields = _json.loads(e["fields"] or "[]")
        except Exception:
            fields = []
        out.append({"slug": e["slug"], "name": e["name"], "fields": fields})
    return {"wiki": wiki, "elements": out}


@mcp.tool
def get_element(slug: str) -> dict:
    """Get a custom element's full definition (fields, html, css, js)."""
    wiki = _require_wiki()
    el = elements.get_element(slug)
    if not el:
        return {"wiki": wiki, "error": f"no element '{slug}' in {wiki}"}
    return {"wiki": wiki, **el}


@mcp.tool
def create_element(name: str, fields: list[str], html: str, css: str = "",
                   js: str = "", slug: str = "") -> dict:
    """Create (or update) a custom element — an HTML5 Web Component with Shadow
    DOM (scoped CSS, encapsulated JS). Invoke it in a page with a ```<slug> fenced
    block of `key: value` lines.

    fields: a list like ["title*", "image", "publisher*"] — a trailing * marks a
    required field (a block missing one renders an error). html is the shadow-DOM
    template; field values interpolate as {{field}} (escaped). css is scoped; js
    runs with (root, props, host) and can build richer DOM. Templates and pages
    can require an element's fields. Slug defaults to a slugified name."""
    wiki = _require_wiki()
    saved = elements.save_element(slug, name, fields, html, css, js)
    store.rerender_all()
    return {"wiki": wiki, "slug": saved, "usage": f"```{saved}"}


@mcp.tool
def delete_element(slug: str) -> dict:
    """Delete a custom element. Pages that used it will render nothing for it."""
    wiki = _require_wiki()
    elements.delete_element(slug)
    store.rerender_all()
    return {"wiki": wiki, "deleted": slug}


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
def set_property(slug: str, key: str, value: str) -> dict:
    """Set an arbitrary property on a page (stored as frontmatter). Reference it
    elsewhere as {{Key}} on the same page or {{Slug.Key}} on another page —
    e.g. set_property('meru', 'HitPoints', '42') then write {{Meru.HitPoints}}."""
    wiki = _require_wiki()
    page = store.set_property(slug, key, value)
    if not page:
        return {"wiki": wiki, "error": f"no page '{slug}' in {wiki}"}
    return {"wiki": wiki, "slug": slug, "key": key, "value": value}


@mcp.tool
def check_pages(seen: dict) -> dict:
    """Re-check pages you already read — cheaply, without pulling them back.

    Anything you read earlier in this conversation may since have been edited by
    the human or another agent, and your transcript will still show the OLD text
    as if it were current. Pass what you last saw and this tells you what to
    re-read, returning no page bodies at all.

    `seen` maps slug -> the `version` (preferred) or `updated_at` you got from
    get_page, e.g. {"meru": "a1b2c3d4e5f6", "characters": "9f8e7d6c5b4a"}.

    Returns `stale` (re-read these with get_page), `unchanged`, `missing`
    (deleted or renamed), `trashed`, and `current` (slug -> the version now), so
    you can update what you're holding. Detects unsaved live edits too — a human
    typing right now is exactly when your copy is most wrong."""
    wiki = _require_wiki()
    if not isinstance(seen, dict) or not seen:
        return {"wiki": wiki, "error": "seen must be a non-empty object of "
                                       "slug -> version (or updated_at)"}
    slugs = list(seen)[:200]

    # One round trip to the web app, which owns the live CRDT rooms.
    info: dict = {}
    try:
        r = httpx.post(f"{WEB}/api/collab/versions", headers=_headers(),
                       json={"slugs": slugs}, timeout=15)
        if r.status_code == 200:
            info = r.json().get("pages", {})
    except Exception:
        pass

    stale, unchanged, missing, trashed, current = [], [], [], [], {}
    for slug in slugs:
        row = info.get(slug)
        if row is None:                       # web app unreachable — use the DB
            page = store.get_page(slug)
            row = ({"exists": False} if not page else
                   {"exists": True,
                    "version": store.content_version(page["markdown"]),
                    "updated_at": page.get("updated_at"),
                    "trashed": bool(page.get("deleted_at")), "live": False})
        if not row.get("exists"):
            missing.append(slug)
            continue
        if row.get("trashed"):
            trashed.append(slug)
        current[slug] = row["version"]
        # Accept either a content version or the older updated_at as the token.
        was = str(seen.get(slug) or "")
        if was in (row["version"], str(row.get("updated_at") or "")):
            unchanged.append(slug)
        else:
            stale.append(slug)
    return {"wiki": wiki, "stale": stale, "unchanged": unchanged,
            "missing": missing, "trashed": trashed, "current": current}


@mcp.tool
def get_metadata(slug: str) -> dict:
    """Everything *about* a page without its body: all properties, tags, parent
    and children, starred, and created/updated/deleted timestamps.

    Use this to DISCOVER what a page records — get_property needs a key you
    already know, this returns them all — and to check freshness (`updated_at`)
    or whether the page is in the bin (`trashed`) before acting on it."""
    wiki = _require_wiki()
    meta = store.page_metadata(slug)
    if meta is None:
        return {"wiki": wiki, "error": f"no page '{slug}' in {wiki}"}
    return {"wiki": wiki, **meta}


@mcp.tool
def set_metadata(slug: str, properties: dict) -> dict:
    """Set several page properties at once, e.g.
    {"HitPoints": "42", "Class": "Guardian"}.

    One rewrite and one history entry for the whole batch, unlike calling
    set_property repeatedly. A null value removes that property. Properties are
    frontmatter, so they can be referenced from any page as {{Key}} (same page)
    or {{Slug.Key}} (cross-page)."""
    wiki = _require_wiki()
    if not isinstance(properties, dict) or not properties:
        return {"wiki": wiki, "error": "properties must be a non-empty object"}
    page = store.set_properties(slug, properties)
    if not page:
        return {"wiki": wiki, "error": f"no page '{slug}' in {wiki}"}
    meta = store.page_metadata(slug) or {}
    return {"wiki": wiki, "slug": slug, "properties": meta.get("properties", {}),
            "updated_at": meta.get("updated_at")}


@mcp.tool
def get_property(slug: str, key: str) -> dict:
    """Get a page's property value (from its frontmatter). To see every property
    at once, use get_metadata."""
    wiki = _require_wiki()
    return {"wiki": wiki, "slug": slug, "key": key,
            "value": store.get_property(slug, key)}


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


@mcp.tool
def version() -> dict:
    """Return the running Waikiki release version. No wiki selection required."""
    return {"waikiki_version": config.VERSION}


@mcp.tool
def list_order() -> dict:
    """List the top-level pages in the wiki's Custom (manual) sidebar order."""
    wiki = _require_wiki()
    return {"wiki": wiki, "order": store.custom_order()}


@mcp.tool
def get_order(slug: str) -> dict:
    """Return a page's 0-indexed position in the Custom order (null if it's not a
    top-level page)."""
    wiki = _require_wiki()
    order = store.custom_order()
    return {"wiki": wiki, "slug": slug,
            "position": order.index(slug) if slug in order else None,
            "count": len(order)}


@mcp.tool
def set_order(slug: str, position: int) -> dict:
    """Move `slug` to `position` (0-indexed) in the Custom sidebar order. The page
    is removed and re-inserted: items between its old and new spots shift by one,
    the rest keep their positions. E.g. set_order('third-article', 1) makes it
    second and bumps the former second page to third."""
    wiki = _require_wiki()
    order = store.move_page_order(slug, position)
    if order is None:
        return {"wiki": wiki, "error": f"no top-level page '{slug}' in {wiki}"}
    return {"wiki": wiki, "order": order, "position": order.index(slug)}


@mcp.tool
def list_docs() -> dict:
    """List Waikiki's built-in documentation pages (the Help wiki: usage, editing,
    templates, AI/chat, etc.), without changing your active wiki. Read one with
    read_doc(slug)."""
    if not wikis.exists(config.HELP_WIKI):
        return {"docs": []}
    tok = db.current_wiki.set(config.HELP_WIKI)
    try:
        docs = [{"slug": p["slug"], "title": p["title"]}
                for p in store.list_pages(include_children=True)]
    finally:
        db.current_wiki.reset(tok)
    return {"docs": docs}


@mcp.tool
def read_doc(slug: str) -> dict:
    """Read a documentation page (Help wiki) by slug — e.g. 'templates',
    'editing-formatting' — without switching your active wiki. See list_docs()."""
    tok = db.current_wiki.set(config.HELP_WIKI)
    try:
        page = store.get_page(slug)
    finally:
        db.current_wiki.reset(tok)
    if not page:
        return {"error": f"no doc '{slug}' — call list_docs() for the list"}
    return {"slug": page["slug"], "title": page["title"], "markdown": page["markdown"]}


@mcp.tool
def read_log(limit: int = 200, query: str = "", kind: str = "") -> dict:
    """Read the access log — every request received and call made across the app
    and this MCP server, plus errors. `kind` filters to HTTP/MCP/CLI/ERROR;
    `query` is a case-insensitive substring. Newest first. Use this to diagnose
    timeouts and failures. No wiki selection required."""
    return {"entries": accesslog.read(limit=min(max(limit, 1), 1000),
                                      query=query, kind=kind)}


# Keys the MCP set_setting tool may write. Deliberately excludes settings with
# heavy side effects (allow_html → re-render all; embedder_* / model_library →
# re-index all) — change those in the browser.
_SETTABLE = {
    "image_style_prompt", "image_cli", "image_model",
    "gen_provider", "gen_model", "gen_model_local", "ollama_url",
    "chat_provider", "chat_model", "theme",
    "retention_versions", "retention_trash_days",
}


@mcp.tool
def get_settings() -> dict:
    """Return all settings for the active wiki (theme, AI providers/models, image
    house style + CLI, retention, …). Settings are per-wiki."""
    wiki = _require_wiki()
    return {"wiki": wiki, "settings": db.all_settings()}


@mcp.tool
def set_setting(key: str, value: str) -> dict:
    """Set one setting for the active wiki. Allowed keys: image_style_prompt,
    image_cli, image_model, gen_provider, gen_model, gen_model_local, ollama_url,
    chat_provider, chat_model, theme, retention_versions, retention_trash_days.
    Other keys (embedder, allow_html) are read-only here to avoid heavy re-index/
    re-render — change those in the browser."""
    wiki = _require_wiki()
    if key not in _SETTABLE:
        return {"wiki": wiki, "error": f"'{key}' is not settable via MCP",
                "allowed": sorted(_SETTABLE)}
    value = "" if value is None else str(value)
    if key == "gen_provider" and value not in ("anthropic", "ollama"):
        return {"wiki": wiki, "error": "gen_provider must be 'anthropic' or 'ollama'"}
    if key == "chat_provider" and value not in ("claude", "gemini"):
        return {"wiki": wiki, "error": "chat_provider must be 'claude' or 'gemini'"}
    if key in ("retention_versions", "retention_trash_days"):
        try:
            value = str(max(0, int(value)))
        except ValueError:
            return {"wiki": wiki, "error": f"{key} must be a non-negative integer"}
    db.set_setting(key, value)
    return {"wiki": wiki, "key": key, "value": value}


@mcp.tool
def generate_image(slug: str, description: str) -> dict:
    """Generate an illustration for a page and return markdown to embed it.

    Two steps run locally: `claude` turns the article + your `description` into a
    vivid image prompt (applying the wiki's house style from Settings), then the
    configured image CLI (agy/gemini) renders a 1024x1024 PNG into the article's
    own folder — shared per article so repeated images stay visually consistent.
    The PNG is stored as a normal wiki image; returns {url, markdown, prompt}.
    Place the markdown yourself with edit_page/append_to_page. Requires the image
    CLI to be installed locally (see the Help → CLI debug view if it fails)."""
    wiki = _require_wiki()
    cli = db.get_setting("image_cli", "agy")
    model = db.get_setting("image_model", "")
    result = imagegen.generate(slug, description, cli, model)
    return {"wiki": wiki, **result}


def _silence_stdout_noise() -> None:
    """Keep third-party libraries off stdout.

    This server speaks JSON-RPC over stdio, so **stdout is the protocol channel**.
    Anything else written there corrupts the stream and the client stops being able
    to parse replies — it then hangs on every subsequent call until restarted,
    with no error on either side. fastembed / huggingface / onnxruntime print
    download-progress bars to stdout on a model's first load, which is exactly the
    kind of stray write that kills the transport.
    """
    for var, val in (("HF_HUB_DISABLE_PROGRESS_BARS", "1"),
                     ("HF_HUB_DISABLE_TELEMETRY", "1"),
                     ("TQDM_DISABLE", "1")):
        os.environ.setdefault(var, val)


def main() -> None:
    _silence_stdout_noise()
    db.init_db()
    accesslog.setup()
    _install_access_log()
    # No active wiki on startup, by design: the agent must choose one. Restoring
    # a previous choice is what let one agent inherit another's wiki (issue #11).
    mcp.run()


if __name__ == "__main__":
    main()
