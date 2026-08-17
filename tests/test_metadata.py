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

def test_get_page_exposes_freshness_and_properties(wiki, monkeypatch, fake_live_http):
    # Isolate from any dev server on the default port: get_page asks the web app
    # for live CRDT text, and without this the test would read a *different*
    # database and report a spurious `live`.
    _offline(monkeypatch, fake_live_http)
    store.create_page("Meru", _fm(tags="character", HitPoints="42"))
    out = mcp_server.get_page("meru")
    assert out["updated_at"]                    # staleness signal present
    assert out["trashed"] is False
    assert out["live"] is False                 # no live room in tests
    assert out["properties"] == {"HitPoints": "42"}
    assert out["tags"] == ["character"]


def test_get_page_returns_resolved_outbound_links(wiki, monkeypatch, fake_live_http):
    _offline(monkeypatch, fake_live_http)
    store.create_page("Igni", "fire")
    store.create_page("Edaphos", "earth spirit")
    store.create_page("Pantheon", "## Family tree\n\n[[Igni]] · [[Edaphos|earth]] · "
                                  "[[Corliss]] · [[#Family tree]] · [[Igni]]")
    links = mcp_server.get_page("pantheon")["links"]
    assert links == [
        {"target": "igni", "title": "Igni", "label": "Igni",
         "exists": True, "count": 2},
        # the reader sees "earth"; slugifying that would miss the page
        {"target": "edaphos", "title": "Edaphos", "label": "earth",
         "exists": True, "count": 1},
        {"target": "corliss", "title": None, "label": "Corliss",
         "exists": False, "count": 1},
    ]


def test_get_page_links_describe_the_live_markdown_it_returned(wiki, monkeypatch,
                                                              fake_live_http):
    """`links` must match the text actually returned, not the saved revision."""
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    store.create_page("Igni", "fire")
    store.create_page("Pantheon", "[[Igni]]")

    class _Live:                       # the web app's unsaved CRDT text
        status_code = 200

        @staticmethod
        def json():
            return {"markdown": "[[Igni]] and now also [[Corliss]]"}

    fake_live_http(lambda url: _Live())
    out = mcp_server.get_page("pantheon")
    assert out["live"] is True
    assert [d["target"] for d in out["links"]] == ["igni", "corliss"]


def _offline(monkeypatch, fake_live_http):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    fake_live_http()                                 # don't touch a live dev server


def test_get_page_hint_asks_for_the_links_it_returned(wiki, monkeypatch, fake_live_http):
    """A docstring is read once; the response is read every time (issue #47)."""
    _offline(monkeypatch, fake_live_http)
    store.create_page("Igni", "fire")
    store.create_page("Pantheon", "[[Igni]] · [[Corliss]] · [[Igni]] again")

    out = mcp_server.get_page("pantheon")
    hint = out["hint"]
    # Counts the rows in `links` (deduplicated), and names the field to look at.
    assert f"links to {len(out['links'])} pages" in hint
    assert "`links`" in hint
    # One line that earns its place. A paragraph here is a field agents skip,
    # at which point the nudge is pure token cost.
    assert len(hint) < 140
    # Guidance, not an accusation: never imply the agent already failed.
    assert not any(w in hint.lower() for w in ("you should have", "failed", "forgot"))


def test_get_page_hint_counts_one_link_in_the_singular(wiki, monkeypatch, fake_live_http):
    _offline(monkeypatch, fake_live_http)
    store.create_page("Igni", "fire")
    store.create_page("Pantheon", "just [[Igni]]")
    assert "links to 1 page." in mcp_server.get_page("pantheon")["hint"]


def test_get_page_omits_the_hint_when_there_is_nothing_to_follow(wiki, monkeypatch, fake_live_http):
    """Stop hinting the moment it stops being true — a hint on every page is one
    agents learn to ignore."""
    _offline(monkeypatch, fake_live_http)
    # A section-only link is same-page, so it is not an outbound link.
    store.create_page("Alone", "## Family tree\n\nSee [[#Family tree]].")
    out = mcp_server.get_page("alone")
    assert out["links"] == [] and "hint" not in out


def test_get_page_flags_a_trashed_page(wiki, monkeypatch, fake_live_http):
    _offline(monkeypatch, fake_live_http)
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


# --- Cheap revalidation (check_pages) ----------------------------------------

def test_content_version_changes_with_text(wiki):
    a = store.content_version("hello")
    assert a == store.content_version("hello")      # stable
    assert a != store.content_version("hello!")     # sensitive
    assert len(a) == 12


def test_check_pages_detects_stale_unchanged_and_missing(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    # No web app in tests → check_pages falls back to the DB path.
    monkeypatch.setattr(mcp_server.httpx, "post",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    store.create_page("Meru", "original")
    store.create_page("Bram", "steady")

    seen = {"meru": mcp_server.get_page("meru")["version"],
            "bram": mcp_server.get_page("bram")["version"],
            "ghost": "whatever"}
    store.update_page("meru", "Meru", "CHANGED", author="human")

    out = mcp_server.check_pages(seen)
    assert out["stale"] == ["meru"]
    assert out["unchanged"] == ["bram"]
    assert out["missing"] == ["ghost"]
    assert out["current"]["meru"] == store.content_version("CHANGED")
    assert "meru" not in out["current"] or out["current"]["meru"] != seen["meru"]


def test_check_pages_accepts_updated_at_as_token(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    monkeypatch.setattr(mcp_server.httpx, "post",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    store.create_page("Meru", "body")
    ts = store.get_page("meru")["updated_at"]
    out = mcp_server.check_pages({"meru": ts})
    assert out["unchanged"] == ["meru"]


def test_check_pages_flags_trashed_and_rejects_empty(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    monkeypatch.setattr(mcp_server.httpx, "post",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    store.create_page("Gone", "bye")
    v = mcp_server.get_page("gone")["version"]
    store.soft_delete("gone")
    assert mcp_server.check_pages({"gone": v})["trashed"] == ["gone"]
    assert "error" in mcp_server.check_pages({})
