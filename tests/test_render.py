from waikiki import render


def test_gfm_table_renders():
    html = render.render_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in html and "<td>1</td>" in html


def test_wikilink_expands_to_internal_link():
    html = render.render_markdown("see [[Tide Pools]]")
    assert '/wiki/tide-pools' in html


def test_wikilink_with_label():
    html = render.render_markdown("[[tide-pools|the pools]]")
    assert '/wiki/tide-pools' in html and 'the pools' in html


def test_raw_html_is_escaped():
    # html=False → embedded HTML must be escaped, not passed through.
    html = render.render_markdown("<script>alert(1)</script>")
    assert "<script>" not in html


def test_fenced_code_renders():
    html = render.render_markdown("```python\nx = 1\n```")
    assert "<pre" in html


def test_slugify():
    assert render.slugify("Hello, World!") == "hello-world"
