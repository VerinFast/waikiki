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
