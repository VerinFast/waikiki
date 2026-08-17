from waikiki import db, render, store


def test_media_render_video_audio():
    assert "<video" in render.render_markdown("![v](/image/1/clip.mp4)")
    assert "<audio" in render.render_markdown("![a](/image/2/song.mp3)")
    assert "<img" in render.render_markdown("![i](/image/3/pic.png)")


def test_external_link_opens_new_tab():
    h = render.render_markdown("[x](https://example.com)")
    assert 'target="_blank"' in h and "noopener" in h


def test_file_link_allowed():
    h = render.render_markdown("[f](file:///Users/x/doc.pdf)")
    assert "file:///Users/x/doc.pdf" in h


def test_javascript_link_blocked():
    h = render.render_markdown("[x](javascript:alert(1))")
    assert 'href="javascript:' not in h   # no clickable javascript: link


def test_html_escaped_by_default_and_allowed_on_opt_in():
    assert "<div" not in render.render_markdown('<div class="x">hi</div>')
    assert "<div" in render.render_markdown('<div class="x">hi</div>', allow_html=True)


def test_templates_seeded_and_create(wiki):
    names = [t["name"] for t in store.templates_list()]
    assert "Meeting notes" in names and "How-to" in names
    p = store.create_from_template("How-to", "Setup Guide")
    assert p["title"] == "Setup Guide"
    assert "# Setup Guide" in store.get_page(p["slug"])["markdown"]


def test_allow_html_on_by_default_and_optional_off(wiki):
    assert db.get_setting("allow_html", "1") == "1"      # on by default (local/trusted)
    store.create_page("H", '<div class="box">hi</div>')
    assert "<div" in store.get_page("h")["html"]         # renders by default
    db.set_setting("allow_html", "0")
    store.rerender_all()
    assert "<div" not in store.get_page("h")["html"]     # can be turned off per wiki


# --- Agent-drafted templates and elements (#34) -------------------------------

def test_draft_rejects_an_unknown_kind():
    from waikiki import authoring
    assert authoring.draft("widget", "something")["ok"] is False


def test_draft_requires_a_description():
    from waikiki import authoring
    res = authoring.draft("element", "   ")
    assert res["ok"] is False and "Describe" in res["error"]


def test_json_is_recovered_from_a_fenced_reply():
    """Models wrap JSON in fences often enough that being strict would turn a
    good draft into an error the user cannot act on."""
    from waikiki import authoring
    got = authoring._extract_json('Sure!\n```json\n{"name": "Infobox"}\n```\nHope that helps.')
    assert got == {"name": "Infobox"}


def test_json_is_recovered_from_a_bare_reply():
    from waikiki import authoring
    assert authoring._extract_json('{"name": "Infobox", "html": "<div></div>"}')["name"] == "Infobox"


def test_unusable_replies_are_reported_not_guessed():
    from waikiki import authoring
    assert authoring._extract_json("I couldn't do that.") is None
    assert authoring._extract_json("") is None


def test_a_drafted_element_returns_every_part(wiki, monkeypatch):
    from waikiki import authoring, chat

    monkeypatch.setattr(chat, "find_cli", lambda b: "/usr/bin/true")

    class R:
        returncode, stderr = 0, ""
        stdout = ('{"name":"Infobox","fields":"title* | Title","html":"<div></div>",'
                  '"css":".x{}","js":"root.querySelector(\'div\')"}')

    monkeypatch.setattr(authoring.clirun, "run", lambda *a, **k: R())
    res = authoring.draft("element", "an infobox")
    assert res["ok"] and res["name"] == "Infobox" and res["fields"] == "title* | Title"
    assert res["html"] and res["css"] and res["js"]


def test_drafting_never_saves(wiki, monkeypatch):
    """The agent drafts; the human saves. Elements ship HTML and JS into every
    page that uses them, so those must stay two steps."""
    from waikiki import authoring, chat, elements

    monkeypatch.setattr(chat, "find_cli", lambda b: "/usr/bin/true")

    class R:
        returncode, stderr = 0, ""
        stdout = '{"name":"Infobox","fields":"title*","html":"<div></div>"}'

    monkeypatch.setattr(authoring.clirun, "run", lambda *a, **k: R())
    before = {e["slug"] for e in elements.list_elements()}
    authoring.draft("element", "an infobox")
    assert {e["slug"] for e in elements.list_elements()} == before


def test_the_draft_ui_is_wired_on_both_editors(wiki):
    """The script must be inside the content block or Jinja discards it —
    the box rendered while the button did nothing."""
    from fastapi.testclient import TestClient

    from waikiki.api import app

    with TestClient(app, client=("127.0.0.1", 1)) as client:
        el = client.get("/elements/new").text
        tpl = client.get("/templates/new").text
    for body, kind in ((el, "element"), (tpl, "template")):
        assert "draftbox" in body, f"{kind}: no describe-it box"
        assert "/api/draft" in body, f"{kind}: box present but not wired up"
        assert f"kind: '{kind}'" in body, f"{kind}: wrong kind posted"
