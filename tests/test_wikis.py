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
    assert dest.exists() and db.is_wiki_db(str(dest))

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
