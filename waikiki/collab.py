"""Real-time collaborative editing via CRDT (Yjs / pycrdt).

This is the co-tenant editing layer. One CRDT "room" per page holds the live
text; browsers bind their editor to it over a y-websocket connection (with
awareness/presence cursors), and Claude — connected through the MCP server —
injects edits into the *same* room. Everyone sees everyone else's changes live,
and concurrent edits merge instead of clobbering.

Flow:
    browser  <--y-websocket-->  [ YRoom.ydoc ]  <--HTTP inject--  MCP server
                                      |
                                 (debounced)
                                      v
                          render HTML + snapshot to SQLite + RAG reindex

Persistence uses a snapshot-diff in the background flusher (rather than a pycrdt
change observer, whose Subscription is unsendable across threads): every tick we
compare each room's current text to the last-saved copy and persist once it has
settled. The web app process owns the rooms; the MCP server mutates them via the
HTTP endpoints in api.py, which call the functions here.
"""
from __future__ import annotations

import asyncio
import time

import anyio
from pycrdt import Text
from pycrdt.websocket import ASGIServer, WebsocketServer

from . import store

server = WebsocketServer(auto_clean_rooms=False)
asgi_app = ASGIServer(server)  # available if you prefer to mount it directly

CLAUDE = {"name": "Claude", "color": "#c0392b"}
_FLUSH_IDLE = 1.5      # seconds a room must be unchanged before we persist it
_CLAUDE_IDLE = 8.0     # seconds before the "Claude is editing" presence clears

_seeded: set[str] = set()
_last_text: dict[str, str] = {}     # slug -> text seen on the previous tick
_stable_since: dict[str, float] = {}  # slug -> when the text last changed
_last_saved: dict[str, str] = {}    # slug -> text last persisted to SQLite
_claude_seen: dict[str, float] = {}  # slug -> monotonic ts of last MCP write


def _ytext(room) -> Text:
    return room.ydoc.get("content", type=Text)


async def ensure_room(slug: str):
    """Get (creating if needed) the room for a page, seeded from the DB once."""
    room = await server.get_room(slug)
    if slug not in _seeded:
        _seeded.add(slug)
        txt = _ytext(room)
        if len(txt) == 0:
            page = store.get_page(slug)
            if page and page["markdown"]:
                with room.ydoc.transaction():
                    txt += page["markdown"]
        current = str(txt)
        _last_text[slug] = current
        _last_saved[slug] = current  # equals the DB copy → no immediate re-save
        _stable_since[slug] = time.monotonic()
    return room


async def append_text(slug: str, text: str) -> str:
    """Append text to the live document (used by Claude via MCP)."""
    room = await ensure_room(slug)
    txt = _ytext(room)
    with room.ydoc.transaction():
        txt += text
    _claude_present(slug, room)
    return str(txt)


async def replace_text(slug: str, markdown: str) -> str:
    """Replace the whole live document (used by Claude via MCP)."""
    room = await ensure_room(slug)
    txt = _ytext(room)
    with room.ydoc.transaction():
        if len(txt):
            del txt[0:len(txt)]
        txt += markdown
    _claude_present(slug, room)
    return str(txt)


async def live_markdown(slug: str) -> str | None:
    """Current in-room text (reflects unsaved edits). None if no room yet."""
    if slug not in _seeded:
        return None
    room = await server.get_room(slug)
    return str(_ytext(room))


def _claude_present(slug: str, room) -> None:
    _claude_seen[slug] = time.monotonic()
    try:
        room.awareness.set_local_state({"user": CLAUDE})
    except Exception:
        pass


def _persist(slug: str, markdown: str) -> None:
    """Sync snapshot: re-render + save + reindex. Runs in a worker thread."""
    page = store.get_page(slug)
    if page is not None:
        store.update_page(slug, page["title"], markdown, author="collab")


async def flusher() -> None:
    """Background loop: debounced snapshot-diff persistence + Claude presence."""
    while True:
        await asyncio.sleep(1.0)
        now = time.monotonic()

        for slug in list(_seeded):
            try:
                room = await server.get_room(slug)
                current = str(_ytext(room))
            except Exception:
                continue

            if current != _last_text.get(slug):
                _last_text[slug] = current           # still changing → reset timer
                _stable_since[slug] = now
                continue
            # Settled: persist if it differs from what's on disk.
            if current != _last_saved.get(slug) and now - _stable_since.get(slug, now) >= _FLUSH_IDLE:
                try:
                    await anyio.to_thread.run_sync(_persist, slug, current)
                    _last_saved[slug] = current
                except Exception as exc:
                    print(f"[waikiki] collab flush failed for {slug}: {exc}")

        for slug, ts in list(_claude_seen.items()):
            if now - ts > _CLAUDE_IDLE:
                _claude_seen.pop(slug, None)
                try:
                    room = await server.get_room(slug)
                    room.awareness.set_local_state(None)
                except Exception:
                    pass
