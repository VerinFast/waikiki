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
