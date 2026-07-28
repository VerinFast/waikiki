from waikiki import help_content, mcp_server, store


def test_mcp_custom_order_tools(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    for t in ("A", "B", "C"):
        store.create_page(t, "x")
    store.set_page_order(["a", "b", "c"])
    assert mcp_server.list_order()["order"] == ["a", "b", "c"]
    assert mcp_server.get_order("b")["position"] == 1
    assert mcp_server.get_order("missing")["position"] is None
    res = mcp_server.set_order("c", 0)
    assert res["order"] == ["c", "a", "b"] and res["position"] == 0
    assert "error" in mcp_server.set_order("nope", 0)


def test_mcp_docs_tools(wiki):
    help_content.seed()
    slugs = [d["slug"] for d in mcp_server.list_docs()["docs"]]
    assert "templates" in slugs and "getting-started" in slugs
    doc = mcp_server.read_doc("templates")
    assert doc["title"] == "Templates" and "create_from_template" in doc["markdown"]
    assert "error" in mcp_server.read_doc("no-such-doc")
