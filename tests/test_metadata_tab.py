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
