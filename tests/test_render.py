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


def test_heading_anchors():
    html = render.render_markdown("## My Section\n\ntext")
    assert 'id="my-section"' in html
    assert 'class="header-anchor" href="#my-section"' in html


def test_wikilink_to_section():
    html = render.render_markdown("jump to [[Guide#Setup Steps]]")
    assert 'href="/wiki/guide#setup-steps"' in html


def test_same_page_section_link():
    html = render.render_markdown("[[#Overview]]")
    assert 'href="#overview"' in html


def test_extract_toc():
    toc = render.extract_toc("# Top\n\n## A\n\n```\n## not-a-heading\n```\n\n### B")
    assert [(h["level"], h["slug"]) for h in toc] == [(1, "top"), (2, "a"), (3, "b")]


def test_toc_slug_matches_anchor_for_wikilinks_and_dupes():
    md = "## [[Meru]]\nx\n\n## Notes\na\n\n## Notes\nb"
    toc = render.extract_toc(md)
    assert [(t["text"], t["slug"]) for t in toc] == [
        ("Meru", "meru"), ("Notes", "notes"), ("Notes", "notes-1")]
    # slugs must equal the ids the renderer assigns
    html = render.render_markdown(md)
    assert 'id="meru"' in html and 'id="notes"' in html and 'id="notes-1"' in html


def test_section_span_for_slug():
    md = "## [[Meru]]\nMeru body.\n\n## Bram\nBram body."
    span = render.section_span_for_slug(md, "meru")
    assert "Meru body" in md[span[0]:span[1]] and "Bram body" not in md[span[0]:span[1]]
    md2 = "## Notes\nfirst\n\n## Notes\nsecond"
    s = render.section_span_for_slug(md2, "notes-1")
    assert "second" in md2[s[0]:s[1]] and "first" not in md2[s[0]:s[1]]
    assert render.section_span_for_slug(md2, "nope") is None


def test_extract_wikilinks_ignores_section_and_self():
    links = render.extract_wikilinks("[[Guide#Setup]] and [[#Local]] and [[Other]]")
    assert links == ["guide", "other"]   # section stripped, same-page skipped


def test_extract_wikilink_refs_keeps_label_and_section():
    refs = render.extract_wikilink_refs(
        "[[Edaphos|earth]] then [[Guide#Setup Steps]] then [[#Local]] then [[Igni]]")
    assert refs == [
        # the visible word is "earth" but the page is edaphos — the whole point
        {"target": "edaphos", "label": "earth", "section": None},
        {"target": "guide", "label": "Guide#Setup Steps", "section": "setup-steps"},
        {"target": "igni", "label": "Igni", "section": None},   # [[#Local]] skipped
    ]


def test_extract_wikilink_refs_labels_match_the_rendered_anchor():
    """`label` must be the text a reader sees, i.e. what wikilink_anchor writes."""
    md = "[[Edaphos|earth]] [[Igni]] [[Guide#Setup]]"
    html = render.render_markdown(md)
    for ref in render.extract_wikilink_refs(md):
        assert f">{ref['label']}</a>" in html


# --- Wikilinks must resolve everywhere, and stay literal in code --------------

def test_wikilinks_resolve_inside_raw_html():
    """Infobox-style templates are raw HTML; markdown never runs inside an HTML
    block, so links there used to render as literal '[Label](/wiki/x)'."""
    html = render.render_markdown(
        "<table><tr><td>[[Product Name]]</td></tr></table>", allow_html=True)
    assert '<a href="/wiki/product-name">Product Name</a>' in html
    assert "](/wiki/" not in html


def test_wikilinks_stay_literal_in_code():
    """The Help pages document the [[Page]] syntax; rewriting it there mangled
    the documentation."""
    assert "<code>[[Page Title]]</code>" in render.render_markdown(
        "Use `[[Page Title]]` to link.")
    fenced = render.render_markdown("```text\n[[Page Title]]\n```")
    assert "[[Page Title]]" in fenced and "/wiki/page-title" not in fenced


def test_wikilinks_not_injected_into_attributes():
    html = render.render_markdown('<img alt="[[Page]]" src="/x.png">', allow_html=True)
    assert 'alt="[[Page]]"' in html and "<a href" not in html


def test_existing_links_are_not_double_wrapped():
    html = render.render_markdown("[[Page]] and [already](/wiki/other)")
    assert html.count("<a ") == 2


def test_wikilink_labels_and_resolver_still_work():
    idx = {"real-slug": "canonical"}
    html = render.render_markdown("[[Real Slug|Shown]]", idx.get)
    assert 'href="/wiki/canonical"' in html and ">Shown<" in html


def test_render_value_escapes_then_links():
    out = render.render_value("<b>x</b> [[Page]]")
    assert "&lt;b&gt;" in out                       # escaped, not injected
    assert '<a href="/wiki/page">Page</a>' in out


# --- relative timestamps (issue #73) ------------------------------------------
#
# "2026-08-21 09:14:02" is precise and useless: it does not tell a person that
# the page changed while they were looking away. The history affordance on an
# article depends on the wording being noticeable, so the rounding is pinned.

from datetime import datetime  # noqa: E402

_NOW = datetime(2026, 8, 21, 12, 0, 0)


def test_time_ago_rounds_the_way_a_person_would_say_it():
    say = lambda ts: render.time_ago(ts, _NOW)          # noqa: E731
    assert say("2026-08-21 11:59:30") == "just now"
    assert say("2026-08-21 11:59:00") == "1 minute ago"
    assert say("2026-08-21 11:57:00") == "3 minutes ago"
    assert say("2026-08-21 11:00:00") == "1 hour ago"
    assert say("2026-08-21 09:00:00") == "3 hours ago"
    assert say("2026-08-20 11:00:00") == "1 day ago"
    assert say("2026-08-19 12:00:00") == "2 days ago"


def test_time_ago_gives_up_on_relative_wording_past_a_month():
    """"47 days ago" is arithmetic the reader has to undo."""
    assert render.time_ago("2026-03-02 12:00:00", _NOW) == "on 2 Mar 2026"


def test_time_ago_never_reads_as_negative_or_blows_up():
    """A clock that moved, and a value that isn't a timestamp at all.

    This formats whatever the database holds, including rows written by an
    import or by an older version of the app, so it has to degrade to showing
    the raw value rather than raising inside a page render.
    """
    assert render.time_ago("2026-08-21 12:05:00", _NOW) == "just now"
    assert render.time_ago("not a timestamp", _NOW) == "not a timestamp"
    assert render.time_ago(None, _NOW) == ""
    assert render.time_ago("", _NOW) == ""
    # tz-aware input is normalised to UTC rather than compared naively
    assert render.time_ago("2026-08-21T11:57:00+00:00", _NOW) == "3 minutes ago"
