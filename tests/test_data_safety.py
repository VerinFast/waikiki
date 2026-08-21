"""The data-safety properties from the 1.0 audit (issue #68).

Each of these was established by experiment in `docs/data-safety.md` and is
pinned here, because a property nobody tests is a property that will quietly
stop being true. They are deliberately about *durability and recovery*, not
features: the worst outcome this app has is a family wiki losing content.
"""
from __future__ import annotations

import gc
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from waikiki import appconfig, backups, config, db, store, wikis, ydoc
from waikiki.api import app


# --- Q1: crash / power loss mid-write ----------------------------------------

def test_every_wiki_file_is_wal_with_full_sync(wiki):
    """WAL is the crash-safety story, so prove it per file rather than assume it.

    Checked on a cold read-only handle, not the live connection: WAL is a
    persistent property of the file, and that is what a recovering process sees.
    """
    for w in wikis.list_wikis():
        tok = db.current_wiki.set(w["slug"])
        try:
            db.get_conn()                       # creates the file + schema
            store.create_page("Seed", "words")
        finally:
            db.current_wiki.reset(tok)

    seen = 0
    for path in sorted(config.WIKIS_DIR.glob("*.db")):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal", path
            # 2 = FULL. WAL would tolerate NORMAL, but FULL is what the app has
            # always run with and what the crash answer in the doc assumes.
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2, path
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
        seen += 1
    assert seen >= 2


@pytest.mark.parametrize("write", ["create", "update", "upsert"])
def test_canonical_ydoc_is_written_before_the_derived_index(wiki, write):
    """A failing reindex must not leave the canonical Y.Doc a revision behind.

    `page_ydoc` is the source of truth; the RAG index is a cache that can be
    rebuilt from the markdown. Writing the cache first meant an embedder that
    wasn't ready (or a missing sqlite-vec, or a model download failing) committed
    the projection and skipped the canonical write. That really happened: see
    question 1 in `docs/data-safety.md`.
    """
    def boom(*a, **k):
        raise RuntimeError("embedder not ready")

    if write != "create":
        store.create_page("Ledger", "version one")

    original = store.rag.reindex_page
    store.rag.reindex_page = boom
    try:
        with pytest.raises(RuntimeError):
            if write == "create":
                store.create_page("Ledger", "version two")
            elif write == "update":
                store.update_page("ledger", "Ledger", "version two")
            else:
                store.upsert_page("Ledger", "version two", slug="ledger")
    finally:
        store.rag.reindex_page = original

    # Every content path, not just the one that happened to be audited: each
    # writes the canonical doc first, so a failing cache leaves truth intact.
    page = store.get_page("ledger")
    assert page["markdown"] == "version two"

    # Assert the blob was PERSISTED, not that a read can reconstruct one.
    # canonical_doc() lazily seeds a doc from the projection when no state is
    # stored, so asserting only on its content passes even when the canonical
    # write never happened — which is precisely the bug this guards.
    assert ydoc._load_state(page["id"]) is not None, (
        "no canonical Y.Doc was persisted; the derived index was written first")
    assert ydoc.content_of(ydoc.canonical_doc(page)) == "version two"


@pytest.mark.parametrize("write", ["create", "update", "upsert", "restore_version"])
def test_canonical_ydoc_matches_the_projection_after_every_write(wiki, write):
    """Rule 6's invariant, checked on each ordinary content path."""
    store.create_page("Recipe", "the original")
    if write == "update":
        store.update_page("recipe", "Recipe", "edited")
    elif write == "upsert":
        store.upsert_page("Recipe", "the original\nplus more", slug="recipe")
    elif write == "restore_version":
        store.update_page("recipe", "Recipe", "edited")
        first = store.page_versions("recipe")[-1]
        store.restore_version("recipe", first["id"])

    page = store.get_page("recipe")
    assert ydoc.content_of(ydoc.canonical_doc(page)) == page["markdown"]


def test_the_canonical_ydoc_blob_is_written_in_one_statement(wiki):
    """No torn Y.Doc: the blob lands whole or not at all, so it always decodes.

    `_store_state` is a single INSERT ... ON CONFLICT, which SQLite makes atomic
    regardless of backend. Guarded because a future refactor that splits it into
    read-modify-write would reintroduce a half-written canonical state.
    """
    import inspect

    source = inspect.getsource(ydoc._store_state)
    assert source.count("conn.execute(") == 1

    store.create_page("Long", "x" * 200_000)
    page = store.get_page("long")
    assert ydoc.content_of(ydoc.canonical_doc(page)) == page["markdown"]


# --- Q2: a corrupted wiki file ------------------------------------------------

def _corrupt(slug: str) -> None:
    """Scribble over a wiki file the way a bad sync or a dying disk would.

    Every cached connection is dropped first: a live SQLite handle keeps its own
    view of the file (and would checkpoint the WAL back over the damage), so
    corrupting underneath one tests nothing. This is the cold-file case — the
    state the app finds on next launch.
    """
    db._local = threading.local()
    db._schema_ready.clear()
    gc.collect()                                  # closes the apsw handles

    path = config.WIKIS_DIR / (slug + ".db")
    for suffix in ("-wal", "-shm"):
        extra = config.WIKIS_DIR / (slug + ".db" + suffix)
        if extra.exists():
            extra.unlink()
    size = path.stat().st_size
    with open(path, "r+b") as handle:
        handle.write(b"NotSQLite format 3\x00")   # header gone
        handle.truncate(size // 3)                # ...and the tail with it


def test_a_corrupt_wiki_does_not_take_its_neighbours_with_it(wiki):
    """Physical separation is the isolation guarantee — including in failure."""
    for slug, text in (("main", "main text"), ("beaconlight", "beacon text")):
        tok = db.current_wiki.set(slug)
        try:
            store.create_page("Home", text)
        finally:
            db.current_wiki.reset(tok)

    _corrupt("beaconlight")

    tok = db.current_wiki.set("beaconlight")
    try:
        with pytest.raises(Exception):
            store.get_page("home")
    finally:
        db.current_wiki.reset(tok)

    tok = db.current_wiki.set("main")
    try:
        assert store.get_page("home")["markdown"] == "main text"
        assert store.list_pages()
    finally:
        db.current_wiki.reset(tok)


def test_a_torn_registry_write_cannot_lose_a_wiki(wiki):
    """`wikis.json` is replaced by rename, so a failed save keeps the old file.

    Before this, a truncated registry parsed as "no wikis" and every wiki the
    user had created themselves vanished from the app.
    """
    slug = wikis.create_wiki("Family Recipes")
    tok = db.current_wiki.set(slug)
    try:
        store.create_page("Nana's Pie", "the only copy")
    finally:
        db.current_wiki.reset(tok)
    before = (config.DATA_DIR / "wikis.json").read_text()

    real_replace = wikis.os.replace

    def fail(src, dst):
        raise OSError(28, "No space left on device")

    wikis.os.replace = fail
    try:
        with pytest.raises(OSError):
            wikis.create_wiki("Doomed")
    finally:
        wikis.os.replace = real_replace

    raw = (config.DATA_DIR / "wikis.json").read_text()
    assert raw == before
    assert json.loads(raw)                       # still parses
    assert wikis.exists(slug)
    assert not any(w["name"] == "Doomed" for w in wikis.list_wikis())
    # ...and no debris left beside it that a later reader could pick up
    assert not list(config.DATA_DIR.glob("wikis.json.tmp*"))


def test_a_torn_app_config_write_cannot_turn_backups_off(wiki):
    """`app_config.json` holds whether backups run at all — same rename rule."""
    appconfig.set("backup_keep", 4)
    before = (config.DATA_DIR / "app_config.json").read_text()

    real_replace = appconfig.os.replace

    def fail(src, dst):
        raise OSError(28, "No space left on device")

    appconfig.os.replace = fail
    try:
        with pytest.raises(OSError):
            appconfig.set("backup_enabled", False)
    finally:
        appconfig.os.replace = real_replace

    assert (config.DATA_DIR / "app_config.json").read_text() == before
    assert backups.enabled() is True
    assert backups.keep() == 4
    assert not list(config.DATA_DIR.glob("app_config.json.tmp*"))


# --- Q3: restore --------------------------------------------------------------

def test_backups_are_on_by_default_and_land_where_the_docs_say(wiki):
    """Settings tells people to restore from `<data>/backups` — hold that true."""
    assert backups.enabled() is True             # nothing configured yet
    assert backups.interval_hours() == backups.DEFAULT_INTERVAL_HOURS
    assert backups.keep() == backups.DEFAULT_KEEP

    slug = wikis.create_wiki("Family Recipes")
    tok = db.current_wiki.set(slug)
    try:
        store.create_page("Nana's Pie", "the only copy of this recipe")
    finally:
        db.current_wiki.reset(tok)

    res = backups.run_backup()
    assert res["ok"] and slug in res["wikis"]     # every wiki, not just the default
    snapshot = config.DATA_DIR / "backups" / res["name"] / f"{slug}.db"
    assert snapshot.exists()


def test_a_backup_snapshot_carries_images_and_history_too(wiki):
    """"A snapshot is a complete copy" is a claim the Settings pane makes."""
    store.create_page("Nana's Pie", "the only copy")
    store.update_page("nanas-pie", "Nana's Pie", "the only copy, revised")
    image_id = store.save_image("pie.png", "image/png", b"\x89PNG-not-really")

    res = backups.run_backup()
    restored = wikis.import_from(
        str(config.DATA_DIR / "backups" / res["name"] / "main.db"), name="Restored")

    tok = db.current_wiki.set(restored)
    try:
        page = store.get_page("nanas-pie")
        assert page["markdown"] == "the only copy, revised"
        assert len(store.page_versions("nanas-pie")) == 2   # yesterday's text too
        assert store.get_image(image_id)["data"] == b"\x89PNG-not-really"
    finally:
        db.current_wiki.reset(tok)


def test_restoring_a_backup_leaves_the_broken_wiki_alone(wiki):
    """Open-as-a-new-wiki, never overwrite: you can compare before you commit."""
    store.create_page("Home", "good text")
    res = backups.run_backup()
    store.update_page("home", "Home", "text I regret")

    restored = wikis.import_from(
        str(config.DATA_DIR / "backups" / res["name"] / "main.db"), name="Restored")
    assert restored != "main"

    tok = db.current_wiki.set(restored)
    try:
        assert store.get_page("home")["markdown"] == "good text"
    finally:
        db.current_wiki.reset(tok)
    assert store.get_page("home")["markdown"] == "text I regret"


# --- Q4: import paths ---------------------------------------------------------

def test_a_part_written_import_is_incomplete_but_never_destructive(wiki):
    """The accepted risk, pinned: a failed local write leaves a *retryable* wiki.

    `_read_bundle` already makes a bad payload all-or-nothing. A failure of the
    local writes is not staged, so the import stops part-way — but nothing is
    deleted, every page that landed is a normal versioned page, and re-running
    the same bundle finishes the job. See question 4 in `docs/data-safety.md`.
    """
    for i in range(1, 7):
        store.create_page(f"Chapter {i}", f"chapter {i} body")
    bundle = store.export_wiki_bundle(label="Source")

    target = wikis.create_wiki("Target")
    tok = db.current_wiki.set(target)
    try:
        store.create_page("Chapter 1", "a local draft I already had", slug="chapter-1")

        real_create = store.create_page
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError(28, "No space left on device")
            return real_create(*a, **k)

        store.create_page = flaky
        try:
            with pytest.raises(OSError):
                store.import_wiki_bundle(bundle)
        finally:
            store.create_page = real_create

        partial = {p["slug"] for p in store.list_pages(include_children=True)}
        assert partial                                   # something landed
        assert len(partial) < 6                          # but not everything
        # the page that existed before the import kept its history
        assert any(v["markdown"] == "a local draft I already had"
                   for v in [store.get_version(v["id"])
                             for v in store.page_versions("chapter-1")])

        res = store.import_wiki_bundle(bundle)           # retry finishes it
        assert res["pages"] == 6
        final = {p["slug"] for p in store.list_pages(include_children=True)}
        assert final == {f"chapter-{i}" for i in range(1, 7)}
        for slug in final:
            assert store.get_page(slug)["markdown"].strip()
    finally:
        db.current_wiki.reset(tok)


# --- Q5: version history as a safety net --------------------------------------

def test_yesterdays_text_is_reachable_from_the_article_page(wiki):
    """The only undo a non-technical user has: article -> Details -> History.

    Pinned as a chain of links a person can actually follow, because a template
    tidy-up that dropped the Details tab would remove the app's undo without
    breaking a single other test.
    """
    store.create_page("Packing List", "yesterday's careful list")
    store.update_page("packing-list", "Packing List", "oops, I deleted everything")

    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.cookies.set("waikiki_wiki", "main")

        article = client.get("/wiki/packing-list")
        assert article.status_code == 200
        assert '/wiki/packing-list/details' in article.text     # click 1

        details = client.get("/wiki/packing-list/details")
        assert details.status_code == 200
        assert 'id="history"' in details.text                   # click 2 opens it
        assert "History (2)" in details.text

        old = store.page_versions("packing-list")[-1]
        assert f'/wiki/packing-list/history/{old["id"]}' in details.text

        view = client.get(f"/wiki/packing-list/history/{old['id']}")
        assert view.status_code == 200
        assert "yesterday's careful list" in view.text
        assert "Changes since this version" in view.text        # a diff, not a guess

        client.post(f"/wiki/packing-list/history/{old['id']}/restore",
                    follow_redirects=False)

    assert store.get_page("packing-list")["markdown"] == "yesterday's careful list"
    # restoring is itself a versioned write, so the regretted text is still there
    assert len(store.page_versions("packing-list")) == 3
