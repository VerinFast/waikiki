"""Renaming a wiki: the label and the address are two different operations.

A wiki has a display **name** and a **slug**. The name is what a person reads.
The slug is the wiki's address: it is in every URL, it is the filename of its
database, it keys each CRDT room, and it is what an agent's `switch_wiki`
pointer holds. So changing the label is free and changing the address moves a
file and invalidates everything pointing at the old one — which is why they are
separate calls rather than one "rename" that guesses.

The interesting tests here are not that the name changes. They are the four
things that hold a slug and would otherwise keep holding the old one.
"""
from __future__ import annotations

import threading

import anyio
import pytest
from pycrdt import Doc, Text

from waikiki import collab, config, db, store, wikis


class _StandInRoom:
    """A room with a Y.Doc and none of a `YRoom`'s machinery."""

    def __init__(self) -> None:
        self.ydoc = Doc()
        self.ydoc.get("content", type=Text)


class _StandInServer:
    """Enough of `WebsocketServer` for the bookkeeping under test.

    Deliberately not the real one. A `YRoom` holds a pycrdt Subscription, which
    is bound to the thread that made it — the reason `collab.py` persists by
    snapshot-diff rather than a change observer — so driving real rooms by hand
    from a test and then starting the app in the same process aborts the
    interpreter when one is touched or collected on the portal thread. What
    these tests are about is which rooms `collab` saves, forgets and refuses to
    write, and that logic never looks inside the room.
    """

    def __init__(self) -> None:
        self.rooms: dict = {}

    async def get_room(self, name: str):
        return self.rooms.setdefault(name, _StandInRoom())

    async def delete_room(self, *, name=None, room=None) -> None:
        self.rooms.pop(name, None)

    async def __aenter__(self):          # the app's lifespan starts the server
        return self

    async def __aexit__(self, *exc) -> None:
        return None


_BOOKS = ("_last_text", "_last_saved", "_stable_since", "_claude_seen")


@pytest.fixture(autouse=True)
def stand_in_rooms(monkeypatch):
    """Point `collab` at the stand-in server, from clean bookkeeping.

    Autouse, and not only for the tests that drive rooms: changing a wiki's
    address releases that wiki's rooms, so the *route* tests reach into
    `collab.server` too. Its rooms and the `_seeded` index are module-level, so
    a test inheriting them from another file would have this route touch a real
    `YRoom` built on that file's thread — which aborts the interpreter rather
    than failing. Clean state in, clean state out.
    """
    monkeypatch.setattr(collab, "server", _StandInServer())
    monkeypatch.setattr(collab, "_seeded", set())
    for book in _BOOKS:
        monkeypatch.setattr(collab, book, {})
    yield


def _seed(slug: str, title: str, text: str) -> None:
    tok = db.current_wiki.set(slug)
    try:
        store.create_page(title, text)
    finally:
        db.current_wiki.reset(tok)


# --- The label ----------------------------------------------------------------

def test_the_display_name_changes_without_touching_the_address(wiki):
    """The case that prompted this: imported as `startupos`, reads StartupOS."""
    slug = wikis.create_wiki("startupos")
    _seed(slug, "Roadmap", "content that must not move")

    wikis.rename(slug, "StartupOS")

    assert wikis.name_of(slug) == "StartupOS"
    assert wikis.exists(slug)                       # address untouched
    assert wikis.db_path(slug).exists()             # file untouched
    tok = db.current_wiki.set(slug)
    try:
        assert store.get_page("roadmap")["markdown"] == "content that must not move"
    finally:
        db.current_wiki.reset(tok)


def test_a_wiki_needs_a_name(wiki):
    with pytest.raises(ValueError, match="needs a name"):
        wikis.rename("main", "   ")


# --- The address --------------------------------------------------------------

def test_changing_the_address_moves_the_file_and_keeps_the_content(wiki):
    slug = wikis.create_wiki("Old Name")
    _seed(slug, "Kept", "every word survives the move")
    old_path = wikis.db_path(slug)

    target = wikis.change_slug(slug, "New Address")

    assert target == "new-address"
    assert not wikis.exists("old-name") and wikis.exists("new-address")
    assert not old_path.exists()
    assert wikis.db_path("new-address").exists()
    # The display name is a separate fact and is left alone.
    assert wikis.name_of("new-address") == "Old Name"
    tok = db.current_wiki.set("new-address")
    try:
        assert store.get_page("kept")["markdown"] == "every word survives the move"
    finally:
        db.current_wiki.reset(tok)


def test_the_kahala_link_rides_along(wiki):
    """The link records a *remote* name, which a local rename does not touch."""
    slug = wikis.create_wiki("Linked")
    wikis.set_link(slug, "https://kahala.example", "startupos")

    wikis.change_slug(slug, "renamed")

    assert wikis.get_link("renamed") == {"base_url": "https://kahala.example",
                                         "remote": "startupos"}


def test_the_default_wiki_pointer_follows_its_wiki(wiki):
    assert wikis.default_slug() == "main"
    wikis.change_slug("main", "primary")
    assert wikis.default_slug() == "primary"


@pytest.mark.parametrize("bad, why", [
    ("   ", "letter or digit"),
    ("!!!", "letter or digit"),
])
def test_an_address_that_is_not_an_address_is_refused(wiki, bad, why):
    with pytest.raises(ValueError, match=why):
        wikis.change_slug("main", bad)


def test_an_address_another_wiki_answers_to_is_refused(wiki):
    with pytest.raises(ValueError, match="already another wiki"):
        wikis.change_slug("main", "beaconlight")
    assert wikis.exists("main") and wikis.exists("beaconlight")


def test_the_help_wiki_keeps_its_address(wiki):
    """`ensure_help_wiki` re-creates `help`, so a renamed one comes back twice."""
    wikis.ensure_help_wiki()
    with pytest.raises(ValueError, match="built in"):
        wikis.change_slug(config.HELP_WIKI, "handbook")
    # ...but its label is ordinary.
    assert wikis.rename(config.HELP_WIKI, "Handbook") == "Handbook"


def test_renaming_to_its_own_address_is_a_no_op(wiki):
    _seed("main", "Stay", "unchanged")
    assert wikis.change_slug("main", "main") == "main"
    tok = db.current_wiki.set("main")
    try:
        assert store.get_page("stay")
    finally:
        db.current_wiki.reset(tok)


def test_an_unrelated_file_in_the_way_is_never_overwritten(wiki):
    """A `.db` with no wiki registered to it is still somebody's data."""
    stray = wikis.db_path("occupied")
    stray.write_bytes(b"not ours to clobber")

    with pytest.raises(ValueError, match="already in the wikis folder"):
        wikis.change_slug("main", "occupied")

    assert stray.read_bytes() == b"not ours to clobber"


# --- What was holding the old slug --------------------------------------------

def test_a_handle_from_before_the_move_cannot_write_into_the_renamed_wiki(wiki):
    """The hazard is an address being *reused*, which is a normal thing to do.

    Rename `startupos` out of the way and clone `startupos` again — now a thread
    that cached a handle under that name before the move is holding an open file
    that belongs to the renamed wiki, and its key matches the new wiki's. Every
    write meant for the new wiki would land in the old one's file, invisibly.
    Handles are per (thread, wiki) and no thread can reach into another's cache,
    so the move has to invalidate them where they are used.

    One worker thread throughout (`max_workers=1`), because the point is a cache
    that the renaming thread cannot touch.
    """
    from concurrent.futures import ThreadPoolExecutor

    def write(slug: str, title: str, text: str) -> str:
        tok = db.current_wiki.set(slug)
        try:
            db.get_conn()                        # cache a handle under this name
            store.create_page(title, text)
            return db.active_wiki()
        finally:
            db.current_wiki.reset(tok)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(write, "beaconlight", "Original", "the first wiki").result()

        wikis.change_slug("beaconlight", "beaconlight-archived")
        reused = wikis.create_wiki("Beaconlight")        # the name is free again
        assert reused == "beaconlight"

        pool.submit(write, "beaconlight", "Second", "the new wiki").result()

    tok = db.current_wiki.set("beaconlight")
    try:
        slugs = {p["slug"] for p in store.list_pages()}
    finally:
        db.current_wiki.reset(tok)
    assert slugs == {"second"}, "a stale handle wrote into the renamed wiki"

    tok = db.current_wiki.set("beaconlight-archived")
    try:
        slugs = {p["slug"] for p in store.list_pages()}
    finally:
        db.current_wiki.reset(tok)
    assert slugs == {"original"}, "the renamed wiki was written to under its old name"


def test_an_agent_pointed_at_the_old_address_is_refused_not_redirected(wiki,
                                                                      monkeypatch):
    """Rule 4: inheriting another wiki silently is the bug, refusing is correct.

    `db.active_wiki()` answers with the registry default for a slug it does not
    know, so an agent still holding the old address would have written its
    pages into whatever wiki happens to be default.
    """
    from waikiki import mcp_server

    slug = wikis.create_wiki("Agent Wiki")
    monkeypatch.setattr(mcp_server, "_ACTIVE", slug)
    assert mcp_server._require_wiki() == slug

    wikis.change_slug(slug, "agent-wiki-renamed")

    with pytest.raises(RuntimeError, match="no longer exists"):
        mcp_server._require_wiki()


def test_the_mcp_tool_follows_the_address_it_just_changed(wiki, monkeypatch):
    from waikiki import mcp_server

    slug = wikis.create_wiki("Tool Wiki")
    monkeypatch.setattr(mcp_server, "_ACTIVE", slug)

    out = mcp_server.rename_wiki(name="Tool Wiki!", address="tools")

    assert out["wiki"] == "tools" and out["address"] == "tools"
    assert wikis.name_of("tools") == "Tool Wiki!"
    assert mcp_server._require_wiki() == "tools"      # followed, not stranded


def test_the_mcp_tool_asks_for_something_to_change(wiki, monkeypatch):
    from waikiki import mcp_server

    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    assert "error" in mcp_server.rename_wiki()


# --- The live editor ----------------------------------------------------------

def test_a_room_left_behind_never_writes_into_the_default_wiki(
        wiki, stand_in_rooms):
    """A CRDT room outlives the request and saves under the wiki its key names.

    For an address the registry no longer knows, that resolves through
    `db.active_wiki()`'s fallback — straight into the default wiki. A page
    appearing in a wiki nobody was editing is about the worst shape this bug
    could take, so the save paths check first.
    """
    import anyio

    slug = wikis.create_wiki("Roomy")
    _seed(slug, "Live", "saved text")

    async def edit_then_rename():
        await collab.ensure_room(slug, "live")
        key = collab.room_key(slug, "live")
        assert key in collab._seeded
        # Unsaved text in the room, then the wiki's address changes underneath.
        collab._last_saved[key] = "saved text"
        collab._last_text[key] = "text typed but never written"
        wikis.change_slug(slug, "roomy-renamed")
        await collab.flush_all()
        return key

    key = anyio.run(edit_then_rename)

    assert key not in collab._seeded              # dropped, not written
    tok = db.current_wiki.set("main")             # the default wiki
    try:
        assert store.list_pages() == []           # nothing leaked into it
    finally:
        db.current_wiki.reset(tok)


def test_releasing_a_wiki_saves_what_was_typed_first(wiki, stand_in_rooms):
    """The graceful path: flush, then forget. Nothing anyone typed is lost."""
    import anyio

    slug = wikis.create_wiki("Typing")
    _seed(slug, "Draft", "original")

    async def type_then_release():
        room = await collab.ensure_room(slug, "draft")
        text = collab._ytext(room)
        with room.ydoc.transaction():
            del text[0:len(text)]
            text += "typed in the editor, never saved"
        return await collab.release_wiki(slug)

    released = anyio.run(type_then_release)

    assert released == 1
    assert collab.room_key(slug, "draft") not in collab._seeded
    tok = db.current_wiki.set(slug)
    try:
        assert store.get_page("draft")["markdown"] == \
            "typed in the editor, never saved"
    finally:
        db.current_wiki.reset(tok)


# --- Through the routes a person actually clicks ------------------------------

def test_the_rename_routes_work_end_to_end(wiki):
    from fastapi.testclient import TestClient

    from waikiki.api import app

    slug = wikis.create_wiki("startupos")
    _seed(slug, "Plan", "route test content")

    with TestClient(app, client=("127.0.0.1", 1)) as c:
        c.cookies.set("waikiki_wiki", slug)
        r = c.post(f"/wikis/{slug}/rename", data={"name": "StartupOS"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert wikis.name_of(slug) == "StartupOS"

        r = c.post(f"/wikis/{slug}/address", data={"new_slug": "startup-os"},
                   follow_redirects=False)
        assert r.status_code == 303, r.status_code

    assert wikis.exists("startup-os") and not wikis.exists(slug)
    # The cookie pointed at the old address, which no longer resolves — a bare
    # POST from that tab would otherwise have landed in the default wiki.
    assert "waikiki_wiki=startup-os" in r.headers.get("set-cookie", "")
    tok = db.current_wiki.set("startup-os")
    try:
        assert store.get_page("plan")["markdown"] == "route test content"
    finally:
        db.current_wiki.reset(tok)


def test_a_refused_address_comes_back_as_a_message_not_a_500(wiki):
    from fastapi.testclient import TestClient

    from waikiki.api import app

    with TestClient(app, client=("127.0.0.1", 1)) as c:
        r = c.post("/wikis/main/address", data={"new_slug": "beaconlight"},
                   follow_redirects=False)

    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert wikis.exists("main")
