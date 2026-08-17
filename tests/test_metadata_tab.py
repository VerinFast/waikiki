"""The Metadata tab: frontmatter properties as a peer of Article.

The auto key/value table used to be prepended to every page's body. It now lives
here, and is editable. Custom elements the author placed in the body are a
different thing and are untouched.
"""
from fastapi.testclient import TestClient

from waikiki import store
from waikiki.api import app


def _client():
    return TestClient(app, client=("127.0.0.1", 1))


def test_the_tab_shows_properties_and_tags(wiki):
    store.create_page("Meru", "---\ntags: city, spire\nRole: capital\nHP: 20 / 100\n---\nbody")
    with _client() as c:
        body = c.get("/wiki/meru/metadata").text
    assert "Role" in body and "capital" in body
    assert "HP" in body and "20 / 100" in body
    assert "city" in body and "spire" in body


def test_editing_a_value_writes_frontmatter(wiki):
    store.create_page("Meru", "---\nRole: capital\n---\nbody text")
    with _client() as c:
        c.post("/wiki/meru/metadata",
               data={"key": ["Role"], "value": ["ruin"]}, follow_redirects=False)
    assert store.get_property("meru", "Role") == "ruin"
    assert "body text" in store.get_page("meru")["markdown"]


def test_values_are_stored_verbatim(wiki):
    """Free text stays free text — '20 / 100' must not be reinterpreted."""
    store.create_page("Meru", "body")
    with _client() as c:
        c.post("/wiki/meru/metadata",
               data={"key": ["HP"], "value": ["20 / 100"]}, follow_redirects=False)
    assert store.get_property("meru", "HP") == "20 / 100"


def test_a_removed_row_removes_the_property(wiki):
    store.create_page("Meru", "---\nRole: capital\nAge: old\n---\nbody")
    with _client() as c:                     # submit only Role: Age is gone
        c.post("/wiki/meru/metadata",
               data={"key": ["Role"], "value": ["capital"]}, follow_redirects=False)
    assert store.get_property("meru", "Age") is None
    assert store.get_property("meru", "Role") == "capital"


def test_key_order_is_preserved(wiki):
    store.create_page("Meru", "body")
    with _client() as c:
        c.post("/wiki/meru/metadata",
               data={"key": ["Zeta", "Alpha"], "value": ["1", "2"]},
               follow_redirects=False)
    md = store.get_page("meru")["markdown"]
    assert md.index("Zeta") < md.index("Alpha"), "the form's order was not kept"


def test_tags_survive_a_property_edit(wiki):
    """Tags share the frontmatter block; editing properties must not drop them."""
    store.create_page("Meru", "---\ntags: city, spire\nRole: capital\n---\nbody")
    with _client() as c:
        c.post("/wiki/meru/metadata",
               data={"key": ["Role"], "value": ["ruin"]}, follow_redirects=False)
    assert store.tags_of("meru") == ["city", "spire"]


def test_a_tags_key_is_refused(wiki):
    """Accepting one here would create a second, competing source for tags."""
    store.create_page("Meru", "---\ntags: city\n---\nbody")
    with _client() as c:
        r = c.post("/wiki/meru/metadata",
                   data={"key": ["tags"], "value": ["hijacked"]},
                   follow_redirects=False)
    assert "error=" in r.headers.get("location", "")
    assert store.tags_of("meru") == ["city"]
    assert store.get_property("meru", "tags") is None


def test_the_article_no_longer_carries_the_property_dump(wiki):
    store.create_page("Meru", "---\nRole: capital\n---\n# Meru\n\nbody")
    with _client() as c:
        article = c.get("/wiki/meru").text
    assert 'class="infobox"' not in article
    assert "capital" not in article


def test_the_tab_is_linked_from_article_and_details(wiki):
    store.create_page("Meru", "body")
    with _client() as c:
        assert '/wiki/meru/metadata' in c.get("/wiki/meru").text
        assert '/wiki/meru/metadata' in c.get("/wiki/meru/details").text


def test_metadata_of_a_missing_page_is_404(wiki):
    with _client() as c:
        assert c.get("/wiki/nope/metadata").status_code == 404


# --- Chat launcher (#20) ------------------------------------------------------

def test_chat_launcher_is_on_an_article_page(wiki):
    store.create_page("Meru", "body")
    with _client() as c:
        body = c.get("/wiki/meru").text
    assert 'id="wk-chat-launch"' in body


def test_chat_launcher_is_on_a_page_with_no_article(wiki):
    """The launcher is everywhere; without a page it chats about the wiki."""
    with _client() as c:
        body = c.get("/index").text
    assert 'id="wk-chat-launch"' in body


def test_chat_is_gone_from_the_details_page(wiki):
    store.create_page("Meru", "body")
    with _client() as c:
        body = c.get("/wiki/meru/details").text
    # The old inline section's own ids/classes — the launcher in base.html
    # reuses the chatform/chatlog *classes*, so match on what was unique to it.
    assert 'class="chatpanel"' not in body
    assert 'id="chatform"' not in body and 'id="chatlog"' not in body
    assert 'id="chatq"' not in body
    # ...and the launcher is there instead.
    assert 'id="wk-chat-launch"' in body


def test_the_wiki_scoped_chat_route_exists(wiki, monkeypatch):
    """Posting with no page must reach chat.answer with slug=None."""
    from waikiki import chat as chat_mod

    seen = {}

    def fake(slug, question, provider="claude", model="", history=None, timeout=180):
        seen["slug"] = slug
        return {"ok": True, "answer": "hi", "provider": provider}

    monkeypatch.setattr(chat_mod, "answer", fake)
    with _client() as c:
        r = c.post("/chat", json={"question": "what is this wiki about?"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert seen["slug"] is None


def test_wiki_scoped_prompt_has_no_article_and_no_excerpts(wiki):
    """No retrieval stand-in: the agent is meant to use MCP (issue #30)."""
    from waikiki import chat as chat_mod

    prompt = chat_mod.build_prompt("", "", "", [], "a question", "SYS", wiki="main")
    assert "# Article" not in prompt
    assert "Other relevant excerpts" not in prompt
    assert "'main' wiki" in prompt


# --- Tag editing (#27) --------------------------------------------------------

def test_tags_are_editable_from_the_metadata_tab(wiki):
    store.create_page("Meru", "---\ntags: city\nRole: capital\n---\nbody")
    with _client() as c:
        c.post("/wiki/meru/tags", data={"tag": ["city", "spire"]},
               follow_redirects=False)
    assert store.tags_of("meru") == ["city", "spire"]


def test_editing_tags_rewrites_the_frontmatter_not_just_the_index(wiki):
    """The markdown is the source of truth.

    Updating page_tags alone would leave the page's own text disagreeing with
    its tags, and the next save from the editor would silently revert them.
    """
    store.create_page("Meru", "---\ntags: city\n---\nbody")
    with _client() as c:
        c.post("/wiki/meru/tags", data={"tag": ["spire"]}, follow_redirects=False)
    assert "tags: spire" in store.get_page("meru")["markdown"]
    assert "city" not in store.get_page("meru")["markdown"]


def test_properties_survive_a_tag_edit(wiki):
    store.create_page("Meru", "---\ntags: city\nRole: capital\nHP: 20 / 100\n---\nbody")
    with _client() as c:
        c.post("/wiki/meru/tags", data={"tag": ["spire"]}, follow_redirects=False)
    assert store.get_property("meru", "Role") == "capital"
    assert store.get_property("meru", "HP") == "20 / 100"


def test_tags_are_normalised(wiki):
    """Lowercased and de-duplicated, so 'Spire' and 'spire' don't diverge —
    pages_with_tag looks up lowercase."""
    store.create_page("Meru", "body")
    with _client() as c:
        c.post("/wiki/meru/tags", data={"tag": ["Spire", " spire ", "CITY"]},
               follow_redirects=False)
    assert store.tags_of("meru") == ["city", "spire"]


def test_a_tag_cannot_smuggle_in_a_separator(wiki):
    """','/';' separate tags on read, so they must not survive inside one."""
    store.create_page("Meru", "body")
    with _client() as c:
        c.post("/wiki/meru/tags", data={"tag": ["a,b"]}, follow_redirects=False)
    assert store.tags_of("meru") == ["a b"]


def test_all_tags_are_removable(wiki):
    store.create_page("Meru", "---\ntags: city\nRole: capital\n---\nbody")
    with _client() as c:
        c.post("/wiki/meru/tags", data={"tag": []}, follow_redirects=False)
    assert store.tags_of("meru") == []
    assert store.get_property("meru", "Role") == "capital", "properties were lost"


def test_the_editor_offers_existing_tags(wiki):
    store.create_page("Other", "---\ntags: spire, ruin\n---\nx")
    store.create_page("Meru", "body")
    with _client() as c:
        body = c.get("/wiki/meru/metadata").text
    assert 'id="alltags"' in body and "spire" in body and "ruin" in body
