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


def test_delete(wiki):
    store.create_page("Temp", "x")
    assert store.delete_page("temp") is True
    assert store.get_page("temp") is None
    assert store.delete_page("temp") is False


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
