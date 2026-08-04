"""Page metadata over MCP: discovery, batch writes, and staleness signals."""
from waikiki import mcp_server, store


def _fm(**props):
    body = "\n".join(f"{k}: {v}" for k, v in props.items())
    return f"---\n{body}\n---\n# Body\n\ntext"


def test_set_properties_is_one_write(wiki):
    store.create_page("Meru", "# Meru\n\nA guardian.")
    before = len(store.page_versions("meru"))
    store.set_properties("meru", {"HitPoints": "42", "Class": "Guardian"})
    after = store.page_versions("meru")
    assert len(after) == before + 1          # one revision for the whole batch
    meta = store.page_metadata("meru")
    assert meta["properties"] == {"HitPoints": "42", "Class": "Guardian"}
    assert "A guardian." in store.get_page("meru")["markdown"]   # body preserved


def test_set_properties_updates_and_removes(wiki):
    store.create_page("Meru", _fm(HitPoints="10", Class="Guardian"))
    store.set_properties("meru", {"hit points": "99"})   # case/space-insensitive
    assert store.page_metadata("meru")["properties"]["HitPoints"] == "99"
    store.set_properties("meru", {"Class": None})        # None removes
    assert "Class" not in store.page_metadata("meru")["properties"]


def test_page_metadata_shape(wiki):
    store.create_page("Parent", "top")
    store.create_page("Meru", _fm(tags="character, spirit", HitPoints="42"))
    store.set_parent("meru", "parent")
    m = store.page_metadata("meru")
    assert m["properties"] == {"HitPoints": "42"}        # tags split out
    assert m["tags"] == ["character", "spirit"]
    assert m["parent"]["slug"] == "parent"
    assert m["trashed"] is False and m["updated_at"]
    assert store.page_metadata("nope") is None


def test_metadata_reports_trashed(wiki):
    store.create_page("Gone", "bye")
    store.soft_delete("gone")
    m = store.page_metadata("gone")
    assert m["trashed"] is True and m["deleted_at"]


# --- MCP surface -------------------------------------------------------------

def test_get_page_exposes_freshness_and_properties(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    store.create_page("Meru", _fm(tags="character", HitPoints="42"))
    out = mcp_server.get_page("meru")
    assert out["updated_at"]                    # staleness signal present
    assert out["trashed"] is False
    assert out["live"] is False                 # no live room in tests
    assert out["properties"] == {"HitPoints": "42"}
    assert out["tags"] == ["character"]


def test_get_page_flags_a_trashed_page(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    store.create_page("Gone", "bye")
    store.soft_delete("gone")
    out = mcp_server.get_page("gone")
    assert out["trashed"] is True and out["deleted_at"]


def test_mcp_get_and_set_metadata(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    store.create_page("Meru", "# Meru\n\nbody")
    res = mcp_server.set_metadata("meru", {"HitPoints": "42", "Home": "Beaconlight"})
    assert res["properties"] == {"HitPoints": "42", "Home": "Beaconlight"}
    got = mcp_server.get_metadata("meru")
    assert got["properties"]["Home"] == "Beaconlight"
    assert got["updated_at"] and got["trashed"] is False
    assert "error" in mcp_server.get_metadata("missing")
    assert "error" in mcp_server.set_metadata("meru", {})       # empty rejected
    assert "error" in mcp_server.set_metadata("missing", {"a": "b"})
