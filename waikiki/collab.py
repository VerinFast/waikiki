"""Real-time collaborative editing via CRDT (Yjs / pycrdt), scoped per wiki.

One CRDT "room" per (wiki, page) holds the live text; browsers bind their editor
to it over a y-websocket connection (with presence), and Claude — through the MCP
server — injects edits into the *same* room. Rooms are keyed by ``wiki::slug`` so
two wikis never share a document, matching the strict isolation guarantee.

Persistence uses a snapshot-diff in the background flusher (rather than a pycrdt
change observer, whose Subscription is unsendable across threads): every tick we
compare each room's text to the last-saved copy and persist once it settles,
under the room's own wiki context.
"""
from __future__ import annotations

import asyncio
import time

import sys

import anyio
from pycrdt import Text
from pycrdt.websocket import ASGIServer, WebsocketServer

from . import db, store

server = WebsocketServer(auto_clean_rooms=False)
asgi_app = ASGIServer(server)

CLAUDE = {"name": "Claude", "color": "#c0392b"}
_FLUSH_IDLE = 1.5
_CLAUDE_IDLE = 8.0

_seeded: set[str] = set()          # room keys (wiki::slug)
_last_text: dict[str, str] = {}
_stable_since: dict[str, float] = {}
_last_saved: dict[str, str] = {}
_claude_seen: dict[str, float] = {}


def room_key(wiki: str, slug: str) -> str:
    return f"{wiki}::{slug}"


def _split(key: str) -> tuple[str, str]:
    wiki, _, slug = key.partition("::")
    return wiki, slug


def _ytext(room) -> Text:
    return room.ydoc.get("content", type=Text)


async def ensure_room(wiki: str, slug: str):
    """Get (creating if needed) the room for a page, seeded from its wiki DB."""
    key = room_key(wiki, slug)
    room = await server.get_room(key)
    if key not in _seeded:
        _seeded.add(key)
        txt = _ytext(room)
        if len(txt) == 0:
            token = db.current_wiki.set(wiki)
            try:
                page = store.get_page(slug)
            finally:
                db.current_wiki.reset(token)
            if page and page["markdown"]:
                with room.ydoc.transaction():
                    txt += page["markdown"]
        current = str(txt)
        _last_text[key] = current
        _last_saved[key] = current
        _stable_since[key] = time.monotonic()
    return room


async def append_text(wiki: str, slug: str, text: str) -> str:
    room = await ensure_room(wiki, slug)
    txt = _ytext(room)
    with room.ydoc.transaction():
        txt += text
    _claude_present(room_key(wiki, slug), room)
    return str(txt)


async def replace_text(wiki: str, slug: str, markdown: str) -> str:
    room = await ensure_room(wiki, slug)
    txt = _ytext(room)
    with room.ydoc.transaction():
        if len(txt):
            del txt[0:len(txt)]
        txt += markdown
    _claude_present(room_key(wiki, slug), room)
    return str(txt)


def _byte_offset(s: str, i: int) -> int:
    """Convert a Python code-point index to a UTF-8 byte offset. pycrdt/yrs Text
    indexes in *bytes*, but the edit planners compute offsets with Python str
    (code points), so any edit at/after a multi-byte char (e.g. an emoji like 🌺)
    would otherwise land at the wrong position — dropping/overwriting characters
    at the edit border, or panicking on a character boundary."""
    return len(s[:i].encode("utf-8"))


async def apply_edit(wiki: str, slug: str, planner) -> dict:
    """Apply a pure edit planner (from waikiki.edits) as a *surgical* CRDT change:
    only the planned [start:end] range is replaced, so it merges with concurrent
    human edits. `planner(current_text) -> (start, end, insert)` or raises."""
    room = await ensure_room(wiki, slug)
    txt = _ytext(room)
    current = str(txt)
    try:
        start, end, insert = planner(current)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    b_start, b_end = _byte_offset(current, start), _byte_offset(current, end)
    with room.ydoc.transaction():
        if b_end > b_start:
            del txt[b_start:b_end]
        if insert:
            txt.insert(b_start, insert)
    _claude_present(room_key(wiki, slug), room)
    return {"ok": True, "length": len(str(txt))}


async def live_markdown(wiki: str, slug: str) -> str | None:
    key = room_key(wiki, slug)
    if key not in _seeded:
        return None
    room = await server.get_room(key)
    return str(_ytext(room))


def _claude_present(key: str, room) -> None:
    _claude_seen[key] = time.monotonic()
    try:
        room.awareness.set_local_state({"user": CLAUDE})
    except Exception:
        pass


def _persist(slug: str, markdown: str) -> None:
    page = store.get_page(slug)
    if page is not None:
        store.update_page(slug, page["title"], markdown, author="collab")


async def flusher() -> None:
    """Debounced snapshot-diff persistence (per room's wiki) + Claude presence."""
    while True:
        await asyncio.sleep(1.0)
        now = time.monotonic()

        for key in list(_seeded):
            wiki, slug = _split(key)
            try:
                room = await server.get_room(key)
                current = str(_ytext(room))
            except Exception:
                continue

            if current != _last_text.get(key):
                _last_text[key] = current
                _stable_since[key] = now
                continue
            if current != _last_saved.get(key) and now - _stable_since.get(key, now) >= _FLUSH_IDLE:
                token = db.current_wiki.set(wiki)
                try:
                    await anyio.to_thread.run_sync(_persist, slug, current)
                    _last_saved[key] = current
                except Exception as exc:
                    print(f"[waikiki] collab flush failed for {key}: {exc}")
                finally:
                    db.current_wiki.reset(token)

        for key, ts in list(_claude_seen.items()):
            if now - ts > _CLAUDE_IDLE:
                _claude_seen.pop(key, None)
                try:
                    room = await server.get_room(key)
                    room.awareness.set_local_state(None)
                except Exception:
                    pass


async def flush_all() -> None:
    """Persist every room that is owed a write, ignoring the debounce.

    `flusher()` only saves text that has been *stable* for `_FLUSH_IDLE`, on a
    one-second loop, so anything typed in the last couple of seconds has never
    been written. Cancelling that task at shutdown just stops the loop — it does
    not flush — so quitting used to drop the tail of whatever someone was
    editing.

    That matters most on the path the user did not choose: the updater quits the
    app itself (SIGTERM) so the lifespan shutdown runs, precisely so this can
    happen. Call it BEFORE cancelling the flusher. See issue #19.
    """
    for key in list(_seeded):
        wiki, slug = _split(key)
        try:
            room = await server.get_room(key)
            current = str(_ytext(room))
        except Exception:
            continue
        if current == _last_saved.get(key):
            continue                      # already durable, nothing owed
        token = db.current_wiki.set(wiki)
        try:
            await anyio.to_thread.run_sync(_persist, slug, current)
            _last_saved[key] = current
        except Exception as exc:
            print(f"[waikiki] final collab flush failed for {key}: {exc}",
                  file=sys.stderr)
        finally:
            db.current_wiki.reset(token)
