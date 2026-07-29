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
