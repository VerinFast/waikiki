import json

from waikiki import elements, store


def test_infobox_seeded(wiki):
    assert "infobox" in [e["slug"] for e in elements.list_elements()]


def test_element_crud_and_field_parsing(wiki):
    elements.save_element("", "Stat Block",
                          ["name*", "hp", "ac* | Armor Class"],
                          "<div>{{name}}</div>", ".x{}", "")
    el = elements.get_element("stat-block")
    assert el and el["name"] == "Stat Block"
    fields = json.loads(el["fields"])
    assert {"name": "name", "required": True, "label": "name"} in fields
    assert any(f["name"] == "ac" and f["required"] and f["label"] == "Armor Class"
               for f in fields)
    elements.delete_element("stat-block")
    assert elements.get_element("stat-block") is None


def test_render_valid_block_becomes_web_component(wiki):
    store.create_page("Hero", "```infobox\ntitle: Spider-Man\nReal name: Peter\n```")
    html = store.get_page("hero")["html"]
    assert "<wk-infobox" in html and 'class="wk-element-defs"' in html
    assert "Peter" in html


def test_render_missing_required_shows_error(wiki):
    store.create_page("Bad", "```infobox\nReal name: Peter\n```")   # no title
    html = store.get_page("bad")["html"]
    assert "element-error" in html and "title" in html
    assert "<wk-infobox" not in html


def test_unregistered_fence_stays_code(wiki):
    store.create_page("Code", "```python\nprint(1)\n```")
    html = store.get_page("code")["html"]
    assert "<wk-python" not in html and "print" in html


def test_mcp_element_tools(wiki, monkeypatch):
    from waikiki import mcp_server
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    res = mcp_server.create_element(name="Card", fields=["heading*", "body"],
                                    html="<div>{{heading}}</div>", css=".c{}", js="")
    assert res["slug"] == "card" and res["usage"] == "```card"
    slugs = [e["slug"] for e in mcp_server.list_elements()["elements"]]
    assert "card" in slugs and "infobox" in slugs
    got = mcp_server.get_element("card")
    assert got["name"] == "Card" and "{{heading}}" in got["html"]
    assert mcp_server.delete_element("card")["deleted"] == "card"
    assert "error" in mcp_server.get_element("card")


def test_element_field_values_get_wikilinks_without_a_shim(wiki):
    """Element fields are lifted out before markdown runs, so [[links]] in them
    reached the component as raw text — every element author had to write their
    own link parser in JS. The server now renders them."""
    store.create_page("Meru", "the target page")
    store.create_page("Hero", "```infobox\ntitle: Bram\nHome: [[Meru]]\n```")
    html = store.get_page("hero")["html"]
    assert 'data-html=' in html
    assert "/wiki/meru" in html                 # resolved server-side
    # raw props are still provided unchanged, so existing elements keep working
    assert "[[Meru]]" in html


def test_element_fields_resolve_by_title_like_prose(wiki):
    """A client-side shim can only slugify; it can't do link-by-title, so links
    to a renamed page silently broke."""
    p = store.create_page("Setup Guide", "body")          # slug: setup-guide
    store.create_page("Ref", "```infobox\ntitle: T\nSee: [[Setup Guide]]\n```")
    assert f"/wiki/{p['slug']}" in store.get_page("ref")["html"]


def test_element_field_values_are_escaped(wiki):
    store.create_page("X", "```infobox\ntitle: T\nEvil: <script>alert(1)</script>\n```")
    html = store.get_page("x")["html"]
    assert "<script>alert(1)</script>" not in html
