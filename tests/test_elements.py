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
