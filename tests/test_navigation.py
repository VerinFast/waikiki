"""Breadcrumbs, and [[wiki links]] inside custom elements.

The client-side halves of this release (find-in-page, the resizable rail) are
verified in a browser rather than here; what's testable server-side is the data
those features and the breadcrumb trail are built from.
"""
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
