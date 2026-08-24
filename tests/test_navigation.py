"""Breadcrumbs, and [[wiki links]] inside custom elements.

The client-side halves of this release (find-in-page, the resizable rail) are
verified in a browser rather than here; what's testable server-side is the data
those features and the breadcrumb trail are built from.
"""
import pytest
import re

from fastapi.testclient import TestClient

from waikiki import db, elements, render, store
from waikiki.api import app


# --- Breadcrumbs --------------------------------------------------------------

def _nest(*titles):
    """Create pages nested each under the previous. Returns their slugs."""
    slugs = []
    for i, title in enumerate(titles):
        page = store.create_page(title, f"body of {title}")
        if i:
            store.set_parent(page["slug"], slugs[-1])
        slugs.append(page["slug"])
    return slugs


def test_ancestors_are_outermost_first(wiki):
    a, b, c = _nest("History", "First Era", "Founding")
    trail = store.ancestors(store.get_page(c))
    assert [t["slug"] for t in trail] == [a, b]
    assert [t["title"] for t in trail] == ["History", "First Era"]


def test_a_top_level_page_has_no_trail(wiki):
    store.create_page("Orphan", "x")
    assert store.ancestors(store.get_page("orphan")) == []


def test_ancestors_of_nothing_is_empty(wiki):
    assert store.ancestors(None) == []


def test_a_parent_cycle_terminates(wiki):
    """set_parent allows reparenting under a descendant; a cycle must not hang.

    Without the seen-set this loops forever and takes the request with it.
    """
    a, b = _nest("Alpha", "Beta")
    # Force the cycle directly: Alpha's parent becomes Beta, which is its child.
    conn = db.get_conn()
    beta_id = conn.execute("SELECT id FROM pages WHERE slug=?", (b,)).fetchone()["id"]
    conn.execute("UPDATE pages SET parent_id=? WHERE slug=?", (beta_id, a))
    conn.commit()

    trail = store.ancestors(store.get_page(b))     # must return, not spin
    assert len(trail) <= 32


def test_a_trashed_ancestor_stops_the_trail(wiki):
    a, b, c = _nest("History", "First Era", "Founding")
    store.delete_page(b)                            # soft delete the middle
    trail = store.ancestors(store.get_page(c))
    assert [t["slug"] for t in trail] == []         # can't link through the bin


def test_breadcrumbs_render_on_the_page(wiki):
    a, b, c = _nest("History", "First Era", "Founding")
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        body = client.get(f"/wiki/{c}").text
    assert 'class="crumbs"' in body
    assert f'href="/wiki/{a}"' in body and f'href="/wiki/{b}"' in body


def test_no_breadcrumbs_for_a_top_level_page(wiki):
    store.create_page("Orphan", "x")
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        body = client.get("/wiki/orphan").text
    assert 'class="crumbs"' not in body


# --- Wiki links inside custom elements ----------------------------------------

def test_wikilink_map_collects_every_link(wiki):
    got = render.wikilink_map(
        ["The [[Great Fire|fire]] of [[Meru]]", None, "[[Meru]] again", 42],
        resolver=lambda k: k)
    assert got == {
        "[[Great Fire|fire]]": '<a href="/wiki/great-fire">fire</a>',
        "[[Meru]]": '<a href="/wiki/meru">Meru</a>',
    }


def test_wikilink_map_is_empty_without_links(wiki):
    assert render.wikilink_map(["plain text", ""], resolver=lambda k: k) == {}


def _element_page(js):
    elements.save_element(
        "timeline-entry", "Timeline Entry",
        '[{"name":"title","required":true},{"name":"body"}]',
        '<div class="te-title"></div><div class="te-body"></div>', "", js)
    return store.create_page(
        "Era",
        "```timeline-entry\ntitle: Founding of [[Meru]]\nbody: see [[Meru]]\n```")


def test_an_element_carries_its_links_even_when_its_js_uses_textcontent(wiki):
    """The bug this fixes: an element writing props via textContent showed raw
    [[brackets]] unless it shipped its own link parser.

    The server can't know what the component's JS will do, so it ships the
    resolved anchors and the runtime sweeps the shadow DOM afterwards.
    """
    import html as H
    import json

    store.create_page("Meru", "the city")
    page = _element_page("root.querySelector('.te-title').textContent = props.title;")
    rendered = store.get_page(page["slug"])["html"]

    assert 'data-links="' in rendered
    raw = rendered.split('data-links="')[1].split('"')[0]
    assert json.loads(H.unescape(raw)) == {"[[Meru]]": '<a href="/wiki/meru">Meru</a>'}


def test_element_links_resolve_by_title_including_red_links(wiki):
    """Resolution stays server-side: a component in a shadow root has no page
    index, so it cannot tell a real page from a red link."""
    import html as H
    import json

    store.create_page("Meru", "the city")
    elements.save_element(
        "timeline-entry", "Timeline Entry", '[{"name":"title","required":true}]',
        '<div class="te-title"></div>', "", "")
    page = store.create_page(
        "Era2", "```timeline-entry\ntitle: [[Meru]] and [[Nowhere]]\n```")
    rendered = store.get_page(page["slug"])["html"]
    raw = rendered.split('data-links="')[1].split('"')[0]
    links = json.loads(H.unescape(raw))
    assert links["[[Meru]]"] == '<a href="/wiki/meru">Meru</a>'
    assert links["[[Nowhere]]"] == '<a href="/wiki/nowhere">Nowhere</a>'


# --- Lead section (text before the first heading) -----------------------------

def test_lead_span_excludes_frontmatter(wiki):
    """Editing the lead must not swallow or duplicate the frontmatter block."""
    md = "---\ntags: a, b\n---\nLead paragraph.\n\n## First\n\nbody\n"
    span = render.lead_span(md)
    assert span and md[span[0]:span[1]].strip() == "Lead paragraph."
    assert "---" not in md[span[0]:span[1]]


def test_lead_span_covers_a_page_with_no_headings(wiki):
    md = "Just a body, no headings at all.\n"
    span = render.lead_span(md)
    assert span and md[span[0]:span[1]].strip() == "Just a body, no headings at all."


@pytest.mark.parametrize("md", [
    "# Head\n\nbody\n",                 # opens on a heading
    "---\ntags: a\n---\n# Head\n",      # frontmatter then a heading
    "---\ntags: a\n---\n",              # frontmatter and nothing else
    "",                                  # empty page
    "---\ntags: a\n---\n\n\n# Head\n",  # only blank lines before the heading
])
def test_no_lead_to_edit(wiki, md):
    """No lead text means no span — and no control offered for one."""
    assert render.lead_span(md) is None


def test_the_lead_is_addressable_by_its_sentinel(wiki):
    md = "---\ntags: a\n---\nLead.\n\n# H\n\nbody\n"
    assert render.section_span_for_slug(md, render.LEAD_ANCHOR) == render.lead_span(md)


def test_lead_section_round_trips_through_the_edit_route(wiki):
    """The reported bug end to end: the lead can now be fetched and saved."""
    store.create_page("Meru", "---\ntags: city\n---\nOriginal opening.\n\n## Later\n\ntail\n")
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        got = client.get(f"/wiki/meru/section?anchor={render.LEAD_ANCHOR}")
        assert got.status_code == 200 and "Original opening." in got.text
        client.post("/wiki/meru/section",
                    data={"anchor": render.LEAD_ANCHOR, "markdown": "Rewritten opening.\n\n"},
                    follow_redirects=False)
    md = store.get_page("meru")["markdown"]
    assert "Rewritten opening." in md and "Original opening." not in md
    assert md.startswith("---\ntags: city\n---\n"), "frontmatter was damaged"
    assert "## Later" in md and "tail" in md, "the rest of the page was damaged"


# --- Jump to article (#29) ----------------------------------------------------

def test_the_pages_api_can_include_children(wiki):
    """The palette must reach child pages; the sidebar hides them."""
    parent = store.create_page("Meru", "x")
    child = store.create_page("Sub Page", "y")
    store.set_parent(child["slug"], parent["slug"])
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        top = client.get("/api/pages").json()
        every = client.get("/api/pages?children=1").json()
    assert child["slug"] not in [p["slug"] for p in top]
    assert child["slug"] in [p["slug"] for p in every]


def test_the_jump_palette_is_present_and_closed(wiki):
    store.create_page("Meru", "x")
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        body = client.get("/wiki/meru").text
    assert 'id="wk-jump"' in body
    assert 'id="wk-jump" hidden' in body, "the palette must start closed"


def test_the_jump_markup_has_no_unrendered_escapes(wiki):
    """A literal \\uXXXX in markup shows as backslash-u to the reader."""
    import re

    store.create_page("Meru", "x")
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        body = client.get("/wiki/meru").text
    box = body.split('class="jump-box"', 1)[1].split("</div>", 1)[0]
    assert not re.search(r"\\u[0-9a-fA-F]{4}", box), "unrendered escape in the palette"


def test_the_nav_has_reload_between_back_and_forward(wiki):
    """Order matters: it is muscle memory from every browser toolbar."""
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        html = client.get("/").text
    bar = html[html.index('class="histnav"'):]
    bar = bar[:bar.index("</div>")]
    labels = re.findall(r'aria-label="(Back|Reload|Forward)"', bar)
    assert labels == ["Back", "Reload", "Forward"], labels


def test_a_waiting_proposal_announces_itself_on_the_article(wiki):
    """Chat can propose but never apply, and the queue lives on Details.

    Without a word on the article, asking chat to change the page looks like it
    did nothing — which is exactly how this was reported.
    """
    store.create_page("Ledger", "the original")
    store.suggestion_add("ledger", "revised", author="ai", note="Proposed rewrite")
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        art = client.get("/wiki/ledger").text
        assert 'class="proposal-note"' in art
        assert "/wiki/ledger/details#suggestions" in art
        # and the anchor it promises actually exists
        assert 'id="suggestions"' in client.get("/wiki/ledger/details").text


def test_no_proposal_no_notice(wiki):
    store.create_page("Quiet", "nothing pending here")
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        assert 'class="proposal-note"' not in client.get("/wiki/quiet").text


def test_the_parent_selector_is_on_edit_and_knows_the_current_parent(wiki):
    """It moved off Page options — and it must not default to 'none'.

    A selector that always reads 'none' turns Move into a button that quietly
    promotes the page to top-level.
    """
    store.create_page("Parent Page", "p")
    store.create_page("Child", "c")
    store.set_parent("child", "parent-page")
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        edit = client.get("/wiki/child/edit").text
    assert 'class="editparent"' in edit
    assert 'value="parent-page" selected' in edit.replace('"selected"', "selected")
    assert 'class="menu-parent"' not in edit
