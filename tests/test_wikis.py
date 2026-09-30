"""Multi-wiki registry + isolation tests — the core guarantee that wikis cannot
see or link to each other."""
import pytest

from waikiki import db, rag, store, wikis
from waikiki import mcp_server


def test_registry_seeded(wiki):
    slugs = {w["slug"] for w in wikis.list_wikis()}
    assert {"main", "beaconlight", "crosslake", "startupos"} <= slugs
    assert wikis.default_slug() == "main"


def test_create_and_delete_wiki(wiki):
    slug = wikis.create_wiki("Acme Corp")
    assert slug == "acme-corp"
    assert wikis.exists("acme-corp")
    assert wikis.delete_wiki("acme-corp") is True
    assert not wikis.exists("acme-corp")


def test_create_wiki_unique_slug(wiki):
    a = wikis.create_wiki("Dup")
    b = wikis.create_wiki("Dup")
    assert a == "dup" and b == "dup-2"


def test_pages_are_isolated_between_wikis(wiki):
    db.current_wiki.set("main")
    store.create_page("Alpha", "secret main content about apples")

    db.current_wiki.set("beaconlight")
    assert store.list_pages() == []            # beaconlight starts empty
    store.create_page("Beta", "beacon content about bridges")
    assert not any(p["slug"] == "alpha" for p in store.list_pages())

    db.current_wiki.set("main")
    assert not any(p["slug"] == "beta" for p in store.list_pages())
    assert any(p["slug"] == "alpha" for p in store.list_pages())


def test_search_is_isolated_between_wikis(wiki):
    db.current_wiki.set("main")
    store.create_page("Apples", "everything about apples and orchards")
    db.current_wiki.set("crosslake")
    store.create_page("Bridges", "everything about bridges and rivers")

    # A search in one wiki never surfaces another wiki's content.
    assert rag.search_pages("apples") == []          # crosslake active
    db.current_wiki.set("main")
    assert rag.search_pages("bridges") == []
    assert rag.search_pages("apples")                # present in main


def test_wikilink_cannot_cross_wikis(wiki):
    # A page named "Shared" in crosslake must not be reachable from main.
    db.current_wiki.set("crosslake")
    store.create_page("Shared", "crosslake-only secret")
    db.current_wiki.set("main")
    # From main, that slug simply does not exist (a red link), never crosslake's.
    assert store.get_page("shared") is None


def test_wiki_stats(wiki):
    db.current_wiki.set("main")
    store.create_page("Home", "see [[Other Page]] and [[Missing Thing]]")
    store.create_page("Other Page", "back to [[Home]]")
    s = wikis.stats("main")
    assert s["articles"] == 2
    # Home -> other-page (resolved) + missing-thing (broken); Other -> home (resolved)
    assert s["links_resolved"] == 2
    assert s["links_broken"] == 1
    assert s["bytes"] > 0


def test_stats_excludes_trashed_articles(wiki):
    db.current_wiki.set("main")
    store.create_page("Keep", "a")
    store.create_page("Toss", "b")
    store.soft_delete("toss")
    s = wikis.stats("main")
    assert s["articles"] == 1 and s["trashed"] == 1


def test_export_import_roundtrip(wiki, tmp_path):
    db.current_wiki.set("main")
    store.create_page("Portable", "content about turtles that should survive export")

    dest = tmp_path / "backup.wiki"
    wikis.export_to("main", str(dest))
    import zipfile
    assert dest.exists() and zipfile.is_zipfile(str(dest))  # now a zip bundle

    slug = wikis.import_from(str(dest), name="Imported")
    assert slug == "imported" and wikis.exists("imported")
    db.current_wiki.set("imported")
    assert any(p["slug"] == "portable" for p in store.list_pages())


def test_import_rejects_non_wiki_file(wiki, tmp_path):
    junk = tmp_path / "notawiki.wiki"
    junk.write_bytes(b"this is not a sqlite database")
    assert db.is_wiki_db(str(junk)) is False
    with pytest.raises(ValueError):
        wikis.import_from(str(junk))


def test_mcp_requires_active_wiki(monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", None)
    with pytest.raises(RuntimeError):
        mcp_server._require_wiki()
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    # _require_wiki also sets the db context; just check it returns the slug.
    assert mcp_server._require_wiki() == "main"


def test_viewing_with_explicit_wiki_repoints_the_cookie(wiki):
    """Forms POST to bare paths and resolve the wiki from the cookie. If viewing
    ?wiki=X left the cookie on Y, every write on that page — including purge and
    restore — would silently hit Y. Viewing must re-point the cookie."""
    from fastapi.testclient import TestClient

    from waikiki.api import app
    with TestClient(app, client=("127.0.0.1", 1)) as c:
        r = c.get("/?wiki=crosslake", headers={"Cookie": "waikiki_wiki=main"})
        assert r.status_code == 200
        assert "waikiki_wiki=crosslake" in r.headers.get("set-cookie", "")

        # ...so a subsequent bare POST lands in the wiki that was on screen,
        # not in the one the stale cookie named.
        c.post("/wiki/save", data={"slug": "", "title": "Landed",
                                   "markdown": "in crosslake"},
               headers={"Cookie": "waikiki_wiki=crosslake"})
        assert c.get("/api/pages/landed?wiki=crosslake").status_code == 200
        assert c.get("/api/pages/landed?wiki=main").status_code == 404


def test_cookie_untouched_without_an_explicit_wiki(wiki):
    """A plain view must not disturb the active wiki."""
    from fastapi.testclient import TestClient

    from waikiki.api import app
    with TestClient(app, client=("127.0.0.1", 1)) as c:
        c.cookies.set("waikiki_wiki", "crosslake")
        c.get("/")
        assert c.cookies.get("waikiki_wiki") == "crosslake"


def test_the_upload_route_opens_a_bundle_too(wiki, tmp_path):
    """The browser fallback for Open must take a bundle, not just a wiki file.

    The desktop app uses a native dialog and its own JS bridge; this route is the
    other door to the same `wikis.import_from`, and rule 12 is about a door a
    person can actually reach, so both are covered.
    """
    from fastapi.testclient import TestClient

    from waikiki.api import app

    db.current_wiki.set("main")
    store.create_page("Shipped", "content that travels as a snapshot")
    bundle = tmp_path / "main-wiki-bundle.zip"
    with open(bundle, "wb") as fh:
        store.export_wiki_bundle(fh)

    with TestClient(app, client=("127.0.0.1", 1)) as c:
        with open(bundle, "rb") as fh:
            r = c.post("/wikis/import",
                       files={"file": ("main-wiki-bundle.zip", fh,
                                       "application/zip")},
                       follow_redirects=False)
    assert r.status_code == 303, r.status_code
    # The route switches to what it just opened, so the cookie names it — a
    # registry diff would also catch the Help wiki the lifespan seeds.
    landed = r.cookies.get("waikiki_wiki")
    assert landed and wikis.exists(landed), r.headers.get("set-cookie")
    assert wikis.name_of(landed) == "Main"      # the bundle's label, not the file
    db.current_wiki.set(landed)
    assert any(p["slug"] == "shipped"
               for p in store.list_pages(include_children=True))


# --- Imported wikis are searchable -------------------------------------------
#
# A wiki file carries `pages` and `pages_fts`, but every search in the app runs
# over `chunks`/`chunks_fts`/`vec_chunks` (`rag.py`). Importing a file whose
# chunk index didn't travel used to produce a wiki that rendered every page and
# resolved every [[link]] and answered *nothing* — no error, no warning.


def _wiki_file_without_chunks(tmp_path, src_slug="main"):
    """A consistent copy of a wiki file with its chunk index emptied.

    This is what a real export from an older/other install looked like: pages and
    `pages_fts` fully populated, `chunks` at zero.
    """
    import sqlite3

    dest = tmp_path / "no-chunks.db"
    db.backup_db(str(wikis.db_path(src_slug)), str(dest))
    conn = sqlite3.connect(str(dest))
    conn.execute("DELETE FROM chunks")  # the FTS trigger clears chunks_fts too
    conn.commit()
    conn.close()
    return dest


def test_import_indexes_a_wiki_whose_chunks_did_not_travel(wiki, tmp_path):
    db.current_wiki.set("main")
    store.create_page("Turtles", "green sea turtles nest on this beach in summer")

    src = _wiki_file_without_chunks(tmp_path)
    slug = wikis.import_from(str(src), name="Unindexed")

    db.current_wiki.set(slug)
    assert store.get_page("turtles")                      # the page came across
    hits = rag.search_chunks("sea turtles nest")
    assert hits and hits[0]["slug"] == "turtles"          # ...and so did search


def test_import_leaves_a_healthy_index_alone(wiki, tmp_path, monkeypatch):
    """A 200-page wiki that arrives indexed must not be re-embedded for nothing."""
    db.current_wiki.set("main")
    store.create_page("Kayaking", "the eskimo roll rights a capsized kayak")

    dest = tmp_path / "healthy.wiki"
    wikis.export_to("main", str(dest))

    calls = []
    monkeypatch.setattr(rag, "reindex_all", lambda: calls.append(1) or 0)
    slug = wikis.import_from(str(dest), name="Healthy")

    assert calls == []
    db.current_wiki.set(slug)
    assert rag.search_chunks("eskimo roll")[0]["slug"] == "kayaking"


def test_import_survives_a_broken_index_rebuild(wiki, tmp_path, monkeypatch):
    """The index is a cache; failing to rebuild it must not fail the import."""
    db.current_wiki.set("main")
    store.create_page("Turtles", "green sea turtles nest on this beach in summer")
    src = _wiki_file_without_chunks(tmp_path)

    def boom():
        raise RuntimeError("no embedder, no disk, no luck")

    monkeypatch.setattr(rag, "reindex_all", boom)
    slug = wikis.import_from(str(src), name="Unindexed")

    assert wikis.exists(slug)
    db.current_wiki.set(slug)
    assert store.get_page("turtles")
