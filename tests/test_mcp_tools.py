import anyio

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


def _branchy_wiki():
    for title in ("Pantheon", "Villages", "Loner"):
        store.create_page(title, "top")
    store.create_page("Igni", "a god")
    store.create_page("Hamlet", "a village")
    store.set_parent("igni", "pantheon")
    store.set_parent("hamlet", "villages")


def test_mcp_list_pages_children(wiki, monkeypatch):
    """The tool can reach sub-pages, and says how many it withheld (issue #45)."""
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")
    _branchy_wiki()

    default = mcp_server.list_pages()
    assert {p["slug"] for p in default["pages"]} == {"pantheon", "villages", "loner"}
    # The default still hides children — but never silently.
    assert default["children_hidden"] == 2
    assert "children=true" in default["hint"]

    every = mcp_server.list_pages(children=True)
    assert {"igni", "hamlet"} <= {p["slug"] for p in every["pages"]}
    assert every["children_hidden"] == 0
    assert "hint" not in every
    # null means "everything", same as true.
    assert ({p["slug"] for p in mcp_server.list_pages(children=None)["pages"]}
            == {p["slug"] for p in every["pages"]})

    branch = mcp_server.list_pages(children=["pantheon"])
    slugs = {p["slug"] for p in branch["pages"]}
    assert "igni" in slugs and "hamlet" not in slugs
    assert branch["children_hidden"] == 1            # the villages' child
    assert "unknown_parents" not in branch

    # A parent that doesn't exist is reported, not silently empty.
    bogus = mcp_server.list_pages(children=["pantheon", "no-such-parent"])
    assert bogus["unknown_parents"] == ["no-such-parent"]

    # Rows are navigable by slug: an agent never sees page ids.
    igni = next(p for p in every["pages"] if p["slug"] == "igni")
    assert igni["parent_slug"] == "pantheon"
    assert mcp_server.get_page(igni["parent_slug"])["title"] == "Pantheon"


def test_mcp_list_pages_schema_accepts_every_shape(wiki, monkeypatch):
    """The bool | list[str] | None union must survive MCP schema generation and
    real client-shaped calls — an unusable schema is a broken tool."""
    _branchy_wiki()
    # Act as one identified agent — the seam _SESSION_OVERRIDE exists for, since a
    # tool dispatched through the server looks up its wiki per session.
    monkeypatch.setitem(mcp_server._ACTIVE_BY_SESSION, "test-session", "main")

    async def go():
        tools = {t.name: t for t in await mcp_server.mcp.list_tools()}
        schema = tools["list_pages"].parameters["properties"]["children"]
        assert schema["default"] is False
        types = {b["type"] for b in schema["anyOf"]}
        assert types == {"boolean", "array", "null"}
        out = {}
        for label, args in (("omitted", {}), ("true", {"children": True}),
                            ("false", {"children": False}), ("null", {"children": None}),
                            ("branch", {"children": ["pantheon"]})):
            res = await mcp_server.mcp.call_tool("list_pages", args)
            out[label] = {p["slug"] for p in res.structured_content["pages"]}
        return out

    token = mcp_server._SESSION_OVERRIDE.set("test-session")
    try:
        out = anyio.run(go)
    finally:
        mcp_server._SESSION_OVERRIDE.reset(token)     # never leak into the next test
    assert out["omitted"] == out["false"] == {"pantheon", "villages", "loner"}
    assert out["true"] == out["null"] == out["false"] | {"igni", "hamlet"}
    assert out["branch"] == out["false"] | {"igni"}


def test_link_following_is_asked_for_in_the_prompt_surface():
    """Agents follow links when something asks them to, and the asking lives in
    prompt text a refactor can silently drop (issue #47). Assert on what the
    client is actually served: the server instructions, which land before the
    first tool call, and get_page's description, which lands at it."""
    async def descriptions():
        return {t.name: t.description or "" for t in await mcp_server.mcp.list_tools()}

    served = anyio.run(descriptions)
    instructions = mcp_server.mcp.instructions

    for text in (instructions, served["get_page"]):
        low = text.lower()
        assert "links" in low
        # Concrete about the moment, like the staleness guidance next to it —
        # not a general plea to be thorough.
        assert "before you write" in low and "edit" in low
        # ...and concrete about the move: fetch the linked page.
        assert "read the linked page" in low or "get_page on that" in low

    # The staleness nudge this one is modelled on must still be there too: the
    # two are the same lesson (the page in your transcript isn't the whole truth).
    assert "check_pages" in instructions


def test_mcp_docs_tools(wiki):
    help_content.seed()
    slugs = [d["slug"] for d in mcp_server.list_docs()["docs"]]
    assert "templates" in slugs and "getting-started" in slugs
    doc = mcp_server.read_doc("templates")
    assert doc["title"] == "Templates" and "create_from_template" in doc["markdown"]
    assert "error" in mcp_server.read_doc("no-such-doc")
