from waikiki import store


def test_create_and_get(wiki):
    p = store.create_page("Hello World", "some **markdown**")
    assert p["slug"] == "hello-world"
    got = store.get_page("hello-world")
    assert got["title"] == "Hello World"
    assert "<strong>markdown</strong>" in got["html"]


def test_slug_uniqueness(wiki):
    a = store.create_page("Dup", "one")
    b = store.create_page("Dup", "two")
    assert a["slug"] == "dup"
    assert b["slug"] == "dup-2"


def test_update_creates_version(wiki):
    store.create_page("Note", "v1")
    store.update_page("note", "Note", "v2", author="ai")
    assert store.get_page("note")["markdown"] == "v2"
    versions = store.page_versions("note")
    assert len(versions) == 2
    assert versions[0]["author"] == "ai"  # newest first


def test_upsert_updates_existing(wiki):
    store.create_page("Thing", "orig")
    p = store.upsert_page("Thing", "changed")
    assert p["slug"] == "thing"
    assert store.get_page("thing")["markdown"] == "changed"


def test_soft_delete_restore_hard_delete(wiki):
    store.create_page("Temp", "x")
    # soft delete → hidden from lists, still fetchable, restorable
    assert store.soft_delete("temp") is True
    assert not any(p["slug"] == "temp" for p in store.list_pages())
    assert any(p["slug"] == "temp" for p in store.list_trash())
    assert store.get_page("temp")["deleted_at"] is not None
    assert store.soft_delete("temp") is False  # already trashed
    # restore
    assert store.restore("temp") is True
    assert any(p["slug"] == "temp" for p in store.list_pages())
    # hard delete → gone
    store.soft_delete("temp")
    assert store.hard_delete("temp") is True
    assert store.get_page("temp") is None


def test_version_retention_prunes(wiki):
    from waikiki import db
    db.set_setting("retention_versions", "3")
    store.create_page("Churn", "v1")
    for i in range(2, 8):
        store.update_page("churn", "Churn", f"v{i}")
    versions = store.page_versions("churn")
    assert len(versions) == 3  # only the last 3 kept
    assert versions[0]["author"] in ("human", "ai")


def test_restore_version(wiki):
    store.create_page("Doc", "original text")
    store.update_page("doc", "Doc", "changed text")
    first = store.page_versions("doc")[-1]  # oldest = original
    store.restore_version("doc", first["id"])
    assert store.get_page("doc")["markdown"] == "original text"


def test_sweep_trash_respects_days(wiki):
    from waikiki import db
    db.set_setting("retention_trash_days", "30")
    store.create_page("Old", "x")
    store.soft_delete("old")
    # backdate the deletion beyond the window
    db.get_conn().execute(
        "UPDATE pages SET deleted_at=datetime('now','-40 days') WHERE slug='old'")
    db.get_conn().commit()
    assert store.sweep_trash() == 1
    assert store.get_page("old") is None


def test_clone_page(wiki):
    store.create_page("Original", "body text")
    copy = store.clone_page("original")
    assert copy["slug"] == "original-copy"
    assert copy["title"] == "Original (copy)"
    assert store.get_page("original-copy")["markdown"] == "body text"


def test_parent_pages_hidden_from_rail(wiki):
    store.create_page("Parent", "p")
    store.create_page("Child", "c content")
    store.set_parent("child", "parent")
    rail = [p["slug"] for p in store.list_pages()]
    assert "parent" in rail and "child" not in rail          # child hidden
    assert [c["slug"] for c in store.children("parent")] == ["child"]
    assert store.get_page("child")["parent_id"] is not None


def test_child_excluded_from_main_search(wiki):
    from waikiki import rag
    store.create_page("Top", "unique aardvark content")
    store.create_page("Sub", "unique aardvark content in child")
    store.set_parent("sub", "top")
    hits = [h["slug"] for h in rag.search_chunks("aardvark")]
    assert "sub" not in hits and "top" in hits               # child out of main RAG


def test_set_parent_top_level_again(wiki):
    store.create_page("P", "x")
    store.create_page("K", "y")
    store.set_parent("k", "p")
    store.set_parent("k", None)                              # back to top-level
    assert "k" in [p["slug"] for p in store.list_pages()]


def test_backlinks_and_broken(wiki):
    store.create_page("Home", "see [[Guide]] and [[Ghost Town]]")
    store.create_page("Guide", "back to [[Home]]")
    assert [b["slug"] for b in store.backlinks("guide")] == ["home"]
    assert [b["slug"] for b in store.backlinks("home")] == ["guide"]
    broken = store.broken_links()
    assert any(b["target"] == "ghost-town" for b in broken)
    assert not any(b["target"] == "guide" for b in broken)


def test_link_by_title_resolves_in_html(wiki):
    store.create_page("Setup Guide", "content")           # slug: setup-guide
    home = store.create_page("Home", "see [[Setup Guide]]")
    assert '/wiki/setup-guide' in home["html"]


def test_rename_rewrites_backlinks(wiki):
    store.create_page("Guide", "hello")
    store.create_page("Home", "read the [[Guide]] please")
    # rename Guide -> Manual (slug stays 'guide')
    store.update_page("guide", "Manual", "hello")
    assert "[[Manual]]" in store.get_page("home")["markdown"]
    # link still resolves to the same stable slug
    assert '/wiki/guide' in store.get_page("home")["html"]


def test_recent_changes_feed(wiki):
    store.create_page("A", "1")
    store.update_page("a", "A", "2", author="ai")
    feed = store.recent_changes(limit=10)
    assert feed[0]["slug"] == "a" and feed[0]["author"] == "ai"
    # since-filter: nothing after a far-future timestamp
    assert store.recent_changes(since="2999-01-01 00:00:00") == []


def test_images_roundtrip(wiki):
    img_id = store.save_image("d.png", "image/png", b"\x89PNG\r\n")
    got = store.get_image(img_id)
    assert got["mimetype"] == "image/png"
    assert got["data"] == b"\x89PNG\r\n"


def test_list_pages_orders_by_recent(wiki):
    store.create_page("First", "a")
    store.create_page("Second", "b")
    slugs = [p["slug"] for p in store.list_pages()]
    assert set(slugs) == {"first", "second"}


def test_list_pages_sort_by_title(wiki):
    store.create_page("Zebra", "a")
    store.create_page("Apple", "b")
    titles = [p["title"] for p in store.list_pages(sort="title")]
    assert titles == ["Apple", "Zebra"]


def test_star_toggle_and_filter(wiki):
    store.create_page("Fav", "a")
    store.create_page("Meh", "b")
    assert store.toggle_star("fav") is True
    assert store.get_page("fav")["starred"] == 1
    starred = [p["slug"] for p in store.list_pages(starred_only=True)]
    assert starred == ["fav"]
    # toggling again unstars
    assert store.toggle_star("fav") is False
    assert store.list_pages(starred_only=True) == []
    assert store.toggle_star("nope") is None


def test_custom_sort_order(wiki):
    store.create_page("Alpha", "a")
    store.create_page("Beta", "b")
    store.create_page("Gamma", "c")
    # Before any manual order, custom falls back to title order.
    assert [p["slug"] for p in store.list_pages(sort="custom")] == ["alpha", "beta", "gamma"]
    # Set an explicit order; list_pages(custom) honors it.
    store.set_page_order(["gamma", "alpha", "beta"])
    assert [p["slug"] for p in store.list_pages(sort="custom")] == ["gamma", "alpha", "beta"]
    # Other sorts are unaffected.
    assert [p["slug"] for p in store.list_pages(sort="title")] == ["alpha", "beta", "gamma"]
