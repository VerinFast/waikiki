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

The web app process owns the rooms (single source of truth). The MCP server is a
separate process, so it mutates rooms via the HTTP endpoints in api.py, which
call the functions here.
"""
from __future__ import annotations

import asyncio
import time

import anyio
from pycrdt import Text
from pycrdt.websocket import ASGIServer, WebsocketServer

from . import store

# One websocket server hosts every room. Keep rooms alive for the process
# lifetime so their ydoc + seed state stay consistent (no auto-clean races).
server = WebsocketServer(auto_clean_rooms=False)
asgi_app = ASGIServer(server)  # mount at /collab in the FastAPI app

CLAUDE = {"name": "Claude", "color": "#c0392b"}
_FLUSH_IDLE = 1.5      # seconds of quiet before snapshotting a room to SQLite
_CLAUDE_IDLE = 8.0     # seconds before the "Claude is editing" presence clears

_seeded: set[str] = set()
_dirty: dict[str, float] = {}          # slug -> monotonic ts of last edit
_claude_seen: dict[str, float] = {}    # slug -> monotonic ts of last MCP write
_subs: dict[str, object] = {}          # slug -> Text.observe Subscription (must be kept alive!)


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
        # Mark the room dirty on every edit (from anyone) so the flusher persists.
        # Use a real closure that swallows all args — pycrdt calls the observer
        # with (event[, txn]), so a positional default like `s=slug` would be
        # clobbered by the transaction. observe() returns a Subscription that
        # MUST be retained, or the callback is garbage-collected.
        def _make_observer(s: str):
            def _cb(*_args):
                _dirty[s] = time.monotonic()
            return _cb

        _subs[slug] = txt.observe(_make_observer(slug))
    return room


async def append_text(slug: str, text: str) -> str:
    """Append text to the live document (used by Claude via MCP)."""
    room = await ensure_room(slug)
    txt = _ytext(room)
    with room.ydoc.transaction():
        txt += text
    _dirty[slug] = time.monotonic()
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
    _dirty[slug] = time.monotonic()
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
    """Background loop: debounced persistence + expiring Claude presence."""
    while True:
        await asyncio.sleep(1.0)
        now = time.monotonic()

        for slug, ts in list(_claude_seen.items()):
            if now - ts > _CLAUDE_IDLE:
                _claude_seen.pop(slug, None)
                try:
                    room = await server.get_room(slug)
                    room.awareness.set_local_state(None)
                except Exception:
                    pass

        for slug, ts in list(_dirty.items()):
            if now - ts < _FLUSH_IDLE:
                continue  # still being edited; wait for a quiet moment
            _dirty.pop(slug, None)
            try:
                room = await server.get_room(slug)
                markdown = str(_ytext(room))
                await anyio.to_thread.run_sync(_persist, slug, markdown)
            except Exception as exc:
                print(f"[waikiki] collab flush failed for {slug}: {exc}")
